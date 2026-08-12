"""위원회 조문 그룹핑 게이트 (기계적, LLM 미사용).

법안 표준 패턴: 제7조에서 설치, 제8조에서 구성, 제9조에서 운영을 규정하는
식으로 같은 위원회가 여러 조문에 분산되는 게 흔하다. 조문 단위로 세면 위원회
1개가 여러 개로 잡혀 비용이 중복 계산된다 — 실측 확인: 2213848의 제8조
(재생에너지자립단지추진지원단, 위원회 아님)가 본문에 "위원회"라는 단어가
포함돼있다는 이유만으로 위원회로 오분류됐었다. 그래서 조문이 아니라
"위원회 개체" 단위로 집계한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COMMITTEE_GATE_VERSION = "committee-gate-v3"

# 실측 버그: 공백이 제거된 PDF 텍스트에서 상한을 크게 두면(예: 30자) "위원회"
# 앞의 조사·동사구·부처명까지 통째로 이름으로 삼켜버린다(예:
# "...하기위하여국무총리소속으로테스트위원회" 전체가 캡처됨). 상한을 줄여도
# (16자) 우연히 그 길이 안에 걸리면 여전히 재현된다 — 근본 원인은 상한이 아니라
# 문장 경계 마커가 없다는 것. 그래서 상한은 넉넉히 두되(부처명+이름 조합이 길 수
# 있음), "소속으로/위하여/이하/한다/둔다" 같은 경계표현이 캡처 안에 있으면
# 그 마지막 경계표현 뒤만 진짜 이름으로 남기는 후처리(_clean_name)를 더한다.
_COMMITTEE_NAME_RE = re.compile(r"([가-힣A-Za-z0-9·ㆍ]{2,80}?(?:위원회|심의회|협의회))")
_NAME_BOUNDARY_RE = re.compile(
    r"소속으로|소속하에|두기위하여|위하여|위해|이하|라한다|한다|된다|둔다|"
    r"관하여|대하여|설치하여|심의하기위하여|구성된"
)
_LEADING_MEMBERSHIP_MODIFIER_RE = re.compile(
    r"^(?:위원장\d+(?:명|인)(?:을|를)?포함(?:한|하여)?)?"
    r"\d+(?:명|인)(?:이내)?의?"
)
_BARE_SUFFIX = {"위원회", "심의회", "협의회"}

# 개체 생성은 아무 "위원회" 문자열이 아니라 법령의 구조적 신호에서만 한다.
_TITLE_NAME_RE = re.compile(
    r"([가-힣A-Za-z0-9·ㆍ]{2,80}?(?:위원회|심의회|협의회))"
    r"(?=의?(?:설치|구성|운영|기능|회의|권한|직무|활동|정원|위원|등|사무|$))"
)
_ALIAS_DEFINITION_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9·ㆍ]{2,80}?(?:위원회|심의회|협의회))"
    r"\(이하[“”\"']?(?P<alias>[가-힣A-Za-z0-9·ㆍ]{1,30})[“”\"']?라한다\)"
)
_DIRECT_ESTABLISH_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9·ㆍ]{2,80}?(?:위원회|심의회|협의회))"
    r"(?:\(이하[^)]{1,50}\))?(?:을|를)?(?:둔다|설치한다|설치하여야한다|구성한다)"
)


def _clean_name(raw: str) -> str | None:
    """경계표현 뒤만 진짜 위원회 이름으로 남긴다. 수식어 없이 접미사만 남으면
    "(이하 "위원회"라 한다)" 같은 약칭 정의일 뿐 새 이름이 아니므로 버린다."""
    matches = list(_NAME_BOUNDARY_RE.finditer(raw))
    if matches:
        raw = raw[matches[-1].end():]
    # `외부전문가로 구성된 15인 이내의 법관평가위원회`의 `15인 이내의`는
    # 개체명이 아니라 구성 상한이다. `10·29이태원...위원회`처럼 실제
    # 이름의 선행 숫자는 `명/인`이 뒤따르지 않으므로 제거되지 않는다.
    raw = _LEADING_MEMBERSHIP_MODIFIER_RE.sub("", raw)
    if raw in _BARE_SUFFIX:
        return None
    return raw


def _title_name(title: str) -> str | None:
    """조문 제목의 정식 개체명을 반환한다.

    '위원회의 구성'처럼 수식어가 없는 범용 제목은 약칭이므로 새 개체를
    만들지 않는다.
    """
    compact = re.sub(r"\s+", "", title or "")
    match = _TITLE_NAME_RE.search(compact)
    if not match:
        return None
    candidate = _clean_name(match.group(1))
    if not candidate or candidate in _BARE_SUFFIX:
        return None
    return candidate


def _definition_candidates(compact: str) -> tuple[list[str], dict[str, set[str]]]:
    """약칭 정의와 설치 동사에 문법적으로 붙은 이름만 후보로 만든다."""
    names: list[str] = []
    aliases: dict[str, set[str]] = {}
    for match in _ALIAS_DEFINITION_RE.finditer(compact):
        name = _clean_name(match.group("name"))
        if not name or len(_COMMITTEE_NAME_RE.findall(name)) != 1:
            continue
        names.append(name)
        alias = str(match.group("alias") or "")
        if alias:
            aliases.setdefault(_normalize_name(name), set()).add(alias)
    for match in _DIRECT_ESTABLISH_RE.finditer(compact):
        name = _clean_name(match.group("name"))
        if name and len(_COMMITTEE_NAME_RE.findall(name)) == 1:
            names.append(name)
    return list(dict.fromkeys(names)), aliases

# L1: 신설 여부 — "둔다/설치한다/구성한다"가 있어야 진짜 신설 조문
_ESTABLISH_RE = re.compile(r"둔다|설치한다|설치하여야|구성한다|설치[·ㆍ]운영")
# 참조형 — "~의 심의를 거쳐"처럼 이미 있는 위원회를 언급만 하는 조문(신설 아님)
_REFERENCE_RE = re.compile(
    r"심의를\s*거쳐|의결을\s*거쳐|자문을\s*받아|협의하여|심의[·ㆍ]의결한|보고하여야|요청할\s*수"
)
# 이관형 — 위원회를 다른 소속으로 옮기거나 명칭만 바꾸는 조문(신설 아님)
_TRANSFER_RE = re.compile(r"소속으로\s*한다|이관한다|명칭을\s*.*으로\s*한다")
# L2: 의무/재량
_DISCRETIONARY_RE = re.compile(r"둘\s*수\s*있다|설치할\s*수\s*있다")

# L3: 유형 신호 — committee_type_gate.py와 동일 패턴(여러 조문에 흩어질 수 있어 병합 필요)
TYPE1_RE = re.compile(r"중앙행정기관으로\s*(본다|보아|간주)")
# 실측(2124118, 공익법인위원회): "사무기구를 둔다"는 사무국/사무처와 같은
# 뜻인데 놓쳐서 TYPE_2(사무국형)로 분류가 안 됐고, 그 결과 사무국 인건비
# (실제 13명 신규채용, 연 13.4억원)를 통째로 계산에서 빠뜨렸다.
TYPE2_RE = re.compile(r"사무(?:국|처|기구)(?:을|를)?\s*(?:둔다|설치)")


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+|·|ㆍ", "", name)


def _is_discretionary_for(
    entity_name: str,
    aliases: set[str],
    compact_text: str,
) -> bool:
    """재량 동사가 해당 개체에 직접 붙었을 때만 True다.

    '자문위원회는 분과위원회를 둘 수 있다'를 자문위원회 자체의
    재량 설치로 잘못 전파하는 버그를 막는다.
    """
    names = {_normalize_name(entity_name)} | {
        _normalize_name(alias) for alias in aliases
    }
    for name in sorted((value for value in names if value), key=len, reverse=True):
        if re.search(
            rf"{re.escape(name)}(?:을|를)?(?:둘수있다|설치할수있다)",
            _normalize_name(compact_text),
        ):
            return True
    return False


@dataclass
class CommitteeEntity:
    name: str
    article_nos: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    committee_type: str | None = None
    discretionary: bool = False

    @property
    def combined_text(self) -> str:
        return "\n".join(self.texts)


def group_committee_articles(articles: list[dict]) -> list[CommitteeEntity]:
    """조문 리스트(articles: [{no,title,text}, ...])를 위원회 개체 단위로 묶는다.

    참조형/이관형 조문만으로는 개체를 새로 만들지 않는다 — 신설 근거가 없기
    때문이다(예: "OO위원회의 심의를 거쳐"만 있고 그 위원회를 신설하는 조문이
    이 법안 안에 없으면, 그건 이미 존재하는 다른 법의 위원회를 참조한 것뿐).
    """
    prepared: list[dict] = []
    found: dict[str, CommitteeEntity] = {}
    aliases_by_key: dict[str, set[str]] = {}

    # 1차: 조문 제목, "이하 ...라 한다" 정의, 설치 동사에 붙은 정식명으로만
    # 개체를 만든다. 본문의 모든 "위원회" 표현을 후보로 삼지 않는 것이 핵심이다.
    for article in articles:
        title = str(article.get("title", ""))
        body = f"{title} {article.get('text', '')}"
        compact = re.sub(r"\s+", "", body)
        is_establish = bool(_ESTABLISH_RE.search(compact))
        is_reference_only = bool(_REFERENCE_RE.search(compact)) and not is_establish
        is_transfer = bool(_TRANSFER_RE.search(compact))
        definition_names, definition_aliases = _definition_candidates(compact)
        title_candidate = _title_name(title)

        candidates = list(definition_names)
        title_key = _normalize_name(title_candidate or "")
        title_is_defined_alias = bool(
            title_key
            and any(
                title_key in {_normalize_name(alias) for alias in aliases}
                for aliases in definition_aliases.values()
            )
        )

        # 제목의 정식명은 본문의 약칭보다 우선한다. 단, 정말 다른 개체를
        # 한 조문에서 동시에 설치하는 경우는 유지한다.
        if title_candidate and not title_is_defined_alias:
            inherited_aliases: set[str] = set()
            for definition_key, aliases in definition_aliases.items():
                if definition_key in title_key or title_key in definition_key:
                    inherited_aliases.update(aliases)
            if inherited_aliases:
                definition_aliases.setdefault(title_key, set()).update(inherited_aliases)
            candidates = [
                name
                for name in candidates
                if _normalize_name(name) not in title_key
                and title_key not in _normalize_name(name)
            ]
            candidates.insert(0, title_candidate)
        elif not candidates and title_candidate:
            candidates.append(title_candidate)

        referenced_existing: set[str] = set()
        for existing_key in found:
            if existing_key in _normalize_name(compact):
                referenced_existing.add(existing_key)
                continue
            if any(
                _normalize_name(alias) in _normalize_name(compact)
                for alias in aliases_by_key.get(existing_key, set())
                if len(_normalize_name(alias)) >= 3
            ):
                referenced_existing.add(existing_key)
        # "A위원회와 B위원회(이하 '지역위원회')"는 두 개체의 집합 약칭이지
        # 세 번째 위원회가 아니다.
        if title_candidate and len(referenced_existing) >= 2 and not definition_names:
            candidates = [name for name in candidates if _normalize_name(name) != title_key]

        prepared.append({
            "article": article,
            "body": body,
            "compact": compact,
            "is_establish": is_establish,
            "is_reference_only": is_reference_only,
            "is_transfer": is_transfer,
            "declared_keys": {
                _normalize_name(name) for name in candidates if name
            },
        })

        if not is_establish or is_reference_only or is_transfer:
            continue
        for raw_name in dict.fromkeys(candidates):
            key = _normalize_name(raw_name)
            if not key or raw_name in _BARE_SUFFIX:
                continue
            canonical_key = next(
                (
                    existing_key
                    for existing_key, aliases in aliases_by_key.items()
                    if key in {_normalize_name(alias) for alias in aliases}
                ),
                None,
            )
            if canonical_key:
                aliases_by_key.setdefault(canonical_key, set()).update(
                    definition_aliases.get(key, set())
                )
                continue
            found.setdefault(key, CommitteeEntity(name=raw_name))
            aliases_by_key.setdefault(key, set()).update(definition_aliases.get(key, set()))

    # 2차: 이미 만든 개체에 구성·운영 조문을 붙인다. 정식명이나 고유 약칭이
    # 실제로 등장하는 경우만 붙이며, '위원회' 같은 범용 약칭은 개체가 하나일 때만 쓴다.
    alias_to_key = {
        _normalize_name(alias): key
        for key, aliases in aliases_by_key.items()
        for alias in aliases
        if _normalize_name(alias)
    }
    for row in prepared:
        compact = row["compact"]
        normalized_compact = _normalize_name(compact)
        matched_keys: list[str] = []
        declared_keys = {
            alias_to_key.get(key, key)
            for key in (row.get("declared_keys") or set())
        }
        for key, entity in found.items():
            if declared_keys:
                if key in declared_keys:
                    matched_keys.append(key)
                continue
            if key in normalized_compact:
                matched_keys.append(key)
                continue
            unique_aliases = {
                _normalize_name(alias)
                for alias in aliases_by_key.get(key, set())
                if alias not in _BARE_SUFFIX and len(_normalize_name(alias)) >= 3
            }
            if any(alias in normalized_compact for alias in unique_aliases):
                matched_keys.append(key)
        if not matched_keys and len(found) == 1:
            only_key = next(iter(found))
            generic_aliases = aliases_by_key.get(only_key, set()) & _BARE_SUFFIX
            compact_title = re.sub(r"\s+", "", str(row["article"].get("title", "")))
            if any(compact_title.startswith(alias) for alias in generic_aliases):
                matched_keys = [only_key]

        for key in dict.fromkeys(matched_keys):
            entity = found[key]
            article_no = str(row["article"].get("no", ""))
            if article_no not in entity.article_nos:
                entity.article_nos.append(article_no)
                entity.texts.append(row["body"])
            if row["is_establish"]:
                entity.discretionary = entity.discretionary or _is_discretionary_for(
                    entity.name,
                    aliases_by_key.get(key, set()),
                    compact,
                )
            if TYPE1_RE.search(compact):
                entity.committee_type = "type1_central_agency"
            elif TYPE2_RE.search(compact) and entity.committee_type != "type1_central_agency":
                entity.committee_type = "type2_secretariat"

    return list(found.values())
