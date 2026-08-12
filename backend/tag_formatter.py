"""[Phase 5] 최종 출력 (Formatter).

숫자 배열을 국회 규격에 맞는 비용추계 요약표/전제/상세산출내역으로 렌더링한다.
계산은 하지 않는다 — Phase 3/4에서 이미 나온 결과를 문자열로만 바꾼다.
"""
from __future__ import annotations

TAG_FORMATTER_VERSION = "tag-formatter-v1"


def format_summary_table(aggregated: dict, *, base_year: int = 2027) -> str:
    years = len(aggregated["year_totals_won"])
    header = "| 구분 | " + " | ".join(f"{base_year + i}" for i in range(years)) + " | 합계 |"
    sep = "|---" * (years + 2) + "|"
    rows = [header, sep]
    for item in aggregated["items"]:
        amounts = item.get("year_amounts") or [0] * years
        amount_ranges = item.get("year_amounts_range")
        calc = item.get("calc_result") or {}
        if calc.get("event_year_allocation_unresolved"):
            cells = ["미정"] * years
            total_cell = f"{int(calc.get('finite_event_total_cost_won') or 0):,}"
        elif amount_ranges:
            cells = [f"{low:,}~{high:,}" for low, high in amount_ranges]
            total_low = sum(low for low, _ in amount_ranges)
            total_high = sum(high for _, high in amount_ranges)
            total_cell = f"{total_low:,}~{total_high:,}"
        else:
            cells = [f"{amount:,}" for amount in amounts]
            total_cell = f"{sum(amounts):,}"
        rows.append(
            "| " + item.get("label", item.get("category", ""))
            + " | " + " | ".join(cells)
            + f" | {total_cell} |"
        )
    total_ranges = aggregated.get("year_totals_won_range")
    if total_ranges:
        total_cells = [f"**{low:,}~{high:,}**" for low, high in total_ranges]
        grand_low, grand_high = aggregated["grand_total_won_range"]
        rows.append(
            "| **합계(추정범위)** | " + " | ".join(total_cells)
            + f" | **{grand_low:,}~{grand_high:,}** |"
        )
    else:
        totals = aggregated["year_totals_won"]
        grand = aggregated.get(
            "grand_total_including_unallocated_won",
            aggregated["grand_total_won"],
        )
        rows.append(
            "| **합계** | " + " | ".join(f"**{t:,}**" for t in totals)
            + f" | **{grand:,}** |"
        )
    return "\n".join(rows)


_CONFIRMED_STATUSES = (
    "calculated_with_rule",
    "calculated_with_rule_range",
    "calculated_with_verified_precedent",
)

# 내부 status 코드(6~7종) → 사용자 노출용 3단계 라벨.
# "구간 추정"에 calculated_with_rule_range를 넣은 게 _CONFIRMED_STATUSES와
# 다른데, 의도적임: _CONFIRMED_STATUSES는 포맷터 내부에서 "저신뢰 문구를
# 붙일지" 판단하는 용도(range도 조문 근거가 확실하면 문구 불필요)이고,
# 아래 매핑은 검토자에게 "구간이니 그대로 못 쓴다"는 신호를 주는 용도라서
# 기준이 다르다.
_USER_LABELS: dict[str, tuple[str, str]] = {
    "calculated_with_rule": ("확정", "✅"),
    "computed": ("확정", "✅"),
    "calculated_with_verified_precedent": ("확정", "✅"),
    "calculated_with_verified_precedent_review": ("구간 추정", "⚠"),
    "calculated_with_rule_range": ("구간 추정", "⚠"),
    "calculated_low_confidence": ("구간 추정", "⚠"),
    "calculated_finite_event_total": ("사건성 총액", "✓"),
    "calculated_discretionary_scenario": ("조건부 추정", "⚠"),
    "review_required": ("검토 필요", "⛔"),
    "blocked_missing_db_anchor": ("검토 필요", "⛔"),
    "blocked_missing_amount": ("검토 필요", "⛔"),
    "blocked_missing_variables": ("검토 필요", "⛔"),
    "blocked_invalid_members": ("검토 필요", "⛔"),
}
_DEFAULT_USER_LABEL: tuple[str, str] = ("검토 필요", "⛔")


def status_to_user_label(status: str | None) -> tuple[str, str]:
    """내부 status 코드를 사용자 노출용 (라벨, 아이콘) 3단계로 좁힌다.

    등록되지 않은 status(향후 새 아키타입 발견 시 추가될 코드 포함)는
    가장 보수적인 라벨인 "검토 필요"로 떨어진다 — 매핑 갱신을 깜빡해도
    확정처럼 보이는 거짓 확신은 절대 나오지 않는다.
    """
    return _USER_LABELS.get(status, _DEFAULT_USER_LABEL)


def format_user_label(status: str | None) -> str:
    label, icon = status_to_user_label(status)
    return f"{icon} {label}"


def format_assumptions(items: list[dict]) -> list[str]:
    assumptions = [
        "위원회 참석수당은 「예산 및 기금운용계획 집행지침」상 1인당 1회 30만원 "
        "한도(법제처 확인)를 유형3(단순자문형) 실측 중앙값으로 적용함.",
        "추계기간 중 단가는 불변가격 기준으로 고정 적용함(NABO 비용추계 관행, "
        "의안 2213848 외 다수에서 확인된 문구: \"수당 단가는 추계기간 동안 "
        "일정하게 유지되는 것으로 가정\"). 인건비 0%는 이 관행을 준용한 것.",
        "5년간 단가는 최초 적용연도 기준을 유지함.",
    ]
    for item in items:
        calc = item.get("calc_result") or {}
        status = calc.get("status")
        if status == "calculated_low_confidence":
            assumptions.append(
                f"[{item.get('label', '')}] (저신뢰 폴백 사용) {calc.get('reason', '')}"
            )
        elif status not in _CONFIRMED_STATUSES:
            assumptions.append(
                f"[{item.get('label', '')}] {calc.get('reason', '확정 계산 보류')}"
            )
    return assumptions


def format_calculation_trace(items: list[dict]) -> list[str]:
    traces = []
    for item in items:
        calc = item.get("calc_result") or {}
        trace = calc.get("trace")
        if trace:
            traces.append(f"[{item.get('label', '')}] {trace}")
    return traces


# calc_result(calculate_committee/calculate_personnel/calculate_capital_expenditure/
# calculate_transfer_payment 출력)에 이미 있는 신뢰도 신호를 missing_inputs
# 스키마(kind/label/resolvable_by_reviewer)로 옮긴다. 여기서 새 판단을 하지
# 않는다 — 계산 엔진이 이미 "이 값은 폴백으로 채웠다"고 알고 있는 것만 구조화한다.
#
# 중요한 한계(이 함수가 아니라 시스템 자체의 한계): 위원회_평가셋 40건
# 백필(missing_inputs_backfill.py)에서 쓴 kind 중 precedent_org/precedent_metric/
# undisclosed_source/increment_baseline/archetype_mismatch/out_of_scope는
# 여기서 절대 나오지 않는다. 그 kind들은 전부 "NABO의 실제 정답을 미리 알아야"
# 진단 가능한 카테고리(선례기구가 뭔지, 이게 증분형인지 등)라, 정답을 모르는
# 채로 도는 프로덕션 계산에서는 원리적으로 감지 불가능하다. 즉 이 함수가
# missing_inputs=[]를 반환해도 "확정 계산"이 아니라 "우리 엔진이 스스로
# 인지하고 있는 불확실성은 없다"는 뜻일 뿐 — 2215198(조문의 "분기별"을
# meetings_source="article"로 확신했지만 NABO는 선례로 6배 다르게 낸 사례)
# 처럼, 엔진 자신도 모르는 채로 확신하는 케이스는 이 함수로 못 잡는다.
def compute_missing_inputs(calc_result: dict) -> list[dict]:
    missing: list[dict] = []

    if calc_result.get("meetings_source") == "fallback_default_n36":
        missing.append({
            "kind": "own_fallback_variable",
            "label": "회의횟수",
            "resolvable_by_reviewer": True,
            "note": "조문에 없어 폴백값(연 4회, 로컬DB n=36 최빈값)을 썼습니다.",
        })

    pm_src = calc_result.get("paid_members_source")
    if pm_src in ("ratio_fallback_n8", "ratio_fallback_n8_with_legal_floor"):
        missing.append({
            "kind": "own_fallback_variable",
            "label": "당연직/유급위원 수",
            "resolvable_by_reviewer": True,
            "note": "당연직 구분이 조문만으로 확정되지 않아 실측 비율(n=8, 33~80%) 구간을 적용했습니다.",
        })
    elif pm_src == "precedent_fallback_n89":
        missing.append({
            "kind": "own_fallback_variable",
            "label": "위원 정수 자체",
            "resolvable_by_reviewer": True,
            "note": "조문에 위원 정수가 없어(시행령 등에 위임) 유사 선례 구간(n=89)으로 추정했습니다.",
        })

    staff_fallback = calc_result.get("staff_headcount_fallback")
    if staff_fallback:
        missing.append({
            "kind": "own_fallback_variable",
            "label": "사무국/소속 공무원 인원·직급",
            "resolvable_by_reviewer": True,
            "note": f"조문에 없어 폴백(인원 {staff_fallback['value']}명, n={staff_fallback['n']})을 썼습니다.",
        })

    if "staff_headcount_range" in calc_result:
        missing.append({
            "kind": "own_fallback_variable",
            "label": "지원인력 규모",
            "resolvable_by_reviewer": True,
            "note": "조문에 없어 위원정수 대비 지원인력 비율(n=2, 40~50%)로 추정했습니다.",
        })

    repl = calc_result.get("replication")
    if repl and not repl.get("confident"):
        missing.append({
            "kind": "replication_adjustment",
            "label": "복제설치 실제 개수",
            "resolvable_by_reviewer": True,
            "note": f"{repl.get('basis', '')} 실제 설치 수는 다를 수 있습니다.".strip(),
        })

    status = calc_result.get("status")
    if status == "blocked_missing_db_anchor":
        missing.append({
            "kind": "internal_db_gap",
            "label": calc_result.get("reason", "DB 앵커 누락"),
            "resolvable_by_reviewer": True,
            "note": "시스템 내부 단가표에 없는 값입니다 — 공식 출처 확인 시 채울 수 있습니다.",
        })
    if status == "blocked_missing_amount":
        missing.append({
            "kind": "own_fallback_variable",
            "label": "1인당 지급액",
            "resolvable_by_reviewer": True,
            "note": "조문에 금액이 명시돼 있지 않아 폴백조차 없습니다.",
        })

    return missing


def format_missing_inputs(missing_inputs: list[dict]) -> str:
    if not missing_inputs:
        return ""
    resolvable = [m for m in missing_inputs if m.get("resolvable_by_reviewer")]
    unresolvable = [m for m in missing_inputs if not m.get("resolvable_by_reviewer")]
    lines = [f"▶ 확인이 필요한 항목 ({len(missing_inputs)}건)"]
    n = 0
    for m in resolvable:
        n += 1
        lines.append(f"  {n}. {m['label']}" + (f" — {m['note']}" if m.get("note") else ""))
    for m in unresolvable:
        n += 1
        lines.append(f"  {n}. {m['label']} (문의 필요, 입력으로 해결 불가)" + (f" — {m['note']}" if m.get("note") else ""))
    if resolvable and not unresolvable:
        lines.append("")
        lines.append("  위 항목이 확인되면 아래 산식으로 계산할 수 있습니다.")
    elif unresolvable:
        lines.append("")
        lines.append("  문의 필요 항목은 본 시스템이 자동 산출하지 않습니다.")
    return "\n".join(lines)


_CATEGORY_TITLES = {
    "statutory": ("법정/공식 지침·고시", ""),
    "empirical": ("실측(로컬DB 채굴)", "※ "),
    "estimated": ("추정(실측값을 escalate, 공식 수치 아님)", "† "),
    "heuristic": ("경험적 추정(실측 근거 없음)", "‡ "),
}
_STALE_WARNING_THRESHOLD_YEARS = 2


def _format_registry_value(entry: dict) -> str:
    value = entry["value"]
    unit = entry.get("unit", "")
    n = entry.get("n")
    formatted = f"{value:,}{unit}" if isinstance(value, int) else f"{value}{unit}"
    if n:
        formatted += f"(n={n})"
    return formatted


def render_appendix(*, current_year: int) -> str:
    """부록 — 단가 출처. current_year는 렌더링 시점의 실제 연도를 호출부에서
    넘긴다(이 모듈은 시계열 판단을 하지 않는다 — Phase 5 포맷터 원칙과 동일하게
    "이미 주어진 재료"만 문자열로 바꾼다).

    라이브 참조(unit_price_registry가 tag_rule_engine의 상수를 직접 import)만으론
    "상수 자체가 낡음"은 못 잡는다(코드 값과 화면 값의 드리프트만 막을 뿐) —
    그래서 basis_year와 current_year를 비교해 갱신 경고를 같이 낸다.
    """
    from .unit_price_registry import UNIT_PRICE_REGISTRY

    lines = ["## 부록 — 단가 출처", ""]
    for category, (title, marker) in _CATEGORY_TITLES.items():
        entries = [e for e in UNIT_PRICE_REGISTRY if e["category"] == category]
        if not entries:
            continue
        lines.append(f"### {title}")
        for e in entries:
            warn = ""
            basis_year = e.get("basis_year")
            if basis_year and current_year - basis_year >= _STALE_WARNING_THRESHOLD_YEARS:
                warn = f" [⚠ 기준연도 {basis_year}년, {current_year - basis_year}년 경과 — 갱신 확인 필요]"
            lines.append(f"- {marker}{e['label']}: {_format_registry_value(e)} ({e['source']}){warn}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_report(aggregated: dict, items: list[dict], *, base_year: int = 2027) -> str:
    parts = [
        "## 비용추계 요약표",
        format_summary_table(aggregated, base_year=base_year),
        "",
        "## 비용추계의 전제",
        *[f"- {a}" for a in format_assumptions(items)],
        "",
        "## 상세 산출 내역",
        *[f"- {t}" for t in format_calculation_trace(items)],
    ]
    return "\n".join(parts)
