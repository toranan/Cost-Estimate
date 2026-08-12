"""Grounded committee membership constraint solver.

The parser preserves member groups; this module validates their source quotes
and converts only complete, supported structures into paid-attendee operands.
It never infers a paid headcount from a committee name or a gold estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Sequence


COMMITTEE_MEMBERSHIP_VERSION = "committee-membership-v1"

_ALLOWED_KINDS = {
    "private_external",
    "government_official",
    "internal_public_organization",
    "unknown",
}
_PAID_KINDS = {"private_external"}
_UNPAID_KINDS = {"government_official", "internal_public_organization"}
_EQUAL_SHARE_RE = re.compile(
    r"(?:(?:각호의?위원|각집단|각구성집단)(?:을|를)?(?:각각)?|"
    r"각각)동수로구성"
)


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


@dataclass(frozen=True)
class PaidMemberEstimate:
    low: int
    high: int
    source: str
    evidence_quotes: tuple[str, ...]
    exact: bool
    reason: str


def _validated_groups(
    article_text: str,
    groups: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not groups:
        return None
    compact_text = _compact(article_text)
    validated: list[dict[str, Any]] = []
    for raw in groups:
        if not isinstance(raw, dict):
            return None
        label = str(raw.get("label") or "").strip()
        kind = str(raw.get("kind") or "unknown").strip()
        allocation = str(raw.get("allocation") or "fixed").strip()
        quote = str(raw.get("evidence_quote") or "").strip()
        count_raw = raw.get("count")
        if (
            not label
            or kind not in _ALLOWED_KINDS
            or allocation not in {"fixed", "equal_share"}
            or not quote
            or _compact(quote) not in compact_text
        ):
            return None
        count = None if count_raw is None else int(count_raw)
        if count is not None and count <= 0:
            return None
        validated.append({
            "label": label,
            "kind": kind,
            "allocation": allocation,
            "count": count,
            "evidence_quote": quote,
        })
    return validated


def derive_paid_members(
    article_text: str,
    *,
    member_groups: Sequence[dict[str, Any]] | None,
    total_members_min: int | None,
    total_members_max: int | None,
) -> PaidMemberEstimate | None:
    """Solve paid members from either explicit groups or equal-share groups.

    `unknown` eligibility makes the structure incomplete and therefore falls
    back to the older broad range/HITL path instead of silently classifying it.
    """
    groups = _validated_groups(article_text, member_groups)
    if not groups or any(group["kind"] == "unknown" for group in groups):
        return None

    quotes = tuple(group["evidence_quote"] for group in groups)
    if all(group["allocation"] == "fixed" and group["count"] for group in groups):
        paid = sum(
            int(group["count"])
            for group in groups
            if group["kind"] in _PAID_KINDS
        )
        all_members = sum(int(group["count"]) for group in groups)
        if total_members_min is not None and all_members < total_members_min:
            return None
        if total_members_max is not None and all_members > total_members_max:
            return None
        return PaidMemberEstimate(
            low=paid,
            high=paid,
            source="article_member_groups_fixed",
            evidence_quotes=quotes,
            exact=True,
            reason=(
                f"원문에 명시된 구성 집단별 인원 중 외부·민간 {paid}명만 "
                "회의수당 대상으로 분리했습니다."
            ),
        )

    if not all(group["allocation"] == "equal_share" for group in groups):
        return None
    if not _EQUAL_SHARE_RE.search(_compact(article_text)):
        return None
    if any(group["count"] is not None for group in groups):
        return None
    if total_members_min is None or total_members_max is None:
        return None

    group_count = len(groups)
    first_total = math.ceil(total_members_min / group_count) * group_count
    last_total = math.floor(total_members_max / group_count) * group_count
    if first_total > last_total:
        return None
    paid_group_count = sum(group["kind"] in _PAID_KINDS for group in groups)
    unpaid_group_count = sum(group["kind"] in _UNPAID_KINDS for group in groups)
    if paid_group_count + unpaid_group_count != group_count or not paid_group_count:
        return None
    low = (first_total // group_count) * paid_group_count
    high = (last_total // group_count) * paid_group_count
    return PaidMemberEstimate(
        low=low,
        high=high,
        source="article_member_groups_equal_share",
        evidence_quotes=quotes,
        exact=low == high,
        reason=(
            f"원문의 총원 {total_members_min}~{total_members_max}명과 {group_count}개 집단 동수 "
            f"제약을 풀어, 공무원·공공기관 내부 집단을 제외한 수당 대상을 "
            f"{low}~{high}명으로 산출했습니다."
        ),
    )
