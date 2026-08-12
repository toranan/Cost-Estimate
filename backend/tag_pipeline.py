"""TAG 시스템 마스터 파이프라인 — Phase 1~5 오케스트레이터.

Phase 1(tag_router) → Phase 2(tag_parsers) → Phase 3(tag_rule_engine) →
Phase 4(tag_timeseries) → Phase 5(tag_formatter) 순서로 실행한다.

위원회는 조문 단위가 아니라 committee_gate.py로 "개체" 단위 그룹핑 후 한 번만
처리한다 — 제7조(설치)·제8조(구성)·제9조(운영)처럼 같은 위원회가 여러 조문에
분산되는 법안 표준 패턴에서 위원회 1개가 여러 개로 중복 계산되는 걸 막는다.

카테고리 4_출자출연금·5_조세감면·6_세외수입감소는 Phase 2 파서 스펙이 아직
없어(마스터 설계에도 파서가 정의 안 됨) 라우팅까지만 하고 계산은
"parser_not_implemented"로 남긴다 — 없는 파서를 지어내지 않는다.
"""
from __future__ import annotations

from dataclasses import replace
import re

from . import tag_rule_engine as rule_engine
from .article_extraction_engine import extract_bill_title, extract_pdf_text, split_articles
from .committee_assembly_internal_gate import is_assembly_internal_committee
from .committee_evidence_adapter import build_evidence_workflow
from .committee_gate import CommitteeEntity, group_committee_articles
from .committee_type_gate import TYPE_3_SIMPLE_ADVISORY, TYPE_4_ASSEMBLY_INTERNAL
from .committee_temporality import (
    apply_temporality_to_calculation,
    decide_committee_temporality,
)
from .tag_committee_precedent_adapter import (
    apply_precedent_fallback,
    extract_bill_metadata,
)
from .tag_parsers import (
    extract_capital_expenditure_vars,
    extract_committee_vars,
    extract_personnel_vars,
    extract_transfer_payment_vars,
)
from .tag_formatter import format_report
from .tag_router import is_cost_trigger, route_article
from .tag_timeseries import (
    aggregate,
    category_to_year_amounts,
    category_to_year_amounts_range,
)

TAG_PIPELINE_VERSION = "tag-pipeline-v5"

_COMMITTEE_HINT_RE = re.compile(r"위원회|심의회|협의회")
_PERSONNEL_HINT_RE = re.compile(
    r"정원|공무원|증원|인건비|보수|봉급|"
    r"(?:인력|직원).{0,20}(?:채용|배치|증원|정원)"
)


def _run_committee_entity(entity: CommitteeEntity, bill_title: str) -> dict:
    v = extract_committee_vars(entity.combined_text)
    # 개체 경계와 정식명은 committee_gate가 이미 여러 조문을 묶어 확정했다.
    # 같은 조문에 시·도/시·군·구 위원회처럼 여러 개체가 함께 나오면 LLM 파서는
    # 다른 개체명을 반환할 수 있으므로, 계산·복제 범위에 쓰는 이름은 현재 게이트
    # 개체로 고정한다. 숫자 변수는 파서 결과를 그대로 유지한다.
    if v.name != entity.name:
        v = replace(v, name=entity.name)
    committee_type = entity.committee_type or TYPE_3_SIMPLE_ADVISORY
    if is_assembly_internal_committee(entity.combined_text):
        committee_type = TYPE_4_ASSEMBLY_INTERNAL
    calc_result = rule_engine.calculate_committee(
        v, committee_type=committee_type, article_text=entity.combined_text
    )
    temporality = decide_committee_temporality(
        entity.combined_text,
        proposed_mode=v.temporal_mode,
        proposed_event_count=v.finite_event_count,
        evidence_quotes=v.temporal_evidence_quotes,
    )
    calc_result = apply_temporality_to_calculation(calc_result, temporality)
    if entity.discretionary and calc_result.get("status") != "review_required":
        # 재량 규정("둘 수 있다")은 설치 여부가 불확실하지만, 산식과 변수가 이미
        # 완결됐다면 "설치하는 경우"의 조건부 추계액은 유효하다. 검토 표시 때문에
        # 계산된 금액까지 0원으로 지우지 않고 별도 상태로 보존한다. 반대로 애초에
        # 변수가 부족해 review_required인 항목은 이 분기에 들어오지 않아 계속 보류된다.
        calc_result = {
            **calc_result,
            "status": "calculated_discretionary_scenario",
            "conditional_on_implementation": True,
            "reason": (
                "재량 규정(둘 수 있다)이므로 실제 설치 여부는 불확실합니다. "
                "표시 금액은 설치하는 경우의 조건부 추계입니다. "
                + str(calc_result.get("reason") or "")
            ),
        }
    label = f"{'/'.join(entity.article_nos)} {entity.name}".strip()
    year_amounts = category_to_year_amounts("1_인건비_물건비", calc_result)
    year_amounts_range = category_to_year_amounts_range(
        "1_인건비_물건비", calc_result
    )
    return {
        "article_no": "/".join(entity.article_nos),
        "category": "1_인건비_물건비",
        "label": label,
        "entity_name": entity.name,
        "calc_result": calc_result,
        "year_amounts": year_amounts,
        "year_amounts_range": year_amounts_range,
    }


def _run_article(article: dict) -> list[dict]:
    """위원회가 아닌 나머지 카테고리를 조문 단위로 Phase 2~3까지 실행한다."""
    article_text = f"{article.get('title', '')}\n{article.get('text', '')}"
    results: list[dict] = []

    for category in article["categories"]:
        try:
            if category == "1_인건비_물건비":
                if _COMMITTEE_HINT_RE.search(article_text):
                    continue  # 위원회는 committee_gate에서 개체 단위로 이미 처리됨
                if _PERSONNEL_HINT_RE.search(article_text):
                    v = extract_personnel_vars(article_text)
                    label, calc_result = "공무원 조직", rule_engine.calculate_personnel(v)
                else:
                    label, calc_result = "1_인건비_물건비(세부유형 미판별)", {
                        "status": "parser_not_implemented",
                        "reason": "공무원조직형이 아니고 위원회도 아니어서 파서를 선택하지 못했습니다.",
                    }
            elif category == "2_이전지출":
                v = extract_transfer_payment_vars(article_text)
                label = v.target_demographic or "이전지출"
                calc_result = rule_engine.calculate_transfer_payment(v)
            elif category == "3_자본지출":
                v = extract_capital_expenditure_vars(article_text)
                label = v.system_type or "자본지출"
                calc_result = rule_engine.calculate_capital_expenditure(v)
            else:
                label, calc_result = category, {
                    "status": "parser_not_implemented",
                    "reason": f"{category} 카테고리는 Phase 2 파서가 아직 정의되지 않았습니다.",
                }
        except ValueError as exc:
            label, calc_result = category, {"status": "extraction_failed", "reason": str(exc)}

        year_amounts = category_to_year_amounts(category, calc_result)
        year_amounts_range = category_to_year_amounts_range(category, calc_result)
        results.append({
            "article_no": article.get("no", ""),
            "category": category,
            "label": f"{article.get('no', '')} {label}".strip(),
            "calc_result": calc_result,
            "year_amounts": year_amounts,
            "year_amounts_range": year_amounts_range,
        })
    return results


def run_pipeline(
    pdf_bytes: bytes,
    *,
    base_year: int = 2027,
    filename: str = "",
    use_precedent_fallback: bool = True,
) -> dict:
    """PDF 원문 → 최종 리포트까지 Phase 1~5 전체 실행."""
    text = extract_pdf_text(pdf_bytes)
    bill_title = extract_bill_title(text)
    bill_no, propose_date = extract_bill_metadata(text, filename=filename)
    raw_articles, _doc_type = split_articles(text)

    routed_articles: list[dict] = []
    for article in raw_articles:
        body = f"{article.get('title', '')} {article.get('text', '')}"
        if not is_cost_trigger(body):
            continue
        categories = route_article(body)
        if not categories:
            continue
        routed_articles.append({**article, "categories": categories})

    items: list[dict] = []

    committee_entities = group_committee_articles(raw_articles)
    committee_items: list[dict] = []
    for entity in committee_entities:
        committee_item = _run_committee_entity(entity, bill_title)
        committee_items.append(committee_item)
        items.append(committee_item)

    evidence_workflow: dict | None = None
    if use_precedent_fallback and committee_items:
        try:
            evidence_workflow = build_evidence_workflow(
                pdf_bytes=pdf_bytes,
                bill_no=bill_no,
                bill_name=bill_title,
                propose_date=propose_date,
                target_names=[entity.name for entity in committee_entities],
            )
            apply_precedent_fallback(
                committee_items,
                evidence_workflow,
                current_bill_no=bill_no,
            )
        except Exception as exc:  # Fallback failure must not erase TAG results.
            evidence_workflow = {
                "status": "unavailable",
                "reason": f"선례 fallback 실행 실패: {exc}",
            }

    for article in routed_articles:
        items.extend(_run_article(article))

    aggregated = aggregate(items)
    report = format_report(aggregated, items, base_year=base_year)

    return {
        "bill_title": bill_title,
        "bill_no": bill_no,
        "propose_date": propose_date,
        "routed_article_count": len(routed_articles),
        "committee_count": len(committee_entities),
        "items": items,
        "aggregated": aggregated,
        "report_markdown": report,
        "evidence_workflow": evidence_workflow,
    }
