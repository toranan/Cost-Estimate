from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import GENERATED_DIR


SEED_DIR = GENERATED_DIR / "assembly_rag_seed_22_ce"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _tokens(value: Any) -> set[str]:
    compact = _compact(value)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _overlap(left: Any, right: Any) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _committee_name(text: str) -> str | None:
    matches = re.findall(r"([가-힣A-Za-z0-9·ㆍ]{2,35}특별위원회)", _compact(text))
    if matches:
        return max(matches, key=len)
    return None


def _is_legislative_special_committee(text: str, articles: list[dict[str, Any]]) -> bool:
    haystack = _compact(
        text[:7000]
        + " "
        + " ".join(
            f"{article.get('no') or ''} {article.get('text') or ''}"
            for article in articles
        )
    )
    return (
        ("국회법" in haystack or "국회" in haystack)
        and "특별위원회" in haystack
        and bool(re.search(r"(둔다|두고|설치|신설|구성)", haystack))
    )


@lru_cache(maxsize=1)
def _load_cases() -> list[dict[str, Any]]:
    structures = {
        row["struct_id"]: row
        for row in _read_jsonl(SEED_DIR / "cost_estimate_structures.jsonl")
        if row.get("struct_id")
    }
    items_by_struct: dict[str, list[dict[str, Any]]] = {}
    items_by_id: dict[str, dict[str, Any]] = {}
    for item in _read_jsonl(SEED_DIR / "cost_estimate_items.jsonl"):
        struct_id = str(item.get("struct_id") or "")
        item_id = str(item.get("item_id") or "")
        if not struct_id or not item_id:
            continue
        items_by_struct.setdefault(struct_id, []).append(item)
        items_by_id[item_id] = item

    amounts_by_item: dict[str, list[dict[str, Any]]] = {}
    for amount in _read_jsonl(SEED_DIR / "cost_estimate_amounts.jsonl"):
        item_id = str(amount.get("item_id") or "")
        if item_id:
            amounts_by_item.setdefault(item_id, []).append(amount)

    variables_by_item: dict[str, list[dict[str, Any]]] = {}
    for variable in _read_jsonl(SEED_DIR / "cost_estimate_variables.jsonl"):
        item_id = str(variable.get("item_id") or "")
        if item_id:
            variables_by_item.setdefault(item_id, []).append(variable)

    cases: list[dict[str, Any]] = []
    for struct_id, structure in structures.items():
        items = items_by_struct.get(struct_id) or []
        searchable = " ".join(
            [
                str(structure.get("bill_name") or ""),
                str(structure.get("committee") or ""),
                *[
                    f"{item.get('item_name') or ''} {item.get('trigger_ref') or ''}"
                    for item in items
                ],
            ]
        )
        compact = _compact(searchable)
        if "국회법" not in compact or "특별위원회" not in compact:
            continue
        cases.append({
            "structure": structure,
            "items": items,
            "amounts_by_item": amounts_by_item,
            "variables_by_item": variables_by_item,
            "searchable": searchable,
            "committee_name": _committee_name(searchable),
        })
    return cases


def _rank_case(query: str, case: dict[str, Any]) -> float:
    searchable = str(case.get("searchable") or "")
    score = _overlap(query, searchable)
    query_name = _committee_name(query)
    case_name = case.get("committee_name")
    if query_name and case_name:
        name_overlap = _overlap(query_name, case_name)
        score += name_overlap * 0.9
        if query_name == case_name:
            score += 0.6
    if "헌법" in query and "헌법" in searchable:
        score += 0.35
    if "국회법" in query and "국회법" in searchable:
        score += 0.2
    return score


def _family(item_name: str) -> str:
    compact = _compact(item_name)
    if "기관부담" in compact:
        return "employer_contribution"
    if "기본경비" in compact:
        return "basic_expense"
    if "자산취득" in compact:
        return "asset_acquisition"
    if "보수" in compact or "인건비" in compact:
        return "personnel_compensation"
    return "committee_operation"


def _assumptions(
    variables: list[dict[str, Any]],
    *,
    source_bill_no: str,
    source_bill_name: str,
) -> list[dict[str, Any]]:
    assumptions: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for variable in variables:
        name = str(variable.get("variable_name") or "가정값")
        value = variable.get("variable_value")
        unit = str(variable.get("variable_unit") or "")
        key = (name, value, unit)
        if key in seen:
            continue
        seen.add(key)
        assumptions.append({
            "name": name,
            "value": value,
            "unit": unit,
            "basis": variable.get("source_text") or "유사 국회 비용추계서의 전제값",
            "source_type": "analogous_cost_estimate",
            "source_bill_no": source_bill_no,
            "source_bill_name": source_bill_name,
            "needs_user_confirm": True,
        })
    return assumptions


def build_analogical_committee_estimate(
    *,
    text: str,
    articles: list[dict[str, Any]],
    years: int = 5,
    exclude_bill_nos: set[str] | None = None,
) -> dict[str, Any] | None:
    """Build a reviewable estimate from a structurally similar official case.

    The source case remains evidence, not an invisible constant. Every copied
    assumption and yearly baseline carries its bill number and review flag.
    """
    if not _is_legislative_special_committee(text, articles):
        return None

    query = " ".join(
        [
            text[:7000],
            *[
                f"{article.get('no') or ''} {article.get('text') or ''}"
                for article in articles
                if article.get("cost_trigger")
            ],
        ]
    )
    excluded = {str(value) for value in (exclude_bill_nos or set()) if value}
    eligible_cases = [
        case
        for case in _load_cases()
        if str((case.get("structure") or {}).get("bill_no") or "") not in excluded
    ]
    ranked = sorted(
        ((_rank_case(query, case), case) for case in eligible_cases),
        key=lambda row: row[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.65:
        return None

    score, selected = ranked[0]
    structure = selected["structure"]
    source_bill_no = str(structure.get("bill_no") or "")
    source_bill_name = str(structure.get("bill_name") or "")
    output_items: list[dict[str, Any]] = []
    for source_item in sorted(
        selected["items"],
        key=lambda row: int(row.get("item_order") or 0),
    ):
        item_name = str(source_item.get("item_name") or "")
        if not item_name or str(source_item.get("item_category") or "") == "합계":
            continue
        amount_rows = [
            row
            for row in selected["amounts_by_item"].get(source_item["item_id"], [])
            if not row.get("is_total") and row.get("amount_thousand") is not None
        ]
        amount_rows.sort(key=lambda row: int(row.get("year_offset") or 0))
        if not amount_rows:
            continue
        series = [int(row["amount_thousand"]) for row in amount_rows[:years]]
        if len(series) < years:
            series.extend([series[-1]] * (years - len(series)))
        formula = next(
            (
                str(row.get("formula_text") or "")
                for row in amount_rows
                if row.get("formula_text")
            ),
            "유사 국회 비용추계 사례의 연도별 산출 구조",
        )
        variables = selected["variables_by_item"].get(source_item["item_id"], [])
        output_items.append({
            "name": item_name,
            "category": source_item.get("item_category") or "운영비",
            "trigger_ref": next(
                (
                    str(article.get("no") or "")
                    for article in articles
                    if article.get("cost_trigger") and "특별위원회" in str(article.get("text") or "")
                ),
                str(source_item.get("trigger_ref") or ""),
            ),
            "formula": formula,
            "formula_family": _family(item_name),
            "variables_needed": [
                str(variable.get("variable_name") or "")
                for variable in variables
                if variable.get("variable_name")
            ],
            "assumptions": _assumptions(
                variables,
                source_bill_no=source_bill_no,
                source_bill_name=source_bill_name,
            ),
            "calculation": {
                "mode": "yearly_series",
                "yearly_amounts_thousand": series,
                "recurrence": "one_time" if sum(1 for value in series if value) == 1 else "annual",
                "start_year": 1,
                "end_year": years,
                "source_note": (
                    f"유사 국회 비용추계서 {source_bill_no}의 항목별 산출 구조를 기준선으로 적용"
                ),
            },
            "analogy_evidence": {
                "bill_no": source_bill_no,
                "bill_name": source_bill_name,
                "item_name": item_name,
                "trigger_ref": source_item.get("trigger_ref"),
                "score": round(score, 3),
                "application": "산식·가정 기준선",
            },
            "requires_review": True,
            "review_reason": "유사 의안의 공식 추계 전제를 적용했으므로 조직 규모와 직무의 동등성 확인 필요",
        })

    if not output_items:
        return None
    return {
        "items": output_items,
        "source": "analogical_cost_estimate",
        "analogy_selection": {
            "bill_no": source_bill_no,
            "bill_name": source_bill_name,
            "score": round(score, 3),
            "reason": "법률 체계, 설치 조직 유형 및 위원회 명칭의 구조적 유사도",
            "requires_review": True,
        },
    }
