"""Deterministic formula routing after the AI has classified a cost item."""
from __future__ import annotations

from dataclasses import dataclass
from backend.general_cost_resolver import FORMULAS


@dataclass(frozen=True)
class FormulaRoute:
    route_key: str
    formula_key: str
    expression: str
    cost_nature: str = "goods_services"


ERCE_FORMULA_ROUTES: dict[str, FormulaRoute] = {
    "information_system_project_plan": FormulaRoute(
        route_key="information_system_project_plan",
        formula_key="PROJECT_ANNUAL_BUDGET_V1",
        expression="해당 사업계획의 연도별 재원배분액(포함된 운영·유지관리비 중복 가산 금지)",
    ),
    "diagnostic_test_subsidy": FormulaRoute(
        route_key="diagnostic_test_subsidy",
        formula_key="DIAGNOSTIC_TEST_SUBSIDY_NET_V1",
        expression="연도별 지원 대상자 수 × 1인당 검사비 − 기존 사업의 연도별 비용",
    ),
    "legal_notification": FormulaRoute(
        route_key="legal_notification",
        formula_key="LEGAL_NOTIFICATION_V1",
        expression="연도별 추가 대상사건 수 × 조치 시행비율 × 사건당 통지 수 × 연도별 송달단가",
    ),
    "evaluation_panel_with_initial_study": FormulaRoute(
        route_key="evaluation_panel_with_initial_study",
        formula_key="EVALUATION_PANEL_WITH_INITIAL_STUDY_V1",
        expression="Σ(평가단별 기구 수 × 민간위원 수 × 연간 회의 수 × 수당) + 첫해 지표개발비",
    ),
    "committee_operation": FormulaRoute(
        route_key="committee_operation",
        formula_key="COMMITTEE_MEETING_ALLOWANCE_V1",
        expression=(
            "위원회 수 × 유급 민간위원 수 × 연간 회의 횟수 × "
            "1인 1회당 회의수당"
        ),
    ),
    "research_plan": FormulaRoute(
        route_key="research_plan",
        formula_key="PERIODIC_RESEARCH_PLAN_V1",
        expression="계획수립 연구용역 단가 × 계획 수립연도(주기별 1회)",
    ),
    "research_survey": FormulaRoute(
        route_key="research_survey",
        formula_key="PERIODIC_RESEARCH_SURVEY_V1",
        expression="실태조사 회당 단가 × 조사 실시연도(주기별 1회)",
    ),
}

# Every existing calculator formula has an explicit ERCE route. These routes
# select only a formula, never a whole precedent or an inferred amount.
DIRECT_FORMULA_ROUTE_KEYS = {
    "personnel_grade": ("PERSONNEL_GRADE_V1", "personnel"),
    "personnel_average": ("PERSONNEL_AVERAGE_V1", "personnel"),
    "personnel_position_difference": ("PERSONNEL_POSITION_DIFFERENCE_V1", "personnel"),
    "personnel_employer_contribution": ("PERSONNEL_EMPLOYER_CONTRIBUTION_V1", "personnel"),
    "personnel_basic_expense": ("PERSONNEL_BASIC_EXPENSE_V1", "personnel"),
    "personnel_asset": ("PERSONNEL_ASSET_V1", "capital"),
    "quantity_unit": ("COMMON_QUANTITY_UNIT_V1", "goods_services"),
    "burial_supplies": ("COMMON_QUANTITY_UNIT_V1", "goods_services"),
    "burial_plot_construction": ("COMMON_QUANTITY_UNIT_V1", "capital"),
    "goods_service_activity": ("GOODS_SERVICE_ACTIVITY_V1", "goods_services"),
    "interagency_meeting_operation": ("GOODS_SERVICE_ACTIVITY_V1", "goods_services"),
    "interagency_annual_operation": ("ANNUAL_OPERATING_BUDGET_V1", "goods_services"),
    "legislative_committee_operation": ("ANNUAL_OPERATING_BUDGET_V1", "goods_services"),
    "committee_annual_operation": ("ANNUAL_OPERATING_BUDGET_V1", "goods_services"),
    "annual_research_survey": ("ANNUAL_RESEARCH_SURVEY_V1", "goods_services"),
    "information_system_operation": ("INFORMATION_SYSTEM_OPERATION_V1", "goods_services"),
    "information_system_operation_staff": ("INFORMATION_SYSTEM_OPERATION_STAFF_V1", "goods_services"),
    "committee_components": ("COMMITTEE_MEETING_ALLOWANCE_V1", "goods_services"),
    "transfer_recipient": ("TRANSFER_RECIPIENT_V1", "transfer"),
    "transfer_subsidy_rate": ("TRANSFER_SUBSIDY_RATE_V1", "transfer"),
    "social_insurance_premium_support": ("SOCIAL_INSURANCE_PREMIUM_SUPPORT_V1", "transfer"),
    "social_insurance_benefit_duration": ("SOCIAL_INSURANCE_BENEFIT_DURATION_V1", "transfer"),
    "social_insurance_baseline_difference": ("SOCIAL_INSURANCE_BASELINE_DIFFERENCE_V1", "transfer"),
    "social_insurance_unrecovered_payment": ("SOCIAL_INSURANCE_UNRECOVERED_PAYMENT_V1", "transfer"),
    "social_insurance_government_share": ("SOCIAL_INSURANCE_GOVERNMENT_SHARE_V1", "transfer"),
    "capital_area": ("CAPITAL_AREA_V1", "capital"),
    "capital_asset": ("CAPITAL_ASSET_V1", "capital"),
    "tax_rate_change": ("TAX_RATE_CHANGE_V1", "tax_revenue"),
    "tax_deduction_change": ("TAX_DEDUCTION_CHANGE_V1", "tax_revenue"),
    "non_tax_fee_change": ("NON_TAX_FEE_CHANGE_V1", "non_tax_revenue"),
    "non_tax_fee_revenue_average": ("NON_TAX_FEE_REVENUE_AVERAGE_V1", "non_tax_revenue"),
    "non_tax_fee_channel_shift": ("NON_TAX_FEE_CHANNEL_SHIFT_V1", "non_tax_revenue"),
    "equity_project_share": ("EQUITY_PROJECT_SHARE_V1", "equity_contribution"),
    "equity_institution_unit": ("EQUITY_INSTITUTION_UNIT_V1", "equity_contribution"),
    "direct_annual_amount": ("DIRECT_ANNUAL_AMOUNT_V1", "goods_services"),
}
for _route_key, (_formula_key, _nature) in DIRECT_FORMULA_ROUTE_KEYS.items():
    ERCE_FORMULA_ROUTES[_route_key] = FormulaRoute(
        route_key=_route_key, formula_key=_formula_key,
        expression=FORMULAS[_formula_key]["expression"], cost_nature=_nature,
    )


def formula_for_route(route_key: str) -> FormulaRoute:
    try:
        return ERCE_FORMULA_ROUTES[route_key]
    except KeyError as exc:
        raise KeyError(f"ERCE formula route is not implemented: {route_key}") from exc


__all__ = ["ERCE_FORMULA_ROUTES", "FormulaRoute", "formula_for_route"]
