"""[Phase 4] 5개년 시계열 시뮬레이션 (Aggregator & Time-Series).

1차년도 베이스라인 비용에 물가/임금 상승률을 복리로 적용해 5년 데이터를
만들고, 여러 조문의 비용을 연도별로 합산한다. 순수 Python 산술만 사용한다.
"""
from __future__ import annotations

TAG_TIMESERIES_VERSION = "tag-timeseries-v5"

# 추계기간 중 단가를 고정할지(불변가격) 명목가격으로 올릴지 두 기준을 명시적으로
# 분리해둔다. 기본은 "constant"(불변가격) — 실제 NABO 비용추계서에서 직접 확인된
# 문구: "회의 개최·참석 횟수 및 수당 단가는 추계기간 동안 일정하게 유지되는 것으로
# 가정"(2213848 외 다수). 이 인용문이 직접 커버하는 범위는 "수당"(allowance)이고,
# 인건비(salary)의 0%는 그 관행을 준용한 것뿐 — 나중에 처우개선율을 반영한
# 실제 비용추계서가 나오면 이 구분 때문에 근거가 꼬이지 않는다.
PRICE_BASIS = "constant"
ESCALATION = {
    "constant": {"salary": 0.0, "allowance": 0.0, "opex": 0.0},
    # 명목가격 기준으로 바꿀 경우의 참고치(2025/2026년 공무원 보수 인상률 실적
    # 3.0%/3.5% 웹서치로 확인) — PRICE_BASIS를 "nominal"로 바꿀 때만 쓰인다.
    "nominal": {"salary": 0.030, "allowance": 0.0, "opex": 0.020},
}
ESCALATION_EVIDENCE = {
    "quote": "회의 개최·참석 횟수 및 수당 단가는 추계기간 동안 일정하게 유지되는 것으로 가정",
    "source": "NABO 비용추계서 실측(의안 2213848 외)",
    "scope": "allowance",  # 인용문이 직접 커버하는 범위 — 인건비(salary)는 관행 준용
    "salary_basis": "미확인 — 불변가격 관행 준용",
}

YEARS = 5


def _inflation_rate(category: str) -> float:
    rates = ESCALATION[PRICE_BASIS]
    return rates["salary"] if category == "1_인건비_물건비" else rates["opex"]


def expand_compound(annual_cost_won: float, *, inflation_rate: float, years: int = YEARS) -> list[int]:
    """Year1=annual_cost, Year(n)=annual_cost*(1+rate)^(n-1)."""
    return [int(round(annual_cost_won * (1 + inflation_rate) ** year)) for year in range(years)]


def _truncate_to_activity_duration(amounts: list[int], duration_months: int) -> list[int]:
    """실측(2126661): 활동기간이 법에 명시된 한시(限時) 위원회는 영구 상설이
    아니다 — 매년 반복 계산하면 15개월짜리 위원회가 5년치로 부풀어 +413%
    과대추정 난다. 1년차 첫 달부터 활동을 시작한다고 가정하고(실제 시작월은
    조문만으로 알 수 없어 가장 단순한 가정), 활동기간이 끝나는 시점 이후는
    0으로 끊는다. 마지막 부분월은 일할이 아니라 월할로 비례배분한다.
    """
    remaining_months = duration_months
    truncated: list[int] = []
    for amount in amounts:
        if remaining_months <= 0:
            truncated.append(0)
        elif remaining_months >= 12:
            truncated.append(amount)
            remaining_months -= 12
        else:
            truncated.append(int(round(amount * remaining_months / 12)))
            remaining_months = 0
    return truncated


def category_to_year_amounts(category: str, calc_result: dict, *, years: int = YEARS) -> list[int]:
    """룰 엔진(Phase 3) 결과 하나를 연도별 금액 리스트로 펼친다.

    status가 calculated_with_rule* 계열이 아니면(blocked/review_required) 전부
    0으로 채운다 — 확정 안 된 값을 임의로 시계열에 섞어 넣지 않는다.
    calculated_with_rule_range(위원회 기본운영비 구간 등)는 구간 중앙값을
    요약표의 점추정치로 쓰되, 원래 구간은 calc_result에 그대로 남아있다.
    """
    status = calc_result.get("status")
    if status not in (
        "calculated_with_rule",
        "calculated_with_rule_range",
        "calculated_low_confidence",
        "calculated_discretionary_scenario",
        "calculated_with_verified_precedent",
        "calculated_with_verified_precedent_review",
        "calculated_finite_event_total",
    ):
        return [0] * years

    if category == "3_자본지출":
        year_1 = calc_result.get("year_1_cost_won") or 0
        year_rest = calc_result.get("year_2_to_5_cost_won") or 0
        return [year_1] + [year_rest] * (years - 1)

    # 사건 횟수는 확정됐지만 발생 연도가 조문에 없으면 임의로 1차년도에
    # 몰아넣지 않는다. aggregate가 이 금액을 unallocated_total_won으로
    # 따로 보존하므로, 연도별 표만 0(미배분)으로 남긴다.
    if calc_result.get("event_year_allocation_unresolved"):
        return [0] * years

    annual_cost = calc_result.get("annual_cost_won")
    if annual_cost is None and status == "calculated_with_rule_range":
        cost_range = calc_result.get("annual_cost_won_range")
        if cost_range:
            annual_cost = sum(cost_range) / 2  # 구간 중앙값(점추정 표시용)

    one_time_cost = calc_result.get("one_time_cost_won") or 0  # 예: 유형1 청사건축비(1년차만)

    if annual_cost is None:
        if one_time_cost:
            return [one_time_cost] + [0] * (years - 1)
        return [0] * years

    amounts = expand_compound(annual_cost, inflation_rate=_inflation_rate(category), years=years)
    if one_time_cost:
        amounts[0] += one_time_cost  # 1회성 비용은 1년차에만 가산, 복리 미적용

    duration_months = calc_result.get("activity_duration_months")
    if duration_months is not None:
        amounts = _truncate_to_activity_duration(amounts, duration_months)

    return amounts


def category_to_year_amounts_range(
    category: str, calc_result: dict, *, years: int = YEARS
) -> list[list[int]] | None:
    """근거 있는 연간 구간을 시계열 구간으로 보존한다.

    ``review_required``는 확정값이 아니라는 뜻이지 금액 정보가 없다는 뜻은
    아니다. ``annual_cost_won_range``가 있으면 중앙값으로 확정하거나 0원으로
    지우지 않고 매년 ``[하한, 상한]``으로 전개한다.
    """
    cost_range = calc_result.get("annual_cost_won_range")
    if not (
        isinstance(cost_range, (list, tuple))
        and len(cost_range) == 2
        and all(isinstance(value, (int, float)) for value in cost_range)
    ):
        return None

    low, high = sorted((float(cost_range[0]), float(cost_range[1])))
    low_amounts = expand_compound(low, inflation_rate=_inflation_rate(category), years=years)
    high_amounts = expand_compound(high, inflation_rate=_inflation_rate(category), years=years)

    duration_months = calc_result.get("activity_duration_months")
    if duration_months is not None:
        low_amounts = _truncate_to_activity_duration(low_amounts, duration_months)
        high_amounts = _truncate_to_activity_duration(high_amounts, duration_months)

    return [[low_amounts[index], high_amounts[index]] for index in range(years)]


def aggregate(items: list[dict], *, years: int = YEARS) -> dict:
    """여러 조문의 연도별 금액을 합산해 Total Cost 테이블을 만든다.

    items: [{"category": "...", "label": "...", "year_amounts": [..]}, ...]
    """
    totals = [0] * years
    range_lows = [0] * years
    range_highs = [0] * years
    has_range = False
    unallocated_total = 0
    for item in items:
        calc = item.get("calc_result") or {}
        if calc.get("event_year_allocation_unresolved"):
            unallocated_total += int(calc.get("finite_event_total_cost_won") or 0)
        amounts = item.get("year_amounts") or [0] * years
        amount_ranges = item.get("year_amounts_range")
        for i in range(years):
            point = amounts[i] if i < len(amounts) else 0
            totals[i] += point
            if amount_ranges and i < len(amount_ranges):
                low, high = amount_ranges[i]
                range_lows[i] += low
                range_highs[i] += high
                has_range = True
            else:
                range_lows[i] += point
                range_highs[i] += point
    return {
        "items": items,
        "year_totals_won": totals,
        "grand_total_won": sum(totals),
        "unallocated_total_won": unallocated_total,
        "grand_total_including_unallocated_won": sum(totals) + unallocated_total,
        "year_totals_won_range": (
            [[range_lows[i], range_highs[i]] for i in range(years)] if has_range else None
        ),
        "grand_total_won_range": (
            [sum(range_lows), sum(range_highs)] if has_range else None
        ),
    }
