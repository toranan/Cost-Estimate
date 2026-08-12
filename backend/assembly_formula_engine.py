from __future__ import annotations

import re
from statistics import median
from typing import Any

from .assembly_formula_templates import infer_template_key


EXTERNAL_DATA_TERMS = (
    "대상자",
    "대상 수",
    "인구",
    "수급자",
    "이용자",
    "신청자",
    "시설 수",
    "개소 수",
    "사업량",
    "수요",
    "감면액",
    "과세표준",
    "세수",
    "면적",
    "공사비",
    "건축비",
    "기존 실적",
    "집행률",
    "신청률",
)

POLICY_INPUT_TERMS = (
    "지원 단가",
    "지원금액",
    "지원 규모",
    "운영 규모",
    "배치 인원",
    "소요인력",
    "회의횟수",
    "참석인원",
    "수당 단가",
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _title(article: dict[str, Any]) -> str:
    no = str(article.get("no") or "")
    match = re.search(r"\(([^)\n]+)\)", no)
    return match.group(1).strip() if match else no.strip()


def _ref(article: dict[str, Any]) -> str:
    return str(article.get("no") or "").replace("\n", " ").strip()


def _article_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    for match in re.finditer(r"제\s*(\d+)\s*조(?:의\s*(\d+))?", str(value or "")):
        base, sub = match.groups()
        refs.add(f"제{base}조" + (f"의{sub}" if sub else ""))
    return refs


def _canonical_family(item: dict[str, Any]) -> str:
    family = str(item.get("formula_family") or infer_template_key(item) or "")
    return {
        "subsidy_payment": "transfer_payment",
    }.get(family, family)


def _committee_entity(text: str) -> str | None:
    compact = _compact(text)
    matches = re.findall(r"([가-힣A-Za-z0-9·ㆍ]{2,40}(?:위원회|심의회|협의회))", compact)
    specific = [
        value
        for value in matches
        if value not in {"위원회", "심의회", "협의회"}
        and not re.search(r"제\d+조(?:의\d+)?위원회$", value)
    ]
    return max(specific, key=len) if specific else None


def _assumption(
    name: str,
    unit: str,
    basis: str,
    source_type: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "value": None,
        "unit": unit,
        "basis": basis,
        "source_type": source_type,
        "needs_user_confirm": True,
    }


def _item(
    *,
    name: str,
    category: str,
    family: str,
    formula: str,
    trigger_ref: str,
    variables: list[str],
    recurrence: str,
    assumptions: list[dict[str, Any]],
    allow_tag_estimate: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "formula_family": family,
        "formula": formula,
        "trigger_ref": trigger_ref,
        "variables_needed": variables,
        "assumptions": assumptions,
        "calculation": {
            "base_amount_thousand": None,
            "recurrence": recurrence,
            "start_year": 1,
            "end_year": 5,
            "growth_variable": None,
            "source_note": "범용 비용유형 분류에서 생성된 산식 후보",
        },
        "allow_tag_estimate": allow_tag_estimate,
        "requires_review": True,
    }


def _is_institution_bill(articles: list[dict[str, Any]]) -> bool:
    text = _compact(" ".join(str(a.get("text") or "") for a in articles))
    has_establishment = bool(
        re.search(r"(설립|설치하여야|설치한다|법인으로한다)", text)
    )
    has_finance = bool(
        re.search(r"(설립에드는비용|매년인건비|경상적경비|시설확충비|출연또는보조|재정지원)", text)
    )
    return has_establishment and has_finance


def _institution_items(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finance_article = next(
        (
            article
            for article in articles
            if re.search(
                r"(설립에\s*드는\s*비용|매년\s*인건비|시설확충비|재정\s*지원)",
                str(article.get("text") or ""),
            )
        ),
        articles[0],
    )
    ref = _ref(finance_article)
    title = _title(finance_article)
    subject = re.sub(r"(국가의)?재정지원", "", title).strip() or "신설 기관"
    return [
        _item(
            name=f"{subject} 설립·시설 조성비",
            category="사업비",
            family="institution_establishment",
            formula="부지비 + 설계비 + 공사비 + 장비·비품비",
            trigger_ref=ref,
            variables=["시설 규모", "단위면적당 공사비", "장비·비품비"],
            recurrence="one_time",
            assumptions=[
                _assumption("시설 규모", "㎡", "설립계획 또는 유사기관 규모 자료 필요", "external_data"),
                _assumption("단위면적당 공사비", "천원/㎡", "공공건축 공사비 또는 유사기관 건립비 자료 필요", "external_data"),
                _assumption("장비·비품비", "천원", "설립계획 또는 유사기관 자산취득 자료 필요", "external_data"),
            ],
        ),
        _item(
            name=f"{subject} 인건비",
            category="인건비",
            family="personnel_compensation",
            formula="직급별 인원 × 직급별 1인당 보수 + 기관부담금",
            trigger_ref=ref,
            variables=["직급별 인원", "직급별 1인당 보수", "기관부담요율"],
            recurrence="annual",
            assumptions=[
                _assumption("직급별 인원", "명", "조직·정원 계획 또는 유사기관 인력현황 필요", "external_data"),
                _assumption("직급별 1인당 보수", "천원/명", "보수표 또는 유사기관 인건비 자료 필요", "external_data"),
                _assumption("기관부담요율", "%", "연금·보험 등 기관부담 기준 확인 필요", "external_data"),
            ],
        ),
        _item(
            name=f"{subject} 연간 운영비",
            category="운영비",
            family="institution_operation",
            formula="인건비 연동 기본경비 + 교육·연구·사업 운영비",
            trigger_ref=ref,
            variables=["기본경비 기준액", "연간 사업 운영비"],
            recurrence="annual",
            assumptions=[
                _assumption("기본경비 기준액", "천원/년", "유사기관 결산 또는 운영계획 자료 필요", "external_data"),
                _assumption("연간 사업 운영비", "천원/년", "사업계획 또는 유사기관 운영비 자료 필요", "external_data"),
            ],
        ),
    ]


def build_generalized_estimate(
    articles: list[dict[str, Any]],
    *,
    years: int = 5,
) -> dict[str, Any] | None:
    cost_articles = [
        article
        for article in articles
        if article.get("cost_trigger")
        and str(article.get("cost_candidate_strength") or "medium") != "weak"
    ]
    if not cost_articles:
        return None

    if _is_institution_bill(cost_articles):
        items = _institution_items(cost_articles)
        for item in items:
            item["calculation"]["end_year"] = years
        return {"items": items, "source": "general_formula_engine"}

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    last_committee_entity: str | None = None
    for article in cost_articles:
        text = str(article.get("text") or "")
        compact = _compact(text)
        rule_name = str((article.get("rule_cost_trigger") or {}).get("rule") or "")
        trigger_type = str(article.get("trigger_type") or "")
        title = _title(article)
        ref = _ref(article)

        candidate: dict[str, Any] | None = None
        if trigger_type == "세수감소":
            candidate = _item(
                name=f"{title} 세수감소",
                category="세입감소",
                family="revenue_loss",
                formula="최근 3~5년 연평균 감면실적 × 적용기한 연장기간",
                trigger_ref=ref,
                variables=["연평균 감면실적", "적용기한 연장기간"],
                recurrence="annual",
                assumptions=[
                    _assumption(
                        "연평균 감면실적",
                        "천원/년",
                        "동일 조문의 공식 추계서 또는 최근 조세 감면실적 필요",
                        "external_data",
                    ),
                    _assumption(
                        "적용기한 연장기간",
                        "년",
                        "개정문의 종료일 변경에서 확인",
                        "document",
                    ),
                ],
                allow_tag_estimate=True,
            )
        elif rule_name == "survey_or_plan_service" or re.search(r"(실태조사|연구용역)", compact):
            period_match = re.search(r"(\d+)년마다", compact)
            period_years = int(period_match.group(1)) if period_match else 1
            frequency = 1 if period_match or re.search(r"연1회|매년", compact) else None
            recurrence = "periodic" if period_match and period_years > 1 else "annual"
            assumptions = [
                _assumption("용역 단가", "천원/회", "동일·유사 조사 연구용역 계약금액 필요", "external_data"),
                {
                    "name": "수행 횟수",
                    "value": frequency,
                    "unit": f"회/{period_years}년" if period_match else "회/년",
                    "basis": "조문에 명시된 수행주기" if frequency else "조문 또는 시행계획의 수행주기 확인 필요",
                    "source_type": "document" if frequency else "policy_input",
                    "needs_user_confirm": not bool(frequency),
                },
            ]
            candidate = _item(
                name=f"{title} 연구용역비",
                category="위탁비",
                family="research_service",
                formula="용역 단가 × 연간 수행 횟수",
                trigger_ref=ref,
                variables=["용역 단가", "수행 횟수"],
                recurrence=recurrence,
                assumptions=assumptions,
                allow_tag_estimate=True,
            )
            if recurrence == "periodic":
                candidate["formula"] = f"용역 단가 × {period_years}년마다 1회 수행"
                candidate["calculation"]["interval_years"] = period_years
        elif rule_name in {"payment_or_subsidy", "new_support_project"} or trigger_type in {"직접지원", "대상확대"}:
            candidate = _item(
                name=f"{title} 지원 소요",
                category="지원금",
                family="transfer_payment",
                formula="지원 대상자 수 × 1인당 지급액 × 지급 횟수 × 집행률",
                trigger_ref=ref,
                variables=["지원 대상자 수", "1인당 지급액", "지급 횟수", "집행률"],
                recurrence="annual",
                assumptions=[
                    _assumption("지원 대상자 수", "명", "행정통계 또는 장래인구·수급자 전망 필요", "external_data"),
                    _assumption("1인당 지급액", "천원/명", "법정 단가 또는 현행 대비 증액분 확인 필요", "external_data"),
                    _assumption("지급 횟수", "회/년", "지급주기 또는 시행기간 확인 필요", "policy_input"),
                    _assumption("집행률", "%", "현행 사업 집행실적 필요", "external_data"),
                ],
            )
        elif (
            rule_name == "facility_or_system" or trigger_type == "시설구축"
        ) and re.search(r"(구축|설치|개발|도입|운영하여야|관리시스템을)", compact):
            candidate = _item(
                name=f"{title} 구축·운영비",
                category="사업비",
                family="facility_system",
                formula="초기 구축비 + 연간 유지관리비",
                trigger_ref=ref,
                variables=["초기 구축비", "연간 유지관리비"],
                recurrence="annual",
                assumptions=[
                    _assumption("초기 구축비", "천원", "정보화·시설 구축계획 또는 유사사업 계약금액 필요", "external_data"),
                    _assumption("연간 유지관리비", "천원/년", "유지관리 요율 또는 유사사업 운영비 필요", "external_data"),
                ],
            )
        elif rule_name == "committee_or_body_operation" or (
            trigger_type == "조직설치"
            and re.search(r"위원회|심의회|협의회", compact)
        ):
            # A committee is commonly defined in one article and composed in
            # the next (for example, "X위원회" followed by "위원회의 구성").
            # Generic follow-up titles inherit the last named entity so the
            # same committee is not charged twice.
            generic_committee_title = bool(re.search(
                r"^위원회(?:의)?(?:구성|조직|운영|회의|위원장|간사|소위원회|분과)",
                _compact(title),
            ))
            entity = None if generic_committee_title else (_committee_entity(title) or _committee_entity(text))
            if entity:
                last_committee_entity = entity
            candidate = _item(
                name=f"{title} 운영비",
                category="운영비",
                family="committee_operation",
                formula="회의수당 단가 × 회의횟수 × 수당지급대상 인원",
                trigger_ref=ref,
                variables=["회의수당 단가", "회의횟수", "수당지급대상 인원"],
                recurrence="annual",
                assumptions=[
                    _assumption("회의수당 단가", "천원/명", "위원회 수당 기준 또는 유사사례 필요", "external_data"),
                    _assumption("회의횟수", "회/년", "조문 또는 운영계획 확인 필요", "policy_input"),
                    _assumption("수당지급대상 인원", "명", "민간·위촉위원 구성 확인 필요", "policy_input"),
                ],
                allow_tag_estimate=True,
            )

        if not candidate:
            continue
        candidate["calculation"]["end_year"] = years
        if candidate["formula_family"] == "committee_operation":
            entity_key = entity or last_committee_entity or candidate["trigger_ref"]
            key = (candidate["formula_family"], entity_key)
        else:
            key = (candidate["formula_family"], candidate["trigger_ref"])
        if key in seen:
            continue
        seen.add(key)
        items.append(candidate)

    return {"items": items, "source": "general_formula_engine"} if items else None


def merge_generalized_estimate(
    current: dict[str, Any] | None,
    generated: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not generated or not generated.get("items"):
        return current
    if not current or not current.get("items"):
        return generated

    generated_families = {
        str(item.get("formula_family") or "") for item in generated.get("items") or []
    }
    current_families = {
        str(item.get("formula_family") or infer_template_key(item) or "")
        for item in current.get("items") or []
    }
    # Institution establishment is a composite cost model. A lone committee
    # operating item is not an adequate substitute for it.
    if "institution_establishment" in generated_families and "institution_establishment" not in current_families:
        return generated

    merged = dict(current)
    # Deterministic rows are the canonical item registry. LLM rows for the
    # same article and cost family are retained only as audit evidence, not as
    # additional amounts. This prevents broad refs such as "제18조, 제19조"
    # from being summed alongside article-specific committee rows.
    merged_items = [dict(item) for item in generated.get("items") or []]
    for draft in current.get("items") or []:
        draft_family = _canonical_family(draft)
        draft_refs = _article_refs(draft.get("trigger_ref"))
        matches = [
            item
            for item in merged_items
            if _canonical_family(item) == draft_family
            and draft_refs
            and bool(draft_refs & _article_refs(item.get("trigger_ref")))
        ]
        if not matches:
            merged_items.append(draft)
            continue
        audit = {
            "name": draft.get("name"),
            "trigger_ref": draft.get("trigger_ref"),
            "formula": draft.get("formula"),
        }
        for existing in matches:
            existing.setdefault("merged_llm_drafts", []).append(audit)
    merged["items"] = merged_items
    merged["source"] = "llm_plus_general_formula_engine"
    return merged


def _token_overlap(a: str, b: str) -> float:
    ca, cb = _compact(a), _compact(b)
    if len(ca) < 2 or len(cb) < 2:
        return 0.0
    ta = {ca[i:i + 2] for i in range(len(ca) - 1)}
    tb = {cb[i:i + 2] for i in range(len(cb) - 1)}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _research_subtype(text: Any) -> str | None:
    compact = _compact(text)
    for subtype, terms in (
        ("survey", ("실태조사", "현황조사")),
        ("master_plan", ("기본계획", "종합계획", "계획수립")),
        ("research", ("연구용역", "정책연구")),
    ):
        if any(term in compact for term in terms):
            return subtype
    return None


def _tag_formula_candidates(
    item: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    template_key = str(item.get("formula_family") or infer_template_key(item) or "")
    candidates: list[dict[str, Any]] = []
    for pattern in tag_patterns:
        for tagged in pattern.get("items") or []:
            tagged_probe = {
                "name": tagged.get("name"),
                "category": tagged.get("category"),
                "formula": " ".join(
                    str(row.get("formula") or "") for row in tagged.get("amounts") or []
                ),
                "variables_needed": [v.get("name") for v in tagged.get("variables") or []],
            }
            tagged_key = infer_template_key(tagged_probe)
            if template_key and tagged_key and tagged_key != template_key:
                continue
            annual = [
                float(row["amount_thousand"])
                for row in tagged.get("amounts") or []
                if not row.get("is_total")
                and row.get("amount_thousand") is not None
                and float(row["amount_thousand"]) != 0
            ]
            if not annual:
                continue
            item_name = str(item.get("name") or "")
            tagged_name = str(tagged.get("name") or "")
            name_overlap = _token_overlap(item_name, tagged_name)
            if template_key == "research_service":
                item_subtype = _research_subtype(item_name)
                tagged_subtype = _research_subtype(tagged_name)
                if item_subtype and tagged_subtype and item_subtype != tagged_subtype:
                    continue
            if name_overlap < 0.12:
                continue
            score = name_overlap
            if item.get("category") == tagged.get("category"):
                score += 0.25
            if tagged_key == template_key:
                score += 0.4
            formulas = [
                str(row.get("formula") or "")
                for row in tagged.get("amounts") or []
                if row.get("formula")
            ]
            variables = [
                {
                    "name": variable.get("name"),
                    "value": variable.get("value"),
                    "unit": variable.get("unit"),
                }
                for variable in tagged.get("variables") or []
                if variable.get("name")
            ]
            candidates.append({
                "score": round(score, 3),
                "base_amount_thousand": int(round(median(annual))),
                "bill_no": pattern.get("bill_no"),
                "bill_name": pattern.get("bill_name"),
                "item_name": tagged.get("name"),
                "item_category": tagged.get("category"),
                "formula": formulas[0] if formulas else None,
                "formula_candidates": formulas[:3],
                "variables": variables[:8],
            })

    candidates.sort(key=lambda row: float(row["score"]), reverse=True)
    return candidates


def _best_tag_formula_candidate(
    item: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
    *,
    threshold: float = 0.45,
) -> dict[str, Any] | None:
    candidates = _tag_formula_candidates(item, tag_patterns)
    if not candidates:
        return None
    best = candidates[0]
    if float(best.get("score") or 0) < threshold:
        return None
    return best


def _find_named_assumption(item: dict[str, Any], variable: str) -> dict[str, Any] | None:
    target = _compact(variable)
    for assumption in item.get("assumptions") or []:
        name = _compact(assumption.get("name"))
        if target and (target == name or target in name or name in target):
            return assumption
    return None


def _find_kosis_lookup(item: dict[str, Any], variable: str) -> dict[str, Any] | None:
    target = _compact(variable)
    for lookup in item.get("kosis_lookups") or []:
        name = _compact(lookup.get("variable"))
        if target and (target == name or target in name or name in target):
            return lookup
    return None


def _find_assumption_candidate(item: dict[str, Any], variable: str) -> dict[str, Any] | None:
    target = _compact(variable)
    for candidate in item.get("assumption_candidates") or []:
        probe = _compact(" ".join(
            str(part or "")
            for part in [
                candidate.get("label"),
                candidate.get("variable_name"),
                candidate.get("item_name"),
            ]
        ))
        if target and (target in probe or probe in target):
            return candidate
    return None


def _find_tag_variable(candidate: dict[str, Any] | None, variable: str) -> dict[str, Any] | None:
    if not candidate:
        return None
    target = _compact(variable)
    for tag_variable in candidate.get("variables") or []:
        name = _compact(tag_variable.get("name"))
        if target and (target == name or target in name or name in target):
            return tag_variable
    return None


def _variable_strategy(
    item: dict[str, Any],
    variable: str,
    tag_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    assumption = _find_named_assumption(item, variable)
    if assumption and assumption.get("value") is not None:
        return {
            "variable": variable,
            "source_type": assumption.get("source_type") or "document_or_standard",
            "status": "resolved",
            "value": assumption.get("value"),
            "unit": assumption.get("unit"),
            "basis": assumption.get("basis") or "조문 또는 표준 전제값에서 확인",
            "requires_review": bool(assumption.get("needs_user_confirm")),
        }

    kosis = _find_kosis_lookup(item, variable)
    if kosis:
        return {
            "variable": variable,
            "source_type": "kosis_api",
            "status": "resolved",
            "value": None,
            "unit": kosis.get("unit"),
            "basis": f"{kosis.get('source') or 'KOSIS'} 조회값 사용",
            "requires_review": False,
        }

    candidate = _find_assumption_candidate(item, variable)
    if candidate:
        return {
            "variable": variable,
            "source_type": "similar_assumption_pool",
            "status": "candidate",
            "value": candidate.get("value"),
            "unit": candidate.get("unit"),
            "basis": (
                f"{candidate.get('bill_no') or ''} {candidate.get('bill_name') or ''} "
                f"유사 전제값 후보, 점수 {candidate.get('score')}"
            ).strip(),
            "requires_review": True,
        }

    tag_variable = _find_tag_variable(tag_candidate, variable)
    if tag_variable:
        return {
            "variable": variable,
            "source_type": "tag_similar_case",
            "status": "candidate",
            "value": tag_variable.get("value"),
            "unit": tag_variable.get("unit"),
            "basis": (
                f"{tag_candidate.get('bill_no') or ''} {tag_candidate.get('bill_name') or ''} "
                "TAG 변수 후보"
            ).strip(),
            "requires_review": True,
        }

    if assumption:
        return {
            "variable": variable,
            "source_type": assumption.get("source_type") or "policy_input",
            "status": "missing",
            "value": None,
            "unit": assumption.get("unit"),
            "basis": assumption.get("basis") or "정책 입력 또는 외부 자료 확인 필요",
            "requires_review": True,
        }

    return {
        "variable": variable,
        "source_type": "policy_input",
        "status": "missing",
        "value": None,
        "unit": None,
        "basis": "조문·표준자료·유사사례에서 확정값을 찾지 못해 사용자 확인 필요",
        "requires_review": True,
    }


def apply_formula_source_strategy(
    estimate: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
) -> int:
    """Record formula choice and variable assumption strategy for each item."""
    updated = 0
    for item in estimate.get("items") or []:
        calc = item.get("calculation") or {}
        base_ready = isinstance(calc, dict) and calc.get("base_amount_thousand") is not None
        tag_candidate = _best_tag_formula_candidate(item, tag_patterns)
        if tag_candidate:
            item["tag_formula_candidate"] = tag_candidate

        committee = item.get("committee_formula") or {}
        if committee:
            selected = {
                "source_type": "standard_structured_formula",
                "formula": committee.get("formula") or item.get("formula"),
                "label": "표준 위원회 회의수당 산식",
                "confidence": 0.9,
                "basis": "위원회 운영비는 회의횟수·수당지급대상 인원·회의수당 단가로 구조화",
                "requires_review": True,
            }
        elif base_ready and item.get("tag_amount_evidence"):
            selected = {
                "source_type": "tag_similar_formula",
                "formula": item.get("tag_amount_evidence", {}).get("formula") or item.get("formula"),
                "label": "공식 추계 사례 산식",
                "confidence": min(0.85, 0.55 + float(item.get("tag_amount_evidence", {}).get("score") or 0) / 2),
                "basis": "유사한 공식 비용추계 사례의 산식과 금액을 적용해 합리적 기준값을 산출",
                "requires_review": True,
            }
        elif tag_candidate and not base_ready:
            selected = {
                "source_type": "tag_similar_formula_candidate",
                "formula": tag_candidate.get("formula") or item.get("formula"),
                "label": "공식 추계 사례 산식 후보",
                "confidence": min(0.8, 0.5 + float(tag_candidate.get("score") or 0) / 2),
                "basis": "가장 유사한 공식 비용추계 사례의 산식을 적용해 합리적 추계 초안을 생성",
                "requires_review": True,
            }
        else:
            selected = {
                "source_type": "standard_formula",
                "formula": item.get("formula"),
                "label": "비용유형별 표준산식",
                "confidence": 0.65 if item.get("formula") else 0.4,
                "basis": "조문 비용유형에서 도출한 표준 산식",
                "requires_review": bool(item.get("requires_review")),
            }

        item["selected_formula"] = selected
        variables = [str(v) for v in item.get("variables_needed") or [] if str(v).strip()]
        item["assumption_strategy"] = [
            _variable_strategy(item, variable, tag_candidate)
            for variable in variables
        ]
        updated += 1
    return updated


def apply_tag_formula_evidence(
    estimate: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
) -> int:
    """High-confidence analogous TAG rows become reviewable base amounts.

    Transfer payments, facility construction, and institution creation are
    deliberately excluded because their scale is policy- and population-specific.
    """
    applied = 0
    for item in estimate.get("items") or []:
        if not item.get("allow_tag_estimate"):
            continue
        # A structured committee formula must be completed from its operands.
        # Replacing one missing operand with another committee's opaque annual
        # total would discard the bill-specific member count and hide the
        # actual data gap from Human-in-the-loop.
        if item.get("committee_formula"):
            continue
        best = _best_tag_formula_candidate(item, tag_patterns)
        if not best:
            continue
        calc = item.setdefault("calculation", {})
        if calc.get("base_amount_thousand") is None:
            calc["base_amount_thousand"] = best["base_amount_thousand"]
            calc["source_note"] = "유사 비용추계 TAG의 연간 금액 중앙값"
            item["tag_amount_evidence"] = best
            item["requires_review"] = True
            item["review_reason"] = "동일 계산유형의 유사사례 금액을 사용했으므로 적용 적합성 확인 필요"
            applied += 1
    return applied


def apply_reasonable_default_amounts(estimate: dict[str, Any]) -> int:
    """Leave unsupported amounts unresolved instead of inventing flat totals.

    The legacy implementation assigned family-wide placeholder totals (for
    example, 4 million won to every committee).  Even a transparent-looking
    placeholder is not bill evidence.  Values may now enter the calculation
    only through document facts, official statistics, compatible TAG cases,
    or explicit user input.
    """
    for item in estimate.get("items") or []:
        calc = item.setdefault("calculation", {})
        if not isinstance(calc, dict):
            calc = {}
            item["calculation"] = calc
        if calc.get("base_amount_thousand") is not None or calc.get("yearly_amounts_thousand"):
            continue
        item["requires_review"] = True
        item["review_reason"] = "공식 근거가 없는 평면 기본금액을 적용하지 않았습니다."
    return 0


def classify_estimation_status(
    estimate: dict[str, Any] | None,
    *,
    technical_reason: str | None = None,
) -> dict[str, Any]:
    if technical_reason and not estimate:
        return {
            "code": "technically_infeasible",
            "label": "기술적 추계 곤란",
            "blocking": True,
            "reason": technical_reason,
            "missing": {},
        }
    if not estimate or not estimate.get("items"):
        return {
            "code": "no_cost_formula",
            "label": "산식 후보 없음",
            "blocking": True,
            "reason": "비용항목 또는 산식 후보를 구성하지 못했습니다.",
            "missing": {},
        }

    computed = 0
    estimated = 0
    external: dict[str, list[str]] = {}
    policy: dict[str, list[str]] = {}
    for item in estimate.get("items") or []:
        calc = item.get("calculation") or {}
        yearly_series = calc.get("yearly_amounts_thousand")
        has_series = (
            calc.get("mode") == "yearly_series"
            and isinstance(yearly_series, list)
            and bool(yearly_series)
            and all(value is not None for value in yearly_series)
        )
        if calc.get("base_amount_thousand") is not None or has_series:
            computed += 1
            if item.get("tag_amount_evidence") or item.get("analogy_evidence") or item.get("requires_review"):
                estimated += 1
            continue
        item_name = str(item.get("name") or "비용항목")
        for assumption in item.get("assumptions") or []:
            if assumption.get("value") is not None:
                continue
            name = str(assumption.get("name") or "")
            source_type = str(assumption.get("source_type") or "")
            compact = _compact(name)
            if source_type == "external_data" or any(_compact(term) in compact for term in EXTERNAL_DATA_TERMS):
                external.setdefault(item_name, []).append(name)
            elif source_type == "policy_input" or any(_compact(term) in compact for term in POLICY_INPUT_TERMS):
                policy.setdefault(item_name, []).append(name)
            else:
                external.setdefault(item_name, []).append(name)

    item_count = len(estimate.get("items") or [])
    if computed == item_count:
        code = "computed_with_estimates" if estimated else "computed"
        return {
            "code": code,
            "label": "유사사례 기반 계산 완료" if estimated else "계산 완료",
            "blocking": False,
            "reason": "모든 비용항목의 계산 기준값이 구성되었습니다.",
            "missing": {},
        }
    if computed:
        return {
            "code": "partially_computed",
            "label": "일부 계산",
            "blocking": False,
            "reason": "일부 비용항목은 계산되었으나 추가 자료가 필요한 항목이 남아 있습니다.",
            "missing": {"external_data": external, "policy_input": policy},
        }
    if external:
        return {
            "code": "needs_external_data",
            "label": "외부 통계·기준자료 필요",
            "blocking": True,
            "reason": "산식은 구성되었지만 대상 규모·단가·실적 등 외부 자료가 필요합니다.",
            "missing": {"external_data": external, "policy_input": policy},
        }
    return {
        "code": "needs_policy_input",
        "label": "정책 전제 입력 필요",
        "blocking": True,
        "reason": "산식은 구성되었지만 사업 규모나 운영방식에 대한 정책 전제가 필요합니다.",
        "missing": {"policy_input": policy},
    }
