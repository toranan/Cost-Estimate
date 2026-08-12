"""조문 추출 엔진 (격리 모듈, 독립형).

analyzer_v2.py에 얽히지 않는 별도 모듈. PDF 바이트 → 조문 리스트만 담당한다.
계산·판정은 절대 하지 않는다 — 이 엔진 다음 단계(committee_rule_engine.py 등)의
입력을 만드는 게 유일한 역할이다.

LLM은 Upstage만 쓴다(현재 프로젝트 설정 그대로).
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

import fitz  # PyMuPDF

from .config import get_env

ARTICLE_EXTRACTION_ENGINE_VERSION = "article-extraction-engine-v2"

UPSTAGE_API_KEY = get_env("UPSTAGE_API_KEY")
UPSTAGE_MODEL = get_env("UPSTAGE_MODEL", "solar-mini")
UPSTAGE_BASE_URL = get_env("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1/chat/completions")
_LLM_MAX_OUTPUT_TOKENS = int(get_env("LLM_MAX_OUTPUT_TOKENS", "8192") or "8192")

# 실측 버그(2213848 제7조): 단순히 "제\d+조"만 찾으면 본문 "안"의 다른 조
# 참조("제12조에 따른 지정")까지 조문 경계로 착각해 제7조를 조기 절단한다.
# 그래서 줄 시작(^)에 있고, 바로 뒤에 "(제목)"이 오거나 항 번호(①②…)가
# 이어지는 것만 진짜 헤더로 인정한다(analyzer_v2.py의 _ARTICLE_HEADER_RE와 동일 원리).
_ARTICLE_HEADER_RE = re.compile(
    r"(?m)^\s*(제\s*\d+\s*조(?:의\s*\d+)?)"
    r"(?:\s*\(([^)]{1,120})\)|\s*(?=[①②③④⑤⑥⑦⑧⑨⑩]|\([0-9]+\)))"
)

_REVISION_START_RE = re.compile(r"[가-힣0-9ㆍ·ㆍ\s]{2,100}?법(?:률)?안?\s*일부를\s*다음과\s*같이\s*개정한다\.")
_ADDENDUM_RE = re.compile(r"(?m)^\s*부\s*칙(?:\s|$)")
_COMPARISON_TABLE_RE = re.compile(r"신\s*[ㆍ·]?\s*구\s*조\s*문\s*대\s*비\s*표")
# `제44조의2제1항 중 “...”를 “...”로 하고, 각 호를 ... 신설하며`처럼
# 완성된 `제N조(제목)` 헤더가 없는 일부개정 지시문을 회복한다.
_INLINE_AMENDMENT_TARGET_RE = re.compile(
    r"(?m)(?P<no>제\s*\d+\s*조(?:의\s*\d+)?)"
    r"(?P<sub>제\s*\d+\s*항)?\s*중\s*"
)


def _post(url: str, headers: dict, payload: Any, timeout: int = 120) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """PDF 바이트 → 순수 텍스트."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc).strip()


def split_articles_regex(text: str) -> list[dict[str, str]]:
    """정규식 기반 조문 분리. LLM 없이 동작하는 기계적 폴백.

    줄 시작에 있는 진짜 조문 헤더만 경계로 삼는다 — 본문 안의 다른 조
    참조("제12조에 따른")는 줄 중간에 나오므로 걸리지 않는다.
    """
    matches = list(_ARTICLE_HEADER_RE.finditer(text))
    out: list[dict[str, str]] = []
    for idx, m in enumerate(matches):
        header = m.group(1).strip()
        title = (m.group(2) or "").strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = re.sub(r"\s+", " ", text[start:end]).strip()
        if len(body) < 10:
            continue
        out.append({"no": header, "title": title, "text": body[:3000]})
    return _merge_article_rows(out, extract_inline_amendment_articles(text))


def _article_key(value: str) -> str:
    match = re.search(r"제\s*(\d+)\s*조(?:의\s*(\d+))?", value or "")
    if not match:
        return re.sub(r"\s+", "", value or "")
    return f"제{match.group(1)}조" + (f"의{match.group(2)}" if match.group(2) else "")


def _merge_article_rows(primary: list[dict[str, str]], recovered: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge by article number, preserving both bodies when they add evidence."""
    rows = [dict(row) for row in primary]
    positions = {_article_key(row.get("no", "")): index for index, row in enumerate(rows)}
    for row in recovered:
        key = _article_key(row.get("no", ""))
        index = positions.get(key)
        if index is None:
            positions[key] = len(rows)
            rows.append(dict(row))
            continue
        old = rows[index]
        old_text = str(old.get("text") or "")
        new_text = str(row.get("text") or "")
        if re.sub(r"\s+", "", new_text) not in re.sub(r"\s+", "", old_text):
            old["text"] = (old_text + " " + new_text).strip()[:4_000]
            old.setdefault("change_type", row.get("change_type", "개정"))
    return rows


def extract_inline_amendment_articles(text: str) -> list[dict[str, str]]:
    """Recover cost-bearing inline replacement commands in amendment bodies.

    Only the operative law-revision section (after `...일부를 다음과 같이
    개정한다` and before the addendum/comparison table) is scanned.  This keeps
    proposal summaries and current-law columns out of the evidence.
    """
    revision = _REVISION_START_RE.search(text or "")
    if not revision:
        return []
    body = (text or "")[revision.end():]
    boundaries = [
        match.start()
        for pattern in (_ADDENDUM_RE, _COMPARISON_TABLE_RE)
        if (match := pattern.search(body)) is not None
    ]
    if boundaries:
        body = body[:min(boundaries)]

    targets = list(_INLINE_AMENDMENT_TARGET_RE.finditer(body))
    rows: list[dict[str, str]] = []
    for index, target in enumerate(targets):
        end = targets[index + 1].start() if index + 1 < len(targets) else len(body)
        block = re.sub(r"\s+", " ", body[target.start():end]).strip()
        if len(block) < 20 or not re.search(r"로\s*(?:하고|한다)|신설하며|개정한다", block):
            continue
        no = _article_key(target.group("no"))
        sub = re.sub(r"\s+", "", target.group("sub") or "")
        rows.append({
            "no": no,
            "title": "",
            "text": block[:3_000],
            "change_type": "개정",
            "source": "inline_amendment_command",
            "target_subsection": sub,
        })
    return rows


_BILL_TITLE_RE = re.compile(r"([가-힣0-9·ㆍ\s]{2,60}?(?:특별법안|법률안|법안|조례안))")


def extract_bill_title(text: str) -> str:
    """의안 원문에서 의안명을 뽑는다. "제1조" 이전 구간에서 찾는다(못 찾으면 빈 문자열)."""
    first_article = _ARTICLE_HEADER_RE.search(text)
    head = text[: first_article.start()] if first_article else text[:2000]
    matches = _BILL_TITLE_RE.findall(head)
    if not matches:
        return ""
    return re.sub(r"\s+", "", max(matches, key=len))


_SPLIT_PROMPT = """아래는 한국 법령 PDF에서 추출한 텍스트야.
비용추계 대상이 되는 조문만 골라서 JSON 배열로 반환해줘.

★ 가장 중요 — 비용추계는 "변경분"만 대상이다 ★
- 이 문서가 신구조문대비표(현행 vs 개정안) 또는 일부개정안이면,
  **신설·개정(변경)된 조항만** 추출하고 **현행(기존) 조항은 제외**한다.
- 제정안(전체가 신규)이면 모든 본문 조문을 추출한다.

[제외할 것]
- 신구대비표의 현행(좌측, 변경 없는 기존) 조항
- "부 칙" 또는 "부칙" 이후 내용
- "참고 관계법령" / "별표" / "별지"

[입력 텍스트]
{text}

[출력 JSON]
{{
  "doc_type": "제정안" | "일부개정안" | "신구조문대비표" | "전부개정안",
  "articles": [
    {{"no": "제5조", "title": "지원", "text": "...", "change_type": "신설"}}
  ]
}}
"""


def _json_from_llm_text(text: str) -> Any:
    cleaned = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        starts = sorted(
            index for marker in ("{", "[") if (index := cleaned.find(marker)) >= 0
        )
        for start in starts:
            try:
                value, _ = decoder.raw_decode(cleaned, start)
                return value
            except json.JSONDecodeError:
                continue
        raise original


def _call_upstage_json(prompt: str, *, temperature: float = 0.1, timeout: int = 120) -> Any:
    if not UPSTAGE_API_KEY:
        raise RuntimeError("UPSTAGE_API_KEY 설정이 필요합니다.")
    data = _post(
        UPSTAGE_BASE_URL,
        {"Authorization": f"Bearer {UPSTAGE_API_KEY}", "Content-Type": "application/json"},
        {
            "model": UPSTAGE_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": _LLM_MAX_OUTPUT_TOKENS,
        },
        timeout=timeout,
    )
    choice = data["choices"][0]
    if str(choice.get("finish_reason") or "") == "length":
        raise RuntimeError("LLM 응답이 max_tokens 한도에서 잘렸습니다. 텍스트를 더 작게 나눠야 합니다.")
    return _json_from_llm_text(str(choice["message"]["content"]))


def split_articles(text: str) -> tuple[list[dict[str, str]], str]:
    """텍스트 → (조문 리스트, 문서유형). LLM 우선, 실패 시 정규식 폴백."""
    if len(text) < 200:
        return split_articles_regex(text), "미상"

    excerpt = text[:30000]
    try:
        parsed = _call_upstage_json(_SPLIT_PROMPT.format(text=excerpt))
    except Exception:  # noqa: BLE001
        return split_articles_regex(text), "미상"

    if not isinstance(parsed, dict):
        return split_articles_regex(text), "미상"

    doc_type = str(parsed.get("doc_type") or "미상")
    articles_raw = parsed.get("articles") or []

    # 안전장치: LLM이 조문 텍스트를 통째로 베끼지 않고 중간에서 잘라버리는 사고가
    # 실측 확인됨(2213848 제7조, 111자에서 절단 — 위원 정수 조항이 통째로 날아감).
    # 같은 조번호를 정규식으로도 뽑아서, 정규식 버전이 확연히 더 길면(LLM판이
    # 잘렸다는 신호) 정규식 버전으로 교체한다.
    regex_rows = split_articles_regex(text)
    regex_by_no = {_article_key(a["no"]): a["text"] for a in regex_rows}

    out: list[dict[str, str]] = []
    for a in articles_raw:
        if not isinstance(a, dict):
            continue
        no = str(a.get("no") or "").strip()
        title = str(a.get("title") or "").strip()
        body = str(a.get("text") or "").strip()
        change_type = str(a.get("change_type") or "").strip()
        if not no or len(body) < 5:
            continue
        regex_body = regex_by_no.get(_article_key(no), "")
        if len(regex_body) > len(body) * 1.5:
            body = regex_body
        out.append({"no": no, "title": title, "text": body, "change_type": change_type})

    return (_merge_article_rows(out, regex_rows) or regex_rows), doc_type


def main() -> None:
    from pathlib import Path

    pdf_path = Path(
        "backend/generated/subsidy_bills_diverse10/files/2213848/"
        "2213848_의사국 의안과_의안원문.pdf"
    )
    text = extract_pdf_text(pdf_path.read_bytes())
    articles, doc_type = split_articles(text)
    print(f"doc_type={doc_type}, 조문 수={len(articles)}")
    for a in articles[:3]:
        print(" -", a["no"], a["title"], "|", a["text"][:60])


if __name__ == "__main__":
    main()
