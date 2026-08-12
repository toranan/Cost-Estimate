"""Execute committee precedents only when the evidence package is complete.

This is an isolated prototype.  It is intentionally not imported by
``tag_pipeline`` or ``server``.

The precedent resolver answers *which historical document is related*.  That
is not enough to copy a number.  This module adds the execution boundary:

1. the target and precedent committee structures must be compatible;
2. every selected historical line item must have an auditable amount;
3. the historical full-year amount is expanded with the target bill's own
   finite activity period when that period is explicit;
4. otherwise the engine abstains instead of inventing an operand.

No target answer amount, target bill number, or bill-specific constant is used
here.  Answer data is consumed only by the separate evaluator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache
import glob
import json
from pathlib import Path
import re
from typing import Any

from backend.article_extraction_engine import extract_pdf_text
from backend.committee_precedent_resolver import (
    EvidenceItem,
    PrecedentResolution,
    _BODY,
    _bill_items,
    _formula_implied_thousand,
    _standing_payroll_item,
    _target_has_standing_members,
    _target_member_limit,
    resolve as resolve_package,
)
from backend.scripts import evaluate_committee_precedent_routes as routes


ENGINE_VERSION = "committee-covered-estimator-v0.1.0"
DEFAULT_HORIZON_YEARS = 5

_FINITE_DURATION_PATTERNS = (
    re.compile(r"(?:유효기간|존속기간|활동기간|효력).{0,40}?시행일?부터(\d+)년간?"),
    re.compile(r"이법은시행일부터(\d+)년간?효력을가진다"),
    re.compile(r"시행일?부터(\d+)개월간?(?:효력|존속|활동)"),
    re.compile(r"(?:유효기간|존속기간|활동기간)은?(\d+)개월"),
)
_COMMITTEE_FAMILY = re.compile(r"위원회|심의회|협의회|회의참석수당|회의수당")


@dataclass(frozen=True)
class ExecutedLineItem:
    item_id: str
    item_name: str
    formula: str
    full_year_thousand: int
    recurrence: str
    yearly_thousand: tuple[int, ...]
    variables: tuple[dict[str, Any], ...]
    evidence_basis: str


@dataclass(frozen=True)
class CoveredCommitteeEstimate:
    bill_no: str
    decision: str
    selected_bill_no: str | None
    relation_route: str | None
    full_year_thousand: int | None
    total_thousand: int | None
    yearly_thousand: tuple[int, ...]
    activity_months: int | None
    line_items: tuple[ExecutedLineItem, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    ready_for_auto_publish: bool
    package: PrecedentResolution

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _resolve_source_path(value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.exists() else None


def _case_full_text(case: dict[str, Any]) -> str:
    path = _resolve_source_path(str(case.get("source_pdf") or ""))
    if path is not None:
        try:
            return extract_pdf_text(path.read_bytes())
        except Exception:  # noqa: BLE001 - source passages remain a safe fallback
            pass
    return "\n".join(str(value) for value in case.get("target_passages") or [])


def _has_full_source(case: dict[str, Any]) -> bool:
    return _resolve_source_path(str(case.get("source_pdf") or "")) is not None


def _target_structure_text(case: dict[str, Any], full_text: str) -> str:
    """Return a conservative target-local window, not the whole long bill.

    A committee name is usually introduced once and its composition/support
    articles follow it.  Taking a bounded suffix recovers alias-only clauses
    such as "위원은 9명으로..." that term-only retrieval misses, while avoiding
    unrelated committees hundreds of articles later.
    """
    compact = _compact(full_text)
    starts = []
    for term in case.get("target_terms") or []:
        term_compact = _compact(str(term))
        if not _BODY.search(term_compact):
            continue
        index = compact.find(term_compact)
        if index >= 0:
            starts.append(index)
    if not starts:
        return " ".join(routes._target_passages(case))
    start = max(0, min(starts) - 300)
    # Eight thousand compact characters is long enough to cover a committee's
    # installation/composition/secretariat block in the audited corpus.
    return compact[start : start + 8_000]


def extract_finite_activity_months(text: str) -> int | None:
    compact = _compact(text)
    for index, pattern in enumerate(_FINITE_DURATION_PATTERNS):
        match = pattern.search(compact)
        if not match:
            continue
        value = int(match.group(1))
        months = value * 12 if index < 2 else value
        if 0 < months <= 120:
            return months
    return None


@lru_cache(maxsize=1)
def _amount_rows() -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = defaultdict(dict)
    for path in glob.glob("backend/generated/**/cost_estimate_amounts.jsonl", recursive=True):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item_id = str(row.get("item_id") or "")
                if not item_id:
                    continue
                key = (
                    row.get("year_offset"),
                    row.get("amount_thousand"),
                    bool(row.get("is_total")),
                    str(row.get("formula_text") or ""),
                )
                grouped[item_id].setdefault(key, row)
    return {
        item_id: tuple(
            sorted(
                rows.values(),
                key=lambda row: (
                    row.get("year_offset") is None,
                    int(row.get("year_offset") or 0),
                    str(row.get("year_label") or ""),
                ),
            )
        )
        for item_id, rows in grouped.items()
    }


def _year_values(item_id: str) -> tuple[int, ...]:
    by_offset: dict[int, set[int]] = defaultdict(set)
    for row in _amount_rows().get(item_id, ()):
        if row.get("is_total") or row.get("year_offset") is None:
            continue
        amount = row.get("amount_thousand")
        if amount is None:
            continue
        by_offset[int(row["year_offset"])].add(int(amount))
    if not by_offset:
        return ()
    # Conflicting duplicate extractions are not silently averaged.
    if any(len(values) != 1 for values in by_offset.values()):
        return ()
    last = max(by_offset)
    return tuple(next(iter(by_offset.get(offset, {0}))) for offset in range(last + 1))


def _full_year_amount(item: EvidenceItem) -> int | None:
    implied = _formula_implied_thousand(item.formula)
    if implied is not None:
        return int(round(implied))
    values = [value for value in _year_values(item.item_id) if value > 0]
    if not values:
        return item.annual_thousand if item.annual_thousand and item.annual_thousand > 0 else None
    counts = Counter(values)
    # Repeated full years beat a prorated first/last year.  On a tie the larger
    # value is the conservative full-year rate, not an arbitrary median.
    return max(counts, key=lambda value: (counts[value], value))


def _recurrence(item: EvidenceItem) -> str:
    values = _year_values(item.item_id)
    positives = [offset for offset, value in enumerate(values) if value > 0]
    return "annual" if len(positives) >= 2 else "one_time"


def _variables(item: EvidenceItem) -> tuple[dict[str, Any], ...]:
    from backend.estimator import tag_db

    raw = [dict(value, source="precedent_tag_variable") for value in tag_db._load()["vars"].get(item.item_id, [])]
    existing = {
        (str(value.get("name") or ""), value.get("value"), str(value.get("unit") or ""))
        for value in raw
    }
    for atom in _semantic_atoms_by_item().get(item.item_id, ()):
        role = str(atom.get("evidence_role") or "")
        if role not in {"paid_members", "annual_meetings", "meeting_unit_price"}:
            continue
        value = atom.get("normalized_value")
        if value is None:
            continue
        row = {
            "type": role,
            "name": role,
            "value": value,
            "unit": str(atom.get("normalized_unit") or ""),
            "source_text": str(atom.get("source_text") or "")[:500],
            "source": "precedent_evidence_atom",
        }
        key = (row["name"], row["value"], row["unit"])
        if key not in existing:
            raw.append(row)
            existing.add(key)
    return tuple(raw)


@lru_cache(maxsize=1)
def _semantic_atoms_by_item() -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in glob.glob("backend/generated/**/committee_evidence_atoms.jsonl", recursive=True):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item_id = str(row.get("source_item_key") or "")
                if item_id:
                    grouped[item_id].append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _evidence_formula(item: EvidenceItem) -> tuple[str, str]:
    if item.formula:
        return item.formula, "precedent_tag_formula"
    formulas = {
        str(atom.get("formula_text") or "").strip()
        for atom in _semantic_atoms_by_item().get(item.item_id, ())
        if atom.get("evidence_role") == "formula_expression" and atom.get("formula_text")
    }
    if len(formulas) == 1:
        return next(iter(formulas)), "precedent_evidence_atom_formula"
    return "historical full-year amount", "precedent_amount_table"


def _operand_implied_thousand(item: EvidenceItem) -> float | None:
    values: dict[str, set[float]] = defaultdict(set)
    for atom in _semantic_atoms_by_item().get(item.item_id, ()):
        role = str(atom.get("evidence_role") or "")
        if role not in {"paid_members", "annual_meetings", "meeting_unit_price"}:
            continue
        try:
            values[role].add(float(atom["normalized_value"]))
        except (KeyError, TypeError, ValueError):
            continue
    if any(len(values[role]) != 1 for role in ("paid_members", "annual_meetings", "meeting_unit_price")):
        return None
    paid = next(iter(values["paid_members"]))
    meetings = next(iter(values["annual_meetings"]))
    price = next(iter(values["meeting_unit_price"]))
    return paid * meetings * price / 1_000.0


def _expand(
    full_year_thousand: int,
    *,
    recurrence: str,
    activity_months: int | None,
    horizon_years: int,
) -> tuple[int, ...]:
    if activity_months is not None:
        remaining = activity_months
        values = []
        for _ in range(horizon_years):
            active = min(12, max(0, remaining))
            values.append(int(round(full_year_thousand * active / 12)))
            remaining -= active
        return tuple(values)
    if recurrence == "one_time":
        return (full_year_thousand,) + (0,) * (horizon_years - 1)
    return (full_year_thousand,) * horizon_years


def _is_committee_item(item: EvidenceItem) -> bool:
    return item.family == "committee" or bool(_COMMITTEE_FAMILY.search(item.item_name))


def estimate(
    case: dict[str, Any],
    *,
    horizon_years: int = DEFAULT_HORIZON_YEARS,
) -> CoveredCommitteeEstimate:
    package = resolve_package(case)
    blockers: list[str] = []
    warnings: list[str] = []
    full_text = _case_full_text(case)
    structure_text = _target_structure_text(case, full_text)
    activity_months = extract_finite_activity_months(full_text)

    if package.decision != "exact_package" or not package.selected_bill_no:
        blockers.append("no_complete_precedent_package")
        return CoveredCommitteeEstimate(
            bill_no=str(case["bill_no"]),
            decision="needs_user_input",
            selected_bill_no=package.selected_bill_no,
            relation_route=package.relation_route,
            full_year_thousand=None,
            total_thousand=None,
            yearly_thousand=(),
            activity_months=activity_months,
            line_items=(),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            ready_for_auto_publish=False,
            package=package,
        )

    selected_by_id = {
        item.item_id: item for item in _bill_items(package.selected_bill_no)
    }
    selected = [
        selected_by_id[item_id]
        for item_id in package.matched_item_ids
        if item_id in selected_by_id and _is_committee_item(selected_by_id[item_id])
    ]
    if not selected:
        blockers.append("no_committee_line_item_in_package")

    # A standing member creates payroll, not merely a meeting allowance.  A
    # predecessor without a standing-payroll line is structurally incomplete.
    if _target_has_standing_members(structure_text) and not any(
        _standing_payroll_item(item) for item in selected
    ):
        blockers.append("standing_member_payroll_missing")

    target_limit = _target_member_limit(structure_text)
    precedent_limits = {
        value for item in selected for value in item.total_member_counts if value > 0
    }
    if (
        target_limit is not None
        and precedent_limits
        and target_limit not in precedent_limits
    ):
        blockers.append(
            f"member_limit_changed:target={target_limit}:precedent={max(precedent_limits)}"
        )

    prepared: list[tuple[EvidenceItem, int, str]] = []
    for item in selected:
        amount = _full_year_amount(item)
        if amount is None or amount <= 0:
            blockers.append(f"amount_not_executable:{item.item_id}")
            continue
        operand_implied = _operand_implied_thousand(item)
        if operand_implied is not None:
            relative_error = abs(amount - operand_implied) / max(operand_implied, 1.0)
            if relative_error > 0.20:
                blockers.append(f"formula_operand_amount_conflict:{item.item_id}")
                continue
        prepared.append((item, amount, _recurrence(item)))

    if blockers:
        return CoveredCommitteeEstimate(
            bill_no=str(case["bill_no"]),
            decision="needs_user_input",
            selected_bill_no=package.selected_bill_no,
            relation_route=package.relation_route,
            full_year_thousand=None,
            total_thousand=None,
            yearly_thousand=(),
            activity_months=activity_months,
            line_items=(),
            blockers=tuple(sorted(set(blockers))),
            warnings=tuple(warnings),
            ready_for_auto_publish=False,
            package=package,
        )

    line_items: list[ExecutedLineItem] = []
    aggregate = [0] * horizon_years
    for item, amount, recurrence in prepared:
        yearly = _expand(
            amount,
            recurrence=recurrence,
            activity_months=activity_months,
            horizon_years=horizon_years,
        )
        for index, value in enumerate(yearly):
            aggregate[index] += value
        formula, evidence_basis = _evidence_formula(item)
        line_items.append(
            ExecutedLineItem(
                item_id=item.item_id,
                item_name=item.item_name,
                formula=formula,
                full_year_thousand=amount,
                recurrence=recurrence,
                yearly_thousand=yearly,
                variables=_variables(item),
                evidence_basis=evidence_basis,
            )
        )

    if activity_months is None and "공포후" in _compact(full_text):
        warnings.append(
            "commencement_date_unknown_first_year_proration_requires_review"
        )
    if activity_months is None and not _has_full_source(case):
        warnings.append("full_source_missing_schedule_not_verified")

    full_year_total = sum(item.full_year_thousand for item in line_items)
    return CoveredCommitteeEstimate(
        bill_no=str(case["bill_no"]),
        decision="calculated",
        selected_bill_no=package.selected_bill_no,
        relation_route=package.relation_route,
        full_year_thousand=full_year_total,
        total_thousand=sum(aggregate),
        yearly_thousand=tuple(aggregate),
        activity_months=activity_months,
        line_items=tuple(line_items),
        blockers=(),
        warnings=tuple(warnings),
        ready_for_auto_publish=not warnings,
        package=package,
    )
