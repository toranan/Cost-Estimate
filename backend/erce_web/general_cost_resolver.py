"""Deterministic executor for the general cost formula registry.

AI/HITL selects a cost nature, formula and evidenced inputs.  This module only
validates operands and executes the formula; it never searches for or invents
missing numbers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


RESOLVER_VERSION = "general-cost-resolver-v4"


@dataclass(frozen=True)
class EvidenceInput:
    value: Any
    unit: str
    source: str
    source_ref: str = ""
    evidence_keys: tuple[str, ...] = ()


@dataclass
class GeneralCostRequest:
    cost_nature: str
    component: str
    formula_key: str
    years: int = 5
    start_year: int | None = None
    inputs: dict[str, EvidenceInput] = field(default_factory=dict)
    target_bill_no: str = ""
    target_bill_id: str = ""


@dataclass
class GeneralCostResolution:
    status: str
    resolver_version: str
    formula_key: str
    formula: str
    annual_amounts_thousand: list[int | None]
    year_labels: list[str]
    resolved_inputs: dict[str, EvidenceInput]
    missing_variables: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FORMULAS: dict[str, dict[str, Any]] = {
    "NON_TAX_FEE_REVENUE_AVERAGE_V1": {
        "expression": "과거 평균 수수료 수입 × 신규 면제비율",
        "required": ("historical_annual_fee_revenues", "exemption_rate"),
        "optional": (),
    },
    "NON_TAX_FEE_CHANNEL_SHIFT_V1": {
        "expression": "기존 채널 수입 면제액 + 다른 채널 예상 수입 × 무료채널 전환율",
        "required": ("historical_annual_fee_revenues", "exemption_rate",
                     "other_channel_base_revenue", "other_channel_base_year",
                     "other_channel_growth_rate", "channel_shift_rate"),
        "optional": (),
    },
    "PROJECT_ANNUAL_BUDGET_V1": {
        "expression": "해당 사업계획의 연도별 재원배분액",
        "required": ("annual_project_budget",),
        "optional": (),
    },
    "DIAGNOSTIC_TEST_SUBSIDY_NET_V1": {
        "expression": "연도별 지원 대상자 수 × 1인당 검사비 − 기존 사업의 연도별 비용",
        "required": ("annual_recipients", "test_unit_cost", "existing_annual_cost"),
        "optional": (),
    },
    "LEGAL_NOTIFICATION_V1": {
        "expression": "연도별 추가 대상사건 수 × 조치 시행비율 × 사건당 통지 수 × 연도별 송달단가",
        "required": ("annual_case_count", "action_rate", "notices_per_case", "notice_unit_price"),
        "optional": (),
    },
    "PERSONNEL_GRADE_V1": {
        "expression": "Σ(직급별 순증인원 × 직급별 1인당 보수)",
        "required": ("headcount_by_grade", "salary_by_grade"),
        "optional": ("growth_rate",),
    },
    "PERSONNEL_AVERAGE_V1": {
        "expression": "순증인원 × 1인당 보수",
        "required": ("headcount", "salary_per_person"),
        "optional": ("growth_rate",),
    },
    "PERSONNEL_POSITION_DIFFERENCE_V1": {
        "expression": "직급·직위 변경 인원 × 1인당 연간 보수 차액",
        "required": ("headcount", "annual_salary_difference"),
        "optional": ("growth_rate",),
    },
    "PERSONNEL_EMPLOYER_CONTRIBUTION_V1": {
        "expression": "보수총액 × 기관부담률",
        "required": ("annual_salary_amount", "employer_contribution_rate"),
        "optional": (),
    },
    "PERSONNEL_BASIC_EXPENSE_V1": {
        "expression": "보수총액 × 기본경비 비율",
        "required": ("annual_salary_amount", "basic_expense_ratio"),
        "optional": ("growth_rate",),
    },
    "PERSONNEL_ASSET_V1": {
        "expression": "순증인원 × 1인당 자산취득비",
        "required": ("headcount", "asset_unit_price_per_person"),
        "optional": (),
    },
    "COMMON_QUANTITY_UNIT_V1": {
        "expression": "수량 × 단가",
        "required": ("quantity", "unit_cost"),
        "optional": ("growth_rate",),
    },
    "GOODS_SERVICE_ACTIVITY_V1": {
        "expression": "수량 × 단가 × 연간 횟수",
        "required": ("service_quantity", "service_unit_cost", "frequency_per_year"),
        "optional": ("growth_rate",),
    },
    "PERIODIC_RESEARCH_PLAN_V1": {
        "expression": "계획수립 연구용역 단가 × 계획 수립연도(주기별 1회)",
        "required": ("plan_unit_cost", "recurrence_interval_years"),
        "optional": ("growth_rate",),
    },
    "ANNUAL_RESEARCH_SURVEY_V1": {
        "expression": "실태조사 연구용역 단가 × 연 1회",
        "required": ("survey_unit_cost",),
        "optional": ("growth_rate",),
    },
    "PERIODIC_RESEARCH_SURVEY_V1": {
        "expression": "실태조사 회당 단가 × 조사 실시연도(주기별 1회)",
        "required": ("survey_unit_cost", "recurrence_interval_years"),
        "optional": ("growth_rate",),
    },
    "INFORMATION_SYSTEM_OPERATION_V1": {
        "expression": "기준 연간 운영비 × 연도별 적용 상승률 누적",
        "required": ("annual_operating_amount",),
        "optional": (
            "growth_rate", "growth_rates_by_year", "base_year", "first_year_fraction",
        ),
    },
    "ANNUAL_OPERATING_BUDGET_V1": {
        "expression": "기준 연간 운영비 × 기준연도 이후 연도별 적용 상승률 누적",
        "required": ("annual_operating_amount",),
        "optional": ("growth_rate", "growth_rates_by_year", "base_year", "first_year_fraction"),
    },
    "INFORMATION_SYSTEM_OPERATION_STAFF_V1": {
        "expression": "운영인원 × 1인당 연간 인력단가 × 연도별 명목임금상승률 누적",
        "required": ("operation_headcount", "annual_labor_cost_per_person"),
        "optional": (
            "growth_rate", "growth_rates_by_year", "base_year", "first_year_fraction",
        ),
    },
    "COMMITTEE_MEETING_ALLOWANCE_V1": {
        "expression": "Σ(유급위원 수 × 연도별 위원회 구성 건수 × 구성당 회의 횟수 × 1인당 회의수당)",
        "required": ("committee_components",),
        "optional": ("growth_rate",),
    },
    "EVALUATION_PANEL_WITH_INITIAL_STUDY_V1": {
        "expression": "Σ(평가단별 기구 수 × 민간위원 수 × 연간 회의 수 × 수당) + 첫해 지표개발비",
        "required": ("committee_components", "initial_study_cost"),
        "optional": (),
    },
    "TRANSFER_RECIPIENT_V1": {
        "expression": "대상자 수 × 참여율 × 1인당 지급액 × 연간 지급횟수",
        "required": ("recipient_count", "benefit_per_recipient"),
        "optional": ("participation_rate", "payments_per_year", "growth_rate"),
    },
    "TRANSFER_SUBSIDY_RATE_V1": {
        "expression": "적격 사업비 × 보조율",
        "required": ("project_cost", "subsidy_rate"),
        "optional": ("participation_rate", "growth_rate"),
    },
    "SOCIAL_INSURANCE_PREMIUM_SUPPORT_V1": {
        "expression": "연도별 대상자 수 × 보험료 산정기준액 × 보험료율 × 지원비율 × 지원개월",
        "required": (
            "annual_recipients", "premium_base_amount", "insurance_rate",
            "support_rate", "support_months",
        ),
        "optional": (),
    },
    "SOCIAL_INSURANCE_BENEFIT_DURATION_V1": {
        "expression": "연도별 수급자 수 × 1인 1기간 급여단가 × 추가 지급기간",
        "required": ("annual_recipients", "benefit_unit_amount", "benefit_duration"),
        "optional": (),
    },
    "SOCIAL_INSURANCE_BASELINE_DIFFERENCE_V1": {
        "expression": "개정안 연도별 재정소요 − 현행·기준선 연도별 재정소요",
        "required": ("proposed_annual_amount", "baseline_annual_amount"),
        "optional": (),
    },
    "SOCIAL_INSURANCE_UNRECOVERED_PAYMENT_V1": {
        "expression": "연도별 선지급액 × (1 − 환수율)",
        "required": ("gross_annual_payment", "recovery_rate"),
        "optional": (),
    },
    "SOCIAL_INSURANCE_GOVERNMENT_SHARE_V1": {
        "expression": "사회보험 재정의 연도별 추가지출 × 국고지원율",
        "required": ("insurance_expenditure_increase", "government_support_rate"),
        "optional": (),
    },
    "CAPITAL_AREA_V1": {
        "expression": "면적 × 면적당 공사단가 × (1 + 설계·감리 비율)",
        "required": ("required_area", "construction_unit_cost"),
        "optional": ("design_supervision_rate", "growth_rate"),
    },
    "CAPITAL_ASSET_V1": {
        "expression": "자산 수량 × 자산 단가",
        "required": ("asset_quantity", "asset_unit_cost"),
        "optional": ("growth_rate",),
    },
    "TAX_RATE_CHANGE_V1": {
        "expression": "과세표준 × 세율 변화 × 적용률",
        "required": ("tax_base", "tax_rate_change"),
        "optional": ("take_up_rate", "growth_rate"),
    },
    "TAX_DEDUCTION_CHANGE_V1": {
        "expression": "적용 인원 × 공제액 변화 × 적용률",
        "required": ("taxpayer_count", "deduction_change"),
        "optional": ("take_up_rate", "growth_rate"),
    },
    "NON_TAX_FEE_CHANGE_V1": {
        "expression": "대상 건수 × 건당 수입 감소액 × 적용률",
        "required": ("transaction_count", "fee_change_per_case"),
        "optional": ("exemption_rate", "growth_rate"),
    },
    "EQUITY_PROJECT_SHARE_V1": {
        "expression": "총사업비 × 정부 분담률",
        "required": ("project_cost", "government_share"),
        "optional": ("growth_rate",),
    },
    "EQUITY_INSTITUTION_UNIT_V1": {
        "expression": "기관 수 × 기관당 출자·출연액",
        "required": ("institution_count", "contribution_per_institution"),
        "optional": ("growth_rate",),
    },
    "DIRECT_ANNUAL_AMOUNT_V1": {
        "expression": "검증된 연간 금액",
        "required": ("annual_amount",),
        "optional": ("growth_rate", "first_year_fraction"),
    },
}


def _coerce_input(value: EvidenceInput | Mapping[str, Any]) -> EvidenceInput:
    if isinstance(value, EvidenceInput):
        return value
    return EvidenceInput(
        value=value.get("value"),
        unit=str(value.get("unit") or ""),
        source=str(value.get("source") or ""),
        source_ref=str(value.get("source_ref") or ""),
        evidence_keys=tuple(str(key) for key in value.get("evidence_keys") or ()),
    )


def request_from_dict(payload: Mapping[str, Any]) -> GeneralCostRequest:
    return GeneralCostRequest(
        cost_nature=str(payload.get("cost_nature") or ""),
        component=str(payload.get("component") or ""),
        formula_key=str(payload.get("formula_key") or ""),
        years=int(payload.get("years") or 5),
        start_year=(int(payload["start_year"]) if payload.get("start_year") is not None else None),
        inputs={
            str(key): _coerce_input(value)
            for key, value in dict(payload.get("inputs") or {}).items()
        },
        target_bill_no=str(payload.get("target_bill_no") or ""),
        target_bill_id=str(payload.get("target_bill_id") or ""),
    )


def _number(inputs: Mapping[str, EvidenceInput], key: str, default: float | None = None) -> float:
    value = inputs.get(key)
    raw = value.value if value else default
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(key)
    return float(raw)


def _paired_sum(inputs: Mapping[str, EvidenceInput], left_key: str, right_key: str) -> float:
    left = inputs[left_key].value
    right = inputs[right_key].value
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            raise ValueError(f"{left_key},{right_key}")
        pairs = ((left[key], right[key]) for key in left)
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            raise ValueError(f"{left_key},{right_key}")
        pairs = zip(left, right)
    else:
        raise ValueError(f"{left_key},{right_key}")
    total = 0.0
    for left_value, right_value in pairs:
        if (
            not isinstance(left_value, (int, float))
            or isinstance(left_value, bool)
            or not isinstance(right_value, (int, float))
            or isinstance(right_value, bool)
        ):
            raise ValueError(f"{left_key},{right_key}")
        total += float(left_value) * float(right_value)
    return total


def _has_valid_value(inputs: Mapping[str, EvidenceInput], key: str) -> bool:
    if key not in inputs:
        return False
    raw = inputs[key].value
    if key in {"annual_project_budget", "historical_annual_fee_revenues"}:
        return isinstance(raw, (list, tuple)) and bool(raw)
    if key in {"annual_case_count", "action_rate", "notices_per_case", "notice_unit_price",
               "annual_recipients", "test_unit_cost", "existing_annual_cost", "quantity", "unit_cost",
               "headcount", "salary_per_person", "annual_salary_difference", "annual_salary_amount",
               "employer_contribution_rate", "basic_expense_ratio", "project_cost", "subsidy_rate",
               "premium_base_amount", "insurance_rate", "support_rate", "support_months",
               "benefit_unit_amount", "benefit_duration", "proposed_annual_amount",
               "baseline_annual_amount", "gross_annual_payment", "recovery_rate",
               "insurance_expenditure_increase", "government_support_rate"}:
        return isinstance(raw, (int, float, list, tuple)) and not isinstance(raw, bool)
    if key in {"headcount_by_grade", "salary_by_grade"}:
        return isinstance(raw, (Mapping, list, tuple)) and bool(raw)
    if key == "committee_components":
        return isinstance(raw, (list, tuple)) and bool(raw) and all(
            isinstance(component, Mapping) for component in raw
        )
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)


def _year_values(value: Any, years: int, *, name: str, default: float | None = None) -> list[float]:
    if value is None:
        if default is None:
            raise ValueError(name)
        return [default] * years
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)] * years
    if isinstance(value, (list, tuple)) and len(value) >= years:
        output: list[float] = []
        for raw in value[:years]:
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                raise ValueError(name)
            output.append(float(raw))
        return output
    raise ValueError(name)


def _committee_annual_amounts(inputs: Mapping[str, EvidenceInput], years: int) -> list[float]:
    """Execute the fixed committee-meeting formula over one or more groups."""
    raw_components = inputs["committee_components"].value
    if not isinstance(raw_components, (list, tuple)) or not raw_components:
        raise ValueError("committee_components")
    totals = [0.0] * years
    for index, component in enumerate(raw_components):
        if not isinstance(component, Mapping):
            raise ValueError(f"committee_components[{index}]")
        paid_members = _year_values(
            component.get("paid_members"), years,
            name=f"committee_components[{index}].paid_members",
        )
        meeting_unit_price = _year_values(
            component.get("meeting_unit_price"), years,
            name=f"committee_components[{index}].meeting_unit_price",
        )
        if component.get("annual_meetings") is not None:
            annual_meetings = _year_values(
                component.get("annual_meetings"), years,
                name=f"committee_components[{index}].annual_meetings",
            )
        else:
            meetings_per_instance = _year_values(
                component.get("meetings_per_instance"), years,
                name=f"committee_components[{index}].meetings_per_instance",
            )
            instances_per_year = _year_values(
                component.get("instances_per_year"), years,
                name=f"committee_components[{index}].instances_per_year",
                default=1.0,
            )
            annual_meetings = [
                meetings * instances
                for meetings, instances in zip(meetings_per_instance, instances_per_year)
            ]
        for year in range(years):
            totals[year] += paid_members[year] * annual_meetings[year] * meeting_unit_price[year]
    return totals


def _base_amount(key: str, inputs: Mapping[str, EvidenceInput]) -> float:
    if key == "PERSONNEL_GRADE_V1":
        return _paired_sum(inputs, "headcount_by_grade", "salary_by_grade")
    if key == "PERSONNEL_AVERAGE_V1":
        return _number(inputs, "headcount") * _number(inputs, "salary_per_person")
    if key == "PERSONNEL_POSITION_DIFFERENCE_V1":
        return _number(inputs, "headcount") * _number(inputs, "annual_salary_difference")
    if key == "PERSONNEL_EMPLOYER_CONTRIBUTION_V1":
        return _number(inputs, "annual_salary_amount") * _number(inputs, "employer_contribution_rate")
    if key == "PERSONNEL_BASIC_EXPENSE_V1":
        return _number(inputs, "annual_salary_amount") * _number(inputs, "basic_expense_ratio")
    if key == "PERSONNEL_ASSET_V1":
        return _number(inputs, "headcount") * _number(inputs, "asset_unit_price_per_person")
    if key == "COMMON_QUANTITY_UNIT_V1":
        return _number(inputs, "quantity") * _number(inputs, "unit_cost")
    if key == "GOODS_SERVICE_ACTIVITY_V1":
        return (
            _number(inputs, "service_quantity")
            * _number(inputs, "service_unit_cost")
            * _number(inputs, "frequency_per_year")
        )
    if key == "PERIODIC_RESEARCH_PLAN_V1":
        return _number(inputs, "plan_unit_cost")
    if key in {"ANNUAL_RESEARCH_SURVEY_V1", "PERIODIC_RESEARCH_SURVEY_V1"}:
        return _number(inputs, "survey_unit_cost")
    if key in {"INFORMATION_SYSTEM_OPERATION_V1", "ANNUAL_OPERATING_BUDGET_V1"}:
        return _number(inputs, "annual_operating_amount")
    if key == "INFORMATION_SYSTEM_OPERATION_STAFF_V1":
        return (
            _number(inputs, "operation_headcount")
            * _number(inputs, "annual_labor_cost_per_person")
        )
    if key == "TRANSFER_RECIPIENT_V1":
        return (
            _number(inputs, "recipient_count")
            * _number(inputs, "benefit_per_recipient")
            * _number(inputs, "participation_rate", 1.0)
            * _number(inputs, "payments_per_year", 1.0)
        )
    if key == "TRANSFER_SUBSIDY_RATE_V1":
        return (
            _number(inputs, "project_cost")
            * _number(inputs, "subsidy_rate")
            * _number(inputs, "participation_rate", 1.0)
        )
    if key == "CAPITAL_AREA_V1":
        return (
            _number(inputs, "required_area")
            * _number(inputs, "construction_unit_cost")
            * (1.0 + _number(inputs, "design_supervision_rate", 0.0))
        )
    if key == "CAPITAL_ASSET_V1":
        return _number(inputs, "asset_quantity") * _number(inputs, "asset_unit_cost")
    if key == "TAX_RATE_CHANGE_V1":
        return (
            _number(inputs, "tax_base")
            * _number(inputs, "tax_rate_change")
            * _number(inputs, "take_up_rate", 1.0)
        )
    if key == "TAX_DEDUCTION_CHANGE_V1":
        return (
            _number(inputs, "taxpayer_count")
            * _number(inputs, "deduction_change")
            * _number(inputs, "take_up_rate", 1.0)
        )
    if key == "NON_TAX_FEE_CHANGE_V1":
        return (
            _number(inputs, "transaction_count")
            * _number(inputs, "fee_change_per_case")
            * _number(inputs, "exemption_rate", 1.0)
        )
    if key == "EQUITY_PROJECT_SHARE_V1":
        return _number(inputs, "project_cost") * _number(inputs, "government_share")
    if key == "EQUITY_INSTITUTION_UNIT_V1":
        return _number(inputs, "institution_count") * _number(inputs, "contribution_per_institution")
    if key == "DIRECT_ANNUAL_AMOUNT_V1":
        return _number(inputs, "annual_amount")
    raise ValueError(key)


def _growth_factors(
    request: GeneralCostRequest,
    inputs: Mapping[str, EvidenceInput],
) -> list[float]:
    """Build cumulative factors from either a scalar or a versioned year table."""
    yearly_input = inputs.get("growth_rates_by_year")
    if yearly_input is None:
        growth = _number(inputs, "growth_rate", 0.0)
        if growth < -1:
            raise ValueError("growth_rate")
        return [(1.0 + growth) ** index for index in range(request.years)]

    if request.start_year is None:
        raise ValueError("start_year")
    base_year = int(_number(inputs, "base_year"))
    raw_rates = yearly_input.value
    if not isinstance(raw_rates, Mapping):
        raise ValueError("growth_rates_by_year")
    rates: dict[int, float] = {}
    for raw_year, raw_rate in raw_rates.items():
        try:
            year = int(raw_year)
        except (TypeError, ValueError) as exc:
            raise ValueError("growth_rates_by_year") from exc
        if not isinstance(raw_rate, (int, float)) or isinstance(raw_rate, bool):
            raise ValueError("growth_rates_by_year")
        rate = float(raw_rate)
        if rate < -1:
            raise ValueError("growth_rates_by_year")
        rates[year] = rate

    if base_year >= request.start_year:
        raise ValueError("base_year")
    factors: list[float] = []
    factor = 1.0
    for year in range(base_year + 1, request.start_year + request.years):
        if year not in rates:
            raise ValueError(f"growth_rates_by_year:{year}")
        factor *= 1.0 + rates[year]
        if year >= request.start_year:
            factors.append(factor)
    return factors


def _fee_revenue_annual_amounts(request: GeneralCostRequest, inputs: Mapping[str, EvidenceInput]) -> list[float]:
    from math import isfinite
    history = inputs["historical_annual_fee_revenues"].value
    if not isinstance(history, (list, tuple)) or not history or any(
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not isfinite(value) or value < 0 for value in history
    ):
        raise ValueError("historical_annual_fee_revenues")
    exemption = _number(inputs, "exemption_rate")
    if not isfinite(exemption) or not 0 <= exemption <= 1:
        raise ValueError("exemption_rate")
    baseline = sum(history) / len(history) * exemption
    if request.formula_key == "NON_TAX_FEE_REVENUE_AVERAGE_V1":
        return [baseline] * request.years
    base = _number(inputs, "other_channel_base_revenue")
    raw_year = _number(inputs, "other_channel_base_year")
    rate = _number(inputs, "other_channel_growth_rate")
    share = _number(inputs, "channel_shift_rate")
    if not isfinite(base) or base < 0:
        raise ValueError("other_channel_base_revenue")
    if not isfinite(raw_year) or raw_year != int(raw_year):
        raise ValueError("other_channel_base_year")
    if not isfinite(rate) or rate < -1:
        raise ValueError("other_channel_growth_rate")
    if not isfinite(share) or not 0 <= share <= 1:
        raise ValueError("channel_shift_rate")
    base_year = int(raw_year)
    if request.start_year is None or request.start_year < base_year:
        raise ValueError("start_year")
    try:
        values = [baseline + base * (1 + rate) ** (request.start_year + i - base_year) * share
                  for i in range(request.years)]
    except OverflowError as exc:
        raise ValueError("other_channel_growth_rate") from exc
    if any(not isfinite(value) for value in values):
        raise ValueError("other_channel_growth_rate")
    return values


def resolve_general_cost(request: GeneralCostRequest) -> GeneralCostResolution:
    if not 1 <= request.years <= 10:
        raise ValueError("years must be between 1 and 10")
    formula = FORMULAS.get(request.formula_key)
    if formula is None:
        raise ValueError(f"unsupported formula: {request.formula_key}")
    inputs = {key: _coerce_input(value) for key, value in request.inputs.items()}
    missing = [
        key for key in formula["required"]
        if not _has_valid_value(inputs, key)
    ]
    labels = [
        str(request.start_year + index) if request.start_year is not None else f"{index + 1}차년도"
        for index in range(request.years)
    ]
    if missing:
        return GeneralCostResolution(
            status="needs_evidence",
            resolver_version=RESOLVER_VERSION,
            formula_key=request.formula_key,
            formula=formula["expression"],
            annual_amounts_thousand=[None] * request.years,
            year_labels=labels,
            resolved_inputs=inputs,
            missing_variables=missing,
            reason_codes=[f"INVALID_OR_MISSING_VARIABLE:{key}" for key in missing],
        )
    try:
        first_fraction = _number(inputs, "first_year_fraction", 1.0)
        growth_factors = _growth_factors(request, inputs)
        if request.formula_key in {"NON_TAX_FEE_REVENUE_AVERAGE_V1", "NON_TAX_FEE_CHANNEL_SHIFT_V1"}:
            annual_won = _fee_revenue_annual_amounts(request, inputs)
        elif request.formula_key == "PROJECT_ANNUAL_BUDGET_V1":
            from math import isfinite
            raw_budget = inputs["annual_project_budget"].value
            if not isinstance(raw_budget, (list, tuple)) or len(raw_budget) != request.years:
                raise ValueError("annual_project_budget")
            annual_won = _year_values(raw_budget, request.years, name="annual_project_budget")
            if any(not isfinite(value) or value < 0 for value in annual_won):
                raise ValueError("annual_project_budget")
        elif request.formula_key == "DIAGNOSTIC_TEST_SUBSIDY_NET_V1":
            from math import isfinite
            operands = {
                key: _year_values(inputs[key].value, request.years, name=key)
                for key in formula["required"]
            }
            for key, values in operands.items():
                if any(not isfinite(value) or value < 0 for value in values):
                    raise ValueError(key)
            annual_won = [
                operands["annual_recipients"][i] * operands["test_unit_cost"][i]
                - operands["existing_annual_cost"][i] for i in range(request.years)
            ]
        elif request.formula_key == "LEGAL_NOTIFICATION_V1":
            from math import isfinite
            keys = formula["required"]
            operands = {
                key: _year_values(inputs[key].value, request.years, name=key)
                for key in keys
            }
            for key, values in operands.items():
                if any(not isfinite(value) or value < 0 for value in values):
                    raise ValueError(key)
            if any(value > 1 for value in operands["action_rate"]):
                raise ValueError("action_rate")
            annual_won = [
                operands["annual_case_count"][i] * operands["action_rate"][i]
                * operands["notices_per_case"][i] * operands["notice_unit_price"][i]
                for i in range(request.years)
            ]
        elif request.formula_key in {"COMMON_QUANTITY_UNIT_V1", "PERSONNEL_AVERAGE_V1",
                                    "PERSONNEL_POSITION_DIFFERENCE_V1",
                                    "PERSONNEL_EMPLOYER_CONTRIBUTION_V1", "PERSONNEL_BASIC_EXPENSE_V1"}:
            from math import isfinite
            left, right = formula["required"]
            for name in (left, right):
                if isinstance(inputs[name].value, (list, tuple)) and len(inputs[name].value) != request.years:
                    raise ValueError(name)
            quantities = _year_values(inputs[left].value, request.years, name=left)
            prices = _year_values(inputs[right].value, request.years, name=right)
            for name, values in ((left, quantities), (right, prices)):
                if any(not isfinite(value) or value < 0 for value in values):
                    raise ValueError(name)
            if right in {"employer_contribution_rate","basic_expense_ratio"} and any(value > 1 for value in prices):
                raise ValueError(right)
            annual_won = [quantity * price for quantity, price in zip(quantities, prices)]
        elif request.formula_key == "TRANSFER_SUBSIDY_RATE_V1":
            from math import isfinite
            costs = _year_values(inputs["project_cost"].value, request.years, name="project_cost")
            rates = _year_values(inputs["subsidy_rate"].value, request.years, name="subsidy_rate")
            participation = _year_values(
                inputs.get("participation_rate").value if inputs.get("participation_rate") else 1.0,
                request.years, name="participation_rate")
            if any(not isfinite(value) or value < 0 for value in costs):
                raise ValueError("project_cost")
            if any(not isfinite(value) or not 0 <= value <= 1 for value in rates):
                raise ValueError("subsidy_rate")
            if any(not isfinite(value) or not 0 <= value <= 1 for value in participation):
                raise ValueError("participation_rate")
            annual_won = [cost * rate * take_up for cost, rate, take_up in zip(costs, rates, participation)]
        elif request.formula_key == "SOCIAL_INSURANCE_PREMIUM_SUPPORT_V1":
            from math import isfinite
            operands = {
                key: _year_values(inputs[key].value, request.years, name=key)
                for key in formula["required"]
            }
            for key, values in operands.items():
                if any(not isfinite(value) or value < 0 for value in values):
                    raise ValueError(key)
            for key in ("insurance_rate", "support_rate"):
                if any(value > 1 for value in operands[key]):
                    raise ValueError(key)
            annual_won = [
                operands["annual_recipients"][i]
                * operands["premium_base_amount"][i]
                * operands["insurance_rate"][i]
                * operands["support_rate"][i]
                * operands["support_months"][i]
                for i in range(request.years)
            ]
        elif request.formula_key == "SOCIAL_INSURANCE_BENEFIT_DURATION_V1":
            from math import isfinite
            operands = {
                key: _year_values(inputs[key].value, request.years, name=key)
                for key in formula["required"]
            }
            for key, values in operands.items():
                if any(not isfinite(value) or value < 0 for value in values):
                    raise ValueError(key)
            annual_won = [
                operands["annual_recipients"][i]
                * operands["benefit_unit_amount"][i]
                * operands["benefit_duration"][i]
                for i in range(request.years)
            ]
        elif request.formula_key == "SOCIAL_INSURANCE_BASELINE_DIFFERENCE_V1":
            from math import isfinite
            proposed = _year_values(
                inputs["proposed_annual_amount"].value, request.years,
                name="proposed_annual_amount",
            )
            baseline = _year_values(
                inputs["baseline_annual_amount"].value, request.years,
                name="baseline_annual_amount",
            )
            if any(not isfinite(value) for value in proposed + baseline):
                raise ValueError("proposed_annual_amount")
            annual_won = [new - old for new, old in zip(proposed, baseline)]
        elif request.formula_key == "SOCIAL_INSURANCE_UNRECOVERED_PAYMENT_V1":
            from math import isfinite
            gross = _year_values(
                inputs["gross_annual_payment"].value, request.years,
                name="gross_annual_payment",
            )
            recovery = _year_values(
                inputs["recovery_rate"].value, request.years,
                name="recovery_rate",
            )
            if any(not isfinite(value) or value < 0 for value in gross):
                raise ValueError("gross_annual_payment")
            if any(not isfinite(value) or not 0 <= value <= 1 for value in recovery):
                raise ValueError("recovery_rate")
            annual_won = [amount * (1.0 - rate) for amount, rate in zip(gross, recovery)]
        elif request.formula_key == "SOCIAL_INSURANCE_GOVERNMENT_SHARE_V1":
            from math import isfinite
            spending = _year_values(
                inputs["insurance_expenditure_increase"].value, request.years,
                name="insurance_expenditure_increase",
            )
            rates = _year_values(
                inputs["government_support_rate"].value, request.years,
                name="government_support_rate",
            )
            if any(not isfinite(value) or value < 0 for value in spending):
                raise ValueError("insurance_expenditure_increase")
            if any(not isfinite(value) or not 0 <= value <= 1 for value in rates):
                raise ValueError("government_support_rate")
            annual_won = [amount * rate for amount, rate in zip(spending, rates)]
        elif request.formula_key in {"COMMITTEE_MEETING_ALLOWANCE_V1", "EVALUATION_PANEL_WITH_INITIAL_STUDY_V1"}:
            annual_won = _committee_annual_amounts(inputs, request.years)
            if request.formula_key == "EVALUATION_PANEL_WITH_INITIAL_STUDY_V1":
                annual_won[0] += _number(inputs, "initial_study_cost")
        elif request.formula_key in {"PERIODIC_RESEARCH_PLAN_V1", "PERIODIC_RESEARCH_SURVEY_V1"}:
            raw_interval = _number(inputs, "recurrence_interval_years")
            interval = int(raw_interval)
            if interval < 1 or raw_interval != interval:
                raise ValueError("recurrence_interval_years")
            base = _base_amount(request.formula_key, inputs)
            raw_offset = _number(inputs, "first_occurrence_offset_years", 0)
            offset = int(raw_offset)
            if offset < 0 or offset != raw_offset:
                raise ValueError("first_occurrence_offset_years")
            annual_won = [
                base if index >= offset and (index - offset) % interval == 0 else 0.0
                for index in range(request.years)
            ]
        else:
            base = _base_amount(request.formula_key, inputs)
            annual_won = [base] * request.years
    except ValueError as exc:
        return GeneralCostResolution(
            status="needs_evidence",
            resolver_version=RESOLVER_VERSION,
            formula_key=request.formula_key,
            formula=formula["expression"],
            annual_amounts_thousand=[None] * request.years,
            year_labels=labels,
            resolved_inputs=inputs,
            missing_variables=[str(exc)],
            reason_codes=[f"INVALID_NUMERIC_INPUT:{exc}"],
        )
    if not 0 <= first_fraction <= 1:
        raise ValueError("invalid first_year_fraction")
    annual_won = [
        value * growth_factors[index]
        for index, value in enumerate(annual_won)
    ]
    annual_won[0] *= first_fraction
    duration = inputs.get("duration")
    if duration is not None and isinstance(duration.value, (int, float)):
        active_years = max(0, min(request.years, int(duration.value)))
        annual_won[active_years:] = [0.0] * (request.years - active_years)
    return GeneralCostResolution(
        status="computed_review",
        resolver_version=RESOLVER_VERSION,
        formula_key=request.formula_key,
        formula=formula["expression"],
        annual_amounts_thousand=[int(round(value / 1_000.0)) for value in annual_won],
        year_labels=labels,
        resolved_inputs=inputs,
    )


__all__ = [
    "EvidenceInput", "FORMULAS", "GeneralCostRequest", "GeneralCostResolution",
    "request_from_dict", "resolve_general_cost",
]
