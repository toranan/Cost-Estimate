from __future__ import annotations

import re
from typing import Any


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _token_overlap(a: str, b: str) -> float:
    ca = _compact(a)
    cb = _compact(b)
    if len(ca) < 2 or len(cb) < 2:
        return 0.0
    ta = {ca[i:i + 2] for i in range(len(ca) - 1)}
    tb = {cb[i:i + 2] for i in range(len(cb) - 1)}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


TEMPLATES: dict[str, dict[str, Any]] = {
    "personnel_compensation": {
        "label": "직급별 보수 산식",
        "standard_formula": "보수 = Σ(직급별 인원 × 직급별 1인당 보수)",
        "calculation_type": "sum_product",
        "variables": ["직급별 인원", "직급별 1인당 보수", "공무원임금상승률"],
        "recurrence": "annual",
        "growth_variable": "공무원임금상승률",
        "notes": "직급별 인원과 보수표 또는 국회 비용추계서의 직급별 보수 전제가 필요합니다.",
    },
    "employer_contribution": {
        "label": "기관부담금 산식",
        "standard_formula": "기관부담금 = 보수 × 기관부담요율",
        "calculation_type": "multiply_rate",
        "variables": ["보수", "기관부담요율"],
        "recurrence": "annual",
        "growth_variable": None,
        "notes": "기관부담요율은 연도별로 달라질 수 있어 기준값 후보 확인이 필요합니다.",
    },
    "basic_expense": {
        "label": "기본경비 산식",
        "standard_formula": "기본경비 = 보수 또는 인건비 × 기본경비 비율",
        "calculation_type": "multiply_rate",
        "variables": ["보수 또는 인건비", "기본경비 비율"],
        "recurrence": "annual",
        "growth_variable": None,
        "notes": "기본경비 비율은 기관별로 달라질 수 있습니다.",
    },
    "asset_acquisition": {
        "label": "자산취득비 산식",
        "standard_formula": "자산취득비 = 증원인원 × 1인당 자산취득비",
        "calculation_type": "unit_cost_times_count",
        "variables": ["증원인원", "1인당 자산취득비"],
        "recurrence": "one_time",
        "growth_variable": None,
        "notes": "통상 최초 연도 1회성 비용으로 검토합니다.",
    },
    "committee_operation": {
        "label": "위원회 운영비 산식",
        "standard_formula": "사업비 = 유사 위원회 연간 운영비 또는 회의수당 단가 × 회의횟수 × 참석인원",
        "calculation_type": "operating_cost",
        "variables": ["연간 운영비 기준액", "회의수당 단가", "회의횟수", "참석인원", "소비자물가상승률"],
        "recurrence": "annual",
        "growth_variable": "소비자물가상승률",
        "notes": "유사 위원회 운영비를 쓰는지, 회의수당 방식으로 쪼개는지 확인해야 합니다.",
    },
    "research_service": {
        "label": "연구용역/실태조사 산식",
        "standard_formula": "사업비 = 용역 단가 × 수행 횟수",
        "calculation_type": "unit_cost_times_frequency",
        "variables": ["용역 단가", "수행 횟수", "수행 주기", "소비자물가상승률"],
        "recurrence": "periodic_or_one_time",
        "growth_variable": None,
        "notes": "기본계획/실태조사는 수행 주기와 최초 수행연도가 핵심 변수이며, 물가상승률은 근거가 있을 때만 적용합니다.",
    },
    "subsidy_payment": {
        "label": "지원금 지급 산식",
        "standard_formula": "지원금 = 지원 대상 수 × 1인당 또는 건당 지원 단가",
        "calculation_type": "unit_cost_times_count",
        "variables": ["지원 대상 수", "지원 단가", "집행률 또는 신청률", "증가율"],
        "recurrence": "annual",
        "growth_variable": None,
        "notes": "대상자 수는 KOSIS/API 또는 소관 부처 자료와 연결해야 합니다.",
    },
    "revenue_loss": {
        "label": "조세 감면 연장 세수감소 산식",
        "standard_formula": "세수감소액 = 최근 3~5년 연평균 감면실적 × 적용기간",
        "calculation_type": "annual_revenue_loss_times_period",
        "variables": ["연평균 감면실적", "적용기한 연장기간"],
        "recurrence": "annual",
        "growth_variable": None,
        "notes": "감면실적은 조세지출예산서·지방세통계연감 또는 동일 조문의 공식 추계서를 사용합니다.",
    },
}


RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("employer_contribution", re.compile(r"기관부담|부담요율|부담금")),
    ("basic_expense", re.compile(r"기본경비|인건비대비|보수액대비")),
    ("asset_acquisition", re.compile(r"자산취득|비품|집기|PC")),
    ("personnel_compensation", re.compile(r"보수|인건비|직급|봉급|급여")),
    ("research_service", re.compile(r"연구용역|실태조사|기본계획|종합계획|계획수립|조사")),
    ("committee_operation", re.compile(r"위원회|심의회|협의회|회의수당|위원회운영|위원회사업비")),
    ("subsidy_payment", re.compile(r"지원금|보조금|수당|급여|감면|지급")),
)


def infer_template_key(item: dict[str, Any]) -> str | None:
    name_context = _compact(" ".join(
        str(part or "")
        for part in (item.get("name"), item.get("category"), item.get("trigger_ref"))
    ))
    # A committee item can mention supporting personnel in its prose formula.
    # Classify by the cost-bearing entity in the item name first; otherwise the
    # generic personnel rule would turn committee operation into a 500M-won
    # staffing default and duplicate the deterministic committee item.
    if (
        re.search(r"위원회|심의회|협의회", name_context)
        and not re.search(r"사무처|사무국|지원인력|직원|인건비|보수", name_context)
    ):
        return "committee_operation"
    text = _compact(" ".join(
        str(part or "")
        for part in [
            item.get("name"),
            item.get("category"),
            item.get("formula"),
            item.get("trigger_ref"),
            " ".join(str(v) for v in item.get("variables_needed") or []),
        ]
    ))
    if (
        re.search(
            r"조세|세금|세수|지방세|국세|취득세|재산세|소득세|법인세|"
            r"부가가치세|인지세|등록면허세|세액|과세",
            text,
        )
        and re.search(r"감면|면제|비과세|세액공제|소득공제|세수감소|적용기한", text)
    ):
        return "revenue_loss"
    for key, pattern in RULES:
        if pattern.search(text):
            return key
    return None


def _formula_evidence_matches(template_key: str, formula: str) -> bool:
    compact = _compact(formula)
    if not compact:
        return False
    if template_key == "employer_contribution":
        return "기관부담" in compact or "부담요율" in compact
    if template_key == "basic_expense":
        return "기본경비" in compact
    if template_key == "asset_acquisition":
        return "자산취득" in compact or "1인당" in compact
    if template_key == "committee_operation":
        return "위원회" in compact or "회의" in compact or "운영" in compact
    if template_key == "research_service":
        return (
            "용역" in compact
            or "실태조사" in compact
            or "기본계획" in compact
            or "종합계획" in compact
            or "계획수립" in compact
        )
    if template_key == "personnel_compensation":
        return "보수" in compact or "인건비" in compact
    if template_key == "subsidy_payment":
        return "지원" in compact or "지급" in compact or "대상" in compact
    if template_key == "revenue_loss":
        return (
            "감면" in compact
            or "면제" in compact
            or "세수감소" in compact
            or "조세지출" in compact
        )
    return False


def find_tag_formula_evidence(
    item: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
    template_key: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    item_name = str(item.get("name") or "")
    category = str(item.get("category") or "")
    evidence: list[dict[str, Any]] = []

    for pattern in tag_patterns:
        for tagged_item in pattern.get("items") or []:
            formulas = [
                str(amount.get("formula") or "")
                for amount in tagged_item.get("amounts") or []
                if amount.get("formula")
            ]
            if not formulas:
                continue
            formula_text = next(
                (formula for formula in formulas if _formula_evidence_matches(template_key, formula)),
                formulas[0],
            )
            if not _formula_evidence_matches(template_key, formula_text):
                continue

            tagged_name = str(tagged_item.get("name") or "")
            tagged_category = str(tagged_item.get("category") or "")
            score = _token_overlap(item_name, tagged_name)
            if category and tagged_category and category == tagged_category:
                score += 0.3
            if score < 0.08:
                continue

            evidence.append({
                "bill_no": pattern.get("bill_no"),
                "bill_name": pattern.get("bill_name"),
                "item_name": tagged_name,
                "item_category": tagged_category,
                "formula_text": formula_text,
                "score": round(score, 3),
            })

    evidence.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return evidence[:limit]


def build_formula_template(
    item: dict[str, Any],
    tag_patterns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    explicit_family = str(item.get("formula_family") or "")
    template_key = explicit_family if explicit_family in TEMPLATES else infer_template_key(item)
    if not template_key:
        return None

    template = dict(TEMPLATES[template_key])
    evidence = find_tag_formula_evidence(item, tag_patterns, template_key)
    confidence = 0.55
    if evidence:
        confidence = min(0.9, 0.65 + max(float(e.get("score") or 0) for e in evidence))

    return {
        "template_key": template_key,
        "label": template["label"],
        "standard_formula": template["standard_formula"],
        "calculation_type": template["calculation_type"],
        "variables": template["variables"],
        "recurrence": template["recurrence"],
        "growth_variable": template["growth_variable"],
        "notes": template["notes"],
        "tag_formula_evidence": evidence,
        "confidence": round(confidence, 3),
        "source": "TAG 산식 패턴 기반 표준 템플릿",
    }
