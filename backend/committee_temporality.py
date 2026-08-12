"""Committee recurrence/effect timing gate.

The language model may *extract* temporal evidence, but it does not get to
decide how often a committee cost is repeated.  This module validates quoted
evidence against the actual bill text and only turns a recurring annual
allowance into a finite-event total when the source contains a bounded event
schedule.  Ambiguous evidence leaves the existing result untouched.

This intentionally does not guess calendar years.  A bill can say "within two
weeks after the first-instance judgment" without revealing the year in which
that judgment will occur.  In that case the total number of events is usable,
while the yearly allocation remains a review item.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence


COMMITTEE_TEMPORALITY_VERSION = "committee-temporality-v1"

_COMPACT_RE = re.compile(r"[^0-9A-Za-z가-힣]")
_PERIODIC_RE = re.compile(
    r"(?:매년|연\s*\d+\s*회\s*이상|월\s*\d+\s*회|"
    r"분기(?:별|마다)|반기(?:별|마다)|정기적으로)"
)
_BOUNDED_TIME_RE = re.compile(
    r"(?:시행\s*(?:후|일부터)|선고일부터|결정일부터|"
    r"요청을?\s*받은\s*날부터|선정일부터|"
    r"임명일부터|발생일부터).{0,80}?"
    r"\d+\s*(?:일|주|개월|년)\s*이내"
)
_EVENT_PURPOSE_RE = re.compile(
    r"후보(?:자)?\s*추천|대상자\s*선정|적격\s*심사|"
    r"임명|위촉|최초\s*구성|구성을?\s*완료"
)
# 실측(2216417/2216503, 위원후보추천위원회): "국회, 대통령 또는 대법원장이
# 위원을 선출ㆍ지명할 때마다"처럼 사건 자체는 명확히 반복되는 트리거인데
# _BOUNDED_TIME_RE가 요구하는 "OO일부터 N일 이내" 식 마감기한이 없다. 이건
# 2213116(시행후 2주 이내 등, 횟수가 확정된 유한사건)과 다른 유형이다 —
# 몇 번 열릴지 자체가 조문만으론 안 정해짐(위원 임기 만료 주기에 달림).
# 이 경우 연간 고정 횟수로 계산을 강행하면 안 되고(2216417 실측: 실제는
# 연도별 800만~4,300만원으로 변동, 우리 flat 4회 산식은 훨씬 낮게 잡음),
# 정직하게 review_required로 멈춘다.
_UNBOUNDED_RECURRING_EVENT_RE = re.compile(
    r"(?:선출|지명|위촉|선정|임명).{0,15}(?:할|하는)?\s*때마다"
)


def _compact(value: str) -> str:
    return _COMPACT_RE.sub("", value or "")


@dataclass(frozen=True)
class TemporalityDecision:
    kind: str  # annual | finite_events | recurring_unbounded | unknown
    confidence: str
    event_count: int | None = None
    evidence_quotes: tuple[str, ...] = ()
    reason: str = ""


def decide_committee_temporality(
    article_text: str,
    *,
    proposed_mode: str | None,
    proposed_event_count: int | None,
    evidence_quotes: Sequence[str] | None,
) -> TemporalityDecision:
    """Validate source-only temporal evidence extracted by the LLM.

    A finite-event decision requires all of the following:
    1. the LLM explicitly proposes ``event_driven``;
    2. every quoted fragment occurs in the target bill text;
    3. each event has a bounded relative deadline;
    4. the relevant text describes a bounded selection/recommendation-type
       task; and
    5. no explicit periodic cadence conflicts with the proposal.

    Failing any condition is not classified as annual.  It is ``unknown`` so
    the caller can keep a range, precedent recommendation, or HITL route.
    """
    text = article_text or ""
    if _PERIODIC_RE.search(text):
        return TemporalityDecision(
            kind="annual",
            confidence="high",
            reason="조문에 정기적 반복 주기가 명시되어 있습니다.",
        )

    if _UNBOUNDED_RECURRING_EVENT_RE.search(text) and not _BOUNDED_TIME_RE.search(text):
        return TemporalityDecision(
            kind="recurring_unbounded",
            confidence="high",
            reason=(
                "선출ㆍ지명 등 사건이 있을 때마다 반복되는 구조이나 조문에 "
                "마감기한이나 반복 주기가 없어 연간 횟수를 확정할 수 없습니다."
            ),
        )

    if proposed_mode != "event_driven":
        return TemporalityDecision(
            kind="unknown",
            confidence="low",
            reason="유효한 사건성 시간 근거가 추출되지 않았습니다.",
        )

    count = int(proposed_event_count or 0)
    quotes = tuple(q.strip() for q in (evidence_quotes or ()) if q and q.strip())
    if count <= 0 or len(quotes) != count:
        return TemporalityDecision(
            kind="unknown",
            confidence="low",
            reason="사건 횟수와 근거 인용문의 개수가 일치하지 않습니다.",
        )

    compact_text = _compact(text)
    for quote in quotes:
        if _compact(quote) not in compact_text:
            return TemporalityDecision(
                kind="unknown",
                confidence="low",
                reason="LLM이 제시한 시간 근거를 원문에서 확인하지 못했습니다.",
            )
        if not _BOUNDED_TIME_RE.search(quote):
            return TemporalityDecision(
                kind="unknown",
                confidence="low",
                reason="인용문에 시작 사건과 기한이 함께 명시되지 않았습니다.",
            )

    joined = " ".join(quotes) + " " + text
    if not _EVENT_PURPOSE_RE.search(joined):
        return TemporalityDecision(
            kind="unknown",
            confidence="low",
            reason="기한은 있지만 위원회의 유한한 업무 사건과 연결되지 않았습니다.",
        )

    return TemporalityDecision(
        kind="finite_events",
        confidence="high",
        event_count=count,
        evidence_quotes=quotes,
        reason=(
            f"원문으로 검증된 서로 다른 사건성 일정 {count}건을 확인했습니다. "
            "발생 연도는 조문만으로 확정하지 않습니다."
        ),
    )


def apply_temporality_to_calculation(
    calc_result: dict[str, Any], decision: TemporalityDecision
) -> dict[str, Any]:
    """Convert an attendance-only annual fallback into a finite-event total."""
    if decision.kind == "recurring_unbounded":
        # 조문에 반복 이벤트는 있지만 연간 횟수를 셀 근거가 없다. flat 연간
        # 폴백(DEFAULT_MEETINGS_PER_YEAR)으로 계산을 강행한 기존 결과를 버리고
        # 정직하게 review_required로 되돌린다 — 없으면 없다고 한다는 원칙을
        # 이벤트성 회의에도 동일하게 적용.
        return {
            "name": calc_result.get("name"),
            "total_members": calc_result.get("total_members"),
            "committee_type": calc_result.get("committee_type"),
            "status": "review_required",
            "reason": decision.reason,
            "temporality": decision.__dict__,
            "temporality_application": "recurring_unbounded_blocked_annual_fallback",
        }
    if decision.kind != "finite_events" or not decision.event_count:
        return calc_result

    paid = calc_result.get("paid_members")
    unit_cost = calc_result.get("unit_cost_won")
    if not isinstance(paid, (int, float)) or not isinstance(unit_cost, (int, float)):
        return {
            **calc_result,
            "temporality": decision.__dict__,
            "temporality_application": "blocked_missing_per_event_operands",
        }

    # Staff salaries, standing-member salaries, rent, and construction costs are
    # duration-based rather than per-meeting.  They must be decomposed before a
    # finite event schedule can safely be applied.
    if any(
        calc_result.get(key)
        for key in (
            "secretariat_staff_cost",
            "standing_member_cost",
            "staff_cost",
            "office_rent_cost_won",
            "one_time_cost_won",
        )
    ):
        return {
            **calc_result,
            "temporality": decision.__dict__,
            "temporality_application": "blocked_mixed_cost_components",
        }

    replication = calc_result.get("replication") or {}
    factor = int(replication.get("factor") or 1)
    per_event = int(round(float(paid) * float(unit_cost) * factor))
    total = per_event * decision.event_count
    return {
        **calc_result,
        "status": "calculated_finite_event_total",
        "annual_cost_won": None,
        "one_time_cost_won": total,
        "event_cost_won": per_event,
        "finite_event_count": decision.event_count,
        "finite_event_total_cost_won": total,
        "event_year_allocation_unresolved": True,
        "meetings_source": "source_bounded_event_schedule",
        "temporality": decision.__dict__,
        "temporality_application": "applied",
        "reason": decision.reason,
        "trace": (
            f"{int(paid)}명 × {int(unit_cost):,}원 × "
            f"원문 사건 {decision.event_count}회 = {total:,}원"
        ),
    }
