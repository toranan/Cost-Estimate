"""Deterministic, offline resolver for complete committee precedents.

This module is deliberately *not* wired into ``tag_pipeline``.  It validates
one missing layer: after a related historical bill is retrieved, does that
bill contain a coherent set of priced TAG items that can be reused together?

The resolver keeps three outcomes separate:

``exact_package``
    The historical relationship, target structure, and every required cost
    component are compatible.  A later integration may execute this package.
``component_only``
    The document is useful evidence, but changed/missing operands make whole
    package reuse unsafe.  It may be shown to a reviewer only.
``abstain``
    The local corpus does not contain a sufficiently grounded precedent.

No target cost-estimate answer, hard-coded bill number, or target amount is
used by the resolver.  Historical labels are consumed only by the evaluator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import re
from typing import Any

from backend.committee_estimator_v3.evidence_store import profiles_by_item
from backend.estimator import tag_db
from backend.scripts import evaluate_committee_precedent_routes as routes


_BODY = re.compile(r"위원회|심의회|협의회")
# Subordinate committee bodies are often mentioned only inside the article
# text (for example, "실무위원회를 둘 수 있다") and can therefore be absent
# from an upstream ``target_terms`` list.  Missing one silently turns an
# incomplete historical package into ``exact_package``.  These are structural
# body types, not bill-specific names or answer labels.
_SUBORDINATE_BODY = re.compile(r"전문위원회|분과위원회|소위원회|실무위원회")
_SUPPORT = re.compile(r"사무국|사무처|사무기구|지원조직|전담조직")
_TOTAL_ITEM = re.compile(r"총\s*(?:추가재정소요|합계|계)|합\s*계|소\s*계")
_FORMULA_OPERAND = re.compile(
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>백만원|억원|만원|천원|원|%|명|인|회|건|개|곳|기관)?"
)
_FORMULA_OPERATOR = re.compile(r"^[\s\u00d7xX*\u00f7/]+$")
_MONEY_SCALE_TO_THOUSAND = {
    "원": 0.001,
    "천원": 1.0,
    "만원": 10.0,
    "백만원": 1_000.0,
    "억원": 100_000.0,
}
_COMPOSITION_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("equal_groups", re.compile(r"동수로|같은수로")),
    (
        "non_public_majority",
        re.compile(r"(?:민간위원|위촉위원|공무원이아닌(?:위원|사람)).{0,25}과반"),
    ),
    (
        # One identically named body can be created under every named
        # institution.  This changes the multiplicative scope even when the
        # committee's own membership clause is word-for-word identical.
        "multi_instance",
        re.compile(
            r"(?:각|각각)[가-힣0-9]{0,45}"
            r"(?:기관|단체|지역|시도|시군구|법원|부처)(?:에|마다|별로)"
        ),
    ),
)


@dataclass(frozen=True)
class EvidenceItem:
    item_id: str
    bill_no: str
    item_name: str
    family: str
    annual_thousand: int | None
    formula: str
    numeric_variable_count: int
    profile_names: tuple[str, ...]
    total_member_counts: tuple[int, ...]
    profile_text: str

    @property
    def coherent(self) -> bool:
        """Whether this row contains an auditable amount or calculation."""
        base = bool(
            (self.annual_thousand is not None and self.annual_thousand > 0)
            or (self.formula and self.numeric_variable_count > 0)
        )
        if not base:
            return False
        implied = _formula_implied_thousand(self.formula)
        if implied is None or self.annual_thousand is None:
            return True
        relative_error = abs(self.annual_thousand - implied) / max(implied, 1.0)
        # Formula tables are commonly rounded to a million won.  A 20% band
        # absorbs that rounding but rejects scale corruption such as a lost
        # zero in an otherwise explicit arithmetic expression.
        return relative_error <= 0.20


@dataclass(frozen=True)
class PrecedentResolution:
    bill_no: str
    decision: str
    selected_bill_no: str | None
    relation_route: str | None
    relation_score: float
    matched_item_ids: tuple[str, ...]
    missing_components: tuple[str, ...]
    conflicts: tuple[str, ...]
    reasons: tuple[str, ...]
    relation_top5: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def _formula_implied_thousand(formula: str) -> float | None:
    """Evaluate an explicit multiplication/division formula in thousand won.

    Only a deliberately small grammar is accepted: numeric operands joined by
    multiplication or division, with exactly one monetary operand.  Textual or
    ambiguous formulas return ``None`` rather than being guessed.
    """
    left = str(formula or "").split("=", 1)[0].strip()
    if not left or not re.search(r"[×xX*÷/]", left):
        return None
    matches = list(_FORMULA_OPERAND.finditer(left))
    if len(matches) < 2:
        return None

    values: list[float] = []
    money_count = 0
    operators: list[str] = []
    previous_end = 0
    for index, match in enumerate(matches):
        between = left[previous_end : match.start()]
        if index == 0:
            # Labels before the first number make the expression ambiguous.
            if between.strip():
                return None
        else:
            if not _FORMULA_OPERATOR.fullmatch(between):
                return None
            operator = re.sub(r"\s+", "", between)
            if len(operator) != 1:
                return None
            operators.append(operator)

        value = float(match.group("number").replace(",", ""))
        unit = str(match.group("unit") or "")
        if unit in _MONEY_SCALE_TO_THOUSAND:
            money_count += 1
            value *= _MONEY_SCALE_TO_THOUSAND[unit]
        elif unit == "%":
            value /= 100.0
        values.append(value)
        previous_end = match.end()

    if left[previous_end:].strip() or money_count != 1 or len(operators) != len(values) - 1:
        return None
    result = values[0]
    for operator, value in zip(operators, values[1:]):
        if operator in {"×", "x", "X", "*"}:
            result *= value
        elif operator in {"÷", "/"}:
            if value == 0:
                return None
            result /= value
        else:
            return None
    return result if result > 0 else None


def _profile_text(profile: dict[str, Any]) -> str:
    attributes = profile.get("raw_attributes") or {}
    return " ".join(
        [
            str(profile.get("source_text") or ""),
            str(attributes.get("profile_field_context") or ""),
        ]
    )


def _bill_items(bill_no: str) -> list[EvidenceItem]:
    data = tag_db._load()
    formulas = tag_db._formula_by_item()
    indexed_profiles = profiles_by_item()
    output: list[EvidenceItem] = []
    for item_id, metadata in data["items"].items():
        if str(metadata.get("bill_no") or "") != bill_no:
            continue
        item_name = str(metadata.get("name") or "")
        if _TOTAL_ITEM.search(_normalise_name(item_name)):
            continue
        variables = data["vars"].get(item_id, [])
        numeric_variable_count = 0
        for variable in variables:
            try:
                if variable.get("value") is not None and float(variable["value"]) > 0:
                    numeric_variable_count += 1
            except (TypeError, ValueError):
                continue
        profiles = indexed_profiles.get(item_id, [])
        counts: set[int] = set()
        for profile in profiles:
            value = profile.get("total_member_count")
            try:
                if value is not None and float(value) > 0:
                    counts.add(int(round(float(value))))
            except (TypeError, ValueError):
                continue
        output.append(
            EvidenceItem(
                item_id=item_id,
                bill_no=bill_no,
                item_name=item_name,
                family=str(metadata.get("family") or ""),
                annual_thousand=data["annual"].get(item_id),
                formula=str(formulas.get(item_id) or ""),
                numeric_variable_count=numeric_variable_count,
                profile_names=tuple(
                    sorted(
                        {
                            _normalise_name(str(profile.get("normalized_name") or ""))
                            for profile in profiles
                            if profile.get("normalized_name")
                        }
                    )
                ),
                total_member_counts=tuple(sorted(counts)),
                profile_text=" ".join(_profile_text(profile) for profile in profiles),
            )
        )
    return output


def _target_bodies(case: dict[str, Any]) -> list[str]:
    bodies: list[str] = []
    for term in case.get("target_terms") or []:
        value = _normalise_name(str(term))
        if value and _BODY.search(value) and value not in bodies:
            bodies.append(value)

    # ``target_terms`` is a hint, not a complete contract.  Recover explicit
    # subordinate bodies from the source-law passages so package completeness
    # is checked against what the bill actually says.  Generic aliases such as
    # bare "위원회" are intentionally ignored because they usually refer back to
    # the already-listed main body and would create duplicate requirements.
    source = _normalise_name(_source_text(case))
    for match in _SUBORDINATE_BODY.finditer(source):
        value = match.group(0)
        if value and not any(value in body or body in value for body in bodies):
            bodies.append(value)
    return bodies


def _target_support_components(case: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for term in case.get("target_terms") or []:
        value = _normalise_name(str(term))
        match = _SUPPORT.search(value)
        if match and match.group(0) not in values:
            values.append(match.group(0))
    return values


def _source_text(case: dict[str, Any]) -> str:
    return " ".join(routes._target_passages(case))


def _name_similarity(left: str, right: str) -> float:
    left_grams = routes._char_ngrams(_normalise_name(left))
    right_grams = routes._char_ngrams(_normalise_name(right))
    return routes._jaccard(left_grams, right_grams)


def _item_names(item: EvidenceItem) -> tuple[str, ...]:
    names = {_normalise_name(item.item_name), *item.profile_names}
    return tuple(name for name in names if name)


def _body_match_score(body: str, item: EvidenceItem) -> float:
    best = 0.0
    for name in _item_names(item):
        if body in name or name in body:
            if min(len(body), len(name)) >= 5:
                best = max(best, 1.0)
                continue
        best = max(best, _name_similarity(body, name))
    return best


def _body_is_bundled_in_profile(body: str, item: EvidenceItem) -> bool:
    """Whether a subordinate body is explicitly covered by a parent item.

    NABO sometimes emits one priced row for the main committee even though
    the supporting law passage in that same row also contains its working or
    specialist committees.  This is narrower than fuzzy name matching: the
    exact subordinate type must occur in the selected item's own provenance.
    """
    return bool(
        _SUBORDINATE_BODY.fullmatch(body)
        and body in _normalise_name(item.profile_text)
    )


def _target_passage_for_body(case: dict[str, Any], body: str) -> str:
    passages = routes._target_passages(case)
    selected = [passage for passage in passages if body in _normalise_name(passage)]
    return " ".join(selected or passages)


def _target_member_limit(text: str) -> int | None:
    compact = _normalise_name(text)
    values = [
        int(value)
        for value in re.findall(r"(\d+)(?:명|인)(?:이내|내외)", compact)
    ]
    values.extend(
        int(value)
        for value in re.findall(
            r"위원장(?:및[가-힣]{1,12}각)?\d*(?:명|인)(?:을|를)?"
            r"포함(?:한|하여)(\d+)(?:명|인)(?:의)?[가-힣]{0,12}위원",
            compact,
        )
    )
    # A passage may separately cap appointed members and the whole body.  The
    # largest cap is the legal total and is the comparable profile field.
    return max(values, default=None)


def _composition_flags(text: str) -> set[str]:
    compact = _normalise_name(text)
    return {
        name for name, pattern in _COMPOSITION_FLAGS if pattern.search(compact)
    }


def _standing_payroll_item(item: EvidenceItem) -> bool:
    text = _normalise_name(f"{item.item_name} {item.formula}").replace("비상임위원", "")
    return bool(
        re.search(r"상임위원.{0,30}(?:인건비|보수|봉급|급여)", text)
        or re.search(r"(?:인건비|보수|봉급|급여).{0,30}상임위원", text)
    )


def _target_has_standing_members(text: str) -> bool:
    """Recognise both noun and predicate forms used in Korean statutes."""
    compact = _normalise_name(text).replace("비상임위원", "")
    return bool(
        "상임위원" in compact
        or re.search(r"위원.{0,25}상임으로", compact)
        or re.search(r"상임으로.{0,25}위원", compact)
    )


@lru_cache(maxsize=1)
def _relation_corpus() -> tuple[dict[str, routes.CandidateText], dict[str, float]]:
    metadata = routes._load_metadata()
    candidates = routes._load_candidate_texts(metadata)
    idf = routes._idf_by_token(candidates.values())
    return candidates, idf


def _relation_ranking(case: dict[str, Any]) -> list[dict[str, Any]]:
    candidates, idf = _relation_corpus()
    cutoff = str(case.get("cutoff_date") or case["propose_date"])
    passages = routes._target_passages(case)
    eligible = [
        candidate
        for candidate in candidates.values()
        if routes._eligible_at_cutoff(
            candidate,
            target_bill_no=str(case["bill_no"]),
            cutoff_date=cutoff,
        )
    ]
    return sorted(
        (
            routes._relation_candidate(case, passages, candidate, idf)
            for candidate in eligible
        ),
        key=lambda row: (
            float(row["relation_score"]),
            str(row["available_date"]),
            str(row["bill_no"]),
        ),
        reverse=True,
    )


def _relation_is_strong(
    first: dict[str, Any],
    ranking: list[dict[str, Any]],
) -> bool:
    if bool(first.get("same_law")):
        # Item compatibility supplies the second signal.  This intentionally
        # accepts the lower source-template score of short amendment clauses,
        # while rejecting unrelated amendments to the same broad law.
        return float(first.get("template_similarity") or 0) >= 0.10
    competitors = ranking[1:]
    second = float(competitors[0]["relation_score"]) if competitors else 0.0
    margin = float(first["relation_score"]) - second
    return bool(
        float(first.get("title_similarity") or 0) >= 0.25
        and float(first.get("template_similarity") or 0) >= 0.65
        and margin >= 0.15
    )


def _direct_package_amount(bill_no: str, bodies: list[str]) -> int | None:
    """Return the priced body bundle without using renamed-body fallbacks."""
    items = [item for item in _bill_items(bill_no) if item.coherent]
    selected: set[str] = set()
    amounts: list[int] = []
    for body in bodies:
        body_items = [item for item in items if _body_match_score(body, item) >= 0.72]
        if not body_items:
            return None
        for item in body_items:
            if item.item_id in selected or item.annual_thousand is None:
                continue
            selected.add(item.item_id)
            amounts.append(item.annual_thousand)
    return sum(amounts) if amounts else None


def _same_law_package_consensus(
    case: dict[str, Any],
    ranking: list[dict[str, Any]],
    bodies: list[str],
) -> tuple[bool, list[tuple[str, int]]]:
    """Check whether at least two prior versions support the top package.

    Same-law amendment titles are not unique event identifiers.  Consensus is
    therefore measured on the priced target-body bundle, not document title or
    raw similarity.  A 10% band absorbs table rounding while keeping materially
    different operating assumptions separate.
    """
    candidates: list[tuple[str, int]] = []
    for row in ranking:
        if not bool(row.get("same_law")):
            continue
        if float(row.get("template_similarity") or 0) < 0.10:
            continue
        bill_no = str(row["bill_no"])
        amount = _direct_package_amount(bill_no, bodies)
        if amount is not None and amount > 0:
            candidates.append((bill_no, amount))
    if not candidates:
        return False, []
    top_no, top_amount = candidates[0]
    supporters = [
        (bill_no, amount)
        for bill_no, amount in candidates
        if abs(amount - top_amount) / max(top_amount, 1) <= 0.10
    ]
    return len(supporters) >= 2 and supporters[0][0] == top_no, candidates


def resolve(case: dict[str, Any]) -> PrecedentResolution:
    """Resolve one source-only case against historical TAG evidence."""
    ranking = _relation_ranking(case)
    if not ranking:
        return PrecedentResolution(
            bill_no=str(case["bill_no"]),
            decision="abstain",
            selected_bill_no=None,
            relation_route=None,
            relation_score=0.0,
            matched_item_ids=(),
            missing_components=(),
            conflicts=(),
            reasons=("no_historical_cost_estimate",),
            relation_top5=(),
        )

    first = ranking[0]
    relation_strong = _relation_is_strong(first, ranking)
    candidate_no = str(first["bill_no"])
    items = _bill_items(candidate_no)
    coherent = [item for item in items if item.coherent]
    bodies = _target_bodies(case)
    support = _target_support_components(case)
    source = _source_text(case)
    matched: dict[str, list[EvidenceItem]] = {}
    missing: list[str] = []
    conflicts: list[str] = []

    # A same title is a weak identity key for amendment bills: unrelated
    # amendments all normalize to the same law name.  Short event passages
    # need either strong source-template continuity or repeated agreement of
    # the priced package across at least two earlier versions.
    amendment_title = bool(re.search(r"(?:일부|전부)개정법률안$", str(case.get("bill_name") or "")))
    package_consensus, package_history = _same_law_package_consensus(
        case,
        ranking,
        bodies,
    )
    if (
        amendment_title
        and bool(first.get("same_law"))
        and float(first.get("template_similarity") or 0) < 0.35
        and not package_consensus
    ):
        conflicts.append("amendment_event_not_corroborated")
    if amendment_title and package_history:
        top_amount = package_history[0][1]
        comparable = [amount for _, amount in package_history[1:]]
        if comparable and all(
            abs(amount - top_amount) / max(top_amount, 1) > 0.20
            for amount in comparable
        ):
            conflicts.append("same_law_package_disagreement")

    # Exact/containment entity matches are preferred.  A high fuzzy match is
    # allowed only inside an already established legislative lineage.
    unmatched_items = list(coherent)
    for body in bodies:
        ranked_items = sorted(
            (
                (_body_match_score(body, item), item)
                for item in coherent
                if item.family == "committee" or _BODY.search(item.item_name)
            ),
            key=lambda pair: (pair[0], pair[1].item_id),
            reverse=True,
        )
        direct = [
            item
            for score, item in ranked_items
            if score >= 0.72
            # A short legal qualifier such as "전문" may be inserted into an
            # otherwise identical body name.  The relaxed name threshold is
            # available only after an independently strong bill relationship;
            # name overlap alone can never create an executable precedent.
            or (relation_strong and score >= 0.65)
        ]
        if direct:
            matched[body] = direct
            used = {item.item_id for item in direct}
            unmatched_items = [item for item in unmatched_items if item.item_id not in used]
        elif bundled := [
            item
            for values in matched.values()
            for item in values
            if _body_is_bundled_in_profile(body, item)
        ]:
            # Keep the parent row as the auditable package evidence.  The set
            # conversion used for ``matched_item_ids`` prevents double-counting
            # when multiple subordinate bodies share the same parent row.
            matched[body] = bundled
        elif bool(first.get("same_law")) and float(first.get("template_similarity") or 0) >= 0.10:
            fallback = next(
                (
                    item
                    for score, item in ranked_items
                    if score >= 0.18 and item in unmatched_items
                ),
                None,
            )
            if fallback:
                matched[body] = [fallback]
                unmatched_items.remove(fallback)
            else:
                missing.append(f"body:{body}")
        else:
            missing.append(f"body:{body}")

    # Reintroduced bills can rename the main body while retaining all cost
    # items.  Allow one-to-one fallback only after the same-law relationship
    # and source-event thresholds have both passed.
    if (
        missing
        and bool(first.get("same_law"))
        and float(first.get("template_similarity") or 0) >= 0.10
    ):
        available_committee = [
            item
            for item in coherent
            if (item.family == "committee" or _BODY.search(item.item_name))
            and all(item not in values for values in matched.values())
        ]
        for key in list(missing):
            if not key.startswith("body:") or not available_committee:
                continue
            body = key.split(":", 1)[1]
            item = available_committee.pop(0)
            matched[body] = [item]
            missing.remove(key)

    for component in support:
        support_items = [
            item for item in coherent if component in _normalise_name(item.item_name)
        ]
        if support_items:
            matched[f"support:{component}"] = support_items
        else:
            missing.append(f"support:{component}")

    if _target_has_standing_members(source):
        standing_items = [item for item in coherent if _standing_payroll_item(item)]
        if standing_items:
            matched["standing_member_payroll"] = standing_items
        else:
            missing.append("standing_member_payroll")

    # Compare only facts attached to the matched item/body.  Full-document
    # flags are intentionally not used because unrelated articles caused the
    # false conflicts found in the retrospective eight-case audit.
    for body, body_items in matched.items():
        if body.startswith("support:") or body == "standing_member_payroll":
            continue
        passage = _target_passage_for_body(case, body)
        target_limit = _target_member_limit(passage)
        target_flags = _composition_flags(passage)
        profile_counts = {
            count for item in body_items for count in item.total_member_counts
        }
        if target_limit is not None and profile_counts and target_limit not in profile_counts:
            conflicts.append(
                f"member_limit:{body}:target={target_limit}:precedent={max(profile_counts)}"
            )
        profile_text = " ".join(item.profile_text for item in body_items)
        precedent_flags = _composition_flags(profile_text)
        for flag in sorted(target_flags - precedent_flags):
            conflicts.append(f"composition:{body}:{flag}_not_supported")

    matched_ids = tuple(
        sorted({item.item_id for values in matched.values() for item in values})
    )
    reasons: list[str] = []
    if not relation_strong:
        reasons.append("relationship_not_strong_enough_for_execution")
    if missing:
        reasons.append("required_cost_component_missing")
    if conflicts:
        reasons.append("target_and_precedent_structure_conflict")

    if relation_strong and matched_ids and not missing and not conflicts:
        decision = "exact_package"
        selected = candidate_no
    elif matched_ids or bool(first.get("same_law")):
        decision = "component_only"
        selected = candidate_no
    else:
        decision = "abstain"
        selected = None
    return PrecedentResolution(
        bill_no=str(case["bill_no"]),
        decision=decision,
        selected_bill_no=selected,
        relation_route=str(first.get("route") or "") or None,
        relation_score=float(first.get("relation_score") or 0),
        matched_item_ids=matched_ids,
        missing_components=tuple(sorted(set(missing))),
        conflicts=tuple(sorted(set(conflicts))),
        reasons=tuple(reasons),
        relation_top5=tuple(str(row["bill_no"]) for row in ranking[:5]),
    )
