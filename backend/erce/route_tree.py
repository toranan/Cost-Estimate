"""Cost nature -> service subtype -> fixed-formula leaf.

Empty groups are known but not implemented; never guess a descendant formula.
"""
from __future__ import annotations
from collections.abc import Sequence


ERCE_ROUTE_TREE = {
    "인건비": {
        "보수": {"직급별": "personnel_grade", "평균보수": "personnel_average",
               "직급·직위변경차액": "personnel_position_difference"},
        "기관부담금": "personnel_employer_contribution",
        "기본경비": "personnel_basic_expense",
    },
    "물건비": {
        "정보시스템": {
            "사업계획금액사용": "information_system_project_plan",
            "운영": {
                "연간총액기반": "information_system_operation",
                "인력단가기반": "information_system_operation_staff",
            },
            "구축견적": {},
            "기능개선견적": {},
        },
        "연구용역": {
            "기본계획": "research_plan",
            "실태조사": {"주기적조사": "research_survey", "매년조사": "annual_research_survey"},
        },
        "위원회": {"선례가정기반": "committee_operation", "구성요소입력": "committee_components",
                "과거운영실적기반": "committee_annual_operation"},
        "국회위원회": {"운영지원사업비": "legislative_committee_operation"},
        "협의체": {"회의개최운영비": "interagency_meeting_operation",
                 "연간운영비": "interagency_annual_operation"},
        "평가사업": {"평가단및초기지표개발": "evaluation_panel_with_initial_study"},
        "통지": {"법적우편송달": "legal_notification"},
        "장례안장": {"유골함및명패": "burial_supplies"},
        "일반활동": "goods_service_activity",
        "수량단가": "quantity_unit",
        "검증된연간금액": "direct_annual_amount",
    },
    "이전지출": {
        "개인지원": {"일반급여": "transfer_recipient", "진단검사비순증지원": "diagnostic_test_subsidy"},
        "사업보조": "transfer_subsidy_rate",
        "사회보험": {
            "보험료지원": "social_insurance_premium_support",
            "급여기간지원": "social_insurance_benefit_duration",
            "기준선차액": "social_insurance_baseline_difference",
            "선지급미환수": "social_insurance_unrecovered_payment",
            "국고지원연동": "social_insurance_government_share",
        },
    },
    "자본지출": {
        "시설공사": "capital_area", "자산취득": "capital_asset",
        "인원기반자산취득": "personnel_asset",
        "묘지조성": "burial_plot_construction",
    },
    "조세감면": {"세율변경": "tax_rate_change", "공제변경": "tax_deduction_change"},
    "세외수입감소": {
        "수수료변경": "non_tax_fee_change",
        "수수료면제": {"기존수입평균": "non_tax_fee_revenue_average",
                       "무료채널전환반영": "non_tax_fee_channel_shift"},
    },
    "출자출연": {"사업분담": "equity_project_share", "기관별출연": "equity_institution_unit"},
}


def route_for_path(path: Sequence[str]) -> str:
    if isinstance(path, (str, bytes)) or not path:
        raise ValueError("route_path must be a nonempty list of tree node names")
    node = ERCE_ROUTE_TREE
    for name in path:
        if not isinstance(node, dict) or name not in node:
            raise KeyError("unknown ERCE tree path: " + "/".join(path))
        node = node[name]
    if not isinstance(node, str):
        raise ValueError("ERCE route_path must reach a registered formula leaf, not a group")
    return node


def formula_leaf_routes(node=None) -> list[str]:
    node = ERCE_ROUTE_TREE if node is None else node
    if isinstance(node, str):
        return [node]
    return [route for child in node.values() for route in formula_leaf_routes(child)]
