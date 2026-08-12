"""Independent committee-cost estimation channel.

The general analyzer intentionally remains untouched.  This module owns a
smaller contract:

1. Detect and group committee articles by the named legal entity.
2. Retrieve comparable official estimates through hybrid RAG.
3. Retrieve structured operands from TAG and rank them with the RAG result.
4. Calculate only the committee meeting allowance formula in Python.
5. Pause on target-specific or unsupported values instead of inventing them.

TAG and RAG are evidence providers, not calculators.  Every numeric output is
reproducible from the operands returned in ``variables``.
"""
from __future__ import annotations

import base64
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .assembly_assumptions import find_assumption_candidates
from .committee_evidence_adapter import (
    apply_evidence_workflow,
    build_evidence_workflow,
)
from .config import GENERATED_DIR, PROJECT_ROOT
from .analyzer_v2 import (
    _detect_doc_type,
    _extract_bill_name,
    _extract_committee_total_members,
    _extract_current_bill_no,
    _extract_pdf_text_from_bytes,
    _gemini_raw_json,
    _strip_data_url,
    split_articles_from_revision_table_pdf,
    split_articles_regex,
    split_articles_structured,
    strip_appendices,
    try_embed,
    vector_search,
)


COMMITTEE_TERMS = ("위원회", "심의회", "협의회")
COMMITTEE_RE = re.compile(r"[\uac00-\ud7a3A-Za-z0-9·ㆍ]{2,45}(?:위원회|심의회|협의회)")
GENERIC_COMMITTEE_TITLE_RE = re.compile(
    r"^(?:(?:그밖에|그밖의|해당|이하|각|본|이|동))?"
    r"(?:위원회|심의회|협의회)(?:의)?"
    r"(?:구성|조직|운영|회의|위원장|간사|분과|소위위원회|구성및운영)?$"
)
OUT_OF_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("사무처·사무국 및 인력", re.compile(r"사무처|사무국|지원단|전담인력|직원|인건비")),
    ("상임위원·상근직 인건비", re.compile(r"상임위원|상근위원|월급|보수|봉급|인건비")),
    ("조사·연구·계획 비용", re.compile(r"실태조사|연구용역|기본계획|종합계획")),
    ("지원사업·보조금", re.compile(r"지원사업|지원금|보조금|장려금|급여|지급")),
)

VARIABLE_META = {
    "meeting_count": {"label": "연간 회의 횟수", "unit": "회/연"},
    "paid_members": {"label": "수당 지급가능 인원", "unit": "명"},
    "allowance_won": {"label": "회의당 수당 단가", "unit": "원/명·회"},
}
OPTIONAL_VARIABLE_META = {
    "incumbent_paid_members": {
        "label": "기존 제도 지급대상 인원(선택)",
        "unit": "명",
    },
}
ALL_VARIABLE_META = {**VARIABLE_META, **OPTIONAL_VARIABLE_META}

FORMULA_FAMILY_META = {
    "meeting_allowance": {
        "label": "회의참석수당",
        "formula": "연간 회의 횟수 × 수당지급대상 인원 × 회의참석수당 단가",
        "supported": True,
    },
    "meeting_and_review_allowance": {
        "label": "회의참석·안건검토수당",
        "formula": "연간 회의 횟수 × 수당지급대상 인원 × 회당 참석·안건검토수당 합계",
        "supported": True,
    },
    "multi_body_meeting_allowance": {
        "label": "복수·지역 위원회 회의수당",
        "formula": "위원회 수 × 연간 회의 횟수 × 수당지급대상 인원 × 회당 수당",
        "supported": False,
    },
    "main_and_subcommittee": {
        "label": "본위원회·분과위원회 회의수당",
        "formula": "본위원회 비용 + 분과위원회별 회의 비용",
        "supported": False,
    },
    "meeting_and_advisory": {
        "label": "회의·자문 비용",
        "formula": "회의참석수당 + 별도 자문료",
        "supported": False,
    },
    "staff_and_operations": {
        "label": "상근인력·사무조직 포함 운영비",
        "formula": "인건비 + 기본경비 + 자산취득비 + 회의 운영비",
        "supported": False,
    },
    "flat_operating_cost": {
        "label": "유사기관 연간 운영비 준용",
        "formula": "유사 위원회 연간 운영비",
        "supported": False,
    },
    "other": {
        "label": "기타 위원회 산식",
        "formula": "구조 확인 필요",
        "supported": False,
    },
}

_GENERIC_SEARCH_TERMS = {
    "법률안", "법안", "일부개정", "전부개정", "위원회", "심의회", "협의회",
    "설치", "운영", "회의", "수당", "지급", "신설", "구성", "개정안",
}

_PUBLIC_AUTHORITIES = tuple(sorted({
    "기획재정부", "교육부", "과학기술정보통신부", "외교부", "통일부",
    "법무부", "국방부", "행정안전부", "국가보훈부", "문화체육관광부",
    "농림축산식품부", "산업통상자원부", "보건복지부", "환경부", "기후에너지환경부",
    "고용노동부", "여성가족부", "성평등가족부", "국토교통부", "해양수산부",
    "중소벤처기업부", "식품의약품안전처", "인사혁신처", "법제처", "질병관리청",
    "경찰청", "소방청", "산림청", "특허청", "조달청", "통계청", "국가통계처",
}, key=len, reverse=True))

_COMMITTEE_GROUPING_PROMPT = """아래는 한국 법률안의 조문 목록이다.
이 법률안이 실제로 신설·폐지하거나 구성을 변경하는 공공 위원회·심의회·협의회를 찾아라.
또한 기존 공공 위원회에 새로운 의무적 심의·의결 절차를 부과하여 업무량이 늘어나는 조문도 포함하라.

판단 규칙:
- 위원회명은 원문에 있는 법적 명칭을 그대로 쓴다.
- '위원회', '해당 위원회' 같은 약칭은 이름으로 보지 않는다.
- 다른 기관이나 다른 법률의 위원회를 단순히 언급한 것은 제외한다.
- '위원회의 심의를 거쳐야 한다'처럼 새 절차의 필수 조건으로 삼은 것은 단순 언급이 아니다.
- 민간 협회·중앙회·법인의 내부 위원회는 제외한다.
- 설치 조문과 이후의 구성·기능·회의 조문이 같은 위원회를 '위원회'라고 줄여 부르면 하나의 그룹으로 묶는다.
- definition_evidence는 설치·신설·구성·변경·폐지 또는 새로운 필수 심의·의결 의무의 근거 문구를 원문에서 그대로 복사한다.
- 숫자나 비용을 추정하지 마라.

JSON만 반환한다.
{{
  "groups": [
    {{
      "entity": "법적 위원회명",
      "definition_article": "제N조",
      "definition_evidence": "원문에서 복사한 설치·변경 근거",
      "related_articles": ["제N조", "제N+1조"]
    }}
  ]
}}

[조문]
{articles}
"""


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _normalise_date(value: Any) -> str | None:
    match = re.search(r"(20\d{2})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", str(value or ""))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _extract_propose_date(text: str) -> str | None:
    match = re.search(
        r"발\s*의\s*연\s*월\s*일\s*[:：]?\s*"
        r"(20\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2})",
        str(text or "")[:3000],
    )
    return _normalise_date(match.group(1)) if match else None


def _article_title(article_no: str) -> str:
    match = re.search(r"\(([^)\n]{1,100})\)", article_no or "")
    return match.group(1).strip() if match else ""


def _article_number_key(value: str) -> str:
    match = re.search(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", str(value or ""))
    if not match:
        return _compact(value)
    return f"제{match.group(1)}조" + (f"의{match.group(2)}" if match.group(2) else "")


def _extract_explicit_amendment_articles(text: str) -> list[dict[str, Any]]:
    """Recover full article blocks declared by an amendment command.

    Some PDFs break ``다음과 같이`` across lines. The shared structured
    parser can then start at a later consequential amendment and silently
    omit the newly inserted article. This fallback is deterministic: it only
    accepts an explicit ``제N조를/을 ... 신설·개정한다`` declaration and the
    matching article header that follows it.
    """
    command_re = re.compile(
        r"(?P<no>제\s*\d+\s*조(?:\s*의\s*\d+)?)\s*[을를]\s*"
        r"다음\s*과\s*같이\s*(?P<action>신설|개정)한다\."
    )
    boundary_re = re.compile(
        r"(?m)^\s*(?:부\s*칙\s*$|"
        r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?\s*"
        r"(?:중|각\s*호|[을를]\s*다음\s*과\s*같이))"
    )
    rows: list[dict[str, Any]] = []
    for command in command_re.finditer(text or ""):
        key = _article_number_key(command.group("no"))
        numbers = re.fullmatch(r"제(\d+)조(?:의(\d+))?", key)
        if not numbers:
            continue
        suffix = rf"\s*의\s*{numbers.group(2)}" if numbers.group(2) else ""
        header_re = re.compile(
            rf"(?m)^\s*(제\s*{numbers.group(1)}\s*조{suffix})"
            rf"(?:\s*\(([^)\n]{{1,120}})\))?"
        )
        header = header_re.search(text, command.end())
        if not header:
            continue
        boundary = boundary_re.search(text, header.end())
        end = boundary.start() if boundary else len(text)
        block = re.sub(r"\s+", " ", text[header.start():end]).strip()
        if len(block) < 20:
            continue
        title = str(header.group(2) or "").strip()
        rows.append({
            "no": f"{key}({title})" if title else key,
            "text": block[:3000],
            "change_type": "신설" if command.group("action") == "신설" else "개정",
            "source": "explicit_amendment_body",
        })
    return rows


def _merge_article_sources(
    primary: list[dict[str, Any]],
    recovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(row) for row in primary]
    index_by_no = {
        _article_number_key(str(row.get("no") or "")): index
        for index, row in enumerate(merged)
    }
    for row in recovered:
        key = _article_number_key(str(row.get("no") or ""))
        current_index = index_by_no.get(key)
        if current_index is None:
            index_by_no[key] = len(merged)
            merged.append(dict(row))
            continue
        current = merged[current_index]
        if len(str(row.get("text") or "")) > len(str(current.get("text") or "")):
            merged[current_index] = dict(row)
    return merged


def _entity_from_title(title: str) -> str | None:
    compact = _compact(title)
    matches = COMMITTEE_RE.findall(compact)
    if not matches:
        return None
    candidates = [value for value in matches if not GENERIC_COMMITTEE_TITLE_RE.match(value)]
    if not candidates:
        return None
    # Article titles are concise; the longest match preserves the legal name.
    return max(candidates, key=len)


def _entity_from_body(text: str) -> str | None:
    compact = _compact(text)
    matches = COMMITTEE_RE.findall(compact)
    cleaned: list[str] = []
    for value in matches:
        # Body regexes can capture a preceding grammatical phrase.  Keep the
        # suffix after common legal connectors.
        for connector in (
            "심의하기위하여",
            "두기위하여",
            "소속으로",
            "에두는",
            "두는",
            "에따른",
            "때에는",
            "위하여",
        ):
            if connector in value:
                value = value.split(connector)[-1]
        unnamed_body = bool(re.search(
            r"(?:각각)?(?:에)?소속되는(?:위원회|심의회|협의회)$",
            value,
        ))
        if (
            2 < len(value) <= 45
            and not unnamed_body
            and not GENERIC_COMMITTEE_TITLE_RE.match(value)
        ):
            cleaned.append(value)
    return min(cleaned, key=len) if cleaned else None


def _contains_committee(value: str) -> bool:
    return any(term in _compact(value) for term in COMMITTEE_TERMS)


def _is_definition(article: dict[str, Any], entity: str) -> bool:
    """Accept structural changes or a newly mandatory committee workload.

    A bill can create cost without creating a new body: making an existing
    public committee's review a required step adds case volume.  We admit
    only explicit procedural predicates (for example ``심의를 거쳐``), not a
    loose occurrence of another committee's name.
    """
    compact = _compact(f"{article.get('no', '')} {article.get('text', '')}")
    entity_at = compact.find(entity)
    if entity_at < 0:
        return False
    after = compact[entity_at + len(entity):entity_at + len(entity) + 100]
    title = _compact(_article_title(str(article.get("no") or "")))
    structural_title = bool(re.search(r"설치|구성|운영|회의", title))
    structural_predicate = bool(re.search(
        r"를?둔다|를?둘수있다|두어야한다|설치|신설|구성한다|운영한다",
        after,
    ))
    before = compact[max(0, entity_at - 160):entity_at]
    explicit_public_owner = bool(re.search(
        r"(?:국가|정부|국무총리|대통령|[\uac00-\ud7a3]+장관|지방자치단체|"
        r"중앙행정기관|국회|법원)(?:의)?(?:소속(?:으로)?|에두는)$|"
        r"(?:정부|[\uac00-\ud7a3]+부|[\uac00-\ud7a3]+청|국회|법원|지방자치단체)에두는$",
        before,
    ))
    mandatory_workload = explicit_public_owner and bool(re.search(
        r"(?:의)?(?:심의|의결|자문)(?:를)?(?:거쳐|받아야)|"
        r"(?:에)?(?:심의|의결|자문)(?:를)?(?:요청|부의)하여야",
        after,
    ))
    return structural_title or structural_predicate or mandatory_workload


def _is_public_fiscal_committee(article: dict[str, Any], entity: str) -> bool:
    """Keep public-body committees; private association organs are out of scope."""
    compact = _compact(str(article.get("text") or ""))
    entity_at = compact.find(entity)
    if entity_at < 0:
        return True
    window = compact[max(0, entity_at - 140):entity_at + len(entity) + 50]
    private_owner = bool(re.search(r"중앙회|협회|조합|법인|민간단체", window))
    # A public actor merely approving or supervising an association does not
    # make the association's internal committee a public fiscal committee.
    # Require an ownership phrase ("장관 소속으로", "교육부에 둔다"), not
    # a loose occurrence of "장관" anywhere in the surrounding article.
    public_owner = bool(re.search(
        r"(?:국가|정부|국무총리|대통령|[가-힣]+장관|지방자치단체|중앙행정기관|국회|법원)"
        r"(?:의)?(?:소속(?:으로)?|에두는)|"
        r"(?:정부|[가-힣]+부|[가-힣]+청|국회|법원|지방자치단체)에두는",
        window,
    ))
    return not private_owner or public_owner


def _is_generic_structural_article(article: dict[str, Any]) -> bool:
    title = _compact(_article_title(str(article.get("no") or "")))
    text = _compact(str(article.get("text") or ""))
    if re.match(r"^(?:위원회|심의회|협의회)(?:의)?(?:구성|운영|회의|위원장|간사|분과)$", title):
        return True
    # An office/staff clause is not a new committee candidate.  It still
    # belongs to the nearest named committee so the channel can explicitly
    # route that cost outside the meeting-allowance scope.
    if re.match(r"^(?:사무처|사무국|사무기구|지원조직)$", title) and re.search(
        r"위원회|심의회|협의회", text
    ):
        return True
    return bool(re.search(
        r"(?:위원회|심의회|협의회)는.{0,80}(?:구성한다|회의를개최|위원을위촉)",
        text,
    ))


def _group_committee_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    named_at: dict[int, str] = {}
    for index, article in enumerate(articles):
        title = _article_title(str(article.get("no") or ""))
        entity = _entity_from_title(title) or _entity_from_body(str(article.get("text") or ""))
        if entity and _is_definition(article, entity) and _is_public_fiscal_committee(article, entity):
            named_at[index] = entity

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, article in enumerate(articles):
        combined = f"{article.get('no', '')} {article.get('text', '')}"
        if not _contains_committee(combined):
            continue
        entity = named_at.get(index)
        if not entity and _is_generic_structural_article(article):
            neighbours = [
                (abs(index - other_index), other_index, name)
                for other_index, name in named_at.items()
                if abs(index - other_index) <= 4
            ]
            if neighbours:
                _, _, entity = min(neighbours)
        if entity:
            grouped[entity].append(article)

    return [
        {"entity": entity, "articles": rows}
        for entity, rows in grouped.items()
    ]


def _committee_grouping_excerpt(articles: list[dict[str, Any]]) -> str:
    """Send only committee-bearing articles to the semantic extractor."""
    rows: list[str] = []
    for article in articles:
        combined = f"{article.get('no', '')} {article.get('text', '')}"
        if not _contains_committee(combined):
            continue
        rows.append(
            f"[{article.get('no', '')}]\n"
            f"{str(article.get('text') or '')[:2500]}"
        )
    return "\n\n".join(rows)[:30000]


def _ground_llm_committee_groups(
    articles: list[dict[str, Any]],
    raw_groups: list[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Accept LLM relationships only when their defining evidence is verbatim.

    The LLM resolves pronouns and legal relationships. This verifier retains
    ownership of existence: the entity, definition article and structural
    predicate must all be present in the supplied bill text.
    """
    indexed: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, article in enumerate(articles):
        key = _article_number_key(str(article.get("no") or ""))
        if key:
            indexed[key] = (index, article)

    grounded: list[dict[str, Any]] = []
    rejected: list[str] = []
    seen_entities: set[str] = set()
    structural_predicate = re.compile(
        r"둔다|둘수있다|설치|신설|구성한다|구성하도록|"
        r"증원|감원|폐지|삭제|"
        r"(?:심의|의결|자문)(?:를)?(?:거쳐|받아야|(?:요청|부의)하여야)"
    )

    for position, raw in enumerate(raw_groups, 1):
        if not isinstance(raw, dict):
            rejected.append(f"group-{position}: JSON object가 아님")
            continue
        entity = _compact(raw.get("entity"))
        definition_key = _article_number_key(str(raw.get("definition_article") or ""))
        evidence = _compact(raw.get("definition_evidence"))
        definition_entry = indexed.get(definition_key)
        if (
            not entity
            or not entity.endswith(COMMITTEE_TERMS)
            or GENERIC_COMMITTEE_TITLE_RE.match(entity)
        ):
            rejected.append(f"group-{position}: 유효한 법적 위원회명이 아님")
            continue
        if entity in seen_entities:
            rejected.append(f"{entity}: 중복 그룹")
            continue
        if not definition_entry:
            rejected.append(f"{entity}: 근거 조문이 원문에 없음")
            continue

        definition_index, definition_article = definition_entry
        definition_text = _compact(
            f"{definition_article.get('no', '')} {definition_article.get('text', '')}"
        )
        if entity not in definition_text:
            rejected.append(f"{entity}: 위원회명이 근거 조문에 없음")
            continue
        if len(evidence) < 8 or evidence not in definition_text:
            rejected.append(f"{entity}: 인용문이 원문과 일치하지 않음")
            continue
        if entity not in evidence or not structural_predicate.search(evidence):
            rejected.append(f"{entity}: 인용문에 설치·변경 근거가 없음")
            continue
        if not _is_public_fiscal_committee(definition_article, entity):
            rejected.append(f"{entity}: 민간단체 내부 위원회")
            continue

        requested_keys = {
            _article_number_key(str(value or ""))
            for value in (raw.get("related_articles") or [])
        }
        requested_keys.add(definition_key)
        grouped_articles: list[dict[str, Any]] = []
        for index, article in enumerate(articles):
            key = _article_number_key(str(article.get("no") or ""))
            if key not in requested_keys:
                continue
            combined = f"{article.get('no', '')} {article.get('text', '')}"
            # A related generic article is safe only near its defining clause.
            if index != definition_index and (
                abs(index - definition_index) > 4 or not _contains_committee(combined)
            ):
                continue
            grouped_articles.append(article)
        if not grouped_articles:
            rejected.append(f"{entity}: 묶을 조문이 없음")
            continue
        seen_entities.add(entity)
        grounded.append({"entity": entity, "articles": grouped_articles})
    return grounded, rejected


def _group_committee_articles_hybrid(
    articles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use semantic grouping first and deterministic rules as availability fallback."""
    excerpt = _committee_grouping_excerpt(articles)
    if not excerpt:
        return [], {"method": "no_committee_articles", "rejected": []}

    parsed = _gemini_raw_json(
        _COMMITTEE_GROUPING_PROMPT.format(articles=excerpt)
    )
    if parsed is None:
        fallback = _group_committee_articles(articles)
        return fallback, {
            "method": "rules_fallback_llm_unavailable",
            "acceptedGroupCount": len(fallback),
            "rejected": [],
        }

    if isinstance(parsed, dict):
        raw_groups = parsed.get("groups")
    elif isinstance(parsed, list):
        raw_groups = parsed
    else:
        raw_groups = None
    if not isinstance(raw_groups, list):
        fallback = _group_committee_articles(articles)
        return fallback, {
            "method": "rules_fallback_invalid_llm_json",
            "acceptedGroupCount": len(fallback),
            "rejected": ["LLM 응답에 groups 배열이 없음"],
        }

    grounded, rejected = _ground_llm_committee_groups(articles, raw_groups)
    return grounded, {
        "method": "llm_semantic_plus_grounding",
        "rawGroupCount": len(raw_groups),
        "acceptedGroupCount": len(grounded),
        "rejected": rejected,
    }


def _distinctive_tokens(text: str) -> set[str]:
    compact = re.sub(
        r"[^\uac00-\ud7a3A-Za-z0-9]",
        "",
        str(text or "").lower(),
    )
    for generic in _GENERIC_SEARCH_TERMS:
        compact = compact.replace(generic, "")
    if len(compact) < 3:
        return {compact} if compact else set()
    # Korean PDF extraction frequently removes or inserts word spaces. Char
    # trigrams preserve subject similarity across those layout differences.
    return {compact[index:index + 3] for index in range(len(compact) - 2)}


def _token_overlap(query: str, candidate: str) -> float:
    left = _distinctive_tokens(query)
    right = _distinctive_tokens(candidate)
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    containment = intersection / max(1, min(len(left), len(right)))
    jaccard = intersection / max(1, len(left | right))
    return 0.7 * containment + 0.3 * jaccard


def _tag_roots() -> list[Path]:
    candidates = [
        GENERATED_DIR / "assembly_rag_seed",
        GENERATED_DIR / "assembly_rag_seed_22_ce",
        PROJECT_ROOT / "backend" / "generated" / "assembly_rag_seed",
        PROJECT_ROOT / "backend" / "generated" / "assembly_rag_seed_22_ce",
    ]
    result: list[Path] = []
    for path in candidates:
        if path.exists() and path not in result:
            result.append(path)
    return result


def _formula_family(formula_text: str) -> str:
    """Normalize noisy TAG prose into a small, auditable formula family."""
    compact = _compact(formula_text)
    if not compact:
        return "other"
    if re.search(r"상임위원|인건비|봉급|보수|기본경비|자산취득|사무처|지원단", compact):
        return "staff_and_operations"
    if "자문" in compact and "회의" in compact:
        return "meeting_and_advisory"
    if re.search(r"분과위원회|소위원회|전문위원회", compact) and re.search(r"본회의|전체회의|본위원회|\+", compact):
        return "main_and_subcommittee"
    if re.search(r"시[·ㆍ]?도수|시[·ㆍ]?도개수|위원회수|위원회개수|위원회별", compact):
        return "multi_body_meeting_allowance"
    if "안건검토" in compact and re.search(r"참석수당|회의수당|회의참석", compact):
        return "meeting_and_review_allowance"
    if re.search(r"유사.*(?:운영비|운영예산)|평균.*운영|연간운영비|운영비용.*준용", compact):
        return "flat_operating_cost"
    has_meeting = bool(re.search(r"회의|참석", compact))
    has_people = bool(re.search(r"위원수|위원수|위원|인원|지급대상", compact))
    has_allowance = bool(re.search(r"수당|사례금|사례비", compact))
    if has_meeting and has_people and has_allowance:
        return "meeting_allowance"
    return "other"


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


@lru_cache(maxsize=1)
def _local_committee_formula_cases() -> list[dict[str, Any]]:
    """Join local TAG tables into deduplicated committee formula precedents."""
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in _tag_roots():
        structures = {
            str(row.get("struct_id") or ""): row
            for row in _read_jsonl_records(root / "cost_estimate_structures.jsonl")
            if row.get("struct_id")
        }
        items = {
            str(row.get("item_id") or ""): row
            for row in _read_jsonl_records(root / "cost_estimate_items.jsonl")
            if row.get("item_id")
            and (
                _contains_committee(str(row.get("item_name") or ""))
                or re.search(r"회의참석|위원수당", str(row.get("item_name") or ""))
            )
        }
        item_formula_index: Counter[str] = Counter()
        for amount in _read_jsonl_records(root / "cost_estimate_amounts.jsonl"):
            if amount.get("is_total"):
                continue
            item_id = str(amount.get("item_id") or "")
            item = items.get(item_id)
            if not item:
                continue
            formula_text = re.sub(r"\s+", " ", str(amount.get("formula_text") or "")).strip()
            if not formula_text:
                continue
            structure = structures.get(str(item.get("struct_id") or "")) or {}
            bill_no = str(structure.get("bill_no") or "")
            signature = (bill_no, str(item.get("item_name") or ""), _compact(formula_text))
            if not bill_no or signature in seen:
                continue
            seen.add(signature)
            family = _formula_family(formula_text)
            item_formula_index[item_id] += 1
            output.append({
                "candidate_id": f"{bill_no}:{item_id}:{item_formula_index[item_id]}",
                "bill_no": bill_no,
                "bill_name": structure.get("bill_name"),
                "propose_date": _normalise_date(structure.get("propose_date")),
                "item_name": item.get("item_name"),
                "trigger_ref": item.get("trigger_ref"),
                "formula_text": formula_text[:1000],
                "family": family,
                "family_label": FORMULA_FAMILY_META[family]["label"],
                "supported": bool(FORMULA_FAMILY_META[family]["supported"]),
            })
    return output


def _formula_candidates_for_cases(
    rag_cases: list[dict[str, Any]],
    *,
    limit: int = 15,
) -> list[dict[str, Any]]:
    ranks = {
        str(row.get("bill_no") or ""): index
        for index, row in enumerate(rag_cases, 1)
        if row.get("bill_no")
    }
    similarities = {
        str(row.get("bill_no") or ""): float(
            row.get("structural_score") or row.get("similarity") or 0
        )
        for row in rag_cases
    }
    rows = [
        {
            **candidate,
            "rag_rank": ranks[str(candidate["bill_no"])],
            "similarity": round(similarities.get(str(candidate["bill_no"]), 0.0), 4),
        }
        for candidate in _local_committee_formula_cases()
        if str(candidate.get("bill_no") or "") in ranks
    ]
    return sorted(
        rows,
        key=lambda row: (
            int(bool(row.get("supported"))),
            -int(row.get("rag_rank") or 9999),
            float(row.get("similarity") or 0),
        ),
        reverse=True,
    )[:limit]


def _select_formula_with_llm(
    *,
    group: dict[str, Any],
    article_text: str,
    change_type: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Let the LLM choose a grounded formula structure, never invent one."""
    supported = [row for row in candidates if row.get("supported")]
    unsupported_families = sorted({
        str(row.get("family_label") or row.get("family"))
        for row in candidates
        if not row.get("supported")
    })
    chosen: dict[str, Any] | None = None
    method = "deterministic_supported_fallback"
    reason = "현재 채널에서 계산 가능한 공식 회의수당 산식 중 검색 순위가 가장 높은 구조를 사용했습니다."
    confidence: float | None = None
    if supported:
        payload = {
            "target": {
                "committee": group.get("entity"),
                "change_type": change_type,
                "articles": article_text[:6500],
            },
            "candidates": [
                {
                    "candidate_id": row["candidate_id"],
                    "bill_no": row["bill_no"],
                    "bill_name": row.get("bill_name"),
                    "item_name": row.get("item_name"),
                    "family": row.get("family"),
                    "formula_text": row.get("formula_text"),
                    "similarity": row.get("similarity"),
                }
                for row in supported[:12]
            ],
        }
        prompt = """당신은 국회 위원회 비용추계의 산식 선정자다.
target 조문이 실제로 유발하는 회의비 구조와 candidates의 공식 산식을 비교하라.

규칙:
- 숫자값이 비슷한 후보가 아니라 비용 구성과 변수 역할이 가장 비슷한 산식을 고른다.
- target에 없는 상근인력, 사무조직, 지역위원회 수, 분과위원회, 자문비를 추가하지 않는다.
- 회의참석수당만 필요한지, 동일 회차에 참석수당과 안건검토수당을 함께 적용하는지가 핵심이다.
- candidates에 있는 candidate_id 하나만 선택할 수 있다.
- 적합한 후보가 없으면 candidate_id를 null로 둔다.
- 숫자나 새 산식을 만들지 않는다.

JSON 형식:
{"candidate_id": "... 또는 null", "confidence": 0.0, "reason": "선정 이유"}

입력:
""" + json.dumps(payload, ensure_ascii=False)
        parsed = _gemini_raw_json(prompt)
        if isinstance(parsed, dict):
            allowed = {str(row["candidate_id"]): row for row in supported}
            selected_id = str(parsed.get("candidate_id") or "")
            chosen = allowed.get(selected_id)
            if chosen:
                method = "llm_formula_rerank_plus_allowlist_grounding"
                reason = str(parsed.get("reason") or "")[:500]
                try:
                    confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
                except (TypeError, ValueError):
                    confidence = None
        if chosen is None:
            preferred_family = (
                "meeting_and_review_allowance"
                if re.search(r"안건검토|사전검토", _compact(article_text))
                else "meeting_allowance"
            )
            chosen = next(
                (row for row in supported if row.get("family") == preferred_family),
                supported[0],
            )
    family = str(chosen.get("family") if chosen else "meeting_allowance")
    meta = FORMULA_FAMILY_META[family]
    return {
        "family": family,
        "label": meta["label"],
        "formula": meta["formula"],
        "method": method if chosen else "default_supported_template",
        "reason": reason if chosen else "검색된 공식 산식이 없어 최소 회의수당 산식을 검토용 기본 구조로 사용했습니다.",
        "confidence": confidence,
        "source": (
            {
                "candidate_id": chosen.get("candidate_id"),
                "bill_no": chosen.get("bill_no"),
                "bill_name": chosen.get("bill_name"),
                "item_name": chosen.get("item_name"),
                "formula_text": chosen.get("formula_text"),
                "similarity": chosen.get("similarity"),
            }
            if chosen else None
        ),
        "candidate_count": len(candidates),
        "unsupported_candidates": unsupported_families,
    }


@lru_cache(maxsize=1)
def _local_committee_chunks() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _tag_roots():
        path = root / "chunks.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = str(row.get("text") or row.get("content") or "")
                if not _contains_committee(text):
                    continue
                doc_type = str(row.get("documentType") or row.get("document_type") or "")
                if doc_type and doc_type != "cost_estimate":
                    continue
                chunk_id = str(row.get("chunkId") or row.get("chunk_id") or "")
                if not chunk_id or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                rows.append({
                    "chunk_id": chunk_id,
                    "bill_id": row.get("billId") or row.get("bill_id"),
                    "bill_no": row.get("billNo") or row.get("bill_no"),
                    "bill_name": row.get("billName") or row.get("bill_name"),
                    "propose_date": _normalise_date(
                        row.get("proposeDate") or row.get("propose_date")
                    ),
                    "content": text,
                })
    return rows


def _local_lexical_search(
    query: str,
    *,
    exclude_bill_nos: set[str],
    cutoff_date: str | None = None,
    k: int = 8,
) -> list[dict[str, Any]]:
    best_by_bill: dict[str, dict[str, Any]] = {}
    for row in _local_committee_chunks():
        bill_no = str(row.get("bill_no") or "")
        if not bill_no or bill_no in exclude_bill_nos:
            continue
        propose_date = _normalise_date(row.get("propose_date"))
        if cutoff_date and (not propose_date or propose_date > cutoff_date):
            continue
        candidate_text = f"{row.get('bill_name', '')} {row.get('content', '')[:1800]}"
        overlap = _token_overlap(query, candidate_text)
        if overlap <= 0:
            continue
        # Exact committee-domain retrieval remains useful even when the
        # distinctive subject vocabulary is short.
        score = min(0.99, 0.35 + 0.64 * overlap)
        candidate = {**row, "similarity": round(score, 4), "retrieval": ["lexical"]}
        current = best_by_bill.get(bill_no)
        if not current or score > float(current.get("similarity") or 0):
            best_by_bill[bill_no] = candidate
    return sorted(best_by_bill.values(), key=lambda row: float(row["similarity"]), reverse=True)[:k]


def _hybrid_rag_search(
    query: str,
    *,
    exclude_bill_nos: set[str],
    cutoff_date: str | None = None,
    k: int = 5,
) -> list[dict[str, Any]]:
    lexical = _local_lexical_search(
        query,
        exclude_bill_nos=exclude_bill_nos,
        cutoff_date=cutoff_date,
        k=max(k * 2, 8),
    )
    semantic: list[dict[str, Any]] = []
    embedding = try_embed(query[:5000])
    if embedding:
        semantic = vector_search(
            embedding,
            source="national_assembly",
            doc_type="cost_estimate",
            k=max(k * 2, 8),
            exclude_bill_nos=exclude_bill_nos,
        )
        semantic = [row for row in semantic if _contains_committee(
            f"{row.get('bill_name', '')} {row.get('content', '')}"
        )]
        if cutoff_date:
            semantic = [
                row for row in semantic
                if (date := _normalise_date(row.get("propose_date") or row.get("proposeDate")))
                and date <= cutoff_date
            ]

    # Reciprocal rank fusion balances exact legal terms with semantic matches.
    fused: dict[str, dict[str, Any]] = {}
    for source_name, ranking in (("semantic", semantic), ("lexical", lexical)):
        for rank, row in enumerate(ranking, 1):
            bill_no = str(row.get("bill_no") or "")
            if not bill_no or bill_no in exclude_bill_nos:
                continue
            current = fused.setdefault(bill_no, {
                "bill_id": row.get("bill_id"),
                "bill_no": bill_no,
                "bill_name": row.get("bill_name"),
                "propose_date": _normalise_date(
                    row.get("propose_date") or row.get("proposeDate")
                ),
                "content": str(row.get("content") or "")[:2000],
                "similarity": float(row.get("similarity") or 0),
                "rrf_score": 0.0,
                "retrieval": [],
            })
            current["rrf_score"] += 1 / (60 + rank)
            current["similarity"] = max(
                float(current.get("similarity") or 0),
                float(row.get("similarity") or 0),
            )
            if source_name not in current["retrieval"]:
                current["retrieval"].append(source_name)
            if len(str(row.get("content") or "")) > len(str(current.get("content") or "")):
                current["content"] = str(row.get("content") or "")[:2000]
    ordered = sorted(
        fused.values(),
        key=lambda row: (float(row["rrf_score"]), float(row["similarity"])),
        reverse=True,
    )
    for row in ordered:
        row["rrf_score"] = round(float(row["rrf_score"]), 6)
        row["similarity"] = round(float(row["similarity"]), 3)
    return ordered[:k]


@lru_cache(maxsize=1)
def _committee_documents_by_bill() -> dict[str, str]:
    chunks: dict[str, list[str]] = defaultdict(list)
    for row in _local_committee_chunks():
        bill_no = str(row.get("bill_no") or "")
        if bill_no:
            chunks[bill_no].append(str(row.get("content") or ""))
    return {bill_no: "\n".join(parts) for bill_no, parts in chunks.items()}


def _primary_authority(text: str) -> str | None:
    compact = _compact(text)
    owned = re.search(
        r"(?:소속(?:하에|으로)?|에)서?[^.]{0,35}(?:위원회|심의회|협의회)를?(?:둔다|설치)",
        compact,
    )
    search_area = compact[:owned.end()] if owned else compact
    for authority in _PUBLIC_AUTHORITIES:
        if f"{authority}장관소속" in search_area or f"{authority}에" in search_area:
            return authority
    mentioned = [
        (compact.find(marker), authority)
        for authority in _PUBLIC_AUTHORITIES
        for marker in (f"{authority}장관", f"{authority}소속", f"{authority}의")
        if marker in compact
    ]
    if mentioned:
        return min(mentioned)[1]
    patterns = (
        r"([\uac00-\ud7a3]{2,18}(?:부|처|청))장관(?:의)?소속",
        r"([\uac00-\ud7a3]{2,18}(?:부|처|청))장관소속하에",
        r"([\uac00-\ud7a3]{2,18}(?:부|처|청))에(?:는)?[^.]{0,30}(?:위원회|심의회|협의회)를?둔다",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return match.group(1)
    return None


def _committee_article_slice(group: dict[str, Any], purpose: str) -> str:
    if purpose == "meeting_count":
        wanted = re.compile(r"기능|심의|의결|업무|계획|회의")
    elif purpose == "paid_members":
        wanted = re.compile(r"구성|위원장|위원|위촉|임명|당연직")
    else:
        return ""
    selected = [
        f"{article.get('no', '')} {article.get('text', '')}"
        for article in group.get("articles") or []
        if wanted.search(_compact(f"{article.get('no', '')} {article.get('text', '')}"))
    ]
    return " ".join(selected)[:3500]


def _variable_search_query(
    key: str,
    *,
    bill_name: str,
    group: dict[str, Any],
    article_text: str,
    total_members: int | None,
    propose_date: str | None,
) -> str:
    authority = _primary_authority(article_text) or ""
    target_year = (propose_date or "")[:4]
    if key == "meeting_count":
        hint = "심의 기능 안건 성격 회의 개최 실적 연간 횟수"
    elif key == "paid_members":
        hint = "위원회 총원 당연직 공무원 위촉직 민간위원 회의수당 지급대상"
    else:
        hint = (
            f"{target_year}년도 예산 및 기금운용계획 집행지침 "
            "위원회 참석수당 안건검토수당 1인당 회당 단가"
        )
    structural_text = _committee_article_slice(group, key)
    total = f"총 {total_members}명" if total_members else ""
    if key == "paid_members":
        identity = (bill_name, str(group.get("entity") or ""))
    elif key == "meeting_count":
        # Meeting frequency follows the committee's function and agenda, not
        # merely the institution name (e.g. "university").
        identity = (authority,)
    else:
        # Allowance follows the applicable fiscal-year guideline.
        identity = (authority, target_year)
    return " ".join(
        part for part in (*identity, total, structural_text, hint) if part
    )[:5000]


def _rerank_variable_cases(
    cases: list[dict[str, Any]],
    *,
    key: str,
    query: str,
    target_text: str,
    target_identity: str,
    total_members: int | None,
    propose_date: str | None,
) -> list[dict[str, Any]]:
    documents = _committee_documents_by_bill()
    target_authority = _primary_authority(target_text)
    target_year = int((propose_date or "0000")[:4] or 0)
    reranked: list[dict[str, Any]] = []
    for case in cases:
        bill_no = str(case.get("bill_no") or "")
        document = documents.get(bill_no) or str(case.get("content") or "")
        retrieval_similarity = float(case.get("similarity") or 0)
        content_similarity = _token_overlap(query, document)
        score = 0.28 * retrieval_similarity + 0.42 * content_similarity

        candidate_authority = _primary_authority(document)
        if target_authority and candidate_authority == target_authority:
            score += 0.26 if key == "meeting_count" else 0.18

        if key == "paid_members" and total_members:
            subject_similarity = _token_overlap(
                target_identity,
                f"{case.get('bill_name', '')} {document[:2500]}",
            )
            score += 0.30 * subject_similarity
            candidate_total = _extract_committee_total_members(document)
            if candidate_total:
                ratio = min(total_members, candidate_total) / max(total_members, candidate_total)
                score += 0.18 * ratio

        if key == "allowance_won" and target_year:
            years = {
                int(value)
                for value in re.findall(
                    r"(20\d{2})년도[^\n]{0,100}(?:예산|기금운용계획)[^\n]{0,100}(?:지침|세부지침)",
                    document,
                )
            }
            if target_year in years:
                score += 0.30
            elif any(abs(year - target_year) <= 1 for year in years):
                score += 0.15
            if re.search(r"안건(?:검토|심의|사전검토)(?:수당|료)?", _compact(document)):
                score += 0.22

        row = dict(case)
        row["retrieval_similarity"] = round(retrieval_similarity, 4)
        row["content_similarity"] = round(content_similarity, 4)
        row["structural_score"] = round(min(0.99, score), 4)
        # Downstream evidence selection uses similarity as the structural
        # comparison score, while retaining the raw retrieval score above.
        row["similarity"] = row["structural_score"]
        reranked.append(row)
    return sorted(
        reranked,
        key=lambda row: (
            float(row.get("structural_score") or 0),
            float(row.get("retrieval_similarity") or 0),
            str(row.get("bill_no") or ""),
        ),
        reverse=True,
    )


def _nearest_committee_total_members(document: str, anchor: int) -> int | None:
    """Bind a paid-member count to the closest stated committee size.

    Official estimates often describe a precedent committee first and the
    proposed committee second.  A document-wide or fixed-pattern-first search
    can therefore pair, for example, the proposed 10 paid members with the
    precedent's 25-member ceiling instead of the proposed 15-member ceiling.
    """
    window_start = max(0, anchor - 1_200)
    window_end = min(len(document), anchor + 1_200)
    window = document[window_start:window_end]
    patterns = (
        re.compile(r"(?:위원회\s*)?정원(?:을|은|이|의)?\s*(\d{1,3})\s*명"),
        re.compile(
            r"위원장[\s\S]{0,90}?포함(?:하여|한)?\s*(\d{1,3})\s*명"
            r"(?:\s*이내)?(?:의)?\s*위원"
        ),
        re.compile(r"(\d{1,3})\s*명\s*이내(?:의)?\s*위원으로\s*구성"),
        re.compile(r"(?:전체\s*)?위원\s*수(?:는|를|가)?\s*(\d{1,3})\s*명"),
    )
    mentions: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(window):
            value = int(match.group(1))
            if not 1 <= value <= 100:
                continue
            absolute_center = window_start + (match.start() + match.end()) // 2
            mentions.append((abs(anchor - absolute_center), value))
    if mentions:
        return min(mentions, key=lambda row: row[0])[1]
    return _extract_committee_total_members(window)


def _rag_case_assumption_candidates(rag_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover explicit operands from every retrieved official case.

    Some TAG rows retain an operand name but have a null value. This fallback
    applies the same deterministic patterns to all retrieved bills and never
    invents a value or reads the held-out target bill.
    """
    documents_by_bill = _committee_documents_by_bill()

    extractors: dict[str, tuple[tuple[tuple[re.Pattern[str], float], ...], str]] = {
        "committee_meeting_count": (
            ((re.compile(r"(?:연간|연)\s*(\d{1,2})\s*회"), 1),),
            "회",
        ),
        "committee_paid_member_count": (
            ((
                re.compile(
                    r"(?:민간|외부|위촉|비상임)\s*위원(?:의\s*수)?"
                    r"\s*(?:은|는|을|으로)?\s*(\d{1,2})\s*명"
                    r"|(\d{1,2})\s*명의\s*(?:민간|외부|위촉|비상임)\s*위원"
                ),
                1,
            ),),
            "명",
        ),
        "committee_allowance_unit": (
            (
                (
                    re.compile(
                        r"(?:회의\s*)?(?:참석\s*)?(?:수당|사례비|사례금)"
                        r"(?:\s*단가)?[\s\S]{0,180}?"
                        r"(?:1회|회당|1인당)?\s*(\d+(?:\.\d+)?)\s*만원"
                        r"|(?:회당|1인당)\s*(\d+(?:\.\d+)?)\s*만원"
                    ),
                    10_000,
                ),
                (
                    re.compile(
                        r"(?:회의\s*)?(?:참석\s*)?(?:수당|사례비|사례금)"
                        r"(?:\s*단가)?[\s\S]{0,140}?"
                        r"(?:1회|회당|1인당)?\s*(\d{2,3}(?:,\d{3})+)\s*원"
                        r"|(?:회당|1인당)\s*(\d{2,3}(?:,\d{3})+)\s*원"
                    ),
                    1,
                ),
            ),
            "원",
        ),
    }
    rows: list[dict[str, Any]] = []
    for rank, case in enumerate(rag_cases, 1):
        bill_no = str(case.get("bill_no") or "")
        document = documents_by_bill.get(bill_no) or ""
        if not bill_no or not document:
            continue
        for assumption_key, (pattern_specs, unit) in extractors.items():
            extracted: list[tuple[float, str, str, int]] = []
            # For allowances, an explicit "20만원" assumption is stronger
            # evidence than the 150,000원 + 50,000원 guideline components
            # commonly quoted in a footnote.  Lower-priority formats are only
            # attempted when the stronger format is absent.
            for pattern, multiplier in pattern_specs:
                current: list[tuple[float, str, str, int]] = []
                for match in pattern.finditer(document):
                    raw = next((group for group in match.groups() if group is not None), None)
                    if raw is None:
                        continue
                    value = float(raw.replace(",", "")) * multiplier
                    if assumption_key == "committee_meeting_count" and not 1 <= value <= 12:
                        continue
                    if assumption_key == "committee_paid_member_count" and not 1 <= value <= 60:
                        continue
                    if assumption_key == "committee_allowance_unit" and not 50_000 <= value <= 600_000:
                        continue
                    context = re.sub(
                        r"\s+",
                        " ",
                        document[max(0, match.start() - 450):match.end() + 650],
                    ).strip()
                    current.append((
                        value,
                        re.sub(r"\s+", " ", match.group(0)).strip(),
                        context,
                        match.start(),
                    ))
                if current:
                    extracted = current
                    break
            if not extracted:
                continue
            chosen = _mode_or_median([
                value for value, _source, _context, _anchor in extracted
            ])
            source, source_context, source_anchor = next(
                (
                    (source, context, anchor)
                    for value, source, context, anchor in extracted
                    if value == chosen
                ),
                (extracted[0][1], extracted[0][2], extracted[0][3]),
            )
            context_total = (
                _nearest_committee_total_members(document, source_anchor)
                if assumption_key == "committee_paid_member_count"
                else _extract_committee_total_members(source_context)
            )
            rows.append({
                "assumption_key": assumption_key,
                "value": int(round(chosen)),
                "unit": unit,
                "bill_no": bill_no,
                "bill_name": case.get("bill_name"),
                "item_name": "검색된 공식 비용추계 선례",
                "source_text": source,
                "source_context": source_context[:1400],
                "score": round(1.0 + float(case.get("similarity") or 0), 3),
                "matched_by_rag": True,
                "rag_rank": rank,
                "rag_similarity": float(case.get("similarity") or 0),
                "retrieval_similarity": float(case.get("retrieval_similarity") or 0),
                "structural_score": float(case.get("structural_score") or case.get("similarity") or 0),
                "candidate_total_members": (
                    context_total or _extract_committee_total_members(document)
                ),
                "propose_date": case.get("propose_date"),
                "recovered_from_rag": True,
            })
    return rows


def _number(patterns: tuple[str, ...], text: str) -> tuple[int | None, str | None]:
    compact = _compact(text)
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return int(match.group(1).replace(",", "")), match.group(0)
    return None, None


def _meeting_count_from_document(text: str) -> tuple[int | None, str | None]:
    value, matched = _number((
        r"(?:연간총|연간|연|매연)(\d{1,3})회",
        r"1년에(\d{1,3})회",
    ), text)
    if value is not None:
        return value, matched
    compact = _compact(text)
    periodic_patterns = (
        (r"분기별(?:로)?(\d{1,2})회", 4),
        (r"반기별(?:로)?(\d{1,2})회", 2),
        (r"월(?:별로)?(\d{1,2})회", 12),
    )
    for pattern, multiplier in periodic_patterns:
        match = re.search(pattern, compact)
        if match:
            return int(match.group(1)) * multiplier, match.group(0)
    if "격월로" in compact:
        return 6, "격월로"
    return None, None


def _paid_members_from_document(text: str) -> tuple[int | None, str | None, bool]:
    compact = _compact(text)
    patterns = (
        r"(?:민간|위촉|외부|비상임)위원(?:은|을|으로|을)?(\d{1,3})명",
        r"(\d{1,3})명의(?:민간|위촉|외부|비상임)위원",
        r"(?:민간|위촉|외부|비상임)위원.{0,12}?(?:수당을지급하는|지급대상은).{0,8}?(\d{1,3})명",
        # A person explicitly "appointed" (위촉) is distinct from an
        # official "named" (임명) to the committee and is fee-eligible.
        # Do not match the ambiguous combined phrase "임명 또는 위촉".
        r"(?<!임명또는)위촉하는사람(?:은)?(\d{1,3})명",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if not match:
            continue
        raw = next((group for group in match.groups() if group and group.isdigit()), None)
        if raw:
            is_upper_bound = "이내" in match.group(0)
            return int(raw), match.group(0), not is_upper_bound
    total = _extract_committee_total_members(text)
    if total and re.search(r"전원(?:을|이)?(?:민간|위촉|외부)", compact):
        return total, "전원 민간·위촉직", True
    return None, None, False


_QUOTE = r'["\'“”‘’「」『』]?'


def _member_delta_from_document(text: str) -> tuple[int | None, str | None]:
    compact = _compact(text)
    patterns = (
        # 서술형: "10명에서 15명으로"
        r"(?:민간|위촉|외부|비상임)?위원.{0,20}?(\d{1,3})명에서.{0,20}?(\d{1,3})명으로",
        r"(\d{1,3})명.{0,15}?(\d{1,3})명으로증원",
        # 표준 개정 명령문(인용치환): "10명"을 "15명"으로 한다 / 제N조 중 "10명"을 "15명"으로
        rf"{_QUOTE}(\d{{1,3}})명{_QUOTE}[을를]{_QUOTE}(\d{{1,3}})명{_QUOTE}으로",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            before, after = int(match.group(1)), int(match.group(2))
            if before == after:
                continue
            return after - before, match.group(0)
    return None, None


def _change_type(group: dict[str, Any], doc_type: str) -> str:
    text = _compact(" ".join(
        f"{article.get('no', '')} {article.get('text', '')} {article.get('change_type', '')}"
        for article in group["articles"]
    ))
    if re.search(r"폐지|삭제", text):
        return "abolished"
    if doc_type == "제정안" or "신설" in text:
        return "new"
    if any(str(article.get("change_type") or "") == "신설" for article in group["articles"]):
        return "new"
    delta, _ = _member_delta_from_document(text)
    if delta is not None or doc_type in {"일부개정안", "신·구조문대비표"}:
        return "existing_change"
    return "unknown"


def _normalise_allowance(candidate: dict[str, Any]) -> int | None:
    source_text = _compact(candidate.get("source_text"))
    # Source prose is preferred because table extraction occasionally shifts
    # decimal points (for example 40만원 -> 4,000,000원).
    total_match = re.search(r"총(\d+(?:\.\d+)?)만원", source_text)
    if total_match:
        return int(round(float(total_match.group(1)) * 10_000))
    manwon = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)만원", source_text)]
    if manwon:
        # If the sentence enumerates components and explicitly says 합계, use
        # the extracted structured total only when it is in a plausible range.
        raw = candidate.get("value")
        try:
            raw_number = float(raw)
        except (TypeError, ValueError):
            raw_number = 0
        if len(manwon) > 1 and 50_000 <= raw_number <= 600_000:
            return int(round(raw_number))
        return int(round(max(manwon) * 10_000))
    try:
        value = float(candidate.get("value"))
    except (TypeError, ValueError):
        return None
    unit = str(candidate.get("unit") or "")
    if "백만원" in unit:
        value *= 1_000_000
    elif "만원" in unit:
        value *= 10_000
    value = int(round(value))
    return value if 50_000 <= value <= 600_000 else None


def _candidate_value(key: str, candidate: dict[str, Any]) -> float | None:
    if key == "allowance_won":
        value = _normalise_allowance(candidate)
        return float(value) if value is not None else None
    try:
        value = float(candidate.get("value"))
    except (TypeError, ValueError):
        return None
    if key == "meeting_count" and not 1 <= value <= 12:
        return None
    if key == "paid_members" and not 1 <= value <= 60:
        return None
    return value


def _candidate_key(key: str) -> str:
    return {
        "meeting_count": "committee_meeting_count",
        "paid_members": "committee_paid_member_count",
        "allowance_won": "committee_allowance_unit",
    }[key]


def _formula_compatible_candidate(
    key: str,
    candidate: dict[str, Any],
    formula_family: str | None,
) -> bool:
    """Keep a variable's evidence consistent with the selected formula."""
    if key != "allowance_won" or formula_family not in {
        "meeting_allowance",
        "meeting_and_review_allowance",
    }:
        return True
    evidence_text = _compact(
        f"{candidate.get('source_text') or ''} "
        f"{candidate.get('source_context') or ''}"
    )
    includes_review = bool(re.search(
        r"안건(?:사전)?검토|검토비|검토사례비",
        evidence_text,
    ))
    if formula_family == "meeting_allowance":
        return not includes_review
    return includes_review


def _representative_value(
    values: list[float],
    *,
    prefer_mode: bool = True,
) -> tuple[float, str]:
    """Choose a defensible peer prior and report how it was selected.

    A repeated value is not automatically a convention.  Use the mode only
    when it is unique, represents at least 40% of a cohort of three or more
    independent cases, and beats the runner-up frequency.  Otherwise the
    median is the more robust general-purpose prior.
    """
    if not values:
        raise ValueError("대표값을 계산할 후보가 없습니다.")
    counts = Counter(values)
    top_count = max(counts.values())
    ranked_counts = sorted(counts.values(), reverse=True)
    runner_up = ranked_counts[1] if len(ranked_counts) > 1 else 0
    modes = [value for value, count in counts.items() if count == top_count]
    has_clear_mode = (
        prefer_mode
        and len(values) >= 3
        and len(modes) == 1
        and top_count >= 2
        and top_count > runner_up
        and top_count / len(values) >= 0.4
    )
    if has_clear_mode:
        return modes[0], "clear_mode"
    return statistics.median(values), "median"


def _mode_or_median(values: list[float]) -> float:
    """Backward-compatible value-only wrapper for local extraction helpers."""
    value, _method = _representative_value(values)
    return value


def _rank_tag_candidates(
    candidates: list[dict[str, Any]],
    rag_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rag_ranks = {str(row.get("bill_no") or ""): rank for rank, row in enumerate(rag_cases, 1)}
    rag_similarities = {
        str(row.get("bill_no") or ""): float(row.get("similarity") or 0)
        for row in rag_cases
    }
    rag_retrieval_similarities = {
        str(row.get("bill_no") or ""): float(row.get("retrieval_similarity") or 0)
        for row in rag_cases
    }
    rag_structural_scores = {
        str(row.get("bill_no") or ""): float(row.get("structural_score") or row.get("similarity") or 0)
        for row in rag_cases
    }
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        base = float(row.get("score") or 0)
        rag_rank = rag_ranks.get(str(row.get("bill_no") or ""))
        if rag_rank:
            base += 0.35 / rag_rank
            row["matched_by_rag"] = True
            row["rag_rank"] = rag_rank
            row["rag_similarity"] = rag_similarities.get(str(row.get("bill_no") or ""), 0.0)
            row["retrieval_similarity"] = rag_retrieval_similarities.get(
                str(row.get("bill_no") or ""), 0.0
            )
            row["structural_score"] = rag_structural_scores.get(
                str(row.get("bill_no") or ""), 0.0
            )
        row["hybrid_score"] = round(base, 4)
        ranked.append(row)
    return sorted(ranked, key=lambda row: float(row.get("hybrid_score") or 0), reverse=True)


def _variable_candidate_preview(
    key: str,
    ranked: list[dict[str, Any]],
    *,
    formula_family: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    wanted = _candidate_key(key)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in ranked:
        if candidate.get("assumption_key") != wanted or not candidate.get("matched_by_rag"):
            continue
        if not _formula_compatible_candidate(key, candidate, formula_family):
            continue
        value = _candidate_value(key, candidate)
        bill_no = str(candidate.get("bill_no") or "")
        if value is None or not bill_no or bill_no in seen:
            continue
        seen.add(bill_no)
        rows.append({
            "bill_no": bill_no,
            "bill_name": candidate.get("bill_name"),
            "value": int(round(value)),
            "unit": VARIABLE_META[key]["unit"],
            "candidate_total_members": candidate.get("candidate_total_members"),
            "structural_score": candidate.get("structural_score"),
            "source_context": str(
                candidate.get("source_context") or candidate.get("source_text") or ""
            )[:1200],
        })
        if len(rows) >= limit:
            break
    return rows


def _select_variable_evidence_with_llm(
    *,
    group: dict[str, Any],
    article_text: str,
    total_members: int | None,
    propose_date: str | None,
    ranked_by_variable: dict[str, list[dict[str, Any]]],
    formula_family: str = "meeting_allowance",
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, Any]]:
    """Let the LLM compare precedent reasons, never create operands.

    Every returned bill number is allow-listed against deterministic RAG/TAG
    candidates. Values continue to come from regex-grounded official text.
    """
    candidates = {
        key: _variable_candidate_preview(
            key,
            ranked,
            formula_family=formula_family,
        )
        for key, ranked in ranked_by_variable.items()
    }
    if sum(len(rows) for rows in candidates.values()) < 3:
        return {}, {}, {
            "method": "deterministic_fallback",
            "reason": "원문 근거가 있는 후보 선례가 부족함",
        }
    payload = {
        "target": {
            "committee": group.get("entity"),
            "authority": _primary_authority(article_text),
            "total_members": total_members,
            "proposal_date": propose_date,
            "selected_formula": FORMULA_FAMILY_META.get(formula_family),
            "articles": article_text[:6500],
        },
        "candidates": candidates,
    }
    prompt = """당신은 국회 위원회 비용추계의 선례 선정자다.
아래 target과 candidates를 비교하여 변수별로 가장 비교 가능한 공식 선례의 bill_no만 선택하라.

우선순위:
1. meeting_count: 위원회 이름보다 심의 기능, 안건 성격, 소속 부처, 실제 개최실적을 우선한다. 상시 규제·인허가 위원회와 중장기 정책·발전 심의위원회를 구분한다.
2. paid_members: 기관 성격, 총원, 당연직·공무원·위촉직 구성 방식을 우선한다. 총원이 다르면 절대값보다 수당 대상 비율을 비교한다.
3. allowance_won: 제안 연도와 가장 가까운 공식 예산지침을 우선하고, 참석수당만인지 안건검토비까지 포함하는지를 맞춘다.

규칙:
- target.selected_formula와 구성요소가 다른 수당 선례는 선택하지 마라.
- 숫자를 만들거나 수정하지 마라.
- candidates에 있는 bill_no만 반환하라.
- 동질적이고 독립적인 선례가 여러 건이면 최대 3건까지 선택할 수 있다.
- 비교할 수 있는 선례가 없으면 bill_nos를 빈 배열로 두라.

JSON 형식:
{
  "meeting_count": {"bill_nos": ["..."], "reason": "..."},
  "paid_members": {"bill_nos": ["..."], "reason": "..."},
  "allowance_won": {"bill_nos": ["..."], "reason": "..."}
}

입력:
""" + json.dumps(payload, ensure_ascii=False)
    parsed = _gemini_raw_json(prompt)
    if not isinstance(parsed, dict):
        return {}, {}, {"method": "deterministic_fallback", "reason": "LLM selection unavailable"}

    selected: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    rejected: list[str] = []
    for key in VARIABLE_META:
        row = parsed.get(key)
        if not isinstance(row, dict):
            continue
        allowed = {str(candidate["bill_no"]) for candidate in candidates.get(key) or []}
        bill_nos: list[str] = []
        for value in row.get("bill_nos") or []:
            bill_no = str(value or "")
            if bill_no in allowed and bill_no not in bill_nos:
                bill_nos.append(bill_no)
            elif bill_no:
                rejected.append(f"{key}:{bill_no}")
        # Frequency is an operational convention with high variance. One
        # analogous committee is not enough to establish an annual cadence;
        # fall back to the structurally scoped cohort unless two independent
        # official precedents are selected.
        if key == "meeting_count" and len(bill_nos) < 2:
            if bill_nos:
                rejected.append(f"{key}:single_precedent_insufficient")
            bill_nos = []
        if bill_nos:
            selected[key] = bill_nos[:3]
            reasons[key] = str(row.get("reason") or "")[:500]
    return selected, reasons, {
        "method": "llm_precedent_ranking_plus_allowlist_grounding",
        "selected": selected,
        "rejected": rejected,
    }


def _suggest_from_tag(
    key: str,
    ranked: list[dict[str, Any]],
    *,
    total_members: int | None = None,
    formula_family: str | None = None,
    require_rag_match: bool = False,
    similarity_scoped: bool = True,
    prefer_closest_case: bool = False,
    preferred_bill_nos: list[str] | None = None,
    preferred_reason: str = "",
) -> tuple[int | None, str, list[dict[str, Any]]]:
    wanted = _candidate_key(key)
    samples: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    for candidate in ranked:
        if candidate.get("assumption_key") != wanted:
            continue
        if not _formula_compatible_candidate(key, candidate, formula_family):
            continue
        value = _candidate_value(key, candidate)
        if value is None:
            continue
        bill_no = str(candidate.get("bill_no") or "")
        bill_family = re.sub(
            r"[^\uac00-\ud7a3A-Za-z0-9]",
            "",
            str(candidate.get("bill_name") or "").lower(),
        )
        signature = bill_family or bill_no or f"{candidate.get('item_name')}:{value}"
        if signature in seen_families:
            continue
        seen_families.add(signature)
        samples.append({
            "value": value,
            "bill_no": bill_no,
            "bill_name": candidate.get("bill_name"),
            "item_name": candidate.get("item_name"),
            "source_text": candidate.get("source_text"),
            "source_context": candidate.get("source_context"),
            "score": candidate.get("hybrid_score") or candidate.get("score"),
            "matched_by_rag": bool(candidate.get("matched_by_rag")),
            "rag_rank": candidate.get("rag_rank"),
            "rag_similarity": candidate.get("rag_similarity"),
            "retrieval_similarity": candidate.get("retrieval_similarity"),
            "structural_score": candidate.get("structural_score"),
            "candidate_total_members": candidate.get("candidate_total_members"),
            "recovered_from_rag": bool(candidate.get("recovered_from_rag")),
        })
        if len(samples) >= 7:
            break
    if not samples:
        return None, "구조화된 TAG 변수에서 신뢰할 수 있는 값을 찾지 못했습니다.", []

    # Evidence priority for an absent document value:
    # RAG-matched official precedents lead the peer group, then TAG supplies
    # enough comparable cases to avoid copying one bill's premise verbatim.
    # A true target-specific operational record would be direct evidence and
    # is handled separately; ordinary retrieved cases remain unconfirmed.
    matched_samples = [sample for sample in samples if sample.get("matched_by_rag")]
    preferred_order = {
        str(bill_no): index for index, bill_no in enumerate(preferred_bill_nos or [])
    }
    preferred_samples = sorted(
        [sample for sample in samples if str(sample.get("bill_no") or "") in preferred_order],
        key=lambda sample: preferred_order[str(sample.get("bill_no") or "")],
    )
    if preferred_samples:
        selected_samples = preferred_samples
    elif not similarity_scoped:
        selected_samples = samples
    elif matched_samples:
        best_similarity = max(float(sample.get("rag_similarity") or 0) for sample in matched_samples)
        selected_samples = [
            sample for sample in matched_samples
            if float(sample.get("rag_similarity") or 0) >= max(0.5, best_similarity - 0.1)
        ]
        if not selected_samples:
            selected_samples = matched_samples[:3]
    else:
        if require_rag_match:
            return (
                None,
                "검색된 유사 공식 의안에서 이 변수의 명시값을 찾지 못했습니다. 사용자 입력이 필요합니다.",
                [],
            )
        selected_samples = samples
    values = [float(sample["value"]) for sample in selected_samples]
    representative_method = ""
    if key == "paid_members" and total_members:
        # Committee size varies substantially.  Normalize comparable cases to
        # the target's total membership and use the median ratio-derived count;
        # a coincidental repeated headcount is not a general convention.
        ratios: list[float] = []
        for sample in selected_samples:
            explicit_total = sample.get("candidate_total_members")
            source = _compact(sample.get("source_text"))
            totals = [int(explicit_total)] if explicit_total else [
                int(value)
                for value in re.findall(r"(?:총|정원(?:은)?)(\d{1,3})명", source)
            ]
            if totals:
                ratio = float(sample["value"]) / max(totals)
                if 0 < ratio <= 1:
                    ratios.append(ratio)
        normalized_counts = [round(total_members * ratio) for ratio in ratios]
        representative_pool = normalized_counts or values
        chosen, representative_method = _representative_value(
            representative_pool,
            prefer_mode=False,
        )
    elif preferred_samples:
        chosen, representative_method = _representative_value(values)
    elif prefer_closest_case and selected_samples:
        chosen = float(selected_samples[0]["value"])
        representative_method = "closest_case"
    else:
        chosen, representative_method = _representative_value(
            values,
            prefer_mode=key != "paid_members",
        )
    chosen_int = int(round(chosen))
    if preferred_samples:
        tier_label = f"LLM이 선택하고 원문으로 검증한 구조 유사 선례 {len(selected_samples)}건"
    elif prefer_closest_case:
        tier_label = "변수별 구조 유사도가 가장 높은 공식 선례"
    elif not similarity_scoped:
        tier_label = f"도메인과 무관한 공공위원회 공식 사례 {len(selected_samples)}건"
    elif any(sample.get("recovered_from_rag") for sample in selected_samples):
        tier_label = f"RAG로 찾은 유사 공식 비용추계 선례 {len(selected_samples)}건"
    elif matched_samples:
        tier_label = f"RAG 유사 선례를 우선한 구조화 TAG 사례 {len(selected_samples)}건"
    else:
        tier_label = f"현재 의안을 제외한 구조화 TAG 사례 {len(selected_samples)}건"
    method_label = {
        "clear_mode": "뚜렷한 최빈값",
        "median": "중앙값",
        "closest_case": "최상위 구조 유사 선례값",
    }.get(representative_method, "대표값")
    basis = (
        f"{tier_label}의 {method_label} {chosen_int:,}{VARIABLE_META[key]['unit']}. "
        + (f"선정 이유: {preferred_reason} " if preferred_samples and preferred_reason else "")
        + "조문에 직접 명시된 값이 아니며 사용자 확인이 필요합니다."
    )
    return chosen_int, basis, selected_samples[:5]


def _variable(
    key: str,
    *,
    value: int | None,
    source_type: str,
    basis: str,
    confirmed: bool,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        **ALL_VARIABLE_META[key],
        "value": value,
        "source_type": source_type,
        "basis": basis,
        "confirmed": confirmed,
        "blocking": value is None,
        "evidence": evidence or [],
    }


# 복제(×N) 감지 — 한 실체가 여러 지역·기관에 복제 설치되면 비용이 단위비용 × N.
# 구분자: · (U+00B7), ․ (U+2024), . , ㆍ (U+318D 아래아), ・ (U+30FB) 등 다양하게 쓰임.
_SEP = r"[·․.ㆍ・]?"
_REPL_EXPLICIT_RE = re.compile(
    rf"(\d{{1,3}})개(?:의)?(?:광역|지방자치단체|시{_SEP}도|지역|기관|법원|권역|지부)"
)
_REPL_SIDO_RE = re.compile(
    rf"각시{_SEP}도|시{_SEP}도지사|시{_SEP}도별|시{_SEP}도마다|시{_SEP}도에각각|시{_SEP}도에둔다"
)
_REPL_FLAG_RE = re.compile(
    rf"지역위원회|각지역|각급|권역별"
)
# 재사용 가능한 행정구역 개수 상수 — 한 번 넣으면 계속 쓰는 고정값.
# 오탐 방지: (a) 실체명에 행정단위가 있거나, (b) 본문에 "…에 …둔다/설치" 설치 콜로케이션일 때만.
# 단순히 본문에 "시·도지사"가 언급되는 정도로는 복제로 보지 않는다.
# (좁은 단위=시·군·구를 먼저 검사)
_JURISDICTION_UNITS: list[tuple[re.Pattern[str], re.Pattern[str], int, str]] = [
    (
        re.compile(rf"시{_SEP}군{_SEP}구|기초지방자치단체|기초자치단체|자치구"),  # 실체명
        re.compile(rf"(?:시{_SEP}군{_SEP}구|기초지방자치단체|자치구)[가-힣]{{0,12}}(?:둔다|설치|구성)"),  # 본문 설치 콜로케이션
        229, "시·군·구(기초자치단체 229)",
    ),
    (
        re.compile(rf"시{_SEP}도위원회|시{_SEP}도[가-힣]{{0,6}}위원회|광역지방자치단체|광역자치단체"),
        re.compile(rf"각\s*시{_SEP}도[가-힣]{{0,12}}(?:둔다|설치|구성)|시{_SEP}도별[가-힣]{{0,12}}(?:둔다|설치)|시{_SEP}도마다[가-힣]{{0,12}}(?:둔다|설치)"),
        17, "시·도(광역자치단체 17)",
    ),
]
# 의무설치("둔다")면 전수 적용, 임의설치("둘 수 있다")면 전수 곱하기 부적절.
_OPTIONAL_RE = re.compile(r"둘수있|설치할수있|구성할수있|임의")


def _replication_factor(text: str, entity: str) -> tuple[int, str, bool]:
    """(배수, 근거, 확신여부).

    - 본문 명시 개수 > 행정구역 상수(시·군·구 229 / 시·도 17) > 기타 복제정황 순.
    - 행정단위는 **실체명** 또는 **본문의 설치 콜로케이션**에서만 인정(단순 언급은 제외 → 오탐 방지).
    - 임의설치면 전수 곱하지 않고 확인필요로 둔다.
    - 배수를 적용해도 실제 설치 수·지방비 여부는 가정이므로 확신=False(검토 대상).
    """
    ent = _compact(entity)
    txt = _compact(text)
    m = _REPL_EXPLICIT_RE.search(ent + txt)
    if m:
        n = int(m.group(1))
        if 1 < n <= 300:
            return n, f"본문의 '{m.group(0)}' 표현에서 복제 설치 수 {n}개를 적용했습니다.", True
    optional = bool(_OPTIONAL_RE.search(txt))
    for name_pat, establish_pat, count, label in _JURISDICTION_UNITS:
        if name_pat.search(ent) or establish_pat.search(txt):
            if optional:
                return 1, (
                    f"{label} 단위 복제 정황이나 임의설치('둘 수 있다')라 전수 적용이 부적절합니다. "
                    "실제 설치 수 확인이 필요합니다."
                ), False
            return count, (
                f"{label} 단위 의무 복제로 판단해 {count}개를 적용했습니다. "
                "실제 설치 수·지방비 여부 확인이 필요합니다."
            ), False
    if _REPL_FLAG_RE.search(ent):
        return 1, (
            "지역·기관에 복제 설치되는 정황이 있으나 설치 수가 명시되지 않았습니다. "
            "실제 비용은 표시값 × 설치 수이며 설치 수 확인이 필요합니다."
        ), False
    return 1, "", True


def _calculate_committee(committee: dict[str, Any], *, years: int = 5) -> None:
    variables = committee.get("variables") or {}
    values = {key: (variables.get(key) or {}).get("value") for key in VARIABLE_META}
    missing = [key for key, value in values.items() if value is None]
    if missing:
        committee["status"] = "blocked_missing_variables"
        committee["missing_variables"] = missing
        committee["annual_amount_thousand"] = None
        committee["total_amount_thousand"] = None
        committee["year_estimates"] = []
        return
    incumbent_value = (
        (variables.get("incumbent_paid_members") or {}).get("value")
    )
    incumbent = float(incumbent_value or 0)
    paid_members = float(values["paid_members"])
    net_paid_members = paid_members - incumbent
    if incumbent < 0 or net_paid_members <= 0:
        committee["status"] = "blocked_missing_variables"
        committee["missing_variables"] = ["incumbent_paid_members"]
        committee["annual_amount_thousand"] = None
        committee["total_amount_thousand"] = None
        committee["year_estimates"] = []
        committee["calculation_error"] = (
            "기존 제도 지급대상 인원은 신규 지급가능 인원보다 작아야 합니다."
        )
        return
    per_unit = int(round(
        float(values["meeting_count"])
        * net_paid_members
        * float(values["allowance_won"])
        / 1000
    ))
    article_text = " ".join(
        f"{a.get('no', '')} {a.get('text', '')}" for a in committee.get("articles") or []
    )
    factor, repl_basis, repl_confident = _replication_factor(article_text, str(committee.get("name") or ""))
    annual = per_unit * factor
    committee["replication"] = {
        "factor": factor,
        "per_unit_thousand": per_unit,
        "basis": repl_basis,
        "confident": repl_confident,
    }
    committee["paid_member_adjustment"] = {
        "gross_paid_members": paid_members,
        "incumbent_paid_members": incumbent,
        "net_paid_members": net_paid_members,
        "applied": incumbent > 0,
        "basis": (
            f"신규 지급가능 {paid_members:g}명 - 기존 제도 {incumbent:g}명 "
            f"= 순증 {net_paid_members:g}명"
            if incumbent > 0
            else "기존 제도 지급대상 인원을 입력하지 않아 신규 지급가능 인원을 사용"
        ),
    }
    committee["annual_amount_thousand"] = annual
    committee["total_amount_thousand"] = annual * years
    committee["year_estimates"] = [
        {"year": year, "amount_thousand": annual}
        for year in range(1, years + 1)
    ]
    unconfirmed = [
        key
        for key in VARIABLE_META
        if not (variables.get(key) or {}).get("confirmed")
    ]
    # 복제 배수를 적용했거나(가정), 복제 정황인데 규모 미상이면 검토 필요로 격상.
    replication_uncertain = (factor > 1) or (not repl_confident)
    committee["missing_variables"] = []
    committee["status"] = "review_required" if (unconfirmed or replication_uncertain) else "computed"
    committee["unconfirmed_variables"] = unconfirmed


def _build_committee(
    group: dict[str, Any],
    *,
    bill_name: str,
    bill_no: str | None,
    doc_type: str,
    index: int,
    propose_date: str | None = None,
) -> dict[str, Any]:
    article_text = " ".join(
        f"{article.get('no', '')} {article.get('text', '')}"
        for article in group["articles"]
    )
    excluded = {bill_no} if bill_no else set()
    total_members = _extract_committee_total_members(article_text)
    change_type = _change_type(group, doc_type)
    formula_query = " ".join(part for part in (
        bill_name,
        str(group.get("entity") or ""),
        _primary_authority(article_text) or "",
        _committee_article_slice(group, "meeting_count"),
        "위원회 회의비 비용추계 산식 회의참석수당 안건검토수당",
    ) if part)[:5000]
    formula_raw_cases = _hybrid_rag_search(
        formula_query,
        exclude_bill_nos=excluded,
        cutoff_date=propose_date,
        k=50,
    )
    formula_rag_cases = _rerank_variable_cases(
        formula_raw_cases,
        key="meeting_count",
        query=formula_query,
        target_text=article_text,
        target_identity=f"{bill_name} {group['entity']}",
        total_members=total_members,
        propose_date=propose_date,
    )
    formula_candidates = _formula_candidates_for_cases(formula_rag_cases)
    formula_selection = _select_formula_with_llm(
        group=group,
        article_text=article_text,
        change_type=change_type,
        candidates=formula_candidates,
    )
    item = {
        "name": f"{group['entity']} 회의수당",
        "category": "운영비",
        "formula": formula_selection["formula"],
        "trigger_ref": ", ".join(str(article.get("no") or "") for article in group["articles"]),
        "variables_needed": [meta["label"] for meta in VARIABLE_META.values()],
    }
    tag_candidates = find_assumption_candidates(
        item,
        form_type="assembly",
        limit=30,
        exclude_bill_nos=excluded,
    )
    variable_queries: dict[str, str] = {}
    variable_rag_cases: dict[str, list[dict[str, Any]]] = {}
    variable_ranked_candidates: dict[str, list[dict[str, Any]]] = {}
    variable_recovered_candidates: dict[str, list[dict[str, Any]]] = {}
    for variable_key in VARIABLE_META:
        variable_query = _variable_search_query(
            variable_key,
            bill_name=bill_name,
            group=group,
            article_text=article_text,
            total_members=total_members,
            propose_date=propose_date,
        )
        variable_query = (
            f"{variable_query} 선택 산식 {formula_selection['label']} "
            f"{formula_selection['formula']}"
        )[:5000]
        raw_cases = _hybrid_rag_search(
            variable_query,
            exclude_bill_nos=excluded,
            cutoff_date=propose_date,
            k=50,
        )
        rag_cases = _rerank_variable_cases(
            raw_cases,
            key=variable_key,
            query=variable_query,
            target_text=article_text,
            target_identity=f"{bill_name} {group['entity']}",
            total_members=total_members,
            propose_date=propose_date,
        )
        recovered = _rag_case_assumption_candidates(rag_cases)
        variable_queries[variable_key] = variable_query
        variable_rag_cases[variable_key] = rag_cases
        variable_recovered_candidates[variable_key] = recovered
        variable_ranked_candidates[variable_key] = _rank_tag_candidates(
            [*tag_candidates, *recovered],
            rag_cases,
        )
    preferred_cases, preferred_reasons, precedent_selection = (
        _select_variable_evidence_with_llm(
            group=group,
            article_text=article_text,
            total_members=total_members,
            propose_date=propose_date,
            ranked_by_variable=variable_ranked_candidates,
            formula_family=formula_selection["family"],
        )
    )

    meeting_count, meeting_match = _meeting_count_from_document(article_text)
    if meeting_count is not None:
        meeting_var = _variable(
            "meeting_count", value=meeting_count, source_type="document",
            basis=f"조문의 '{meeting_match}' 표현에서 연간 회의 횟수로 변환했습니다.",
            confirmed=True,
        )
    else:
        suggestion, basis, evidence = _suggest_from_tag(
            "meeting_count",
            variable_ranked_candidates["meeting_count"],
            require_rag_match=True,
            prefer_closest_case=False,
            preferred_bill_nos=preferred_cases.get("meeting_count"),
            preferred_reason=preferred_reasons.get("meeting_count", ""),
        )
        source_type = (
            "similar_case_prior"
            if any(row.get("recovered_from_rag") for row in evidence)
            else "tag_prior"
        ) if suggestion is not None else "missing"
        meeting_var = _variable(
            "meeting_count", value=suggestion, source_type=source_type,
            basis=basis, confirmed=False, evidence=evidence,
        )

    paid_value: int | None = None
    paid_basis = ""
    paid_confirmed = False
    paid_source = "missing"
    paid_evidence: list[dict[str, Any]] = []
    if change_type == "new":
        paid_value, paid_match, exact = _paid_members_from_document(article_text)
        if paid_value is not None:
            paid_basis = f"조문의 '{paid_match}' 표현에서 수당지급대상 인원을 추출했습니다."
            paid_confirmed = exact
            paid_source = "document"
        else:
            paid_value, paid_basis, paid_evidence = _suggest_from_tag(
                "paid_members",
                variable_ranked_candidates["paid_members"],
                total_members=total_members,
                require_rag_match=True,
                prefer_closest_case=True,
                preferred_bill_nos=preferred_cases.get("paid_members"),
                preferred_reason=preferred_reasons.get("paid_members", ""),
            )
            paid_source = (
                "similar_case_prior"
                if any(row.get("recovered_from_rag") for row in paid_evidence)
                else "tag_prior"
            ) if paid_value is not None else "missing"
    elif change_type == "existing_change":
        paid_value, paid_match = _member_delta_from_document(article_text)
        if paid_value is not None:
            paid_basis = f"조문의 인원 변경 '{paid_match}'에서 추가 인원 {paid_value}명을 계산했습니다."
            paid_confirmed = True
            paid_source = "document_delta"
        else:
            paid_basis = "기존 위원회의 현재 인원은 대상 의안 외부 정보이므로 증가 인원 수를 입력해야 합니다. 다른 의안의 인원 수는 복사할 수 없습니다."
    else:
        paid_basis = "폐지·삭제 또는 신설 여부가 명확하지 않아 추가 수당지급대상 인원을 입력해야 합니다."
    paid_var = _variable(
        "paid_members", value=paid_value, source_type=paid_source,
        basis=paid_basis, confirmed=paid_confirmed, evidence=paid_evidence,
    )
    if change_type == "existing_change":
        paid_var["label"] = "추가 수당지급대상 인원 증가분"

    incumbent_var = _variable(
        "incumbent_paid_members",
        value=None,
        source_type="optional_input",
        basis=(
            "이미 같은 기능의 위원회가 운영 중이라면 현재 수당 지급대상 "
            "인원을 입력하세요. 입력값은 신규 지급가능 인원에서 차감됩니다."
        ),
        confirmed=False,
    )
    incumbent_var["blocking"] = False

    allowance, allowance_basis, allowance_evidence = _suggest_from_tag(
        "allowance_won",
        variable_ranked_candidates["allowance_won"],
        formula_family=formula_selection["family"],
        require_rag_match=True,
        similarity_scoped=True,
        prefer_closest_case=False,
        preferred_bill_nos=preferred_cases.get("allowance_won"),
        preferred_reason=preferred_reasons.get("allowance_won", ""),
    )
    allowance_source = (
        "similar_case_prior"
        if any(row.get("recovered_from_rag") for row in allowance_evidence)
        else "tag_prior"
    ) if allowance is not None else "missing"
    allowance_var = _variable(
        "allowance_won", value=allowance,
        source_type=allowance_source,
        basis=allowance_basis, confirmed=False, evidence=allowance_evidence,
    )
    if formula_selection["family"] == "meeting_and_review_allowance":
        allowance_var["label"] = "회의당 참석·안건검토 수당 합계"

    exclusions: list[str] = []
    for label, pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern.search(_compact(article_text)) and label not in exclusions:
            exclusions.append(label)

    committee = {
        "id": f"committee-{index}",
        "name": group["entity"],
        "change_type": change_type,
        "change_type_label": {
            "new": "신규 설치",
            "existing_change": "기존 위원회 구성 변경",
            "abolished": "폐지·삭제",
            "unknown": "변경 유형 확인 필요",
        }[change_type],
        "formula": formula_selection["formula"],
        "formula_selection": formula_selection,
        "total_members": total_members,
        "variables": {
            "meeting_count": meeting_var,
            "paid_members": paid_var,
            **(
                {"incumbent_paid_members": incumbent_var}
                if change_type == "new"
                else {}
            ),
            "allowance_won": allowance_var,
        },
        "articles": [
            {
                "no": article.get("no"),
                "text": article.get("text"),
                "change_type": article.get("change_type"),
                "source": article.get("source"),
            }
            for article in group["articles"]
        ],
        "out_of_scope": exclusions,
        "references": [
            {
                **case,
                "matched_variables": sorted({
                    key
                    for key, cases in variable_rag_cases.items()
                    if any(
                        str(candidate.get("bill_no") or "")
                        == str(case.get("bill_no") or "")
                        for candidate in cases
                    )
                }),
            }
            for case in {
                str(candidate.get("bill_no") or ""): candidate
                for cases in variable_rag_cases.values()
                for candidate in cases
                if candidate.get("bill_no")
            }.values()
        ],
        "retrieval": {
            "rag_case_count": sum(len(cases) for cases in variable_rag_cases.values()),
            "rag_case_count_by_variable": {
                key: len(cases) for key, cases in variable_rag_cases.items()
            },
            "tag_candidate_count": len(tag_candidates),
            "rag_recovered_variable_count": sum(
                len(rows) for rows in variable_recovered_candidates.values()
            ),
            "rag_tag_join_count": len({
                str(candidate.get("bill_no") or "")
                for rows in variable_ranked_candidates.values()
                for candidate in rows
                if candidate.get("matched_by_rag") and candidate.get("bill_no")
            }),
            "current_bill_excluded": bool(bill_no),
            "strategy": "formula-first LLM reranking + variable-specific RAG/TAG evidence",
            "formula_query": formula_query,
            "formula_candidate_count": len(formula_candidates),
            "formula_selection": formula_selection,
            "precedent_selection": precedent_selection,
            "queries": variable_queries,
            "top_case_by_variable": {
                key: (
                    {
                        "bill_no": cases[0].get("bill_no"),
                        "bill_name": cases[0].get("bill_name"),
                        "structural_score": cases[0].get("structural_score"),
                        "retrieval_similarity": cases[0].get("retrieval_similarity"),
                    }
                    if cases else None
                )
                for key, cases in variable_rag_cases.items()
            },
            "evidence_priority": [
                "target_article_explicit",
                "rag_tag_matched_precedent",
                "tag_peer_prior",
                "human_confirmation",
            ],
        },
    }
    _calculate_committee(committee)
    return committee


def analyze_committee_articles(
    *,
    bill_name: str,
    bill_no: str | None,
    doc_type: str,
    articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = _group_committee_articles(articles)
    return [
        _build_committee(
            group,
            bill_name=bill_name,
            bill_no=bill_no,
            doc_type=doc_type,
            index=index,
        )
        for index, group in enumerate(groups, 1)
    ]


def analyze_committee_document(filename: str, content_b64: str) -> dict[str, Any]:
    started = time.time()
    pdf_bytes = base64.b64decode(_strip_data_url(content_b64))
    raw_text = _extract_pdf_text_from_bytes(pdf_bytes)
    if not raw_text:
        raise ValueError("PDF에서 텍스트를 추출하지 못했습니다. OCR 처리된 PDF로 다시 올려주세요.")
    text = strip_appendices(raw_text)
    raw_doc_type = _detect_doc_type(raw_text)
    articles: list[dict[str, Any]] = []
    doc_type = raw_doc_type
    if raw_doc_type == "일부개정안":
        articles = split_articles_from_revision_table_pdf(pdf_bytes)
        if articles:
            doc_type = "신·구조문대비표"
    if not articles:
        articles, parsed_type = split_articles_structured(text)
        doc_type = parsed_type if articles else doc_type
    if not articles:
        articles = split_articles_regex(text)
    if raw_doc_type in {"일부개정안", "전부개정안"}:
        articles = _merge_article_sources(
            articles,
            _extract_explicit_amendment_articles(text),
        )
    if not articles:
        raise ValueError("개정 조문을 분리하지 못했습니다. 신·구조문대비표와 조문 텍스트 레이어를 확인해 주세요.")

    bill_name = _extract_bill_name(text, filename)
    bill_no = _extract_current_bill_no(raw_text, filename)
    propose_date = _extract_propose_date(raw_text)
    committee_groups, grouping_diagnostics = _group_committee_articles_hybrid(articles)
    committees = [
        _build_committee(
            group,
            bill_name=bill_name,
            bill_no=bill_no,
            doc_type=doc_type,
            index=index,
            propose_date=propose_date,
        )
        for index, group in enumerate(committee_groups, 1)
    ]
    try:
        evidence_workflow = build_evidence_workflow(
            pdf_bytes=pdf_bytes,
            bill_no=bill_no,
            bill_name=bill_name,
            propose_date=propose_date,
            target_names=[str(group.get("entity") or "") for group in committee_groups],
        )
        apply_evidence_workflow(committees, evidence_workflow)
    except Exception as exc:  # Evidence enrichment must not erase base analysis.
        evidence_workflow = {
            "status": "unavailable",
            "reason": f"선례 근거 계층을 생성하지 못했습니다: {exc}",
            "covered": None,
            "components": [],
        }
        for committee in committees:
            committee["evidence_route"] = {
                "level": "input",
                "label": "근거 확인 필요",
                "summary": "기본 조문 분석결과만 표시했습니다. 선례 근거 계층을 다시 확인해 주세요.",
                "selected_bill_no": None,
            }
    status_counts = Counter(row.get("status") for row in committees)
    computed_amounts = [
        int(row["total_amount_thousand"])
        for row in committees
        if row.get("total_amount_thousand") is not None
    ]
    return {
        "channel": "committee",
        "channelLabel": "위원회 비용추계",
        "billName": bill_name,
        "billNo": bill_no,
        "proposeDate": propose_date,
        "documentType": doc_type,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "elapsedSec": round(time.time() - started, 2),
        "totalArticles": len(articles),
        "scope": {
            "status": "in_scope" if committees else "out_of_scope",
            "committeeCount": len(committees),
            "description": (
                "위원회 회의수당과 직접 운영비만 계산합니다. 사무처·인건비·조사용역은 이 채널 범위 밖으로 제외합니다."
                if committees else
                "비용추계 산식을 구성할 위원회·심의회·협의회 조문이 없습니다."
            ),
        },
        "summary": {
            "computed": status_counts.get("computed", 0),
            "reviewRequired": status_counts.get("review_required", 0),
            "blocked": status_counts.get("blocked_missing_variables", 0),
            "previewTotalThousand": (
                sum(computed_amounts) if computed_amounts else None
            ),
            "isPartial": any(row.get("out_of_scope") for row in committees),
        },
        "committees": committees,
        "evidenceWorkflow": evidence_workflow,
        "architecture": {
            "committeeGrouping": grouping_diagnostics,
            "pipeline": [
                "개정 조문 추출",
                "원문 규칙 fallback + LLM 보조 위원회 실체 구조화",
                "위원회 실체 별 조문 병합",
                "시점 제한 TAG 선례 패키지 검증",
                "완전 선례 자동 계산 / 구조 선례 추천 / 근거 부족 입력 분기",
                "Python 5개년 계산",
            ],
            "calculationPolicy": "산식·필수 변수·내부 금액이 일관된 공식 선례만 자동 계산하고, 유사 선례는 검토용 추천으로만 표시합니다.",
        },
    }


def recompute_committee_result(
    result: dict[str, Any],
    user_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    if result.get("channel") != "committee":
        raise ValueError("위원회 채널 결과가 아닙니다.")
    by_id = {str(row.get("id")): row for row in result.get("committees") or []}
    touched_committee_ids: set[str] = set()
    for entry in user_inputs:
        committee = by_id.get(str(entry.get("committeeId") or entry.get("committee_id") or ""))
        key = str(entry.get("variable") or entry.get("key") or "")
        if not committee or key not in ALL_VARIABLE_META:
            continue
        try:
            number = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValueError(f"{ALL_VARIABLE_META[key]['label']}에 유효한 숫자를 입력해 주세요.")
        if not math.isfinite(number):
            raise ValueError("유효한 숫자리를 입력해 주세요.")
        if key == "incumbent_paid_members":
            if number < 0:
                raise ValueError("기존 제도 지급대상 인원은 0 이상이어야 합니다.")
        elif key != "paid_members" and number <= 0:
            raise ValueError(f"{ALL_VARIABLE_META[key]['label']}은(는) 0보다 큰 값이어야 합니다.")
        variable = committee["variables"][key]
        variable["value"] = int(round(number))
        variable["source_type"] = "user_confirmed"
        variable["basis"] = "사용자가 입력하거나 추천 값으로 확정했습니다."
        variable["confirmed"] = True
        variable["blocking"] = False
        touched_committee_ids.add(str(committee.get("id") or ""))
        if committee.get("calculation_mode") == "verified_package":
            committee["calculation_mode"] = "user_recalculated"
            committee["evidence_route"] = {
                **(committee.get("evidence_route") or {}),
                "level": "verified",
                "label": "사용자 확정값으로 재계산",
                "summary": "검증된 선례를 기반으로 사용자가 확정한 변수를 반영했습니다.",
            }
    for committee in result.get("committees") or []:
        if (
            committee.get("calculation_mode") == "verified_package"
            and str(committee.get("id") or "") not in touched_committee_ids
        ):
            continue
        _calculate_committee(committee)
    counts = Counter(row.get("status") for row in result.get("committees") or [])
    result["summary"] = {
        "computed": counts.get("computed", 0),
        "reviewRequired": counts.get("review_required", 0),
        "blocked": counts.get("blocked_missing_variables", 0),
        "previewTotalThousand": (
            sum(
                int(row["total_amount_thousand"])
                for row in result.get("committees") or []
                if row.get("total_amount_thousand") is not None
            )
            if any(
                row.get("total_amount_thousand") is not None
                for row in result.get("committees") or []
            )
            else None
        ),
        "isPartial": any(row.get("out_of_scope") for row in result.get("committees") or []),
    }
    result["generatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return result
