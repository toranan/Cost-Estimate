from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    ORGANIZATION = "organization_change"
    COMMITTEE = "committee_operation"
    RESEARCH_PLAN = "research_plan"
    PERSONNEL = "personnel"
    SUBSIDY = "subsidy"
    FACILITY_SYSTEM = "facility_system"
    TAX_REVENUE = "tax_revenue"
    ADMINISTRATIVE = "administrative"
    OTHER = "other"


class RowStatus(str, Enum):
    COMPUTED_REVIEW = "computed_review"
    NEEDS_EVIDENCE = "needs_evidence"
    NEEDS_USER_INPUT = "needs_user_input"
    EXCLUDED = "excluded"


@dataclass
class Segment:
    id: str
    article_ref: str
    text: str
    change_type: str = ""


@dataclass
class AtomicEvent:
    id: str
    segment_ids: list[str]
    article_refs: list[str]
    quotes: list[str]
    actor: str
    action: str
    object: str
    bearer: str
    event_type: str
    obligation: str
    cost_mechanism: str
    additionality: str
    recurrence_text: str
    explanation: str
    grounded: bool = True

    def query_text(self) -> str:
        return " ".join(
            value
            for value in (
                self.object,
                self.action,
                self.event_type,
                self.cost_mechanism,
                self.actor,
                self.bearer,
            )
            if value
        )


@dataclass
class TagCandidate:
    item_id: str
    bill_no: str
    bill_name: str
    item_name: str
    category: str
    family: str
    similarity: float
    variables: list[dict[str, Any]] = field(default_factory=list)
    amounts: list[dict[str, Any]] = field(default_factory=list)
    formula: str = ""
    propose_date: str = ""
    context_families: list[str] = field(default_factory=list)
    context_similarity: float = 0.0
    context_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Recurrence:
    mode: str
    interval_years: int | None
    source: str
    basis: str


@dataclass
class EstimateRow:
    event: AtomicEvent
    status: RowStatus
    reason_codes: list[str] = field(default_factory=list)
    reason: str = ""
    formula: str = ""
    recurrence: Recurrence | None = None
    year_amounts_thousand: list[int | None] = field(default_factory=list)
    selected_candidates: list[TagCandidate] = field(default_factory=list)
    selection_method: str = ""
    selection_reason: str = ""
    selection_confidence: float = 0.0
    non_attachment_evidence: list[dict[str, Any]] = field(default_factory=list)
    narrative_evidence: list[dict[str, Any]] = field(default_factory=list)
    variable_evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_thousand(self) -> int:
        return sum(value or 0 for value in self.year_amounts_thousand)


@dataclass
class EstimateResult:
    bill_no: str | None
    bill_name: str
    doc_type: str
    years: int
    engine_version: str
    prompt_version: str
    dataset_fingerprint: str
    input_hash: str
    cache_hit: bool
    rows: list[EstimateRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def year_totals_thousand(self) -> list[int]:
        totals = [0] * self.years
        for row in self.rows:
            for index, value in enumerate(row.year_amounts_thousand[: self.years]):
                totals[index] += value or 0
        return totals

    def total_thousand(self) -> int:
        return sum(self.year_totals_thousand())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for row in payload["rows"]:
            status = row.get("status")
            if isinstance(status, Enum):
                row["status"] = status.value
        return payload
