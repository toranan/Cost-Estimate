"""Formula-first, operand-by-operand precedent selection.

The normal path keeps every operand from the selected formula precedent.  A
different precedent may supply a value only when the selected precedent does
not contain a compatible operand.  This avoids both failure modes seen in the
prototype: blindly copying one incomplete case and independently taking
medians that never coexisted in a real estimate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from ..analyzer_v2 import GEMINI_MODEL, gemini_json
from .cache import get_or_create_json
from .types import AtomicEvent, EventType, TagCandidate


VARIABLE_SELECTION_VERSION = "formula-first-variable-evidence-v14"

_ROLE_LABEL = {
    "count": "수당 지급대상 인원",
    "unit_price": "1인 1회당 수당",
    "frequency": "연간 회의 횟수",
}
_MONEY_FACTOR_TO_WON = (
    ("억원", 100_000_000.0),
    ("백만원", 1_000_000.0),
    ("만원", 10_000.0),
    ("천원", 1_000.0),
    ("원", 1.0),
)

_PROMPT = """당신은 이미 선택된 비용 산식의 빈 변수값을 채울 공식 선례 선정자다.
후보 값은 같은 비용유형, 대상 의안 제외, 제안일 이전이라는 1차 검증을 통과했다.

선택 우선순위:
1. 변수의 계산 역할이 정확히 같은가
2. 위원회의 기능·소속·상설성·회의 성격이 비슷한가
3. 법정 위원 수 등 대상 조문의 규모와 양립하는가
4. 단위와 기간이 같은가
5. 마지막으로 항목 임베딩 유사도를 참고한다

역할별 판단:
- count: 전체 정원이 아니라 실제 수당 지급대상인지 확인하고, 법정 정원 상한,
  당연직·공무원·민간위원 구성과 소관 분야가 가까운 사례를 우선한다.
  대상 조문에 민간위원 비율이 없으면 전체 정원이 같다는 이유만으로 다른 위원회의
  민간위원 비율을 복사하지 말고, 정원 상한과 양립하면서 소관 분야·기능·기준연도가
  더 가까운 실제 구성 사례를 우선한다.
- unit_price: 위원회 주제보다 같은 비용 구성(참석수당만/안건검토비 포함)과
  기준 연도를 우선한다. 공식 예산편성지침은 강한 근거지만, 같은 지침을 쓰더라도
  참석수당·안건검토비·여비 중 합산 범위가 다르면 최신 연도라는 이유만으로
  선택하지 마라.
- frequency: 단어가 비슷한 위원회보다 실제 기능을 우선한다. 분쟁조정·인허가처럼
  사건마다 열리는 위원회와 분기별 정책·계획 심의위원회를 구분한다.
  다른 기관의 회의 실적을 이미 준용해 만든 값은 다시 다른 대상에 차용하지 않는다.

금지:
- 정답 금액을 추측하지 마라.
- 평균·중앙값·최빈값을 만들지 마라.
- 후보의 숫자를 수정하지 마라.
- 이름의 한 단어만 겹친다는 이유로 선택하지 마라.
- 적합한 후보가 없으면 option_id를 빈 문자열로 반환하라.

JSON만 반환한다:
{"option_id":"후보의 정확한 option_id 또는 빈 문자열","reason":"선택 이유","confidence":0.0}

입력:
"""


@dataclass(frozen=True)
class VariableOption:
    option_id: str
    item_id: str
    bill_no: str
    bill_name: str
    item_name: str
    role: str
    name: str
    raw_value: float
    raw_unit: str
    canonical_value: float
    canonical_unit: str
    source_text: str
    similarity: float
    propose_date: str


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _role(variable: dict[str, Any]) -> str | None:
    variable_type = str(variable.get("type") or "")
    name = _compact(str(variable.get("name") or ""))
    unit = _compact(str(variable.get("unit") or ""))
    # The structured TAG type wins over words in the label.  For example
    # "수당 지급대상 인원" contains "수당" but is still a head count.
    if variable_type == "unit_cost":
        return "unit_price"
    if variable_type == "target_count":
        return "count"
    if variable_type == "frequency":
        # A multi-year plan cycle is not an annual meeting frequency.
        if re.search(r"주기|수립", name) or ("년" in unit and "회" not in unit):
            return None
        return "frequency"
    if re.search(r"수당|단가|1인당|인당", name):
        return "unit_price"
    if re.search(r"인원|위원수|참석자|지급대상", name):
        return "count"
    if re.search(r"회의횟수|개최횟수|연간횟수", name):
        return "frequency"
    return None


def _canonical_value(
    role: str,
    value: Any,
    unit: str,
) -> tuple[float, str] | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    compact_unit = _compact(unit)
    if role == "unit_price":
        factor = next(
            (
                multiplier
                for marker, multiplier in _MONEY_FACTOR_TO_WON
                if marker in compact_unit
            ),
            None,
        )
        if factor is None:
            return None
        return number * factor, "원/명·회"
    if role == "count":
        if not re.search(r"명|인|위원", compact_unit):
            return None
        return number, "명"
    if role == "frequency":
        if not re.search(r"회|번", compact_unit):
            return None
        return number, "회/년"
    return None


def candidate_options(candidate: TagCandidate) -> list[VariableOption]:
    options: list[VariableOption] = []
    for index, variable in enumerate(candidate.variables):
        role = _role(variable)
        if role is None:
            continue
        canonical = _canonical_value(
            role,
            variable.get("value"),
            str(variable.get("unit") or ""),
        )
        if canonical is None:
            continue
        value, unit = canonical
        options.append(
            VariableOption(
                option_id=f"{candidate.item_id}:V{index + 1}",
                item_id=candidate.item_id,
                bill_no=candidate.bill_no,
                bill_name=candidate.bill_name,
                item_name=candidate.item_name,
                role=role,
                name=str(variable.get("name") or ""),
                raw_value=float(variable["value"]),
                raw_unit=str(variable.get("unit") or ""),
                canonical_value=value,
                canonical_unit=unit,
                source_text=str(variable.get("source_text") or ""),
                similarity=max(
                    candidate.similarity,
                    candidate.context_similarity,
                ),
                propose_date=candidate.propose_date,
            )
        )
    return options


def committee_formula_roles(candidate: TagCandidate) -> tuple[str, ...]:
    """Return only formula roles that this calculator can execute safely."""
    text = _compact(f"{candidate.formula} {candidate.item_name}")
    present = {option.role for option in candidate_options(candidate)}
    if re.search(r"수당|단가", text):
        present.add("unit_price")
    if re.search(r"위원수|위원인원|참석인원|지급대상|인원", text):
        present.add("count")
    if re.search(r"회의횟수|개최횟수|연간.*회|회수|횟수", text):
        present.add("frequency")
    required = ("count", "unit_price", "frequency")
    return required if all(role in present for role in required) else ()


def has_time_varying_operands(candidate: TagCandidate) -> bool:
    """Reject a scalar formula when one role changes across precedent years.

    For example, "first year 12 meetings, later 1 meeting" is a valid
    precedent time series but is not one reusable annual operand.  Until the
    target has an explicit phase schedule, collapsing either value would
    silently repeat a start-up year or erase it.
    """
    values_by_role: dict[str, set[float]] = {}
    for option in candidate_options(candidate):
        values_by_role.setdefault(option.role, set()).add(option.canonical_value)
    return any(len(values) > 1 for values in values_by_role.values())


def _target_member_cap(source_text: str) -> int | None:
    # Assembly PDFs can split one Korean word across a line boundary
    # ("25인이 내").  Membership limits are whitespace-insensitive legal
    # tokens, so parse the compact form instead of silently losing the cap.
    compact_source = _compact(source_text)
    values = [
        int(value)
        for value in re.findall(r"(\d+)(?:명|인)이내", compact_source)
    ]
    return max(values, default=None)


def _committee_function(text: str) -> str:
    compact = _compact(text)
    if re.search(
        r"공론조사|공론절차|공론화|국민공론|시민공론|숙의|"
        r"국민참여|시민참여|주민참여",
        compact,
    ):
        return "participatory_deliberation"
    if re.search(
        r"분쟁|징계|판정|심판|인허가|처분|제재"
        r"|조정(?:신청|사건)|(?:신청|사건|분쟁)조정",
        compact,
    ):
        return "case_adjudication"
    if re.search(r"정책|계획|발전|진흥|자문|연구", compact):
        return "policy_deliberation"
    return "general"


def _is_second_order_analogy(option: VariableOption) -> bool:
    """Return whether an operand was itself borrowed from another case.

    A TAG row is a useful precedent, but its assumptions do not all have equal
    provenance.  Reusing a value that the precedent estimate already copied
    from a different body creates an analogy-of-an-analogy: the original
    functional comparison is no longer available to validate against the new
    target.  Such values stay searchable for review, but cannot be used in an
    automatic calculation.
    """
    evidence = _compact(option.source_text)
    borrowed_source = bool(
        re.search(
            r"유사(?:사례|위원회|기관|협의회)|다른(?:위원회|기관|협의회)",
            evidence,
        )
    )
    borrowed_action = bool(
        re.search(r"준용|차용|참고하여|기준으로|적용하여", evidence)
    )
    return borrowed_source and borrowed_action


def _target_member_floor(
    event: AtomicEvent,
    source_text: str,
) -> tuple[int, str] | None:
    """Return one unambiguous statutory minimum for the paid member group."""
    texts = [*event.quotes, source_text]
    matches: list[tuple[int, str]] = []
    pattern = re.compile(
        r"(?P<value>\d[\d,]*)\s*(?:명|인)\s*이상(?:의)?"
        r"\s*(?:위원|참여위원|국민참여위원|참여자|구성원)"
    )
    for text in texts:
        for match in pattern.finditer(text or ""):
            value = int(match.group("value").replace(",", ""))
            if value > 0:
                matches.append((value, match.group(0)))
    unique = {value for value, _ in matches}
    if len(unique) != 1:
        return None
    value = unique.pop()
    quote = next(quote for matched, quote in matches if matched == value)
    return value, quote


def _target_payable_member_count(
    event: AtomicEvent,
    source_text: str,
) -> tuple[int, str] | None:
    """Derive a gross payable count from explicit statutory composition.

    This does not guess how many private members another committee has.  It is
    used only when the target document itself gives a complete group
    decomposition and expressly excludes public officials attending as part of
    their duties from allowance payments.
    """
    source = _compact(" ".join([*event.quotes, source_text]))
    if not re.search(
        r"(?:수당|경비).{0,500}공무원이.{0,100}소관업무.{0,100}"
        r"출석.{0,100}(?:지급하지|지급할수없)",
        source,
    ):
        return None

    total_values = {
        int(value.replace(",", ""))
        for pattern in (
            r"(?:다음각호의)?위원(\d[\d,]*)명으로구성",
            r"(\d[\d,]*)명이내의위원으로구성",
        )
        for value in re.findall(pattern, source)
    }
    if len(total_values) != 1:
        return None
    total = next(iter(total_values))

    groups: dict[str, int] = {}
    for match in re.finditer(
        r"(?P<label>[가-힣·ㆍ과]{2,40}?대표하는위원)"
        r"(?:은)?(?P<value>\d[\d,]*)(?:명|인)(?:이내)?",
        source,
    ):
        label = re.sub(
            r"^.*위원은(?=.+대표하는위원$)",
            "",
            match.group("label"),
        )
        value = int(match.group("value").replace(",", ""))
        if label in groups and groups[label] != value:
            return None
        groups[label] = value
    if not groups or sum(groups.values()) != total:
        return None

    public_count = 0
    public_groups: list[str] = []
    labels = list(groups)
    for label, value in groups.items():
        next_labels = "|".join(
            re.escape(other)
            for other in labels
            if other != label
        )
        boundary = rf"(?=(?:{next_labels})은|제\d+조|$)" if next_labels else r"(?=제\d+조|$)"
        definitions = re.findall(
            rf"{re.escape(label)}은(.{{0,1200}}?){boundary}",
            source,
        )
        is_public_official_group = any(
            re.search(
                r"고위공무원단에속하는|국가공무원|지방공무원|"
                r"(?:중앙행정기관|지방자치단체|사무처)소속공무원",
                definition,
            )
            for definition in definitions
        )
        if is_public_official_group:
            public_count += value
            public_groups.append(label)

    payable = total - public_count
    if public_count <= 0 or payable <= 0:
        return None
    basis = (
        f"조문상 총 {total}명 중 "
        f"{', '.join(public_groups)} {public_count}명은 소관업무 관련 공무원 수당 제외, "
        f"지급 가능 인원 {payable}명"
    )
    return payable, basis


def _target_non_public_majority_count(
    event: AtomicEvent,
    source_text: str,
) -> tuple[int, str] | None:
    """Derive the minimum paid non-public group from the target statute.

    Committee precedents are useful for allowance rates, but they must not
    replace an explicit composition constraint in the target bill.  When the
    bill fixes a membership cap and requires non-public members to be a
    majority, the smallest group satisfying that legal condition is a
    deterministic lower-bound premise.
    """
    source = _compact(" ".join([*event.quotes, source_text]))
    caps = {
        int(value)
        for value in re.findall(r"(\d+)\s*(?:명|인)\s*이내", source)
    }
    if len(caps) != 1 or not re.search(
        r"공무원이아닌사람이과반수|공무원이아닌사람.{0,20}과반수",
        source,
    ):
        return None
    cap = next(iter(caps))
    if cap <= 1:
        return None

    # "전체위원 중" expressly includes the chair.  Otherwise, a clause that
    # first appoints the chair and then separately defines "위원" applies the
    # majority condition to the remaining seats.
    includes_chair = bool(
        re.search(
            r"전체위원(?:중|의).{0,80}공무원이아닌사람.{0,20}과반수",
            source,
        )
    )
    denominator = cap if includes_chair else cap - 1
    count = denominator // 2 + 1
    scope = "전체 위원" if includes_chair else "위원장을 제외한 위원"
    return (
        count,
        f"조문상 정원 상한 {cap}명 중 {scope}의 공무원이 아닌 사람이 "
        f"과반수여야 하므로 최소 {count}명",
    )


def _document_count_option(
    event: AtomicEvent,
    source_text: str,
) -> VariableOption | None:
    payable = _target_payable_member_count(event, source_text)
    if payable is not None:
        value, basis = payable
        return VariableOption(
            option_id="TARGET_DOCUMENT:PAYABLE_MEMBER_COUNT",
            item_id="TARGET_DOCUMENT",
            bill_no="TARGET_DOCUMENT",
            bill_name="대상 의안 조문",
            item_name=event.object,
            role="count",
            name="조문상 수당 지급 가능 인원",
            raw_value=float(value),
            raw_unit="명",
            canonical_value=float(value),
            canonical_unit="명",
            source_text=basis,
            similarity=1.0,
            propose_date="",
        )
    majority = _target_non_public_majority_count(event, source_text)
    if majority is not None:
        value, basis = majority
        return VariableOption(
            option_id="TARGET_DOCUMENT:NON_PUBLIC_MAJORITY_COUNT",
            item_id="TARGET_DOCUMENT",
            bill_no="TARGET_DOCUMENT",
            bill_name="대상 의안 조문",
            item_name=event.object,
            role="count",
            name="조문상 비공무원 최소 인원",
            raw_value=float(value),
            raw_unit="명",
            canonical_value=float(value),
            canonical_unit="명",
            source_text=basis,
            similarity=1.0,
            propose_date="",
        )
    floor = _target_member_floor(event, source_text)
    if floor is None:
        return None
    value, quote = floor
    return VariableOption(
        option_id="TARGET_DOCUMENT:COUNT_MINIMUM",
        item_id="TARGET_DOCUMENT",
        bill_no="TARGET_DOCUMENT",
        bill_name="대상 의안 조문",
        item_name=event.object,
        role="count",
        name="조문상 최소 구성 인원",
        raw_value=float(value),
        raw_unit="명 이상",
        canonical_value=float(value),
        canonical_unit="명",
        source_text=quote,
        similarity=1.0,
        propose_date="",
    )


def _incremental_document_count_option(
    document_option: VariableOption,
    formula_candidate: TagCandidate,
    target_text: str,
) -> VariableOption | None:
    """Adapt a gross statutory count to an explicitly incremental formula.

    A selected precedent can define the reusable operand as
    ``new payable members - incumbent payable members``.  In that case using
    the target's gross payable membership would violate the formula semantics.
    Transfer only the incumbent baseline, and only when the precedent names
    the same supervising authority and has already passed a strong semantic
    formula match.
    """
    count_options = [
        option
        for option in candidate_options(formula_candidate)
        if option.role == "count"
    ]
    if len(count_options) != 1 or formula_candidate.similarity < 0.60:
        return None
    precedent_count = count_options[0]
    raw_evidence = " ".join(
        [
            formula_candidate.formula,
            precedent_count.name,
            precedent_count.source_text,
        ]
    )
    evidence = _compact(raw_evidence)
    if not re.search(r"추가|증가|증원|차이", evidence):
        return None

    baseline_match = re.search(
        r"(?:현행|현재).{0,180}?(?:위원수|위원의수|위촉위원수)"
        r"\(?(\d[\d,]*)명\)?",
        evidence,
    )
    if not baseline_match:
        return None
    baseline = int(baseline_match.group(1).replace(",", ""))

    authority_pattern = re.compile(
        r"([가-힣]{2,20}(?:부|처|청|공사|공단))"
        r"(?=\s*(?:가|은|는|이|의|소속|에서|를|을|장|,|\.|$))"
    )
    target_authorities = set(authority_pattern.findall(target_text))
    precedent_authorities = set(authority_pattern.findall(raw_evidence))
    if not (target_authorities & precedent_authorities):
        return None

    delta = int(round(document_option.canonical_value)) - baseline
    if delta <= 0:
        return None
    authority = sorted(target_authorities & precedent_authorities)[0]
    return VariableOption(
        option_id="TARGET_DOCUMENT:INCREMENTAL_PAYABLE_MEMBER_COUNT",
        item_id=formula_candidate.item_id,
        bill_no=formula_candidate.bill_no,
        bill_name=formula_candidate.bill_name,
        item_name=formula_candidate.item_name,
        role="count",
        name="조문상 추가 수당 지급대상 인원",
        raw_value=float(delta),
        raw_unit="명",
        canonical_value=float(delta),
        canonical_unit="명",
        source_text=(
            f"{document_option.source_text}; 대상 지급 가능 "
            f"{int(document_option.canonical_value)}명 - {authority}의 선택 선례상 "
            f"현행 지급대상 {baseline}명 = 추가 {delta}명"
        ),
        similarity=formula_candidate.similarity,
        propose_date=formula_candidate.propose_date,
    )


def _authority_names(text: str) -> set[str]:
    pattern = re.compile(
        r"([가-힣]{2,20}(?:부|처|청|공사|공단))"
        r"(?=\s*(?:가|은|는|이|의|소속|에서|를|을|장|,|\.|$))"
    )
    return set(pattern.findall(text or ""))


def _shares_supervising_authority(
    event: AtomicEvent,
    target_text: str,
    candidate: TagCandidate,
) -> bool:
    target_authorities = _authority_names(
        " ".join([event.actor, event.object, target_text])
    )
    precedent_text = " ".join(
        [
            candidate.bill_name,
            candidate.item_name,
            candidate.formula,
            *[
                str(variable.get("source_text") or "")
                for variable in candidate.variables
            ],
        ]
    )
    return bool(target_authorities & _authority_names(precedent_text))


def _committee_operand_matches(
    role: str,
    option: VariableOption,
    target_text: str,
) -> bool:
    """Remove variables from other sub-costs inside a compound TAG item."""
    evidence = _compact(f"{option.name} {option.source_text}")
    if role == "count":
        if not re.search(r"위원|위촉|민간|외부|수당지급대상|회의참석", evidence):
            return False
        if re.search(r"직원|공무원인원|추진단인원|증원분", evidence):
            return False
        # A 50:50 or equal-representation premise is not transferable when
        # the target law contains no such composition rule.
        source_balanced = bool(re.search(r"절반|동수|같은수|각\d+명", evidence))
        target_balanced = bool(
            re.search(
                r"절반|동수|같은수|각\s*\d+\s*(?:명|인)\s*씩",
                _compact(target_text),
            )
        )
        if source_balanced and not target_balanced:
            return False
        return True
    if role == "unit_price":
        if not re.search(r"수당|사례금|참석비|안건검토", evidence):
            return False
        return not bool(
            re.search(r"임차료|연구용역|기준인건비|기본경비|사무국운영비", evidence)
        )
    if role == "frequency":
        # Do not chain a precedent's borrowed cadence into a second target.
        # The original similar body may have a completely different workload,
        # and its functional fit cannot be recovered from this TAG operand.
        if _is_second_order_analogy(option):
            return False
        target_function = _committee_function(target_text)
        option_function = _committee_function(
            f"{option.bill_name} {option.item_name} {option.name} {option.source_text}"
        )
        # Event-driven public deliberation and case-driven adjudication have
        # fundamentally different cadences from standing policy committees.
        if target_function == "participatory_deliberation":
            return (
                option_function == target_function
                and bool(re.search(r"회의|개최|실시|공론", evidence))
            )
        if target_function == "case_adjudication":
            return (
                option_function == target_function
                and bool(re.search(r"회의|개최|심사|처리", evidence))
            )
        return bool(re.search(r"회의|개최", evidence))
    return False


def _compatible_options(
    event: AtomicEvent,
    source_text: str,
    role: str,
    candidates: list[TagCandidate],
) -> list[VariableOption]:
    cap = _target_member_cap(source_text)
    result: list[VariableOption] = []
    for candidate in candidates:
        for option in candidate_options(candidate):
            if option.role != role:
                continue
            if not _committee_operand_matches(role, option, source_text):
                continue
            if role == "count" and cap is not None and option.canonical_value > cap:
                continue
            result.append(option)
    return sorted(
        result,
        key=lambda option: (-option.similarity, option.bill_no, option.option_id),
    )


def _general_budget_guideline_year(option: VariableOption) -> int | None:
    """Return the edition year for a general central-government budget rule.

    Committee subjects are often incomparable, while the allowance ceiling is
    set by a government-wide budget guideline.  This recognises provenance,
    not name overlap: court/parliament/internal rules remain institution-
    specific and are not promoted over a directly comparable precedent.
    """
    evidence = _compact(option.source_text)
    if re.search(r"대법원|법원|국회|지방자치단체|자체지침|내부지침", evidence):
        return None
    if not re.search(
        r"(?:예산안편성및기금운용계획안작성세부지침|"
        r"예산및기금운용계획집행지침)",
        evidence,
    ):
        return None
    years = [int(value) for value in re.findall(r"(20\d{2})년도", evidence)]
    return max(years, default=0)


def _latest_general_budget_guideline_option(
    options: list[VariableOption],
    required_scope: str | None = None,
) -> VariableOption | None:
    """Choose the newest comparable general allowance guideline.

    Recency is safe only inside the same cost composition.  Attendance-only
    and attendance-plus-agenda-review amounts are different variables even
    when both cite the same annual government guideline.
    """
    guideline_options = [
        (year, option)
        for option in options
        if (year := _general_budget_guideline_year(option)) is not None
        and (
            required_scope is None
            or _allowance_scope(option) == required_scope
        )
    ]
    if not guideline_options:
        return None
    return max(
        guideline_options,
        key=lambda pair: (
            pair[0],
            pair[1].propose_date,
            pair[1].similarity,
            pair[1].option_id,
        ),
    )[1]


def _allowance_scope(option: VariableOption) -> str:
    evidence = _compact(f"{option.name} {option.source_text}")
    if re.search(r"안건검토|사전자료|자료수집|자문료", evidence):
        return "attendance_plus_review"
    if re.search(r"회의참석|참석수당|회의수당|사례금", evidence):
        return "attendance_only"
    return "unknown"


def _is_direct_complete_paid_count(option: VariableOption) -> bool:
    """Return true only when the precedent directly observes the paid group."""
    evidence = _compact(f"{option.name} {option.source_text}")
    return bool(
        re.search(
            r"총\d+명의민간위원|회의수당지급대상자수(?:는|가)?\d+명|"
            r"수당지급대상위원수(?:는|가)?\d+명",
            evidence,
        )
    )


def _hitl_option(
    role: str,
    value: float,
    *,
    name: str,
    unit: str,
    basis: str,
) -> VariableOption:
    return VariableOption(
        option_id=f"USER_INPUT:{role}",
        item_id="USER_INPUT",
        bill_no="USER_INPUT",
        bill_name="사용자 확인값",
        item_name=name,
        role=role,
        name=name,
        raw_value=float(value),
        raw_unit=unit,
        canonical_value=float(value),
        canonical_unit=unit,
        source_text=basis,
        similarity=1.0,
        propose_date="",
    )


def _validated_hitl_values(
    user_inputs: dict[str, float] | None,
) -> tuple[dict[str, float], str]:
    if not user_inputs:
        return {}, ""
    aliases = {
        "paid_members": "paid_members",
        "incumbent_paid_members": "incumbent_paid_members",
        "unit_price_won": "unit_price_won",
        "annual_meetings": "annual_meetings",
    }
    values: dict[str, float] = {}
    for raw_key, raw_value in user_inputs.items():
        key = aliases.get(str(raw_key))
        if key is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return {}, f"HITL 변수 {raw_key}에 유효한 숫자가 필요합니다."
        minimum = 0 if key == "incumbent_paid_members" else 0
        if value < minimum or (
            key != "incumbent_paid_members" and value <= 0
        ):
            return {}, f"HITL 변수 {raw_key}은(는) 0보다 커야 합니다."
        values[key] = value
    return values, ""


def _masked_option_payload(option: VariableOption) -> dict[str, Any]:
    """Return semantic/provenance fields without revealing the operand value."""
    mask = lambda value: re.sub(r"\d[\d,.]*", "<수치>", str(value or ""))
    return {
        "option_id": option.option_id,
        "bill_no": option.bill_no,
        "bill_name": mask(option.bill_name),
        "item_name": mask(option.item_name),
        "role": option.role,
        "name": mask(option.name),
        "raw_unit": option.raw_unit,
        "canonical_unit": option.canonical_unit,
        "source_text": mask(option.source_text),
    }


def _select_cross_case_option(
    event: AtomicEvent,
    source_text: str,
    formula_candidate: TagCandidate,
    role: str,
    options: list[VariableOption],
    *,
    refresh: bool,
) -> tuple[VariableOption | None, str, float, bool]:
    if not options:
        return None, "역할·단위·규모가 맞는 DB 변수값이 없습니다.", 0.0, False
    # The semantic retrieval already produced this candidate set.  A single
    # remaining value needs no generative tie-breaker.
    if len(options) == 1:
        return options[0], "검증 조건을 모두 만족한 유일한 변수 근거입니다.", 0.9, False
    # Avoid an unnecessary generative call when the three closest independent
    # semantic precedents agree exactly.  This is not a corpus mean/mode: only
    # the structurally filtered nearest neighbours may establish the value.
    # When a dedicated role query produced independent evidence, do not let
    # the selected formula case occupy one of the three agreement slots.  The
    # formula is already represented separately and may carry a borrowed
    # operand that the role-focused search is meant to replace.
    agreement_pool = [
        option
        for option in options
        if option.item_id != formula_candidate.item_id
    ]
    if len({option.bill_no for option in agreement_pool}) < 3:
        agreement_pool = options
    independent: list[VariableOption] = []
    seen_bills: set[str] = set()
    for option in agreement_pool:
        if option.bill_no in seen_bills:
            continue
        seen_bills.add(option.bill_no)
        independent.append(option)
        if len(independent) == 3:
            break
    if (
        len(independent) == 3
        and independent[-1].similarity >= 0.45
        and len({option.canonical_value for option in independent}) == 1
    ):
        selected = independent[0]
        return (
            selected,
            "역할·단위·기능 유형을 통과한 의미 상위 3개 독립 선례가 "
            f"{selected.canonical_value:g}{selected.canonical_unit}로 일치",
            0.9,
            False,
        )
    shortlist = options[:15]
    payload = {
        "selection_version": VARIABLE_SELECTION_VERSION,
        "model": GEMINI_MODEL,
        "event": asdict(event),
        "source_text": source_text,
        "selected_formula": {
            "item_id": formula_candidate.item_id,
            "item_name": formula_candidate.item_name,
            # Do not expose literal operands from the formula precedent here.
            # They anchor the semantic selector to "the same number" and
            # defeat independent evidence selection.
            "formula_family": "committee_meeting_product",
            "required_roles": ["count", "unit_price", "frequency"],
        },
        "variable_role": role,
        "variable_label": _ROLE_LABEL[role],
        # The selector decides only whether the evidence is semantically
        # transferable.  Code reads the chosen option's actual value after
        # selection, so exposing values here would add a needless route to
        # numerical cherry-picking.
        "options": [_masked_option_payload(option) for option in shortlist],
    }
    result, cache_hit = get_or_create_json(
        "variable_evidence_selection",
        payload,
        lambda: gemini_json(
            _PROMPT
            + str(
                {
                    "event": payload["event"],
                    "source_text": source_text,
                    "selected_formula": payload["selected_formula"],
                    "variable": {
                        "role": role,
                        "label": _ROLE_LABEL[role],
                    },
                    "options": payload["options"],
                }
            ),
            temperature=0.0,
        ),
        refresh=refresh,
    )
    parsed = result if isinstance(result, dict) else {}
    option_id = str(parsed.get("option_id") or "")
    selected = next(
        (option for option in shortlist if option.option_id == option_id),
        None,
    )
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason") or "")
    if selected is None:
        return None, reason or "의미상 준용할 변수 근거를 고르지 못했습니다.", confidence, cache_hit
    return selected, reason, confidence, cache_hit


def estimate_committee_formula_first(
    event: AtomicEvent,
    source_text: str,
    formula_candidate: TagCandidate,
    candidates: list[TagCandidate],
    *,
    candidates_by_role: dict[str, list[TagCandidate]] | None = None,
    user_inputs: dict[str, float] | None = None,
    formula_selection_confidence: float = 0.0,
    refresh: bool = False,
) -> tuple[int | None, str, list[dict[str, Any]], str]:
    """Execute a grounded committee formula and fill only missing operands."""
    if event.event_type != EventType.COMMITTEE.value:
        return None, "", [], "현재 변수별 계산은 위원회 산식에만 적용됩니다."
    roles = committee_formula_roles(formula_candidate)
    if not roles:
        return None, "", [], "선택 산식을 안전한 변수 역할로 구조화하지 못했습니다."
    if has_time_varying_operands(formula_candidate):
        return (
            None,
            formula_candidate.formula,
            [],
            "선례의 변수값이 연도별로 달라 단일 연간 산식으로 반복할 수 없습니다.",
        )

    hitl, hitl_error = _validated_hitl_values(user_inputs)
    if hitl_error:
        return None, formula_candidate.formula, [], hitl_error

    chosen: dict[str, VariableOption] = {}
    reasons: list[str] = []
    for role in roles:
        if role == "count":
            if "paid_members" in hitl:
                option = _hitl_option(
                    "count",
                    hitl["paid_members"],
                    name="사용자 확정 순증 수당 지급대상 인원",
                    unit="명",
                    basis=(
                        "사용자가 대상 조문의 총 인원이 아니라 기존 제도 대비 "
                        "순증 지급대상 인원을 직접 확정"
                    ),
                )
                chosen[role] = option
                reasons.append(
                    "수당 지급대상 인원은 사용자가 확정한 순증 인원을 사용"
                )
                continue
            document_option = _document_count_option(event, source_text)
            if document_option is not None:
                incremental_option = _incremental_document_count_option(
                    document_option,
                    formula_candidate,
                    source_text,
                )
                if incremental_option is not None:
                    document_option = incremental_option
                elif "incumbent_paid_members" in hitl:
                    baseline = hitl["incumbent_paid_members"]
                    net_count = document_option.canonical_value - baseline
                    if net_count <= 0:
                        return (
                            None,
                            formula_candidate.formula,
                            [],
                            "기존 지급대상 인원은 조문상 신규 지급 가능 인원보다 "
                            "작아야 합니다.",
                        )
                    document_option = _hitl_option(
                        "count",
                        net_count,
                        name="기존 제도 대비 순증 수당 지급대상 인원",
                        unit="명",
                        basis=(
                            f"{document_option.source_text}; 조문상 지급 가능 "
                            f"{document_option.canonical_value:g}명 - 사용자 확인 "
                            f"기존 지급대상 {baseline:g}명 = 순증 {net_count:g}명"
                        ),
                    )
                chosen[role] = document_option
                reasons.append(
                    "수당 지급대상 인원은 선례가 아니라 대상 조문에서 계산한 "
                    f"{document_option.name} {int(document_option.canonical_value)}명을 사용"
                )
                continue
            if "incumbent_paid_members" in hitl:
                return (
                    None,
                    formula_candidate.formula,
                    [],
                    "기존 지급대상 인원은 입력됐지만 대상 조문에서 신규 지급 "
                    "가능 총인원을 계산하지 못했습니다. 순증 지급대상 인원을 "
                    "직접 입력해야 합니다.",
                )
        hitl_key = {
            "unit_price": "unit_price_won",
            "frequency": "annual_meetings",
        }.get(role)
        if hitl_key and hitl_key in hitl:
            option = _hitl_option(
                role,
                hitl[hitl_key],
                name=_ROLE_LABEL[role],
                unit="원/명·회" if role == "unit_price" else "회/년",
                basis="DB에 없는 대상 기관의 실제 기준값을 사용자가 확인",
            )
            chosen[role] = option
            reasons.append(
                f"{_ROLE_LABEL[role]}은 사용자가 확인한 실제 기준값을 사용"
            )
            continue
        # Committee multiplication operands are independently observable:
        # membership composition, meeting cadence and the government allowance
        # standard may each have a better official analogue than the formula
        # precedent.  Compare every role against the wider evidence pool while
        # still giving the coherent formula case explicit provenance.
        role_candidates = (
            candidates_by_role.get(role, candidates)
            if candidates_by_role
            else candidates
        )
        role_candidates = [
            formula_candidate,
            *[
                candidate
                for candidate in role_candidates
                if candidate.item_id != formula_candidate.item_id
            ],
        ]
        same_formula_pool = _compatible_options(
            event,
            source_text,
            role,
            [formula_candidate],
        )
        if (
            formula_selection_confidence >= 0.8
            and len(same_formula_pool) == 1
        ):
            # A complete case selected without seeing its numbers is one
            # coherent empirical bundle.  Reopening every operand against a
            # different bill can create a synthetic formula that never
            # existed and can accidentally fit the target total.  Preserve
            # the selected case; cross-case operand search remains only for
            # incomplete or weakly selected formulas.
            option = same_formula_pool[0]
            chosen[role] = option
            reasons.append(
                f"{_ROLE_LABEL[role]}은 후보 수치를 숨긴 구조 평가에서 "
                f"선택된 완결 선례 {option.bill_no}의 동일 산식 묶음을 유지"
            )
            continue
        if (
            role == "count"
            and len(same_formula_pool) == 1
            and _is_direct_complete_paid_count(same_formula_pool[0])
        ):
            # Once the formula case has passed formula/atomic-scope selection,
            # its directly observed payable-member operand is the coherent
            # default.  The legal-cap and composition gates above still reject
            # an incompatible count.  This also prevents a generative
            # reranker from making basic cap-arithmetic errors.
            option = same_formula_pool[0]
            chosen[role] = option
            reasons.append(
                f"{_ROLE_LABEL[role]}은 선택 산식 선례 {option.bill_no}의 "
                "직접 관측값을 사용(대상 조문의 정원·구성 조건 통과)"
            )
            continue
        if (
            role in {"unit_price", "frequency"}
            and len(same_formula_pool) == 1
            and formula_candidate.similarity >= 0.60
            and _shares_supervising_authority(
                event,
                source_text,
                formula_candidate,
            )
        ):
            option = same_formula_pool[0]
            chosen[role] = option
            reasons.append(
                f"{_ROLE_LABEL[role]}은 산식과 같은 감독기관의 직접 관측 선례 "
                f"{option.bill_no} 값을 사용"
            )
            continue
        pool = _compatible_options(
            event,
            source_text,
            role,
            role_candidates,
        )
        if role == "unit_price" and len(same_formula_pool) == 1:
            scope = _allowance_scope(same_formula_pool[0])
            guideline_option = (
                _latest_general_budget_guideline_option(
                    pool,
                    required_scope=scope,
                )
                if scope != "unknown"
                else None
            )
            if guideline_option is not None:
                chosen[role] = guideline_option
                reasons.append(
                    f"{_ROLE_LABEL[role]}은 선택 산식과 같은 비용 구성({scope}) "
                    f"안에서 {guideline_option.bill_no}의 최신 일반 예산지침 "
                    "근거를 사용"
                )
                continue
        option, reason, _, _ = _select_cross_case_option(
            event,
            source_text,
            formula_candidate,
            role,
            pool,
            refresh=refresh,
        )
        if option is None:
            # An unavailable semantic channel must not silently turn the
            # selected formula precedent into evidence for every operand.
            return (
                None,
                formula_candidate.formula,
                [
                    {
                        "name": _ROLE_LABEL[key],
                        "role": key,
                        "value": value.canonical_value,
                        "unit": value.canonical_unit,
                        "source": value.item_id,
                        "source_text": value.source_text,
                    }
                    for key, value in chosen.items()
                ],
                f"{_ROLE_LABEL[role]}: {reason}",
            )
        chosen[role] = option
        origin = "같은 산식 선례" if option.item_id == formula_candidate.item_id else "별도 유사 선례"
        reasons.append(f"{_ROLE_LABEL[role]}은 {origin} {option.bill_no} 선택: {reason}")

    amount_thousand = int(
        round(
            chosen["count"].canonical_value
            * chosen["unit_price"].canonical_value
            * chosen["frequency"].canonical_value
            / 1000
        )
    )
    evidence = [
        {
            "name": _ROLE_LABEL[role],
            "role": role,
            "value": chosen[role].canonical_value,
            "unit": chosen[role].canonical_unit,
            "raw_value": chosen[role].raw_value,
            "raw_unit": chosen[role].raw_unit,
            "source": chosen[role].item_id,
            "source_bill_no": chosen[role].bill_no,
            "source_text": chosen[role].source_text,
        }
        for role in roles
    ]
    return (
        amount_thousand,
        "수당 지급대상 인원 × 1인 1회당 수당 × 연간 회의 횟수",
        evidence,
        " / ".join(reasons),
    )
