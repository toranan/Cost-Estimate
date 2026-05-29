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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .calculator import compute_year_estimates
from .config import PROJECT_ROOT, SCRIPT_DIR, get_env

try:
    from .kosis_lookup import get_variable as kosis_get_variable, KOSIS_MAP, STATIC_VALUES
    _KOSIS_AVAILABLE = True
    _KOSIS_VARS = set(KOSIS_MAP.keys()) | set(STATIC_VALUES.keys())
except Exception as _exc:  # noqa: BLE001
    sys.stderr.write(f"[KOSIS 모듈 비활성화] {_exc}\n")
    _KOSIS_AVAILABLE = False
    _KOSIS_VARS = set()


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


def _refresh_qa_summary(report: dict[str, Any]) -> dict[str, Any]:
    issues = report.get("issues") or []
    has_error = any(i.get("level") == "error" for i in issues)
    has_warn = any(i.get("level") == "warn" for i in issues)
    report["has_error"] = has_error
    report["has_warn"] = has_warn
    report["issue_count"] = len(issues)
    report["summary"] = (
        "❌ 사용자 입력/검증 필수" if has_error else
        "⚠️ 사용자 검토 권장" if has_warn else
        "✅ 자동 분석 완료"
    )
    return report


def _prepend_qa_issue(report: dict[str, Any], issue: dict[str, Any]) -> None:
    report.setdefault("issues", [])
    report["issues"].insert(0, issue)
    _refresh_qa_summary(report)


def _missing_formula_variables(estimate: dict | None) -> dict[str, list[str]]:
    """KOSIS/정적 값으로 자동 충족되지 않은 산식 변수를 항목별로 반환."""
    if not estimate:
        return {}
    missing: dict[str, list[str]] = {}
    for item in estimate.get("items") or []:
        calc = item.get("calculation") or {}
        if isinstance(calc, dict) and calc.get("base_amount_thousand") is not None:
            continue
        item_name = str(item.get("name") or "?")
        looked_up = {str(k.get("variable")) for k in item.get("kosis_lookups") or []}
        for raw_var in item.get("variables_needed") or []:
            var = str(raw_var).strip()
            if not var:
                continue
            if var in looked_up:
                continue
            missing.setdefault(item_name, []).append(var)
    return missing


def _review_variables(estimate: dict | None) -> dict[str, list[str]]:
    if not estimate:
        return {}
    out: dict[str, list[str]] = {}
    for item in estimate.get("items") or []:
        if not item.get("requires_review"):
            continue
        vars_needed = [str(v) for v in item.get("variables_needed") or [] if str(v).strip()]
        if vars_needed:
            out[str(item.get("name") or "?")] = vars_needed
    return out


def _blocked_year_estimates(missing_by_item: dict[str, list[str]]) -> list[dict[str, Any]]:
    missing = sorted({v for values in missing_by_item.values() for v in values})
    note = "계산 불가: 필수 변수 누락" + (f" ({', '.join(missing[:6])})" if missing else "")
    return [
        {
            "year": year,
            "amount_thousand": None,
            "note": note,
            "missing_vars": missing,
        }
        for year in range(1, 6)
    ]


def _cap_confidence(value: Any, cap: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return min(confidence, cap)


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

ANALYZE_MAX_ARTICLES = int(get_env("ANALYZE_MAX_ARTICLES", "0") or "0")
ARTICLE_WORKERS = max(1, int(get_env("ANALYZE_ARTICLE_WORKERS", "6") or "6"))
MIN_AVG_SIMILARITY = float(get_env("MIN_AVG_SIMILARITY", "0.45") or "0.45")


# ── HTTP 헬퍼 ─────────────────────────────────────────────────────────────────

def _post(url: str, headers: dict, payload: Any, timeout: int = 120) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else None


# ── PDF + 조문 분할 ───────────────────────────────────────────────────────────

def _strip_data_url(content_b64: str) -> str:
    if "," in content_b64 and content_b64.startswith("data:"):
        return content_b64.split(",", 1)[1]
    return content_b64


def _extract_pdf_with_pdfkit(pdf_bytes: bytes) -> str:
    """Fallback for PDFs where PyMuPDF misses text but macOS PDFKit can read it."""
    swift_script = SCRIPT_DIR / "extract_pdf_text.swift"
    if not swift_script.exists():
        return ""

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    environment = {
        "HOME": "/tmp/swift-home",
        "CLANG_MODULE_CACHE_PATH": "/tmp/swift-module-cache",
    }
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(environment["CLANG_MODULE_CACHE_PATH"]).mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["swift", str(swift_script), str(tmp_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[PDFKit 추출 실패] {exc}\n")
        return ""
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if completed.returncode != 0:
        sys.stderr.write(f"[PDFKit 추출 실패] {completed.stderr.strip()[:200]}\n")
        return ""

    text = re.sub(r"<<<PAGE:\d+>>>", "\n", completed.stdout)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_pdf_from_b64(content_b64: str) -> str:
    pdf_bytes = base64.b64decode(_strip_data_url(content_b64))
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = "\n".join(p.get_text() for p in doc).strip()
    if text:
        return text
    return _extract_pdf_with_pdfkit(pdf_bytes)


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
비용추계 대상이 되는 조문만 골라서 JSON 배열로 반환해줘.

★ 가장 중요 — 비용추계는 "변경분"만 대상이다 ★
- 이 문서가 신구조문대비표(현행 vs 개정안) 또는 일부개정안이면,
  **신설·개정(변경)된 조항만** 추출하고 **현행(기존) 조항은 제외**한다.
- 현행 조례는 이미 시행 중이라 추가 비용이 없으므로 비용추계 대상이 아니다.
- "(신설)", "신설", "개정", "<신·구조문대비표>", "현행 | 개정안" 같은 표시를 단서로 사용.
- 제정안(전체가 신규)이면 모든 본문 조문을 추출한다.

[change_type 판정]
- "신설": 현행에 없던 조항이 새로 생김
- "개정": 기존 조항의 내용/금액이 바뀜
- "삭제": 기존 조항이 없어짐
- "제정": 제정안이라 전체가 신규

[제외할 것]
- 신구대비표의 현행(좌측, 변경 없는 기존) 조항
- 입법예고 안내문 (의견제출, 제출기한 등 행정 안내)
- "부 칙" 또는 "부칙" 이후 내용
- "참고 관계법령" / "별표" / "별지" / "참고자료"
- "주요 내용 요약" 같이 정리된 부분

[포함할 것]
- 신설·개정·삭제된 조항 (제정안이면 전체 본문 조항)
- 조 번호, 조 제목, 조 본문 텍스트, 변경 유형

[입력 텍스트]
{text}

[출력 JSON]
{{
  "doc_type": "제정안" | "일부개정안" | "신구조문대비표" | "전부개정안",
  "articles": [
    {{"no": "제5조", "title": "지원", "text": "...", "change_type": "신설"}},
    {{"no": "제6조", "title": "관리비", "text": "...", "change_type": "개정"}}
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


def split_articles(text: str) -> tuple[list[dict[str, str]], str]:
    """LLM 본문 추출 (1순위) + 정규식 폴백.

    반환: (조문 리스트, 문서유형). 신구대비표/개정안이면 변경 조항만 포함.
    """
    if len(text) < 200:
        return split_articles_regex(text), "미상"

    excerpt = text[:30000]
    try:
        parsed = _gemini_raw_json(_SPLIT_PROMPT.format(text=excerpt))
        doc_type = "미상"
        # list 또는 {"articles": [...]} 둘 다 처리
        if isinstance(parsed, list):
            articles_raw = parsed
        elif isinstance(parsed, dict):
            doc_type = parsed.get("doc_type") or "미상"
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
            change_type = (a.get("change_type") or "").strip()
            if not no or len(body) < 5:
                continue
            label = f"{no}({title})" if title else no
            out.append({
                "no": label,
                "text": body[:1500],
                "change_type": change_type or "미상",
            })
        if out:
            return out, doc_type
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[LLM 조문 분할 실패, 정규식 폴백] {exc}\n")

    return split_articles_regex(text), "미상"


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
    if not OPENAI_API_KEY and not (AZURE_KEY and AZURE_ENDPOINT):
        sys.stderr.write("[embed_batch 비활성화] 임베딩 API 설정이 없습니다.\n")
        return [None] * len(texts)
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
                  doc_type: str | None = None, k: int = 5,
                  bill_id_filter: str | None = None) -> list[dict]:
    """match_assembly_chunks RPC 호출 (Supabase에 등록된 함수).

    bill_id_filter가 있으면 RPC 결과 후 클라이언트 사이드 필터링.
    RPC 함수 시그니처를 바꾸지 않기 위해 더 많이 받고 필터링.
    """
    url = f"{SUPA_URL}/rest/v1/rpc/match_assembly_chunks"
    headers = {
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
    }
    # bill_id_filter 있으면 더 많이 받고 필터링 (RPC 시그니처 변경 회피)
    fetch_k = k * 5 if bill_id_filter else k
    payload = {
        "query_embedding": emb, "match_count": fetch_k,
        "filter_source": source, "filter_doc_type": doc_type,
    }
    try:
        results = _post(url, headers, payload, timeout=30) or []
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[vector_search 실패] {e}: {e.read().decode('utf-8','ignore')[:200]}\n")
        return []
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[vector_search 실패] {exc}\n")
        return []

    if bill_id_filter:
        results = [r for r in results if r.get("bill_id") == bill_id_filter]
        return results[:k]
    return results


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

FINAL_PROMPT = """당신은 지방의회 비용추계 전문가입니다. 새 조례안에 대해 NABO(국회예산정책처) 공식 기준에 따라 종합 판단하세요.

[조례안명] {bill_name}
[감지된 분야] {field}

[조문별 비용유발 분석]
{articles_summary}

━━━ NABO 공식 분류 기준 (반드시 이 기준으로 판단) ━━━

verdict 값은 아래 5개 중 하나여야 합니다:

1. "추계서"
   - 법안 시행 시 직접적 재정지출 순증가 또는 재정수입 순증감 발생
   - 예상비용 연평균 10억원 이상 또는 한시적 비용 총 30억원 이상

2. "미첨부_1호" — 소요비용이 적어 재정 영향 미미
   - 예상비용 연평균 10억원 미만
   - 한시적 비용으로서 총 30억원 미만

3. "미첨부_2호" — 국가안전보장·군사기밀 관련

4. "미첨부_3호" — 추계 기술적 곤란
   - 조항이 선언적·권고적 형식
   - 구체적 내용이 시행령 등에 위임됨
   - 유사사례·관련 자료 부족

5. "미대상" — 재정규모 변화 없음
   - 정의 조항, 명칭 변경, 절차 정비 등

━━━ 참조 자료 (목적별) ━━━

[NABO 분류 기준 (Part I)]
{classification_refs}

[비용추계 방법론 (LEGAL_REF)]
{methodology_refs}

[NABO 분야별 실제 사례 (Part II — {field} 분야)]
{nabo_cases}

[유사 비용추계서 사례 (의안 RAG)]
{similar_estimates}

[유사 의안의 비용추계 구조 패턴 (TAG)]
{tag_patterns}

[유사 미첨부사유 사례]
{similar_non_attach}

━━━ KOSIS 자동 조회 가능 변수 (variables_needed에 정확히 이 이름으로 넣으면 자동 조회) ━━━
- "소비자물가상승률" (KOSIS 연도별 %)
- "명목임금상승률" (KOSIS 연도별 %)
- "공무원임금상승률" (인사혁신처 고시 %)
- "주민등록인구" (KOSIS 연도별 명)
- "65세이상인구" (KOSIS 연도별 명)
- "등록장애인수" (KOSIS 연도별 명)
- "기초생활수급자수" (KOSIS 연도별 명)

━━━ 출력 JSON 형식 ━━━
{{
  "verdict": "추계서" | "미첨부_1호" | "미첨부_2호" | "미첨부_3호" | "미대상",
  "verdict_label": "비용추계서" | "미첨부 1호 (비용 미미)" | "미첨부 2호 (안보·기밀)" | "미첨부 3호 (기술적 곤란)" | "미대상 (재정변화 없음)",
  "verdict_reason_nabo": "NABO 기준 중 어느 항목에 해당하는지 한 줄 (예: '예상비용 연평균 5억원으로 10억원 미만 → 미첨부 1호')",
  "reason_summary": "종합 판단 2~3문장",
  "confidence": 0.0~1.0,
  "if_needs_estimate": {{
    "items": [
      {{
        "name": "항목명",
        "category": "인건비|운영비|사업비|지원금|위탁비",
        "formula": "산식 텍스트",
        "trigger_ref": "근거 조문",
        "variables_needed": ["대상자 수", "단가", "소비자물가상승률", ...],
        "calculation": {{
          "base_amount_thousand": 숫자 또는 null,
          "recurrence": "annual" | "one_time" | "unknown",
          "start_year": 1,
          "end_year": 5,
          "growth_variable": "소비자물가상승률" 또는 null,
          "source_note": "base_amount_thousand를 둔 근거. TAG/RAG/조문에 명시된 숫자가 없으면 null"
        }}
      }}
    ],
    "year_estimates": [
      {{"year": 1, "amount_thousand": null, "note": "금액은 시스템 Python 계산기가 산출하므로 null"}}
    ]
  }} 또는 null,
  "if_non_attachment": {{
    "type": "1호|2호|3호|미대상",
    "reason_text": "미첨부 사유 텍스트 (NABO 기준 명시)"
  }} 또는 null
}}

━━━ 중요 ━━━
- verdict는 NABO 5개 분류 중 정확히 하나여야 한다.
- 금액 계산은 하지 마라. year_estimates.amount_thousand는 null로 둔다.
- calculation.base_amount_thousand는 RAG/TAG/조문에 명시된 천원 단위 금액을 확인할 수 있을 때만 넣는다.
- 대상자 수, 단가, 횟수 등 필수 숫자가 불명확하면 base_amount_thousand는 반드시 null이다.
- verdict_reason_nabo는 반드시 NABO 분류 5개 중 어디에 해당하는지 명시한다.
"""

# ── 분야 자동 분류 ───────────────────────────────────────────────────────────

FIELD_DETECT_PROMPT = """다음 조례안 본문을 보고 NABO 공식 분야 분류 중 어디에 해당하는지 판단하세요.

[조례안 본문 (앞부분)]
{text}

분류 (정확히 하나만 선택):
1. "보건복지" - 의료, 보육, 노인, 장애인, 한부모, 사회복지
2. "산업농업" - 산업, 농업, 어업, 국토, 교통, 건설
3. "교육과학" - 교육, 과학, 문화, 여성가족, 청소년
4. "환경노동" - 환경, 에너지, 노동, 일자리
5. "국방보훈" - 국방, 보훈, 법제사법
6. "안전행정" - 안전, 재난, 행정, 자치
7. "세입" - 조세, 지방세, 부담금

JSON: {{"field": "선택한 분야명", "confidence": 0.0~1.0, "reason": "한 줄"}}
"""


def detect_field(text: str) -> dict:
    """조례안 본문에서 NABO 6+1 분야 자동 분류."""
    parsed = gemini_json(FIELD_DETECT_PROMPT.format(text=text[:3000]), temperature=0.0)
    if not parsed or "field" not in parsed:
        return {"field": "기타", "confidence": 0.0, "reason": "분야 분류 실패"}
    return parsed


# ── NABO 금액 게이트 검증 ────────────────────────────────────────────────────

def _validate_verdict_with_amount(verdict: str, year_estimates: list[dict] | None) -> tuple[str, str | None]:
    """NABO 금액 기준(10억/30억)으로 verdict 자동 검증/보정.

    Returns: (corrected_verdict, correction_note 또는 None)
    """
    if not year_estimates:
        return verdict, None
    amounts = [
        int(y["amount_thousand"]) for y in year_estimates
        if y.get("amount_thousand") is not None
    ]
    if not amounts:
        return verdict, None

    annual_avg = sum(amounts) / len(amounts) / 1000  # 백만원
    total = sum(amounts) / 1000  # 백만원
    THRESHOLD_ANNUAL = 1_000_000  # 10억원 = 1,000,000천원
    THRESHOLD_TOTAL = 3_000_000   # 30억원 = 3,000,000천원

    avg_thousand = sum(amounts) / len(amounts)
    total_thousand = sum(amounts)

    is_minor = (
        avg_thousand < THRESHOLD_ANNUAL
        and total_thousand < THRESHOLD_TOTAL
    )

    if verdict == "추계서" and is_minor:
        return "미첨부_1호", (
            f"Python 게이트: 계산된 연평균 {avg_thousand/1000:.1f}백만원, "
            f"총 {total_thousand/1000:.1f}백만원 → NABO 미첨부 1호 기준(연 10억/총 30억 미만) 충족 → 강제 변경"
        )
    if verdict == "미첨부_1호" and not is_minor:
        return "추계서", (
            f"Python 게이트: 계산된 연평균 {avg_thousand/1000:.1f}백만원, "
            f"총 {total_thousand/1000:.1f}백만원 → NABO 미첨부 1호 기준 초과 → 추계서로 강제 변경"
        )
    return verdict, None


# ── 메인 분석 함수 ─────────────────────────────────────────────────────────────

def analyze_v2(filename: str, content_b64: str) -> dict[str, Any]:
    """server.py가 호출하는 진입점. 입력: 파일명 + base64 PDF. 출력: 결과 dict."""
    t0 = time.time()
    workflow_issues: list[dict[str, Any]] = []

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY 설정이 필요합니다.")

    # 1) PDF 추출
    text = extract_pdf_from_b64(content_b64)
    if not text:
        raise ValueError("PDF에서 텍스트를 추출하지 못했습니다. (스캔본이면 OCR 필요)")
    articles, doc_type = split_articles(text)
    if not articles:
        raise ValueError("조문이 탐지되지 않았습니다.")

    # 개정안/신구대비표면 "변경분만 분석됨" 안내
    if doc_type in ("일부개정안", "신구조문대비표", "전부개정안"):
        workflow_issues.append({
            "level": "info",
            "category": f"문서 유형: {doc_type}",
            "detail": f"{doc_type}이므로 신설·개정된 조항만 비용추계 대상으로 분석했습니다. (현행 조항 제외)",
            "action": "변경분 기준 추가 재정소요만 산정됩니다.",
        })

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
    if ANALYZE_MAX_ARTICLES > 0:
        arts = articles[:ANALYZE_MAX_ARTICLES]
        if len(articles) > len(arts):
            workflow_issues.append({
                "level": "warn",
                "category": "일부 조문 미분석",
                "detail": f"전체 {len(articles)}개 중 {len(arts)}개 조문만 분석했습니다.",
                "action": "ANALYZE_MAX_ARTICLES 설정을 높이거나 전체 조문 분석으로 재실행해야 합니다.",
            })
    else:
        arts = articles

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
    with ThreadPoolExecutor(max_workers=ARTICLE_WORKERS) as pool:
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

    if not bill_emb:
        workflow_issues.append({
            "level": "error",
            "category": "임베딩 비활성화",
            "detail": "본문 임베딩을 만들지 못해 유사 사례 검색을 수행하지 못했습니다.",
            "action": "OPENAI_API_KEY 또는 Azure OpenAI 임베딩 설정을 확인해야 합니다.",
        })

    if not legal_chunks:
        workflow_issues.append({
            "level": "warn",
            "category": "법령 RAG 근거 없음",
            "detail": "legal_reference 검색 결과가 없어 내장 일반 판단 기준을 사용했습니다.",
            "action": "ingest_legal_reference 실행 여부와 match_assembly_chunks RPC를 확인해야 합니다.",
        })

    if similar_estimates:
        avg_similarity = sum(float(s.get("similarity", 0)) for s in similar_estimates) / len(similar_estimates)
        if avg_similarity < MIN_AVG_SIMILARITY:
            workflow_issues.append({
                "level": "warn",
                "category": "유사 비용추계서 신뢰도 낮음",
                "detail": f"평균 유사도 {avg_similarity:.0%}로 기준 {MIN_AVG_SIMILARITY:.0%}보다 낮습니다.",
                "action": "산식과 금액은 초안으로만 보고 수동 검증해야 합니다.",
            })
    else:
        avg_similarity = 0.0
        workflow_issues.append({
            "level": "warn",
            "category": "유사 비용추계서 없음",
            "detail": "본문 기준 유사 비용추계서를 찾지 못했습니다.",
            "action": "추계 항목과 산식은 사용자 검토 없이는 확정할 수 없습니다.",
        })

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
    if not tag_patterns:
        workflow_issues.append({
            "level": "warn",
            "category": "TAG 산식 패턴 없음",
            "detail": "유사 의안의 구조화된 비용항목/산식/금액 패턴을 찾지 못했습니다.",
            "action": "산식 생성 결과를 검토하고 필요한 변수를 직접 보완해야 합니다.",
        })

    # 5-0.1) 분야 자동 분류
    field_info = detect_field(text)
    detected_field = field_info.get("field", "기타")

    # 5-0.2) 목적별 RAG 분리 검색 (NABO Part I, LEGAL_REF, NABO Part II 분야 매칭)
    classification_chunks = (
        vector_search(bill_emb, source="legal_reference", k=2,
                      bill_id_filter="NABO_2021_GUIDE_I")
        if bill_emb else []
    )
    methodology_chunks = (
        vector_search(bill_emb, source="legal_reference", k=2,
                      bill_id_filter="LEGAL_REF_COST_ESTIMATION")
        if bill_emb else []
    )
    nabo_case_chunks = (
        vector_search(bill_emb, source="legal_reference", k=3,
                      bill_id_filter="NABO_2021_GUIDE_II")
        if bill_emb else []
    )

    def _fmt_chunks(chunks: list[dict], max_len: int = 600) -> str:
        if not chunks:
            return "(없음)"
        return "\n---\n".join((c.get("content") or "")[:max_len] for c in chunks)

    final = gemini_json(FINAL_PROMPT.format(
        bill_name=bill_name,
        field=detected_field,
        articles_summary=articles_summary,
        classification_refs=_fmt_chunks(classification_chunks),
        methodology_refs=_fmt_chunks(methodology_chunks),
        nabo_cases=_fmt_chunks(nabo_case_chunks, 800),
        similar_estimates=similar_est_text or "(없음)",
        tag_patterns=tag_patterns_text,
        similar_non_attach=similar_na_text or "(없음)",
    )) or {}

    # 5-1) KOSIS 변수값 자동 채우기
    estimate = final.get("if_needs_estimate")
    if estimate and estimate.get("items"):
        for item in estimate["items"]:
            kosis_results = _lookup_kosis_variables(item.get("variables_needed", []))
            if kosis_results:
                item["kosis_lookups"] = kosis_results

    # 5-2) Python 계산기로 연도별 금액 산출
    if estimate and estimate.get("items"):
        missing_by_item = _missing_formula_variables(estimate)
        if missing_by_item:
            calculated, calc_issues = compute_year_estimates(estimate, tag_patterns=tag_patterns, allow_estimated=True)
            if calculated:
                estimate["calculation_status"] = "estimated_by_tag"
                estimate["year_estimates"] = calculated
                workflow_issues.append({
                    "level": "warn",
                    "category": "통계/변수 확인 필요",
                    "detail": "필수 통계 또는 단가가 부족해 TAG 유사사례 금액으로 초안을 산정했습니다.",
                    "action": "아래 변수는 통계청, 예산서, 사업계획서 또는 담당부서 자료로 확인해야 합니다.",
                    "items": missing_by_item,
                })
                if calc_issues:
                    workflow_issues.append({
                        "level": "warn",
                        "category": "추정 계산 근거",
                        "detail": "일부 항목에 유사 비용추계서 TAG 금액을 사용했습니다.",
                        "action": "requires_review 항목의 금액과 산식을 확인해야 합니다.",
                        "items": calc_issues,
                    })
            else:
                estimate["calculation_status"] = "blocked_missing_variables"
                estimate["year_estimates"] = _blocked_year_estimates(missing_by_item)
                workflow_issues.append({
                    "level": "error",
                    "category": "금액 계산 차단",
                    "detail": "필수 변수와 유사 TAG 금액이 모두 부족해 금액을 산정하지 못했습니다.",
                    "action": "누락 변수를 입력한 뒤 Python 계산기로 재계산해야 합니다.",
                    "items": missing_by_item,
                })
        else:
            calculated, calc_issues = compute_year_estimates(estimate, tag_patterns=tag_patterns, allow_estimated=True)
            if calculated:
                estimate["calculation_status"] = (
                    "estimated_by_tag"
                    if any(item.get("requires_review") for item in estimate.get("items") or [])
                    else "computed_by_python"
                )
                estimate["year_estimates"] = calculated
                if calc_issues:
                    workflow_issues.append({
                        "level": "warn",
                        "category": "일부 항목 계산 제외",
                        "detail": f"Python 계산기가 {len(calc_issues)}개 항목을 계산하지 못했습니다.",
                        "action": "각 항목의 calculation.base_amount_thousand, recurrence, 증가율 변수를 확인해야 합니다.",
                        "items": calc_issues,
                    })
            else:
                estimate["calculation_status"] = "blocked_no_structured_formula"
                estimate["year_estimates"] = _blocked_year_estimates({"계산 구조": ["base_amount_thousand", "recurrence"]})
                workflow_issues.append({
                    "level": "error",
                    "category": "금액 계산 차단",
                    "detail": "Python 계산기가 처리할 수 있는 구조화 산식이 없습니다.",
                    "action": "항목별 calculation.base_amount_thousand와 recurrence를 확인해야 합니다.",
                    "items": calc_issues,
                })
        review_vars = _review_variables(estimate)
        if review_vars:
            estimate["verification_needed"] = review_vars

    # 5-2.5) NABO 금액 게이트 - verdict와 계산 결과가 일치하는지 검증
    raw_verdict = final.get("verdict", "unknown")
    year_ests_for_gate = (estimate or {}).get("year_estimates") if estimate else None
    corrected_verdict, gate_note = _validate_verdict_with_amount(raw_verdict, year_ests_for_gate)
    if gate_note:
        final["verdict"] = corrected_verdict
        # verdict_label도 갱신
        label_map = {
            "추계서": "비용추계서",
            "미첨부_1호": "미첨부 1호 (비용 미미)",
            "미첨부_2호": "미첨부 2호 (안보·기밀)",
            "미첨부_3호": "미첨부 3호 (기술적 곤란)",
            "미대상": "미대상 (재정변화 없음)",
        }
        final["verdict_label"] = label_map.get(corrected_verdict, corrected_verdict)
        workflow_issues.append({
            "level": "warn",
            "category": "NABO 금액 게이트 자동 보정",
            "detail": gate_note,
            "action": "AI 판단을 NABO 공식 금액 기준에 따라 자동 보정했습니다.",
        })

    # 5-3) QA 리포트 — 무엇이 부족한지 사용자에게 명시
    qa_report = _build_qa_report(
        estimate=estimate,
        similar_estimates=similar_estimates,
        tag_patterns=tag_patterns,
        legal_chunks=legal_chunks,
    )
    for issue in reversed(workflow_issues):
        _prepend_qa_issue(qa_report, issue)

    confidence = final.get("confidence", 0.0)
    if any(i.get("level") == "error" for i in workflow_issues):
        confidence = _cap_confidence(confidence, 0.55)
    elif not legal_chunks or not similar_estimates or not tag_patterns:
        confidence = _cap_confidence(confidence, 0.70)
    elif avg_similarity < MIN_AVG_SIMILARITY:
        confidence = _cap_confidence(confidence, 0.75)

    # 6) 응답 조립
    return {
        "filename":     filename,
        "billName":     bill_name,
        "docType":      doc_type,
        "generatedAt":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsedSec":   round(time.time() - t0, 1),
        "totalArticles": len(articles),
        "analyzedArticles": len(article_results),

        "articles": article_results,

        "verdict": {
            "type":          final.get("verdict", "unknown"),
            "label":         final.get("verdict_label", "판단 불가"),
            "summary":       final.get("reason_summary", ""),
            "confidence":    float(confidence),
            "nabo_reason":   final.get("verdict_reason_nabo", ""),
        },
        "field": field_info,

        "estimate":      final.get("if_needs_estimate"),
        "nonAttachment": final.get("if_non_attachment"),
        "qaReport":      qa_report,
        "workflow": {
            "status": "blocked" if any(i.get("level") == "error" for i in workflow_issues)
                      else "degraded" if workflow_issues else "ok",
            "issues": workflow_issues,
            "analyzedAllArticles": len(article_results) == len(articles),
            "rag": {
                "legalReferenceCount": len(legal_chunks),
                "similarCostEstimateCount": len(similar_estimates),
                "similarNonAttachmentCount": len(similar_non_attach),
                "avgCostEstimateSimilarity": round(avg_similarity, 3),
            },
            "tagPatternCount": len(tag_patterns),
        },

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
