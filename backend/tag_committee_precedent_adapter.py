"""Bridge verified committee precedent evidence into the TAG pipeline.

The TAG rule engine remains the primary calculator.  This adapter is called
only after Phase 3 and keeps three outcomes separate:

* a target grounded by its own article/rule is left untouched;
* a complete historical package may replace a low-confidence/blocked result;
* a merely structural precedent is attached for review and never executed.

The current bill is excluded by the underlying evidence engines and checked
again here before any historical amount is accepted.
"""
from __future__ import annotations

import re
from typing import Any


TAG_COMMITTEE_PRECEDENT_ADAPTER_VERSION = "tag-committee-precedent-adapter-v1"

_FULL_BILL_NO = re.compile(r"(?<!\d)((?:1[7-9]|2[0-9])\d{5})(?!\d)")
_SHORT_BILL_NO = re.compile(r"의안\s*번호\s*(\d{1,5})(?!\d)")
_PROPOSED = re.compile(
    r"발\s*의\s*연\s*월\s*일\s*[:：]?\s*"
    r"(20\d{2})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})"
)
_ASSEMBLY_TERMS = (
    (22, (2024, 5, 30), (2028, 5, 29)),
    (21, (2020, 5, 30), (2024, 5, 29)),
    (20, (2016, 5, 30), (2020, 5, 29)),
    (19, (2012, 5, 30), (2016, 5, 29)),
)


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def extract_bill_metadata(text: str, *, filename: str = "") -> tuple[str | None, str | None]:
    """Extract a full Assembly bill number and ISO proposal date."""
    head = str(text or "")[:4_000]
    proposed = _PROPOSED.search(head)
    propose_date = None
    proposal_tuple = None
    if proposed:
        year, month, day = (int(value) for value in proposed.groups())
        proposal_tuple = (year, month, day)
        propose_date = f"{year:04d}-{month:02d}-{day:02d}"

    full = _FULL_BILL_NO.search(f"{filename}\n{head}")
    if full:
        return full.group(1), propose_date

    short = _SHORT_BILL_NO.search(head)
    if not short or proposal_tuple is None:
        return None, propose_date
    for term, start, end in _ASSEMBLY_TERMS:
        if start <= proposal_tuple <= end:
            return f"{term}{int(short.group(1)):05d}", propose_date
    return None, propose_date


def _same_body(left: str, right: str) -> bool:
    left_key = _compact(left)
    right_key = _compact(right)
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def _matching_lines(
    committee_name: str,
    lines: list[dict[str, Any]],
    *,
    only_committee: bool,
) -> list[dict[str, Any]]:
    matched = [
        line
        for line in lines
        if _same_body(committee_name, str(line.get("item_name") or ""))
    ]
    return matched or (lines if only_committee else [])


def _matching_component(
    committee_name: str,
    components: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return next(
        (
            component
            for component in components
            if _same_body(committee_name, str(component.get("target_name") or ""))
        ),
        None,
    )


def _candidate_preview(component: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not component:
        return []
    return [
        {
            "bill_no": candidate.get("bill_no"),
            "bill_name": candidate.get("bill_name"),
            "committee_name": candidate.get("committee_name"),
            "score": candidate.get("score"),
            "paid_members": candidate.get("paid_members"),
            "annual_meetings": candidate.get("annual_meetings"),
            "meeting_unit_price": candidate.get("meeting_unit_price"),
            "source_texts": list(candidate.get("source_texts") or [])[:2],
        }
        for candidate in (component.get("candidate_top5") or [])[:3]
    ]


def apply_precedent_fallback(
    committee_items: list[dict[str, Any]],
    workflow: dict[str, Any],
    *,
    current_bill_no: str | None,
) -> None:
    """Apply evidence in place while preserving TAG anchor priority."""
    covered = workflow.get("covered") or {}
    package = covered.get("package") or {}
    selected_bill_no = str(covered.get("selected_bill_no") or "") or None
    exact_package = bool(
        covered.get("decision") == "calculated"
        and package.get("decision") == "exact_package"
        and selected_bill_no
    )
    self_match = bool(
        current_bill_no
        and selected_bill_no
        and str(current_bill_no) == str(selected_bill_no)
    )
    lines = list(covered.get("line_items") or [])
    components = list(workflow.get("components") or [])
    only_committee = len(committee_items) == 1

    for item in committee_items:
        calc = item.get("calc_result") or {}
        committee_name = str(item.get("entity_name") or calc.get("name") or "")

        # Article-grounded, deterministic TAG output always wins.  A precedent
        # is a fallback, not an alternative calculator competing with anchors.
        if calc.get("status") in {"calculated_with_rule", "calculated_finite_event_total"}:
            calc["evidence_route"] = {
                "level": "tag_anchor",
                "label": "조문·DB 앵커로 계산",
                "current_bill_excluded": bool(current_bill_no),
            }
            continue

        matched_lines = _matching_lines(
            committee_name,
            lines,
            only_committee=only_committee,
        )
        if exact_package and not self_match and matched_lines:
            years = max(
                (len(line.get("yearly_thousand") or []) for line in matched_lines),
                default=5,
            )
            yearly_thousand = [0] * years
            for line in matched_lines:
                for index, amount in enumerate(line.get("yearly_thousand") or []):
                    yearly_thousand[index] += int(amount or 0)
            review_required = bool(
                not covered.get("ready_for_auto_publish")
                or "재량 규정" in str(calc.get("reason") or "")
            )
            status = (
                "calculated_with_verified_precedent_review"
                if review_required
                else "calculated_with_verified_precedent"
            )
            item["year_amounts"] = [value * 1_000 for value in yearly_thousand]
            item["calc_result"] = {
                **calc,
                "status": status,
                "reason": (
                    f"현재 의안 {current_bill_no or '미상'}은 후보에서 제외하고, "
                    f"선행 의안 {selected_bill_no}의 완결된 산식·변수·금액 일관성을 "
                    "검증해 적용했습니다. "
                    + (
                        "시행일 등 시계열 전제는 추가 확인이 필요합니다."
                        if review_required
                        else ""
                    )
                ).strip(),
                "annual_cost_won": sum(
                    int(line.get("full_year_thousand") or 0)
                    for line in matched_lines
                )
                * 1_000,
                "precedent_total_cost_won": sum(yearly_thousand) * 1_000,
                "selected_precedent_bill_no": selected_bill_no,
                "precedent_line_items": matched_lines,
                "warnings": list(covered.get("warnings") or []),
                "trace": " + ".join(
                    str(line.get("formula") or line.get("item_name") or "")
                    for line in matched_lines
                ),
                "evidence_route": {
                    "level": "verified_precedent",
                    "label": "검증된 선례로 계산",
                    "selected_bill_no": selected_bill_no,
                    "current_bill_excluded": bool(current_bill_no),
                },
            }
            continue

        component = _matching_component(committee_name, components)
        candidates = [
            candidate
            for candidate in _candidate_preview(component)
            if not current_bill_no
            or str(candidate.get("bill_no") or "") != str(current_bill_no)
        ]
        if candidates:
            calc["evidence_route"] = {
                "level": "recommended_precedent",
                "label": "선례 전제 검토 필요",
                "selected_bill_no": candidates[0].get("bill_no"),
                "current_bill_excluded": bool(current_bill_no),
            }
            calc["reference_candidates"] = candidates
        elif self_match:
            calc["evidence_route"] = {
                "level": "blocked_self_match",
                "label": "현재 의안 재사용 차단",
                "selected_bill_no": None,
                "current_bill_excluded": True,
            }
        else:
            calc.setdefault(
                "evidence_route",
                {
                    "level": "input_required",
                    "label": "근거 부족·사용자 입력 필요",
                    "selected_bill_no": None,
                    "current_bill_excluded": bool(current_bill_no),
                },
            )
