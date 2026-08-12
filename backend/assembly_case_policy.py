from __future__ import annotations

import re
from typing import Any


# Validation provenance only. Runtime decisions never branch on bill numbers.
VALIDATED_CASES = {
    "legislative_special_committee_bundle": {
        "confirmed": ["2126636", "2212534", "2213195", "2214994", "2215199"],
        "lesson": "국회 특별위원회는 인건비, 기관부담금, 기본경비, 자산취득비와 사업비를 분리한다.",
    },
    "simple_committee_meeting_cost": {
        "confirmed": ["2126640"],
        "lesson": "별도 사무조직이 없는 심의위원회는 회의횟수, 수당대상 인원과 수당 단가를 우선 적용한다.",
    },
    "delegated_scope_technical_difficulty": {
        "confirmed": ["2126641"],
        "lesson": "사업 범위와 수행 방식이 하위규정에 전부 위임되면 기술적 곤란을 우선 검토한다.",
    },
    "institution_establishment_composite": {
        "confirmed": ["2126648", "2215954"],
        "lesson": "기관 신설은 인력, 기본경비, 자산, 시설과 운영비를 복합 비용으로 분리한다.",
    },
    "research_service_analogy": {
        "confirmed": ["2217814"],
        "lesson": "실태조사는 조사 주기와 범위가 같은 사례만 단가 근거로 사용한다.",
    },
    "transfer_payment_variables": {
        "confirmed": ["2212022"],
        "lesson": "직접지원은 대상 수, 증분 단가, 지급 횟수와 집행률을 분리한다.",
    },
    "minor_existing_program_expansion": {
        "failed_then_learned": ["2126664"],
        "lesson": "기존 사업의 대상 범위 문구 변경과 안내 의무만으로 대규모 신규 사업을 가정하지 않는다.",
    },
}


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _article_ref(value: Any) -> str:
    match = re.search(r"제\d+조(?:의\d+)?", _compact(value))
    return match.group(0) if match else ""


def _article_title(value: Any) -> str:
    match = re.search(r"\(([^)]+)\)", str(value or ""))
    return _compact(match.group(1)) if match else ""


def _mark_no_incremental_cost(
    article: dict[str, Any],
    *,
    policy: str,
    reason: str,
    basis: str,
) -> None:
    article["cost_candidate_strength"] = "weak"
    article["estimate_feasibility"] = "no_incremental_cost"
    article["incremental_cost_status"] = policy
    article["case_policy"] = policy
    article["reason"] = reason
    article["exclusion_basis"] = basis


def _continuation_targets(document_text: str) -> set[str]:
    compact = _compact(document_text)
    targets: set[str] = set()
    for match in re.finditer(
        r"종전의.{0,220}?는이법(제\d+조(?:의\d+)?)에따른.{0,160}?로본다",
        compact,
    ):
        targets.add(match.group(1))
    return targets


def _deleted_legacy_refs(document_text: str) -> set[str]:
    compact = _compact(document_text)
    return {
        match.group(1)
        for match in re.finditer(
            r"(제\d+조(?:의\d+)?)(?:를|을)(?:각각)?삭제한다",
            compact,
        )
    }


def _matches_transferred_legacy_provision(
    article: dict[str, Any],
    deleted_refs: set[str],
) -> bool:
    title = _article_title(article.get("no"))
    if not title or not deleted_refs:
        return False
    for similar in article.get("similar_refs") or []:
        content = str(similar.get("content") or "")
        legacy_ref = _article_ref(content)
        legacy_title = _article_title(content)
        if legacy_ref in deleted_refs and legacy_title == title:
            return True
    return False


def _title_similarity(left: str, right: str) -> float:
    def grams(value: str) -> set[str]:
        compact = re.sub(r"(등의|등|의|및)", "", _compact(value))
        if len(compact) < 2:
            return {compact} if compact else set()
        return {compact[index:index + 2] for index in range(len(compact) - 1)}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _matches_referenced_existing_provision(
    article: dict[str, Any],
    document_text: str,
) -> bool:
    compact_document = _compact(document_text)
    if not re.search(r"(종전의.{0,120}?규정|다른법령).{0,120}?갈음", compact_document):
        return False
    own_ref = _article_ref(article.get("no"))
    referenced = {
        match.group(0)
        for match in re.finditer(r"제\d+조(?:의\d+)?", _compact(article.get("text")))
        if match.group(0) != own_ref
    }
    title = _article_title(article.get("no"))
    if not referenced or not title:
        return False
    for similar in article.get("similar_refs") or []:
        content = str(similar.get("content") or "")
        legacy_ref = _article_ref(content)
        legacy_title = _article_title(content)
        if legacy_ref in referenced and _title_similarity(title, legacy_title) >= 0.6:
            return True
    return False


def apply_validated_case_policy(
    articles: list[dict[str, Any]],
    *,
    document_text: str = "",
) -> list[dict[str, Any]]:
    """Apply reusable policies learned from answer-sheet comparisons."""
    continuation_targets = _continuation_targets(document_text)
    deleted_legacy_refs = _deleted_legacy_refs(document_text)
    integrated_plan_refs: set[str] = set()

    for article in articles:
        text = _compact(article.get("text"))
        rule = article.get("rule_cost_trigger") or {}
        ref = _article_ref(article.get("no"))
        title = _article_title(article.get("no"))

        if ref in continuation_targets:
            _mark_no_incremental_cost(
                article,
                policy="existing_program_continuation",
                reason=(
                    "부칙의 경과조치에서 종전 제도·기관을 이 조문에 따른 제도·기관으로 보도록 규정하여 "
                    "법률 제정 자체로 인한 신규 설치비나 운영비는 발생하지 않는 것으로 판단했습니다."
                ),
                basis="부칙상 종전 제도·기관 승계",
            )
            continue

        if _matches_transferred_legacy_provision(article, deleted_legacy_refs):
            _mark_no_incremental_cost(
                article,
                policy="transferred_existing_provision",
                reason=(
                    "동일 명칭과 내용의 종전 법률 조문을 삭제하고 이 법으로 옮겨 규정한 것으로 확인되어 "
                    "기존 사업을 계속 수행하는 데 따른 비용은 신규 추가재정소요에서 제외했습니다."
                ),
                basis="삭제되는 종전 법률 조문과 동일한 제도 이전",
            )
            continue

        if _matches_referenced_existing_provision(article, document_text):
            _mark_no_incremental_cost(
                article,
                policy="referenced_existing_program",
                reason=(
                    "조문이 현행 법률의 동일 제도를 인용하고 부칙에서 종전 규정을 이 법으로 갈음하도록 하므로, "
                    "기존 제도의 계속 수행에 해당하여 법률 제정 자체의 추가재정소요에서는 제외했습니다."
                ),
                basis="현행 법률상 동일 제도 및 부칙상 규정 승계",
            )
            continue

        if (
            ("책무" in title or re.search(r"(책임을강조|필요한시책을마련)", text))
            and re.search(r"(지원|시책|정책)", text)
            and not re.search(r"(지급액|지원금액|\d+명|\d+개소|\d+원|\d+만원)", text)
        ):
            article["cost_candidate_strength"] = "weak"
            article["estimate_feasibility"] = "non_attachment_review"
            article["incremental_cost_status"] = "declarative_unquantified"
            article["case_policy"] = "declarative_unquantified"
            article["reason"] = (
                "국가와 지방자치단체의 책무와 정책 방향을 선언한 규정으로, 구체적인 사업·대상·지원 수준이 "
                "정해지지 않아 독립적인 추가재정소요를 합리적으로 산정하기 어렵습니다."
            )
            article["exclusion_basis"] = "선언적 책무 및 구체적 사업 규모 부재"
            continue

        if (
            re.search(r"(기본계획|종합계획)", title + text)
            and re.search(
                r"(기존|현행)?[^.]{0,120}(계획|시책).{0,30}연계하여.{0,40}수립",
                text,
            )
        ):
            integrated_plan_refs.add(ref)
            _mark_no_incremental_cost(
                article,
                policy="integrated_plan_basic_expense",
                reason=(
                    "기존 법정계획과 연계하여 수립하도록 한 행정계획으로, 별도 사업조직이나 지급 의무가 없어 "
                    "통상적인 행정인력과 기본경비 범위에서 수행 가능한 것으로 판단했습니다."
                ),
                basis="기존 법정계획 연계 및 기본경비 수행",
            )
            continue

        if (
            "시행계획" in title
            and re.search(r"(종합계획|기본계획)에따라", text)
            and integrated_plan_refs
        ):
            integrated_plan_refs.add(ref)
            _mark_no_incremental_cost(
                article,
                policy="integrated_plan_basic_expense",
                reason=(
                    "기존 계획과 연계된 종합계획의 연도별 집행계획으로서 독립적인 신규 사업이 아니라 "
                    "관계 기관의 통상적인 행정인력과 기본경비로 수행하는 업무로 판단했습니다."
                ),
                basis="연계 계획의 연도별 시행 및 기본경비 수행",
            )
            continue

        if (
            re.search(r"(실태조사|현황조사)", title + text)
            and re.search(r"(종합계획|기본계획|시행계획).{0,30}(반영|활용)", text)
            and integrated_plan_refs
            and not re.search(r"(신규로|별도의|전담기관|센터를설치)", text)
        ):
            _mark_no_incremental_cost(
                article,
                policy="linked_existing_survey",
                reason=(
                    "기존 법정계획과 연계되는 정책자료 조사로서 별도 신규 조직이나 지원사업을 수반하지 않아 "
                    "기존 조사·행정체계에서 수행 가능한 항목으로 분류했습니다."
                ),
                basis="기존 계획·조사체계 연계",
            )
            continue

        if (
            re.search(r"(?:출연금|보조금|교부액|지원금).{0,80}차등지원할수있", text)
            and not re.search(r"(총액을증액|추가로지원|신규예산|지원금액을증액)", text)
        ):
            _mark_no_incremental_cost(
                article,
                policy="differential_allocation_only",
                reason=(
                    "평가 결과에 따라 기존 출연금·보조금을 차등 배분하는 규정으로, "
                    "지원 총액의 증가 의무는 확인되지 않아 추가재정소요에서 제외했습니다."
                ),
                basis="기존 지원예산의 차등 배분이며 총액 증가 규정 부재",
            )
            continue

        if (
            re.search(r"(지원|보조)", text)
            and re.search(r"할수있", text)
            and not re.search(r"(매월|연1회|매년|\d+명|\d+개소|\d+원|\d+만원|지원율)", text)
        ):
            article["cost_candidate_strength"] = "weak"
            article["estimate_feasibility"] = "non_attachment_review"
            article["incremental_cost_status"] = "discretionary_unquantified"
            article["case_policy"] = "discretionary_unquantified"
            article["reason"] = (
                "지원 여부가 재량이고 대상·단가·지원율·시행 규모가 정해지지 않아 현 단계에서는 "
                "합리적인 추가재정소요를 산정하기 어렵습니다."
            )
            article["exclusion_basis"] = "재량규정 및 정책 규모 불확정"
            continue

        if (
            article.get("trigger_type") in {"대상확대", "의무부과"}
            and re.search(r"(적극적으로)?안내하여야|알려야|홍보하여야", text)
            and not re.search(r"(지급|지원금|급여|수당|보조금|비용을지원)", text)
        ):
            article["cost_candidate_strength"] = "weak"
            article["estimate_feasibility"] = "minor_or_absorbable"
            article["case_policy"] = "minor_existing_program_expansion"
            article["reason"] = (
                "기존 사업의 안내·홍보 의무로서 통상적인 행정운영 범위에서 집행될 가능성이 높아 "
                "독립적인 대규모 비용항목으로 보지 않습니다."
            )
            continue

        if (
            article.get("trigger_type") == "대상확대"
            and not re.search(r"(지급|지원금|급여|수당|보조금|비용을지원|지원한다|지원하여야)", text)
        ):
            article["cost_candidate_strength"] = "weak"
            article["estimate_feasibility"] = "minor_or_absorbable"
            article["case_policy"] = "minor_existing_program_expansion"
            article["reason"] = (
                "적용 대상 범위는 변경되지만 조문 자체에 신규 급여·지원 단가 또는 별도 사업 의무가 없어 "
                "기존 사업 내 흡수 가능성과 소액 미첨부 여부를 우선 검토합니다."
            )
            continue

        if (
            rule.get("estimate_feasibility") == "non_attachment_review"
            and re.search(r"(구체적|필요한사항).{0,45}(대통령령|부령|장관이정한다)", text)
            and not re.search(r"(매월|연1회|매년|\d+명|\d+개소|\d+원|\d+만원)", text)
        ):
            article["case_policy"] = "delegated_scope_technical_difficulty"

        # ── 조직 신설 + 대통령령 위임 다수 + 구체 정원 미명시
        #    → 미첨부 3호(기술적 곤란) 후보로 분류
        #    (중대범죄수사청·공소청 등 검찰조직 신설 케이스)
        if article.get("trigger_type") == "조직설치":
            delegation_hits = len(re.findall(
                r"대통령령으로정한다|대통령령이정하는|대통령령으로정하는바에|부령으로정한다",
                text,
            ))
            has_concrete_size = bool(re.search(
                r"\d+(?:명|인)이내|\d+(?:명|인)으로구성|위원장.{0,15}\d+(?:명|인)|정원.{0,25}\d+(?:명|인)"
                r"|차관.{0,10}(예에준|보수)|과장급|사무처장",
                text,
            ))
            if delegation_hits >= 2 and not has_concrete_size:
                article["case_policy"] = "organization_delegation_heavy"
                article["estimate_feasibility"] = "non_attachment_review"
                article["non_attachment_risk"] = "high"
                article["cost_candidate_strength"] = "medium"
                article["exclusion_basis"] = (
                    "조직 신설 재정소요 예상, 조직구조·정원 대통령령 위임"
                )
                article["reason"] = (
                    "조직 신설에 따른 추가재정소요가 예상되나 조직구조·정원·직급 등 "
                    "핵심 산정 변수가 대통령령에 위임되어 현 단계에서 합리적 추정이 곤란하여 "
                    "미첨부 3호(기술적 곤란) 검토 대상으로 분류합니다."
                )
                article["review_reason"] = article["reason"]

    # LLM 단독 후보는 공공 재정지출 근거가 조문이나 결정 규칙에서 확인될 때만 유지한다.
    # 면허·등록·교육·신고 같은 통상 행정과 민간기관 자체 의무를 재정수반요인으로
    # 과대 분류하는 것을 막되, 규칙 엔진이 포착한 지원·조직·조사 후보는 보존한다.
    public_fiscal_signal = re.compile(
        r"(국가|정부|지방자치단체|장관).{0,80}"
        r"(비용|경비|예산|재정지원|보조|지원금|수당|인건비|설치.{0,20}운영|사업을실시)"
        r"|"
        r"(비용의전부또는일부|경비를보조|예산의범위|국고|보조금|지원금을지급)"
    )
    private_obligation = re.compile(
        r"(기관|시설|의료기관|사용자|사업주)의장.{0,100}"
        r"(하여야한다|조치를강구|채용|배치)"
    )
    routine_administration = re.compile(
        r"국가시험"
        r"|(?:면허|자격).{0,50}(?:등록|발급|조건)"
        r"|보수교육"
        r"|실태및취업상황.{0,30}신고"
    )
    # 재정수입 감소(조세 감면·면제 등) 및 일몰(적용기한) 연장 신호.
    # 조세특례 개정은 조문이 날짜 치환만으로 구성되는 경우가 많아
    # 지출 신호가 없어도 재정수반요인(세수 감소)이므로 억제하지 않는다.
    tax_term_signal = re.compile(
        r"조세|세금|세수|지방세|국세|취득세|재산세|소득세|법인세|부가가치세|인지세|"
        r"등록면허세|세액|과세|조세특례|지방세특례"
    )
    revenue_or_sunset_signal = re.compile(
        r"감면|면제|비과세|세액공제|소득공제|과세특례|중과세.{0,10}배제|세율.{0,15}인하"
        r"|적용기한"
        r"|\d{4}년\d{1,2}월\d{1,2}일까지.{0,80}\d{4}년\d{1,2}월\d{1,2}일까지"
    )
    tax_refs = {
        _compact(article.get("no"))
        for article in articles
        if tax_term_signal.search(
            _article_title(article.get("no")) + " " + _compact(article.get("text"))
        )
    }
    for article in articles:
        if not article.get("cost_trigger"):
            continue
        text = _compact(article.get("text"))
        article_ref = _compact(article.get("no"))
        probe = _article_title(article.get("no")) + " " + text
        # Keep a separator between title and body. Concatenating
        # "직원등의임면" and "제15조..." created the accidental token
        # "면제" and falsely classified an appointment article as a tax exemption.
        if (
            revenue_or_sunset_signal.search(probe)
            and (tax_term_signal.search(probe) or article_ref in tax_refs)
        ):
            # A tax reduction is a revenue loss even when an earlier LLM/rule
            # labeled the beneficiary-side effect as a direct subsidy.
            article["trigger_type"] = "세수감소"
            article["formula_family"] = "revenue_loss"
            article["reason"] = (
                "조세·부담금 감면 또는 적용기한 연장으로 재정수입 감소(또는 지출 지속)가 "
                "예상되어 재정수반요인으로 유지합니다."
            )
            continue
        if article.get("rule_cost_trigger"):
            continue
        policy = str(article.get("case_policy") or "")
        has_public_fiscal_signal = bool(public_fiscal_signal.search(text))
        is_private_obligation = bool(private_obligation.search(text))
        is_routine_administration = bool(routine_administration.search(
            _article_title(article.get("no")) + text
        ))
        transferred_without_fiscal_signal = (
            policy == "transferred_existing_provision"
            and not has_public_fiscal_signal
        )
        if (
            transferred_without_fiscal_signal
            or is_routine_administration
            or (is_private_obligation and not has_public_fiscal_signal)
            or (not policy and not has_public_fiscal_signal)
        ):
            article["cost_trigger"] = False
            article["trigger_type"] = "없음"
            article["cost_candidate_strength"] = "none"
            article["estimate_feasibility"] = "not_applicable"
            article["case_policy"] = "unverified_ai_candidate_suppressed"
            article["reason"] = (
                "공공 재정지출을 직접 수반하는 지원·지급·조직·사업 근거가 확인되지 않아 "
                "통상 행정 또는 민간 부담 규정으로 분류했습니다."
            )
    return articles
