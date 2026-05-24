"""analyzer_v2.py

새 조례안 PDF/텍스트 → RAG/TAG 기반 비용추계 분석 + 추계서 자동 생성.

이전 analyzer.py는 단순 규칙 기반. v2는:
  - PyMuPDF로 PDF 텍스트 추출
  - 조문 단위 분할
  - 조문별 비용유발 분석 (Gemini)
  - Supabase 벡터 검색 (match_assembly_chunks RPC)
  - TAG 산식 패턴 매칭
  - 종합 판단 + 추계서/사유서 생성

입출력은 server.py가 사용하기 좋은 dict 형태.
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import fitz  # PyMuPDF

from .config import get_env

try:
    from .kosis_lookup import get_variable as kosis_get_variable, KOSIS_MAP, STATIC_VALUES
    _KOSIS_AVAILABLE = True
    _KOSIS_VARS = set(KOSIS_MAP.keys()) | set(STATIC_VALUES.keys())
except Exception as _exc:  # noqa: BLE001
    sys.stderr.write(f"[KOSIS 모듈 비활성화] {_exc}\n")
    _KOSIS_AVAILABLE = False
    _KOSIS_VARS = set()


COMPUTE_PROMPT = """당신은 비용추계 계산 전문가입니다. 아래 조례안의 비용 항목 산식과 KOSIS 통계값으로 5개년 추계 금액을 계산하세요.

[조례안] {bill_name}

[비용 항목 + 산식 + KOSIS 자동조회값]
{items_text}

★ 절대 규칙 ★
1. 산식에 필요한 모든 변수의 실제 값(KOSIS 또는 명시된 수치)이 있을 때만 계산
2. 변수 값이 없으면 절대 추정/가정하지 말 것 → amount_thousand=null, missing_vars에 명시
3. "100단지로 가정", "평균 50명으로 추정" 같은 임의 가정 금지
4. 물가/임금 상승률 등 KOSIS 값은 그대로 사용 (복리 계산 OK)
5. 계산 가능한 항목만 계산, 나머지는 솔직히 null + 누락 변수 보고

반드시 아래 JSON 형식으로만 응답:
{{
  "year_estimates": [
    {{"year": 1, "amount_thousand": 숫자또는null, "note": "계산 가능: 산식 + 사용값 / 계산 불가: 누락 사유",
      "missing_vars": ["부족한 변수명1", "부족한 변수명2"] 또는 []}},
    {{"year": 2, ...}},
    {{"year": 3, ...}},
    {{"year": 4, ...}},
    {{"year": 5, ...}}
  ]
}}"""


def _compute_year_estimates(estimate: dict, bill_name: str) -> list[dict] | None:
    """KOSIS 값과 산식을 이용해 연도별 금액 계산 (2차 Gemini 호출)."""
    items = estimate.get("items") or []
    if not items:
        return None

    item_lines = []
    for i, item in enumerate(items, 1):
        kosis_str = ""
        for k in item.get("kosis_lookups") or []:
            vals = ", ".join(
                f"{yv['year']}={yv['value']}{k['unit']}"
                for yv in k.get("year_values", [])
            )
            kosis_str += f"\n    · {k['variable']}: {vals}"
        item_lines.append(
            f"{i}. [{item.get('category')}] {item.get('name')}\n"
            f"   산식: {item.get('formula')}\n"
            f"   필요 변수: {', '.join(item.get('variables_needed') or [])}"
            + (f"\n   KOSIS 값:{kosis_str}" if kosis_str else "")
        )
    items_text = "\n\n".join(item_lines)

    result = gemini_json(COMPUTE_PROMPT.format(
        bill_name=bill_name, items_text=items_text,
    ), temperature=0.0)
    if not result:
        return None
    year_ests = result.get("year_estimates")
    if not isinstance(year_ests, list):
        return None
    # amount_thousand이 숫자가 아닌 경우 null로 정리
    cleaned = []
    for y in year_ests:
        if not isinstance(y, dict):
            continue
        raw = y.get("amount_thousand")
        try:
            amt = int(float(raw)) if raw is not None else None
        except (TypeError, ValueError):
            amt = None
        missing = y.get("missing_vars") or []
        if not isinstance(missing, list):
            missing = []
        cleaned.append({
            "year":            y.get("year"),
            "amount_thousand": amt,
            "note":            str(y.get("note") or ""),
            "missing_vars":    [str(v) for v in missing],
        })
    return cleaned or None


def _build_qa_report(
    estimate: dict | None,
    similar_estimates: list[dict],
    tag_patterns: list[dict],
    legal_chunks: list[dict],
) -> dict[str, Any]:
    """사용자가 보완해야 할 부분을 명시한 QA 리포트 생성.

    추정/가정 없이 '뭐가 없는지'만 사실 그대로 보고.
    """
    issues: list[dict[str, Any]] = []

    # 1) 유사 사례 신뢰도
    if similar_estimates:
        avg_sim = sum(float(s.get("similarity", 0)) for s in similar_estimates) / len(similar_estimates)
        if avg_sim < 0.5:
            issues.append({
                "level":    "warn",
                "category": "유사 사례 신뢰도 낮음",
                "detail":   f"검색된 유사 추계서 평균 유사도 {avg_sim:.0%} (50% 미만)",
                "action":   "이 의안과 유사한 추계 사례가 부족합니다. 수동 검토 필수.",
            })
    else:
        issues.append({
            "level":    "warn",
            "category": "유사 사례 없음",
            "detail":   "RAG 검색에서 유사 추계서를 찾지 못했습니다.",
            "action":   "새로운 유형의 의안일 가능성. 수동 검토 필수.",
        })

    # 2) TAG 패턴 매칭
    if not tag_patterns:
        issues.append({
            "level":    "info",
            "category": "TAG 구조 패턴 없음",
            "detail":   "유사 의안의 구조화된 산식/금액 데이터를 찾지 못했습니다.",
            "action":   "산식 자체 검토 필요.",
        })

    # 3) 법령 근거
    if not legal_chunks:
        issues.append({
            "level":    "info",
            "category": "법령 근거 검색 실패",
            "detail":   "비용추계 법령 PDF RAG가 비어있어 기본 판단 기준을 적용했습니다.",
            "action":   "법령 적용 여부 확인.",
        })

    items = (estimate or {}).get("items") or []

    # 4) KOSIS 자동 조회 불가 변수 수집
    kosis_missing: dict[str, list[str]] = {}  # 항목별
    for item in items:
        kosis_done = {k["variable"] for k in (item.get("kosis_lookups") or [])}
        for var in item.get("variables_needed") or []:
            v = str(var).strip()
            if v and v not in _KOSIS_VARS and v not in kosis_done:
                kosis_missing.setdefault(item.get("name", "?"), []).append(v)

    if kosis_missing:
        total = sum(len(v) for v in kosis_missing.values())
        issues.append({
            "level":    "warn",
            "category": f"통계청 자동조회 불가 변수 {total}개",
            "detail":   "KOSIS에 매핑되지 않은 변수가 있어 자동 조회가 안 됩니다.",
            "action":   "아래 변수의 실제 값을 직접 확인해 입력해야 합니다.",
            "items":    kosis_missing,
        })

    # 5) 계산 불가능한 연도 (missing_vars가 있거나 amount=null)
    year_ests = (estimate or {}).get("year_estimates") or []
    uncomputed = [y for y in year_ests if y.get("amount_thousand") is None]
    if uncomputed:
        # 누락 변수 통합
        all_missing: set[str] = set()
        for y in uncomputed:
            for mv in y.get("missing_vars") or []:
                all_missing.add(mv)
        issues.append({
            "level":    "error",
            "category": f"연도별 금액 계산 불가 {len(uncomputed)}/{len(year_ests)}년",
            "detail":   f"필요 변수가 부족해 계산하지 못한 연도가 있습니다.",
            "action":   "아래 누락 변수를 채우면 자동 계산 가능합니다."
                        if all_missing else "산식 자체를 점검해야 합니다.",
            "missing_vars": sorted(all_missing) if all_missing else [],
        })

    # 6) 항목별 추계서 미생성
    if not items and not (estimate is None):
        issues.append({
            "level":    "error",
            "category": "비용 항목 추출 실패",
            "detail":   "조례안에서 구체적 비용 항목을 추출하지 못했습니다.",
            "action":   "조례안 원문을 다시 확인하거나 미첨부 사유서로 전환 검토.",
        })

    # 종합 요약
    has_error = any(i["level"] == "error" for i in issues)
    has_warn  = any(i["level"] == "warn"  for i in issues)
    summary = (
        "❌ 사용자 입력/검증 필수" if has_error else
        "⚠️ 사용자 검토 권장"      if has_warn  else
        "✅ 자동 분석 완료"
    )

    return {
        "summary":    summary,
        "has_error":  has_error,
        "has_warn":   has_warn,
        "issue_count": len(issues),
        "issues":     issues,
    }


def _lookup_kosis_variables(variables_needed: list[str]) -> list[dict[str, Any]]:
    """variables_needed에서 KOSIS 매핑된 변수만 골라 최근 5년 값 조회."""
    if not _KOSIS_AVAILABLE or not variables_needed:
        return []
    current_year = datetime.now().year
    years = [str(current_year - i) for i in range(5, 0, -1)]
    results: list[dict[str, Any]] = []
    for var_name in variables_needed:
        clean_name = str(var_name).strip()
        if clean_name not in _KOSIS_VARS:
            continue
        try:
            full = kosis_get_variable(clean_name)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[KOSIS 조회 실패: {clean_name}] {exc}\n")
            continue
        if full.get("error"):
            continue
        year_values: list[dict[str, Any]] = []
        if "all" in full:
            data = full["all"]
            if isinstance(data, dict):
                for yr in years:
                    if yr in data:
                        year_values.append({"year": yr, "value": data[yr]})
            elif isinstance(data, list):
                for row in data:
                    yr = str(row.get("year", ""))
                    if yr in years and row.get("value") is not None:
                        year_values.append({"year": yr, "value": row["value"]})
        results.append({
            "variable":   clean_name,
            "unit":       full.get("unit", ""),
            "source":     full.get("source", ""),
            "year_values": year_values,
        })
    return results

GEMINI_API_KEY  = get_env("GEMINI_API_KEY")
GEMINI_MODEL    = get_env("GEMINI_MODEL", "gemini-2.5-pro")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
OPENAI_API_KEY  = get_env("OPENAI_API_KEY")
OPENAI_EMBED_MODEL = get_env("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
AZURE_EMBED_MODEL  = get_env("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
AZURE_API_VER   = "2024-02-01"
SUPA_URL        = get_env("SUPABASE_URL").rstrip("/")
SUPA_KEY        = get_env("SUPABASE_SERVICE_ROLE_KEY")
AZURE_KEY       = get_env("AZURE_OPENAI_API_KEY")
AZURE_ENDPOINT  = get_env("AZURE_OPENAI_ENDPOINT").rstrip("/")

_ARTICLE_RE = re.compile(r"제\s*\d+\s*조(?:의\d+)?(?:\s*\([^)]+\))?")


# ── HTTP 헬퍼 ─────────────────────────────────────────────────────────────────

def _post(url: str, headers: dict, payload: Any, timeout: int = 120) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else None


# ── PDF + 조문 분할 ───────────────────────────────────────────────────────────

def extract_pdf_from_b64(content_b64: str) -> str:
    pdf_bytes = base64.b64decode(content_b64)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc).strip()


def split_articles_regex(text: str) -> list[dict[str, str]]:
    """정규식 기반 폴백."""
    splits = _ARTICLE_RE.split(text)
    headers = _ARTICLE_RE.findall(text)
    out: list[dict[str, str]] = []
    for h, body in zip(headers, splits[1:]):
        clean = re.sub(r"\s+", " ", body).strip()
        if len(clean) < 10:
            continue
        out.append({"no": h.strip(), "text": clean[:1500]})
    return out


_SPLIT_PROMPT = """아래는 한국 법령/조례 PDF에서 추출한 텍스트야.
본문 조문만 골라서 JSON 배열로 반환해줘.

[제외할 것]
- 입법예고 안내문 (의견제출, 제출기한 등 행정 안내)
- "부 칙" 또는 "부칙" 이후 내용
- "참고 관계법령" / "별표" / "별지" / "참고자료"
- 조례 본문의 "주요 내용 요약" 같이 정리된 부분

[포함할 것]
- 진짜 조문만 (제1조, 제2조 등 본문 조항)
- 조 번호, 조 제목, 조 본문 텍스트

[입력 텍스트]
{text}

[출력 JSON]
{{
  "articles": [
    {{"no": "제1조", "title": "목적", "text": "이 조례는 ..."}},
    {{"no": "제2조", "title": "정의", "text": "이 조례에서 ..."}}
  ]
}}
"""


def _gemini_raw_json(prompt: str) -> Any:
    """gemini_json 과 달리 list/dict 모두 그대로 반환."""
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        data = _post(url, {"Content-Type": "application/json"}, {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        }, timeout=120)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[Gemini raw 오류] {exc}\n")
        return None


def split_articles(text: str) -> list[dict[str, str]]:
    """LLM 본문 추출 (1순위) + 정규식 폴백."""
    if len(text) < 200:
        return split_articles_regex(text)

    excerpt = text[:30000]
    try:
        parsed = _gemini_raw_json(_SPLIT_PROMPT.format(text=excerpt))
        # list 또는 {"articles": [...]} 둘 다 처리
        if isinstance(parsed, list):
            articles_raw = parsed
        elif isinstance(parsed, dict):
            articles_raw = parsed.get("articles") or []
            if not articles_raw and len(parsed) > 0:
                # 키 이름이 다를 수 있음 — 첫 list value 사용
                for v in parsed.values():
                    if isinstance(v, list):
                        articles_raw = v
                        break
        else:
            articles_raw = []

        out = []
        for a in articles_raw:
            if not isinstance(a, dict):
                continue
            no = (a.get("no") or a.get("number") or "").strip()
            title = (a.get("title") or "").strip()
            body = (a.get("text") or a.get("content") or "").strip()
            if not no or len(body) < 5:
                continue
            label = f"{no}({title})" if title else no
            out.append({"no": label, "text": body[:1500]})
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[LLM 조문 분할 실패, 정규식 폴백] {exc}\n")

    return split_articles_regex(text)


# ── 임베딩 + 벡터 검색 ─────────────────────────────────────────────────────────

def embed_openai(text: str) -> list[float]:
    data = _post(
        "https://api.openai.com/v1/embeddings",
        {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        {
            "model": OPENAI_EMBED_MODEL,
            "input": text,
        },
    )
    return data["data"][0]["embedding"]


def embed_azure(text: str) -> list[float]:
    url = (f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_EMBED_MODEL}"
           f"/embeddings?api-version={AZURE_API_VER}")
    data = _post(url, {
        "api-key": AZURE_KEY, "Content-Type": "application/json",
    }, {"input": [text]})
    return data["data"][0]["embedding"]


def embed(text: str) -> list[float]:
    if OPENAI_API_KEY:
        return embed_openai(text)
    if AZURE_KEY and AZURE_ENDPOINT:
        return embed_azure(text)
    raise RuntimeError("OPENAI_API_KEY 또는 Azure OpenAI 임베딩 설정이 필요합니다.")


def try_embed(text: str) -> list[float] | None:
    """임베딩은 RAG 보조 기능이다. 실패해도 Gemini 분석은 계속 진행한다."""
    try:
        return embed(text)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[embedding 비활성화] {exc}\n")
        return None


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """여러 텍스트를 한 번에 임베딩. OpenAI는 input list 받음 → 호출 1번."""
    if not texts:
        return []
    try:
        if OPENAI_API_KEY:
            data = _post(
                "https://api.openai.com/v1/embeddings",
                {
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                {"model": OPENAI_EMBED_MODEL, "input": texts},
            )
            ordered = sorted(data["data"], key=lambda x: x["index"])
            return [it["embedding"] for it in ordered]
        # Azure 폴백 — 1건씩 (Azure는 input list 지원 다름)
        return [try_embed(t) for t in texts]
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[embed_batch 실패, 1건씩 폴백] {exc}\n")
        return [try_embed(t) for t in texts]


def vector_search(emb: list[float], source: str | None = None,
                  doc_type: str | None = None, k: int = 5) -> list[dict]:
    """match_assembly_chunks RPC 호출 (Supabase에 등록된 함수)."""
    url = f"{SUPA_URL}/rest/v1/rpc/match_assembly_chunks"
    headers = {
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "query_embedding": emb, "match_count": k,
        "filter_source": source, "filter_doc_type": doc_type,
    }
    try:
        return _post(url, headers, payload, timeout=30) or []
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[vector_search 실패] {e}: {e.read().decode('utf-8','ignore')[:200]}\n")
        return []


def fetch_tag_patterns(bill_ids: list[str], limit: int = 3) -> list[dict]:
    """유사 의안의 TAG 구조화 데이터(structures+items+variables+amounts) 조회."""
    if not bill_ids:
        return []
    bill_ids = bill_ids[:limit]
    headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}

    def _get(table: str, params: str) -> list[dict]:
        url = f"{SUPA_URL}/rest/v1/{table}?{params}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[fetch_tag {table} 실패] {exc}\n")
            return []

    ids_csv = ",".join(bill_ids)
    structures = _get(
        "cost_estimate_structures",
        f"select=id,bill_id,bill_no,bill_name&bill_id=in.({urllib.parse.quote(ids_csv)})",
    )
    if not structures:
        return []

    struct_ids = [str(s["id"]) for s in structures]
    items = _get(
        "cost_estimate_items",
        f"select=id,structure_id,item_category,item_name,trigger_ref"
        f"&structure_id=in.({','.join(struct_ids)})&order=structure_id,item_order",
    )
    item_ids = [str(i["id"]) for i in items]
    variables = _get(
        "cost_estimate_variables",
        f"select=item_id,variable_type,variable_name,variable_value,variable_unit"
        f"&item_id=in.({','.join(item_ids)})",
    ) if item_ids else []
    amounts = _get(
        "cost_estimate_amounts",
        f"select=item_id,year_label,year_offset,amount_thousand,formula_text,is_total"
        f"&item_id=in.({','.join(item_ids)})&order=item_id,year_offset",
    ) if item_ids else []

    # 결합
    by_item: dict[int, dict] = {}
    for it in items:
        by_item[it["id"]] = {
            "category":    it["item_category"],
            "name":        it["item_name"],
            "trigger_ref": it["trigger_ref"],
            "variables":   [],
            "amounts":     [],
        }
    for v in variables:
        if v["item_id"] in by_item:
            by_item[v["item_id"]]["variables"].append({
                "name":  v["variable_name"],
                "value": v["variable_value"],
                "unit":  v["variable_unit"],
            })
    for a in amounts:
        if a["item_id"] in by_item:
            by_item[a["item_id"]]["amounts"].append({
                "year_label":      a["year_label"],
                "amount_thousand": a["amount_thousand"],
                "formula":         a["formula_text"],
                "is_total":        a["is_total"],
            })

    by_struct: dict[int, list[dict]] = {}
    for iid, item_data in by_item.items():
        sid = next((i["structure_id"] for i in items if i["id"] == iid), None)
        if sid is not None:
            by_struct.setdefault(sid, []).append(item_data)

    out = []
    for s in structures:
        out.append({
            "bill_no":   s["bill_no"],
            "bill_name": s["bill_name"],
            "items":     by_struct.get(s["id"], []),
        })
    return out


def format_tag_patterns(patterns: list[dict]) -> str:
    """TAG 패턴을 Gemini 프롬프트용 텍스트로 포맷."""
    if not patterns:
        return "(유사 의안의 TAG 구조 패턴 없음)"
    blocks = []
    for p in patterns:
        item_lines = []
        for it in p.get("items", [])[:5]:
            amt = next((a for a in it["amounts"] if a.get("is_total")), None) or \
                  (it["amounts"][0] if it["amounts"] else None)
            amt_str = f"{amt['amount_thousand']:,}천원" if amt and amt.get("amount_thousand") else "-"
            formula = (amt.get("formula") if amt else "") or "-"
            vars_str = ", ".join(
                f"{v['name']}({v['value']}{v['unit']})" if v.get("value") else v["name"]
                for v in it["variables"][:3]
            )
            item_lines.append(
                f"  - [{it['category']}] {it['name']} (근거: {it['trigger_ref']})"
                f"\n    산식: {formula}  |  기준금액: {amt_str}"
                f"\n    변수: {vars_str}"
            )
        blocks.append(
            f"▶ {p['bill_no']} {p['bill_name'][:40]}\n"
            + "\n".join(item_lines)
        )
    return "\n\n".join(blocks)


# ── Gemini ────────────────────────────────────────────────────────────────────

def gemini_json(prompt: str, temperature: float = 0.1) -> dict | None:
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        data = _post(url, {"Content-Type": "application/json"}, {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": temperature,
            },
        }, timeout=180)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if isinstance(parsed, list):
            parsed = next((x for x in parsed if isinstance(x, dict)), None)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        sys.stderr.write(f"[Gemini 오류] {exc}\n")
        return None


# ── 프롬프트 ──────────────────────────────────────────────────────────────────

ARTICLE_PROMPT = """당신은 지방조례안의 비용유발 여부를 판단하는 전문가입니다.

[조문]
{article_text}

[판단 기준 (법령 PDF 발췌)]
{legal_ref}

다음 JSON으로 답하세요:
{{
  "cost_trigger": true 또는 false,
  "trigger_type": "직접지원|위탁대행|시설구축|조직설치|대상확대|의무부과|없음",
  "obligation_strength": "mandatory|semi_mandatory|discretionary|aspirational",
  "reason": "왜 이렇게 판단했는지 한 줄"
}}
"""

FINAL_PROMPT = """당신은 지방의회 비용추계 전문가입니다. 새 조례안에 대해 종합 판단하세요.

[조례안명] {bill_name}

[조문별 비용유발 분석]
{articles_summary}

[유사 비용추계서 사례 (RAG)]
{similar_estimates}

[유사 의안의 비용추계 구조 패턴 (TAG: 항목/산식/변수/금액)]
{tag_patterns}

[유사 미첨부사유 사례 (RAG)]
{similar_non_attach}

[비용추계 법령 기준 (RAG)]
{legal_ref}

[KOSIS 자동 조회 가능 변수 (variables_needed에 정확히 이 이름으로 넣으면 시스템이 자동으로 통계값을 채워줍니다)]
- "소비자물가상승률" (KOSIS 연도별 %)
- "명목임금상승률" (KOSIS 연도별 %)
- "공무원임금상승률" (인사혁신처 고시 %)
- "주민등록인구" (KOSIS 연도별 명)
- "65세이상인구" (KOSIS 연도별 명)
- "등록장애인수" (KOSIS 연도별 명)
- "기초생활수급자수" (KOSIS 연도별 명)

다음 JSON으로 답하세요:
{{
  "verdict": "추계필요" | "미첨부_A" | "미첨부_B" | "미첨부_C",
  "verdict_label": "추계 필요" | "비용 없음(A)" | "추계 곤란(B)" | "기존예산 흡수(C)",
  "reason_summary": "종합 판단 2~3문장",
  "confidence": 0.0~1.0,
  "if_needs_estimate": {{
    "items": [
      {{
        "name": "항목명",
        "category": "인건비|운영비|사업비|지원금|위탁비",
        "formula": "산식 텍스트",
        "trigger_ref": "근거 조문",
        "variables_needed": ["대상자 수", "단가", "소비자물가상승률", ...]
      }}
    ],
    "year_estimates": [
      {{"year": 1, "amount_thousand": 숫자 또는 null, "note": "..."}}
    ]
  }} 또는 null,
  "if_non_attachment": {{
    "type": "A|B|C",
    "reason_text": "미첨부 사유 텍스트"
  }} 또는 null
}}
"""


# ── 메인 분석 함수 ─────────────────────────────────────────────────────────────

def analyze_v2(filename: str, content_b64: str) -> dict[str, Any]:
    """server.py가 호출하는 진입점. 입력: 파일명 + base64 PDF. 출력: 결과 dict."""
    t0 = time.time()

    # 1) PDF 추출
    text = extract_pdf_from_b64(content_b64)
    if not text:
        raise ValueError("PDF에서 텍스트를 추출하지 못했습니다.")
    articles = split_articles(text)
    if not articles:
        raise ValueError("조문이 탐지되지 않았습니다.")

    # 조례안명 추출 (PDF 첫 줄에서)
    first_lines = text[:500].split("\n")
    bill_name = next((l.strip() for l in first_lines if len(l.strip()) > 5), filename)

    # 2) 법령 PDF (legal_reference) RAG
    legal_query = "비용추계 미첨부 가능 기준 정의 규정 선언적 권고적"
    leg_emb = try_embed(legal_query)
    legal_chunks = vector_search(leg_emb, source="legal_reference", k=4) if leg_emb else []
    legal_ref = "\n---\n".join((c.get("content") or "")[:800] for c in legal_chunks)
    if not legal_ref:
        legal_ref = (
            "비용추계 판단 기준: 재정 지출 또는 수입 감소를 수반하는 조항은 추계 대상이다. "
            "직접 지원, 보조금, 위탁, 시설 설치, 조직 신설, 인력 배치, 대상 확대, 의무적 사업 수행은 "
            "비용유발 가능성이 높다. 정의·목적·선언적 규정, 단순 명칭 변경, 기존 제도 범위 내 정리는 "
            "비용 미수반 가능성이 있다. 대상자·단가·시행 여부가 불확정하면 미첨부 B, 기존 예산으로 "
            "흡수 가능하면 미첨부 C로 검토한다."
        )

    # 3) 조문별 처리 — 임베딩 배치 + 조문 분석 병렬
    arts = articles[:12]

    # 3-A. 임베딩: 모든 조문 + 전체 본문을 한 번에 (호출 1번)
    full_q = "\n".join(a["text"] for a in articles[:5])[:6000]
    emb_inputs = [a["text"][:2000] for a in arts] + [full_q]
    emb_results = embed_batch(emb_inputs)
    art_embs   = emb_results[:-1]
    bill_emb   = emb_results[-1] if emb_results else None

    # 3-B. 조문별 처리 함수 (각 worker 안에서 vector_search 2번 + gemini 1번)
    def process_article(idx: int, art: dict[str, str], art_emb: list[float] | None) -> dict[str, Any]:
        art_legal = vector_search(art_emb, source="legal_reference", k=2) if art_emb else []
        art_similar = (
            vector_search(art_emb, source="national_assembly", doc_type="cost_estimate", k=2)
            if art_emb else []
        )
        prompt = ARTICLE_PROMPT.format(
            article_text=art["text"], legal_ref=legal_ref[:2000],
        )
        result = gemini_json(prompt) or {}
        return {
            "_idx": idx,
            **art,
            "cost_trigger": bool(result.get("cost_trigger", False)),
            "trigger_type": result.get("trigger_type", "없음"),
            "obligation_strength": result.get("obligation_strength", "aspirational"),
            "reason": result.get("reason", ""),
            "legal_refs": [
                {
                    "chunk_id":   c.get("chunk_id"),
                    "similarity": round(float(c.get("similarity", 0)), 3),
                    "content":    (c.get("content") or "")[:2000],
                }
                for c in art_legal
            ],
            "similar_refs": [
                {
                    "bill_id":   c.get("bill_id"),
                    "bill_no":   c.get("bill_no"),
                    "bill_name": c.get("bill_name"),
                    "similarity": round(float(c.get("similarity", 0)), 3),
                    "content":   (c.get("content") or "")[:2000],
                }
                for c in art_similar
            ],
        }

    # 3-C. 병렬 실행 (Gemini RPM 고려해 동시 6개)
    article_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(process_article, i, art, art_embs[i] if i < len(art_embs) else None)
            for i, art in enumerate(arts)
        ]
        for fut in as_completed(futures):
            article_results.append(fut.result())
    # 원래 순서 복원
    article_results.sort(key=lambda x: x["_idx"])
    for r in article_results:
        r.pop("_idx", None)

    # 4) 본문 임베딩으로 유사 RAG 검색 (위에서 이미 계산됨)
    similar_estimates = (
        vector_search(bill_emb, source="national_assembly", doc_type="cost_estimate", k=5)
        if bill_emb else []
    )
    similar_non_attach = (
        vector_search(bill_emb, source="national_assembly", doc_type="non_attachment_reason", k=3)
        if bill_emb else []
    )

    # 5) 종합 판단 + 추계서 생성
    articles_summary = "\n".join(
        f"{a['no']} | cost_trigger={a['cost_trigger']} | "
        f"type={a['trigger_type']} | strength={a['obligation_strength']} | "
        f"reason={a['reason'][:80]}"
        for a in article_results
    )
    similar_est_text = "\n---\n".join(
        f"[{s.get('bill_no')} {s.get('bill_name','')[:40]}]\n{(s.get('content') or '')[:600]}"
        for s in similar_estimates[:3]
    )
    similar_na_text = "\n---\n".join(
        f"[{s.get('bill_no')} {s.get('bill_name','')[:40]}]\n{(s.get('content') or '')[:400]}"
        for s in similar_non_attach[:2]
    )
    # 5-0) 유사 의안 TAG 구조 패턴 조회
    similar_bill_ids = [s.get("bill_id") for s in similar_estimates[:3] if s.get("bill_id")]
    tag_patterns = fetch_tag_patterns(similar_bill_ids, limit=3)
    tag_patterns_text = format_tag_patterns(tag_patterns)

    final = gemini_json(FINAL_PROMPT.format(
        bill_name=bill_name,
        articles_summary=articles_summary,
        similar_estimates=similar_est_text or "(없음)",
        tag_patterns=tag_patterns_text,
        similar_non_attach=similar_na_text or "(없음)",
        legal_ref=legal_ref[:2000],
    )) or {}

    # 5-1) KOSIS 변수값 자동 채우기
    estimate = final.get("if_needs_estimate")
    if estimate and estimate.get("items"):
        for item in estimate["items"]:
            kosis_results = _lookup_kosis_variables(item.get("variables_needed", []))
            if kosis_results:
                item["kosis_lookups"] = kosis_results

    # 5-2) KOSIS 값으로 연도별 금액 자동 계산
    if estimate and estimate.get("items"):
        calculated = _compute_year_estimates(estimate, bill_name)
        if calculated:
            estimate["year_estimates"] = calculated

    # 5-3) QA 리포트 — 무엇이 부족한지 사용자에게 명시
    qa_report = _build_qa_report(
        estimate=estimate,
        similar_estimates=similar_estimates,
        tag_patterns=tag_patterns,
        legal_chunks=legal_chunks,
    )

    # 6) 응답 조립
    return {
        "filename":     filename,
        "billName":     bill_name,
        "generatedAt":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsedSec":   round(time.time() - t0, 1),
        "totalArticles": len(articles),
        "analyzedArticles": len(article_results),

        "articles": article_results,

        "verdict": {
            "type":        final.get("verdict", "unknown"),
            "label":       final.get("verdict_label", "판단 불가"),
            "summary":     final.get("reason_summary", ""),
            "confidence":  float(final.get("confidence", 0.0)),
        },

        "estimate":      final.get("if_needs_estimate"),
        "nonAttachment": final.get("if_non_attachment"),
        "qaReport":      qa_report,

        "references": {
            "similar_bills_cost_estimate": [
                {
                    "bill_id":    s.get("bill_id"),
                    "bill_no":    s.get("bill_no"),
                    "bill_name":  s.get("bill_name"),
                    "similarity": round(float(s.get("similarity", 0)), 3),
                    "content":    (s.get("content") or "")[:2000],
                }
                for s in similar_estimates
            ],
            "similar_bills_non_attachment": [
                {
                    "bill_id":    s.get("bill_id"),
                    "bill_no":    s.get("bill_no"),
                    "bill_name":  s.get("bill_name"),
                    "similarity": round(float(s.get("similarity", 0)), 3),
                    "content":    (s.get("content") or "")[:2000],
                }
                for s in similar_non_attach
            ],
            "legal_references": [
                {
                    "chunk_id":   c.get("chunk_id"),
                    "similarity": round(float(c.get("similarity", 0)), 3),
                    "content":    (c.get("content") or "")[:2000],
                }
                for c in legal_chunks
            ],
        },
    }
