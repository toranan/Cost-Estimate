"""Adapt the isolated committee evidence engines to the UI contract.

This module keeps the three product states explicit:

* verified package: executable historical relationship and operands;
* review recommendation: structurally useful candidates, never auto-confirmed;
* input required: no transferable evidence route.

It does not read a target answer and does not write to the evidence database.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
import tempfile
from typing import Any

from backend.committee_component_recommender import recommend
from backend.committee_covered_estimator import estimate


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def _same_body(left: str, right: str) -> bool:
    left_key = _compact(left)
    right_key = _compact(right)
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def build_evidence_workflow(
    *,
    pdf_bytes: bytes,
    bill_no: str | None,
    bill_name: str,
    propose_date: str | None,
    target_names: list[str],
) -> dict[str, Any]:
    """Run source-only evidence routing once for an uploaded committee bill."""
    names = list(dict.fromkeys(name for name in target_names if name))
    if not bill_no or not propose_date or not names:
        return {
            "status": "input_required",
            "reason": "의안번호·제안일·위원회 명칭을 모두 확인해야 시점 오염 없이 선례를 검색할 수 있습니다.",
            "covered": None,
            "components": [],
        }

    with tempfile.TemporaryDirectory(prefix="committee-evidence-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(pdf_bytes)
        case = {
            "bill_no": str(bill_no),
            "bill_name": bill_name,
            "propose_date": propose_date,
            "cutoff_date": propose_date,
            "source_pdf": str(source),
            "target_terms": names,
        }
        covered = estimate(case).to_dict()
        components = [asdict(row) for row in recommend(case)]

    if covered.get("decision") == "calculated":
        status = (
            "verified"
            if covered.get("ready_for_auto_publish")
            else "verified_review_required"
        )
    elif (covered.get("package") or {}).get("decision") == "exact_package":
        status = "incomplete_verified_package"
    elif any(row.get("candidate_top5") for row in components):
        status = "recommendation_available"
    else:
        status = "input_required"
    return {
        "status": status,
        "reason": "정답이 아닌 의안 원문과 제안일 이전 DB 근거만 사용했습니다.",
        "covered": covered,
        "components": components,
    }


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


def _candidate_preview(component: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not component:
        return []
    rows: list[dict[str, Any]] = []
    for candidate in (component.get("candidate_top5") or [])[:3]:
        rows.append(
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
        )
    return rows


def apply_evidence_workflow(
    committees: list[dict[str, Any]],
    workflow: dict[str, Any],
) -> None:
    """Annotate UI rows and override amounts only for a verified package."""
    covered = workflow.get("covered") or {}
    package = covered.get("package") or {}
    components = workflow.get("components") or []
    lines = covered.get("line_items") or []
    covered_calculated = covered.get("decision") == "calculated"

    for committee in committees:
        component = _matching_component(str(committee.get("name") or ""), components)
        candidates = _candidate_preview(component)
        matched_lines = _matching_lines(
            str(committee.get("name") or ""),
            lines,
            only_committee=len(committees) == 1,
        )
        if covered_calculated and matched_lines:
            years = max((len(line.get("yearly_thousand") or []) for line in matched_lines), default=5)
            yearly = [0] * years
            for line in matched_lines:
                for index, amount in enumerate(line.get("yearly_thousand") or []):
                    yearly[index] += int(amount or 0)
            committee["calculation_mode"] = "verified_package"
            committee["status"] = (
                "computed"
                if covered.get("ready_for_auto_publish")
                else "review_required"
            )
            committee["formula"] = " + ".join(
                str(line.get("formula") or line.get("item_name") or "")
                for line in matched_lines
            )
            committee["annual_amount_thousand"] = sum(
                int(line.get("full_year_thousand") or 0) for line in matched_lines
            )
            committee["total_amount_thousand"] = sum(yearly)
            committee["year_estimates"] = [
                {"year": index + 1, "amount_thousand": amount}
                for index, amount in enumerate(yearly)
            ]
            committee["verified_line_items"] = matched_lines
            committee["evidence_route"] = {
                "level": "verified",
                "label": "검증된 선례로 계산",
                "summary": (
                    f"의안 {covered.get('selected_bill_no')} 선례의 산식·변수·금액 일관성과 "
                    "대상 위원회 구조를 확인했습니다."
                ),
                "selected_bill_no": covered.get("selected_bill_no"),
                "warnings": list(covered.get("warnings") or []),
            }
            continue

        if candidates or package.get("selected_bill_no"):
            selected = package.get("selected_bill_no") or (
                candidates[0].get("bill_no") if candidates else None
            )
            committee["evidence_route"] = {
                "level": "recommended",
                "label": "선례 기반 추정·검토",
                "summary": "구조가 비슷한 공식 추계서를 참고했지만, 조문에 없는 변수는 확인해야 합니다.",
                "selected_bill_no": selected,
                "blockers": list(covered.get("blockers") or []),
            }
            committee["reference_candidates"] = candidates
            # The legacy committee channel calculates a convenient preview
            # from unconfirmed TAG priors.  A recommendation is not evidence
            # that those target-specific operands are true. Keep the values
            # editable as suggestions, but publish no monetary result until
            # every required operand is grounded or explicitly confirmed.
            required = {"meeting_count", "paid_members", "allowance_won"}
            waiting_for_confirmation = False
            for key, variable in (committee.get("variables") or {}).items():
                if key not in required or variable.get("confirmed"):
                    continue
                variable["blocking"] = True
                waiting_for_confirmation = True
            if waiting_for_confirmation:
                committee["status"] = "blocked_missing_variables"
                committee["annual_amount_thousand"] = None
                committee["total_amount_thousand"] = None
                committee["year_estimates"] = [
                    {**row, "amount_thousand": None}
                    for row in (committee.get("year_estimates") or [])
                ]
        else:
            committee["evidence_route"] = {
                "level": "input",
                "label": "필수값 입력 필요",
                "summary": "현재 DB에서 이전 근거를 검증할 수 없어 금액을 임의로 만들지 않았습니다.",
                "selected_bill_no": None,
            }
            committee["reference_candidates"] = []
