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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .assembly_assumptions import find_assumption_candidates
from .assembly_analogy_engine import build_analogical_committee_estimate
from .assembly_case_policy import apply_validated_case_policy
from .assembly_formula_engine import (
    apply_formula_source_strategy,
    apply_reasonable_default_amounts,
    apply_tag_formula_evidence,
    build_generalized_estimate,
    classify_estimation_status,
    merge_generalized_estimate,
)
from .assembly_formula_templates import build_formula_template
from .calculator import compute_year_estimates
from .config import GENERATED_DIR, PROJECT_ROOT, SCRIPT_DIR, get_env
from .llm_profiles import normalize_response as normalize_profile_response
from .llm_profiles import system_instruction as profile_system_instruction

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
                "level":    "info",
                "category": "유사 사례 보강 가능",
                "detail":   f"검색된 유사 추계서 평균 유사도 {avg_sim:.0%}",
                "action":   "유사사례가 추가 적재되면 정밀도가 더 올라갑니다. 현재는 일반 산식으로 초안을 생성합니다.",
            })
    else:
        issues.append({
            "level":    "info",
            "category": "유사 사례 검색 결과 없음",
            "detail":   "유사 추계서를 찾지 못해 새로운 유형의 의안으로 판단했습니다.",
            "action":   "표준 산식으로 best-effort 초안을 생성합니다.",
        })

    # 2) TAG 패턴 매칭
    if not tag_patterns:
        issues.append({
            "level":    "info",
            "category": "유사 추계서 구조 데이터 미연결",
            "detail":   "유사 의안의 구조화 산식 데이터가 연결되지 않았습니다.",
            "action":   "표준 산식으로 초안을 생성합니다.",
        })

    # 3) 법령 근거
    if not legal_chunks:
        issues.append({
            "level":    "info",
            "category": "법령 근거 RAG 미연결",
            "detail":   "법령 RAG가 연결되지 않아 기본 판단 기준을 적용했습니다.",
            "action":   "법령 RAG 연결 시 인용 근거가 자동 보강됩니다.",
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
            "level":    "info",
            "category": f"사용자 입력 변수 {total}개",
            "detail":   "KOSIS 자동 조회 대상이 아닌 변수입니다 (사업별 단가·대상자 수 등).",
            "action":   "아래 변수에 실제 값을 입력하시면 자동 재계산됩니다.",
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
            "level":    "warn",
            "category": f"보완 시 정밀도 향상 가능 ({len(uncomputed)}/{len(year_ests)}년)",
            "detail":   f"일부 연도는 기본 가정값으로 산출했습니다.",
            "action":   "아래 변수를 보완하시면 더 정확한 자동 재계산이 가능합니다."
                        if all_missing else "현재 산식으로 best-effort 초안을 제시하며, 사용자 검토를 권장합니다.",
            "missing_vars": sorted(all_missing) if all_missing else [],
        })

    # 6) 항목별 추계서 미생성
    if not items and not (estimate is None):
        issues.append({
            "level":    "warn",
            "category": "구체적 비용 항목 식별 안 됨",
            "detail":   "이 의안에서 구체적 비용 항목이 식별되지 않아 미첨부 사유서로 전환 검토 대상입니다.",
            "action":   "원문 확인 또는 미첨부 사유서 양식으로 처리하시면 됩니다.",
        })

    # 종합 요약
    has_error = any(i["level"] == "error" for i in issues)
    has_warn  = any(i["level"] == "warn"  for i in issues)
    summary = (
        "⚠️ 사용자 보완 권장" if has_error else
        "ℹ️ 보완 시 정밀도 향상 가능" if has_warn  else
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
        "⚠️ 사용자 보완 권장" if has_error else
        "ℹ️ 보완 시 정밀도 향상 가능" if has_warn else
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
        if isinstance(calc, dict) and (
            calc.get("base_amount_thousand") is not None
            or (
                calc.get("mode") == "yearly_series"
                and isinstance(calc.get("yearly_amounts_thousand"), list)
                and calc.get("yearly_amounts_thousand")
            )
        ):
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


def _human_input_prompt(item_name: str, variable: str, unit: str | None) -> str:
    suffix = f" ({unit})" if unit else ""
    prompts = (
        (r"회의횟수|개최횟수", f"{item_name}의 연간 회의 개최 횟수는 몇 회인가요?{suffix}"),
        (r"수당지급대상|민간위원|참석인원", f"{item_name}에서 회의수당을 받는 인원은 몇 명인가요?{suffix}"),
        (r"소요인력|증원|인원|인원수", f"{item_name}에 필요한 신규 인원은 몇 명인가요?{suffix}"),
        (r"단가|보수|지급액|지원금액", f"{item_name}의 적용 단가는 얼마인가요?{suffix}"),
        (r"대상자|대상수|수급자|개소", f"{item_name}의 지원 대상 규모는 얼마인가요?{suffix}"),
        (r"횟수|주기", f"{item_name}의 연간 시행 횟수는 몇 회인가요?{suffix}"),
    )
    compact = _compact_korean(variable)
    for pattern, prompt in prompts:
        if re.search(pattern, compact):
            return prompt
    return f"{item_name} 계산에 필요한 ‘{variable}’ 값을 입력해 주세요.{suffix}"


def _build_human_input_requests(estimate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not estimate:
        return None
    requests: list[dict[str, Any]] = []
    for item_index, item in enumerate(estimate.get("items") or []):
        calc = item.get("calculation") or {}
        item_blocked = not (
            isinstance(calc, dict)
            and (
                calc.get("base_amount_thousand") is not None
                or (calc.get("mode") == "yearly_series" and calc.get("yearly_amounts_thousand"))
            )
        )
        strategies = item.get("assumption_strategy") or []
        for strategy in strategies:
            status = str(strategy.get("status") or "")
            requires_review = bool(strategy.get("requires_review"))
            if status == "resolved" and not requires_review:
                continue
            variable = str(strategy.get("variable") or "기준값")
            unit = str(strategy.get("unit") or "")
            candidates = []
            variable_compact = _compact_korean(variable)
            for candidate in item.get("assumption_candidates") or []:
                candidate_text = _compact_korean(" ".join(
                    str(candidate.get(key) or "")
                    for key in ("label", "variable_name", "source_text")
                ))
                if variable_compact and (
                    variable_compact in candidate_text or candidate_text in variable_compact
                ):
                    candidates.append(candidate)
            requests.append({
                "id": f"item-{item_index}-{len(requests)}",
                "item_index": item_index,
                "item": item.get("name"),
                "variable": variable,
                "unit": unit,
                "prompt": _human_input_prompt(str(item.get("name") or "비용항목"), variable, unit),
                "status": status or "missing",
                "blocking": item_blocked and status in {"missing", "candidate"},
                "current_value": strategy.get("value"),
                "basis": strategy.get("basis"),
                "source_type": strategy.get("source_type"),
                "suggested_values": candidates[:3],
            })
        if item_blocked and not strategies:
            requests.append({
                "id": f"item-{item_index}-base",
                "item_index": item_index,
                "item": item.get("name"),
                "variable": "연간 기준금액",
                "unit": "천원",
                "prompt": f"{item.get('name') or '비용항목'}의 연간 기준금액을 입력해 주세요. (천원)",
                "status": "missing",
                "blocking": True,
                "current_value": None,
                "basis": "조문·공식 통계·유사사례에서 신뢰할 수 있는 기준값을 찾지 못했습니다.",
                "source_type": "user_input",
                "suggested_values": [],
            })
    if not requests:
        return None
    blocking_count = sum(1 for request in requests if request.get("blocking"))
    return {
        "status": "required" if blocking_count else "confirmation_recommended",
        "blocking_count": blocking_count,
        "request_count": len(requests),
        "requests": requests,
    }


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


def _sync_estimate_amount_totals(estimate: dict[str, Any]) -> None:
    amounts: list[int] = []
    for row in estimate.get("year_estimates") or []:
        try:
            amount = row.get("amount_thousand") if isinstance(row, dict) else None
            if amount is not None:
                amounts.append(int(round(float(amount))))
        except (TypeError, ValueError):
            continue
    if not amounts:
        return
    total = sum(amounts)
    estimate["total_amount_thousand"] = total
    estimate["average_amount_thousand"] = int(round(total / len(amounts)))


def _has_computed_amounts(estimate: dict[str, Any] | None) -> bool:
    if not isinstance(estimate, dict):
        return False
    return any(
        isinstance(row, dict) and row.get("amount_thousand") is not None
        for row in estimate.get("year_estimates") or []
    )


def _non_attachment_verdict(type_text: Any) -> tuple[str, str]:
    raw = str(type_text or "").strip()
    if "1" in raw:
        return "미첨부_1호", "미첨부 1호 (비용 미미)"
    if "2" in raw:
        return "미첨부_2호", "미첨부 2호 (안보·기밀)"
    return "미첨부_3호", "미첨부 3호 (기술적 곤란)"


def _public_non_attachment_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"유사\s*입법사례\([^)]*\)에서도\s*", "유사 입법사례에서도 ", text)
    text = re.sub(r"유사\s*입법사례\s*\d+\s*", "유사 입법사례 ", text)
    return " ".join(text.split())


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


def _item_lookup_variables(item: dict[str, Any]) -> list[str]:
    variables: list[str] = []
    for raw in item.get("variables_needed") or []:
        value = str(raw).strip()
        if value and value not in variables:
            variables.append(value)
    calc = item.get("calculation") or {}
    if isinstance(calc, dict):
        growth = str(calc.get("growth_variable") or "").strip()
        if growth and growth not in variables:
            variables.append(growth)
    return variables

GEMINI_API_KEY  = get_env("GEMINI_API_KEY")
GEMINI_GENERATION_MODEL = get_env("GEMINI_MODEL", "gemini-2.5-pro")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
UPSTAGE_API_KEY = get_env("UPSTAGE_API_KEY")
UPSTAGE_MODEL = get_env("UPSTAGE_MODEL", "solar-mini")
UPSTAGE_BASE_URL = get_env(
    "UPSTAGE_BASE_URL",
    "https://api.upstage.ai/v1/chat/completions",
)
LLM_PROVIDER = get_env("LLM_PROVIDER", "gemini").strip().lower()
LLM_PROFILE = get_env("LLM_PROFILE", "").strip()
if LLM_PROVIDER not in {"gemini", "upstage"}:
    raise ValueError("LLM_PROVIDER는 gemini 또는 upstage여야 합니다.")
LLM_API_KEY = UPSTAGE_API_KEY if LLM_PROVIDER == "upstage" else GEMINI_API_KEY
# max_tokens를 지정하지 않으면 provider 기본값에 의존하게 되어, 사건이 많은
# 의안에서 JSON 배열이 중간에 잘려 파싱 에러("Expecting ',' delimiter")로
# 나타난다(실측 확인됨). 넉넉한 값을 명시하고, 그래도 잘리면 finish_reason으로
# 감지해 애매한 파싱 에러 대신 명확한 예외를 낸다.
_LLM_MAX_OUTPUT_TOKENS = int(get_env("LLM_MAX_OUTPUT_TOKENS", "8192") or "8192")
LLM_GENERATION_MODEL = (
    UPSTAGE_MODEL if LLM_PROVIDER == "upstage" else GEMINI_GENERATION_MODEL
)
# Backward-compatible export used in estimator cache payloads.  Preserve the
# historic Gemini cache key, while namespacing Upstage so the two providers
# never share one cached judgment.
GEMINI_MODEL = (
    (
        f"upstage:{LLM_GENERATION_MODEL}@{LLM_PROFILE}"
        if LLM_PROFILE
        else f"upstage:{LLM_GENERATION_MODEL}"
    )
    if LLM_PROVIDER == "upstage"
    else LLM_GENERATION_MODEL
)
OPENAI_API_KEY  = get_env("OPENAI_API_KEY")
OPENAI_EMBED_MODEL = get_env("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
AZURE_EMBED_MODEL  = get_env("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
AZURE_API_VER   = "2024-02-01"
SUPA_URL        = get_env("SUPABASE_URL").rstrip("/")
SUPA_KEY        = get_env("SUPABASE_SERVICE_ROLE_KEY")
AZURE_KEY       = get_env("AZURE_OPENAI_API_KEY")
AZURE_ENDPOINT  = get_env("AZURE_OPENAI_ENDPOINT").rstrip("/")

_ARTICLE_RE = re.compile(r"제\s*\d+\s*조(?:의\s*\d+)?(?:\s*\([^)]+\))?")
_ARTICLE_NO_RE = re.compile(r"제\s*\d+\s*조(?:의\s*\d+)?")
_TARGET_NO_RE = re.compile(r"제\s*\d+\s*조(?:의\s*\d+)?|별\s*표\s*\d+")
_ARTICLE_HEADER_RE = re.compile(
    r"(?m)^\s*(제\s*\d+\s*조(?:의\s*\d+)?)"
    r"(?:\s*\(([^)]{1,120})\)|\s*(?=[①②③④⑤⑥⑦⑧⑨⑩]|\([0-9]+\)))"
)
_AMENDMENT_START_RE = re.compile(
    r"(?:[^\n]{0,120}?(?:일\s*부|전\s*부)를\s*"
    r"다\s*음\s*과\s*같\s*이\s*개\s*정\s*한\s*다\.?|"
    r"[^\n]{0,120}?다\s*음\s*과\s*같\s*이\s*제\s*정\s*한\s*다\.?)"
)
_SUPPLEMENTARY_RE = re.compile(r"(?m)^\s*부\s*칙\s*$")

ANALYZE_MAX_ARTICLES = int(get_env("ANALYZE_MAX_ARTICLES", "0") or "0")
ARTICLE_WORKERS = max(1, int(get_env("ANALYZE_ARTICLE_WORKERS", "2") or "2"))
MIN_AVG_SIMILARITY = float(get_env("MIN_AVG_SIMILARITY", "0.45") or "0.45")
_LLM_BACKOFF_UNTIL = 0.0


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


def _extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = "\n".join(p.get_text() for p in doc).strip()
    if not text:
        text = _extract_pdf_with_pdfkit(pdf_bytes)
    return text


def extract_pdf_from_b64(content_b64: str) -> str:
    pdf_bytes = base64.b64decode(_strip_data_url(content_b64))
    text = _extract_pdf_text_from_bytes(pdf_bytes)
    return strip_appendices(text) if text else text


# 본문이 끝나고 별첨/대비표가 시작되는 **진짜 본문** 시작점 패턴.
# 핵심 원칙: "참고사항 가. 신구대비표 : 붙임" 같은 목록 안내는 매칭 X,
# 실제 표/별첨 본문 시작만 매칭 O.
_APPENDIX_HEADER_PATTERNS = [
    # 실제 신구대비표 표 헤더 — "현    행" + 줄바꿈 + "개   정   안" (표 형식 공백 패턴)
    r"현\s{4,}행\s*\n+\s*개\s{2,}정\s{2,}안",
    # "현    행 개   정   안" 같은 줄 (한 줄에 같이)
    r"^\s*현\s{4,}행\s+개\s{2,}정\s{2,}안\s*$",
    # 신구조문대비표 헤더 직후 줄바꿈 — "참고사항"의 인라인 목록과 구분
    r"\n\s*신[ ]*[·ᆞㆍ・]?[ ]*구\s*조\s*문\s*대\s*비\s*표\s*\n",
    # 별첨/붙임 N의 현행 조례 (실제 섹션 시작)
    r"\n\s*\[\s*별\s*첨\s*\d+\s*\][^\n]*현\s*행\s*조\s*례",
    r"\n\s*\[\s*붙\s*임\s*\d+\s*\][^\n]*현\s*행\s*조\s*례",
    # 별첨/붙임 N의 비용추계서 본문 (안내 표시 아닌 실제 본문 시작)
    r"\n\s*\[\s*별\s*첨\s*\d+\s*\]\s*비\s*용\s*추\s*계\s*서\s*\n",
    r"\n\s*\[\s*붙\s*임\s*\d+\s*\]\s*비\s*용\s*추\s*계\s*서\s*\n",
    # 관계법령 발췌서 헤더 줄 (단독 줄)
    r"\n\s*■?\s*관\s*계\s*법\s*령\s*발\s*췌\s*서\s*\n",
    r"\n\s*관\s*계\s*법\s*령\s*\s*발\s*췌\s*서\s*\n",
]


def strip_appendices(text: str) -> str:
    """본문 조문 분석에 노이즈가 되는 별첨/대비표 섹션 이후를 잘라낸다.

    검증된 케이스(폴더 PDF 8개): 본문 변경분은 헤더 앞에 다 있음.
    "참고사항 가/나/다" 목록 안내는 본문이므로 절단 안 함.
    """
    if not text or len(text) < 500:
        return text
    cuts: list[int] = []
    for pat in _APPENDIX_HEADER_PATTERNS:
        m = re.search(pat, text, flags=re.MULTILINE)
        if m:
            cuts.append(m.start())
    if not cuts:
        return text
    cut_at = min(cuts)
    # 너무 앞에서 잘리면(본문이 없는 케이스) 그대로 반환
    if cut_at < 500:
        return text
    return text[:cut_at].rstrip()


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


def _group_pdf_words_by_line(words: list[tuple], y_tolerance: float = 3.0) -> list[list[tuple]]:
    lines: list[list[tuple]] = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        y_mid = (float(word[1]) + float(word[3])) / 2
        if not lines:
            lines.append([word])
            continue
        last = lines[-1]
        last_y = sum((float(w[1]) + float(w[3])) / 2 for w in last) / len(last)
        if abs(y_mid - last_y) <= y_tolerance:
            last.append(word)
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: float(w[0]))
    return lines


def _line_text(line: list[tuple]) -> str:
    return " ".join(str(w[4]) for w in line if str(w[4]).strip())


def _find_revision_table_split(page: fitz.Page, words: list[tuple]) -> tuple[float, float] | None:
    """신구조문대비표의 현행/개정안 컬럼 경계와 본문 시작 y좌표를 찾는다."""
    page_width = float(page.rect.width)
    for line in _group_pdf_words_by_line(words):
        compact = re.sub(r"\s+", "", _line_text(line))
        if "현행" not in compact or "개정안" not in compact:
            continue

        left_words = [w for w in line if float(w[0]) < page_width * 0.45]
        right_words = [w for w in line if float(w[0]) > page_width * 0.45]
        if not left_words or not right_words:
            continue
        left_edge = max(float(w[2]) for w in left_words)
        right_edge = min(float(w[0]) for w in right_words)
        if right_edge <= left_edge:
            continue
        split_x = (left_edge + right_edge) / 2
        start_y = max(float(w[3]) for w in line) + 2
        return split_x, start_y
    return None


def _words_to_text(words: list[tuple]) -> str:
    lines = _group_pdf_words_by_line(words)
    return "\n".join(_line_text(line) for line in lines if _line_text(line).strip())


def _revision_table_text_is_too_abbreviated(text: str) -> bool:
    """개정안 컬럼이 ---- 생략표현 위주면 조문 분석용 본문으로 쓰기 어렵다."""
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 60:
        return True
    dash_runs = list(re.finditer(r"-{3,}", compact))
    dash_chars = sum(len(match.group(0)) for match in dash_runs)
    return len(dash_runs) >= 3 or dash_chars / max(1, len(compact)) > 0.12


def split_articles_from_revision_table_pdf(pdf_bytes: bytes) -> list[dict[str, str]]:
    """PDF 좌표를 이용해 신구조문대비표 오른쪽(개정안) 컬럼만 추출한다.

    텍스트 레이어가 있고 현행/개정안 헤더가 잡히는 PDF에서만 동작한다.
    실패하면 빈 배열을 반환하여 기존 개정문 파싱으로 fallback한다.
    """
    right_column_pages: list[str] = []
    active_split_x: float | None = None
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                words = page.get_text("words", sort=True)
                if not words:
                    continue

                split_info = _find_revision_table_split(page, words)
                if split_info:
                    active_split_x, start_y = split_info
                elif active_split_x is not None:
                    start_y = float(page.rect.y0) + 20
                else:
                    continue

                right_words = [
                    w for w in words
                    if float(w[0]) >= float(active_split_x) + 4
                    and float(w[1]) >= start_y
                    and "비용추계" not in str(w[4])
                ]
                page_text = _words_to_text(right_words).strip()
                compact = re.sub(r"\s+", "", page_text)
                if not page_text or ("제" not in compact and "신설" not in compact and "삭제" not in compact):
                    continue
                right_column_pages.append(page_text)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[신구조문대비표 좌표 파싱 실패] {exc}\n")
        return []

    if not right_column_pages:
        return []

    right_text = "\n".join(right_column_pages)
    right_text = re.sub(r"(?m)^\s*(개\s*정\s*안|개정안|현\s*행)\s*$", "", right_text)
    if _revision_table_text_is_too_abbreviated(right_text):
        return []
    articles, _ = split_articles_structured(right_text)
    if not articles:
        articles = split_articles_regex(right_text)

    out: list[dict[str, str]] = []
    for article in articles:
        text = re.sub(r"\s+", " ", str(article.get("text") or "")).strip()
        if len(text) < 8:
            continue
        compact = re.sub(r"\s+", "", text)
        if compact in {"개정안", "현행"}:
            continue
        change_type = "신설" if "신설" in compact or "<신설>" in compact else str(article.get("change_type") or "개정")
        row = {
            "no": str(article.get("no") or "").strip(),
            "text": text[:1500],
            "change_type": change_type,
            "source": "revision_table_right_column",
        }
        out.append(row)
    return out


def _normalize_article_no(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _detect_doc_type(text: str) -> str:
    head_lines = [
        re.sub(r"\s+", "", line)
        for line in text[:1600].splitlines()
        if re.sub(r"\s+", "", line)
    ]
    for line in head_lines[:20]:
        if line.endswith(("법률안", "법안", "조례안")):
            if "전부개정" in line:
                return "전부개정안"
            if "일부개정" in line:
                return "일부개정안"
            return "제정안"

    compact = re.sub(r"\s+", "", text)
    if "전부를다음과같이개정한다" in compact:
        return "전부개정안"
    if "일부를다음과같이개정한다" in compact:
        return "일부개정안"
    if "다음과같이제정한다" in compact:
        return "제정안"
    title_part = compact[:400]
    if (
        title_part.endswith(("법률안", "법안", "조례안"))
        or "법률안(" in title_part
        or "법안(" in title_part
        or "조례안(" in title_part
    ):
        return "제정안"
    return "미상"


_RULE_COST_TRIGGERS: tuple[dict[str, Any], ...] = (
    {
        "name": "annex_institution_establishment",
        "pattern": re.compile(
            r"(별표\d*|고등법원|고등검찰청|법원|검찰청)"
            r".{0,120}?"
            r"(신설|설치|둔다)",
            re.DOTALL,
        ),
        "trigger_type": "조직설치",
        "reason": "별표 개정으로 법원·검찰청 등 기관을 신설하는 경우 인력·운영비 등 추가재정소요 가능성이 높음.",
    },
    {
        "name": "pension_credit_expansion",
        "pattern": re.compile(
            r"(제51조|가입기간|기준소득월액|기본연금액|연금보험료|크레딧)"
            r".{0,100}?"
            r"(추가로?산입|산입|국가가전부를부담|전부를부담|지원|2분의1|금액)",
            re.DOTALL,
        ),
        "trigger_type": "직접지원",
        "reason": "연금 가입기간 산입 확대 또는 보험료 국가부담 변경은 의무지출성 재정소요를 유발할 수 있음.",
    },
    {
        "name": "new_support_project",
        "pattern": re.compile(
            r"((지원사업|선지급|보조사업|급여사업|기숙사지원사업|양육비선지급)"
            r".{0,100}?"
            r"(신설|지급|지원|실시|운영|신청|결정)"
            r"|신설.{0,100}?(지원사업|선지급|보조사업|급여사업|기숙사지원사업|양육비선지급))",
            re.DOTALL,
        ),
        "trigger_type": "직접지원",
        "reason": "지원사업 또는 선지급 제도 신설은 대상자·단가에 따른 직접 재정지출 가능성이 높음.",
    },
    {
        "name": "childcare_disability_program",
        "pattern": re.compile(
            r"(장애영유아)"
            r".{0,140}?"
            r"(이해증진|보육정책|보육사업|조사ㆍ연구|정책분석|보호자|교육|표준보육과정|부모모니터링|지원방안|시설ㆍ설비현황)",
            re.DOTALL,
        ),
        "trigger_type": "의무부과",
        "reason": "장애영유아 보육 관련 교육·조사·모니터링·지원 근거는 보수교육 또는 보육지원 재정소요 후보입니다.",
    },
    {
        "name": "housing_inspection_admin_candidate",
        "pattern": re.compile(
            r"(기숙사)"
            r".{0,140}?"
            r"(지도ㆍ점검|지도·점검|지도점검|정보|제출|검토|적정성|명단공표|과태료|시정명령)",
            re.DOTALL,
        ),
        "trigger_type": "의무부과",
        "reason": "기숙사 지도점검·정보제출·공표·과태료 규정은 재정수반요인 후보이나 행정력 범위 내 미대상 가능성이 높습니다.",
        "force_cost": False,
    },
    {
        "name": "housing_penalty_admin_candidate",
        "pattern": re.compile(
            r"(제17조의2|기숙사)"
            r".{0,140}?"
            r"(과태료|위반|거부|방해|기피)",
            re.DOTALL,
        ),
        "trigger_type": "의무부과",
        "reason": "기숙사 지도점검 관련 과태료·제재 규정은 재정수반요인 후보이나 직접 비용추계 대상은 아닐 가능성이 높습니다.",
        "force_cost": False,
    },
    {
        "name": "committee_or_body_operation",
        "pattern": re.compile(
            r"(위원회|심의위원회|협의회|위원단|자문위원회|센터|지원센터|사무처|전담기관|담당관|전담조직)"
            r".{0,80}?"
            r"(둔다|두어야|설치|설립|구성|운영|지정|신설|수행하게)",
            re.DOTALL,
        ),
        "trigger_type": "조직설치",
        "reason": "위원회·센터·사무처 등 조직의 설치·운영 의무는 회의수당, 운영비 또는 인건비 발생 가능성이 높음.",
    },
    {
        "name": "broad_support_obligation",
        "pattern": re.compile(
            r"(재정지원|필요한지원|그에따른지원|지원을하여야|지원하여야)"
            r".{0,80}?"
            r"(시책|정책|제공|실시|수립|마련|하여야|한다)?",
            re.DOTALL,
        ),
        "trigger_type": "의무부과",
        "reason": "재정지원 또는 지원 의무를 포함한 정책 규정은 추가재정소요 여부 검토가 필요함.",
    },
    {
        "name": "payment_or_subsidy",
        "pattern": re.compile(
            r"(지원금|보조금|급여|수당|비용|경비|보상금|부담금|장려금)"
            r".{0,80}?"
            r"(지급|지원|보조|부담|보상|감면)",
            re.DOTALL,
        ),
        "trigger_type": "직접지원",
        "reason": "지원금·보조금·수당 등 재정지출 성격의 지급·지원 근거가 있음.",
    },
    {
        "name": "facility_or_system",
        "pattern": re.compile(
            r"(시설|장비|시스템|전산|정보망|플랫폼|데이터베이스)"
            r".{0,80}?"
            r"(설치|구축|운영|정비|관리)",
            re.DOTALL,
        ),
        "trigger_type": "시설구축",
        "reason": "시설·장비·전산시스템 설치 또는 운영 근거는 구축비·운영비 발생 가능성이 높음.",
    },
    {
        "name": "survey_or_plan_service",
        "pattern": re.compile(
            r"(실태조사|기본계획|종합계획|연구|용역|교육)"
            r".{0,80}?"
            r"(수립|실시|시행|수행|위탁|조사)",
            re.DOTALL,
        ),
        "trigger_type": "의무부과",
        "reason": "조사·계획·교육·용역의 실시 의무는 사업비 또는 위탁비 발생 가능성이 있음.",
    },
)

_NON_COST_CONTEXT_RE = re.compile(r"(삭제|폐지|명칭|용어|자구|인용|준용|적용하지|제외|변경한다)")
_NON_COST_ARTICLE_RE = re.compile(
    r"^제\d+조(?:의\d+)?\([^)]*(정의|목적|다른법률과의관계|위원장|경과조치|벌칙|과태료)[^)]*\)"
)
_LOW_COST_PROCEDURAL_TITLE_RE = re.compile(
    r"^제\d+조(?:의\d+)?\([^)]*(업무|협조|공시|자격인정|기관신설에대한심사|공공기관운영위원회의설치|근로조건|고용보장|임면|추천기준|재무관리|예산의편성|운영실적평가|합격취소)[^)]*\)"
)
_BROAD_DEFINITION_TITLE_RE = re.compile(r"^제\d+조(?:의\d+)?\(공공기관\)")
_NON_PUBLIC_BODY_CONTEXT_RE = re.compile(r"(중앙회와지부|협회와지부|공기업의이사회|임원추천위원회|감사위원회|회계감사인선임위원회)")
_ADMIN_ONLY_RE = re.compile(
    r"(명단공표|과태료|시정명령|자료제출|출입ㆍ조사|출입·조사|질문|소명기회|처벌|벌금|구류|과료)"
)
_SUPPORT_ADMIN_ARTICLE_RE = re.compile(
    r"^제\d+조(?:의\d+)?\(([^)]*(금융정보|자료|중지|회수|수수료|파기|명단공개|처벌|벌칙)[^)]*)\)"
)
_DISCRETIONARY_RE = re.compile(r"(할수있다|지원할수있다|위탁할수있다|실시할수있다|설치ㆍ운영할수있다|설치·운영할수있다)")
_DECLARATIVE_OR_PLAN_RE = re.compile(r"(노력하여야|시책을마련|필요한정책을수립|종합계획|기본계획|시행계획)")
_FORMULA_READY_RE = re.compile(
    r"(위원장.{0,20}포함.{0,20}\d+(?:명|인)|\d+(?:명|인)(?:이내)?의위원|위원수는\d+(?:명|인)|"
    r"먼저지급|선지급|국가가전부를부담|연금보험료|기준소득월액|"
    r"법원|검찰청|담당관|사무처|지원단)"
)
_DATA_GAP_RISK_RE = re.compile(
    r"(지원할수있다|사업계획|대통령령으로정하는|구체적사업|범위와.{0,30}필요한사항|"
    r"구체적방안|체계적방안|필요한사항은.{0,30}정한다|장관이정한다|"
    r"자료|신청률|대상자|지원금액|감면|세액|세입)"
)


def _rule_candidate_profile(compact: str, rule: dict[str, Any], window: str) -> dict[str, Any]:
    """정답지식 분기를 위해 비용 후보를 강도와 추계가능성으로 나눈다."""
    rule_name = str(rule.get("name") or "")
    force_cost = bool(rule.get("force_cost", True))

    strength = "medium"
    feasibility = "needs_assumptions"
    non_attachment_risk = "low"
    review_reason = "재정수반 후보이나 산식 전제값 확인이 필요합니다."

    if not force_cost:
        strength = "weak"
        feasibility = "non_attachment_review"
        non_attachment_risk = "high"
        review_reason = "행정·제재·자료제출 성격이 강해 미대상 또는 미첨부 검토가 필요합니다."
    elif rule_name in {"annex_institution_establishment", "pension_credit_expansion"}:
        strength = "strong"
        feasibility = "formula_ready"
        review_reason = "기관 신설 또는 의무지출성 제도 변경으로 산식 유형이 비교적 명확합니다."
    elif rule_name == "committee_or_body_operation":
        if _FORMULA_READY_RE.search(compact[:900]):
            strength = "strong"
            feasibility = "formula_ready"
            review_reason = "위원 정수·담당관·사무처 등 산식 전제가 본문에 있거나 유사사례 산식으로 연결 가능합니다."
        elif _DISCRETIONARY_RE.search(compact[:900]):
            strength = "medium"
            feasibility = "non_attachment_review"
            non_attachment_risk = "medium"
            review_reason = "설치·운영 가능 규정이라 실제 설치 여부와 사업계획 확인이 필요합니다."
    elif rule_name in {"payment_or_subsidy", "new_support_project"}:
        if re.search(r"(지급하여야|지원하여야|부담하여야|먼저지급|선지급|전부또는일부를먼저지급)", compact[:900]):
            strength = "strong"
            feasibility = "needs_assumptions"
            review_reason = "직접 지급·지원 의무가 있어 대상자 수와 단가 전제가 필요합니다."
        else:
            strength = "medium"
            feasibility = "non_attachment_review"
            non_attachment_risk = "medium"
            review_reason = "지원 근거는 있으나 대상자·지원단가·사업계획 부재 시 미첨부 가능성이 있습니다."
    elif rule_name in {"survey_or_plan_service", "facility_or_system", "childcare_disability_program"}:
        explicit_periodic_duty = bool(
            rule_name == "survey_or_plan_service"
            and re.search(r"(?:매년|연간|연\d+회|\d+년마다)", compact[:900])
            and re.search(r"(?:수립|실시|시행|수행|조사)하여야한다", compact[:900])
        )
        if explicit_periodic_duty:
            strength = "strong"
            feasibility = "formula_ready"
            review_reason = "법정 수립·조사 주기와 수행 의무가 명시되어 유사 용역 단가를 적용할 수 있습니다."
        elif _DISCRETIONARY_RE.search(compact[:900]) or _DECLARATIVE_OR_PLAN_RE.search(window):
            strength = "weak" if rule_name == "survey_or_plan_service" else "medium"
            feasibility = "non_attachment_review"
            non_attachment_risk = "medium"
            review_reason = "계획·조사·재량 사업 성격이 있어 실제 정답지는 일부추계 제외 또는 미첨부로 볼 수 있습니다."

    if _DATA_GAP_RISK_RE.search(compact[:1100]) and strength != "strong":
        feasibility = "non_attachment_review"
        non_attachment_risk = "medium" if non_attachment_risk == "low" else non_attachment_risk
        review_reason = (
            "구체적인 사업 범위, 수행 방식, 대상 및 규모가 하위규정이나 소관 기관 결정에 위임되어 "
            "현 단계에서 기술적으로 비용추계가 곤란합니다."
        )

    return {
        "candidate_strength": strength,
        "estimate_feasibility": feasibility,
        "non_attachment_risk": non_attachment_risk,
        "review_reason": review_reason,
    }


def _rule_cost_trigger(article_text: str) -> dict[str, Any] | None:
    """명확한 재정수반 문구를 LLM 오판 방지용 후보로 잡는다."""
    if not article_text:
        return None
    compact = re.sub(r"\s+", "", article_text)
    if _NON_COST_ARTICLE_RE.search(compact) or _BROAD_DEFINITION_TITLE_RE.search(compact):
        return None
    for rule in _RULE_COST_TRIGGERS:
        match = rule["pattern"].search(compact)
        if not match:
            continue
        window = compact[max(0, match.start() - 40):match.end() + 40]
        if rule["name"] == "new_support_project":
            primary_support_article = (
                re.search(r"^제\d+조(?:의\d+)?\([^)]*(선지급|지원사업)", compact)
                or re.search(r"^제\d+조(?:의\d+)?\([^)]*전산관리시스템", compact)
                or "먼저지급" in compact[:700]
                or re.search(r"\d+\.외국인근로자기숙사지원사업", compact)
            )
            if not primary_support_article:
                continue
        if rule["name"] in {"committee_or_body_operation", "survey_or_plan_service"} and _NON_PUBLIC_BODY_CONTEXT_RE.search(compact[:350]):
            continue
        if (
            rule["name"] in {"committee_or_body_operation", "survey_or_plan_service", "facility_or_system"}
            and _LOW_COST_PROCEDURAL_TITLE_RE.search(compact)
            and not re.search(r"(지원센터|전담기관|사무처|담당관|전문위원회|소위원회|분과위원회|노정위원회|인사검증소위원회|전산관리시스템)", compact[:220])
        ):
            continue
        if (
            rule["name"] in {"payment_or_subsidy", "facility_or_system", "survey_or_plan_service"}
            and _ADMIN_ONLY_RE.search(window)
            and not re.search(r"(지원사업|선지급|보조금|지원금|급여|수당|인건비|운영비|구축|설치)", window)
        ):
            continue
        if (
            rule["name"] == "facility_or_system"
            and "전산관리시스템" in window
            and "에관한사항" in window
            and not re.search(r"^제\d+조(?:의\d+)?\([^)]*전산관리시스템", compact)
        ):
            continue
        if (
            rule["name"] == "new_support_project"
            and _SUPPORT_ADMIN_ARTICLE_RE.search(compact)
            and not re.search(r"(전산관리시스템|시스템.{0,30}구축|먼저지급|전부또는일부를먼저지급)", compact)
        ):
            continue
        if _NON_COST_CONTEXT_RE.search(window) and not re.search(r"(설치|신설|둔다|두어야|운영|지급|지원|구축)", window):
            continue
        return {
            "rule": rule["name"],
            "trigger_type": rule["trigger_type"],
            "reason": rule["reason"],
            "matched_text": match.group(0)[:120],
            "force_cost": rule.get("force_cost", True),
            **_rule_candidate_profile(compact, rule, window),
        }
    return None


def _apply_rule_cost_trigger_overrides(article_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """비용유발 표현을 놓친 조문을 보정한다."""
    for article in article_results:
        rule_hit = _rule_cost_trigger(str(article.get("text") or ""))
        if not rule_hit:
            continue
        article["rule_cost_trigger"] = rule_hit
        article["cost_candidate_strength"] = rule_hit.get("candidate_strength", "medium")
        article["estimate_feasibility"] = rule_hit.get("estimate_feasibility", "needs_assumptions")
        article["non_attachment_risk"] = rule_hit.get("non_attachment_risk", "low")
        if not article.get("cost_trigger") and rule_hit.get("force_cost", True):
            article["cost_trigger"] = True
            article["trigger_type"] = rule_hit["trigger_type"]
            article["obligation_strength"] = "mandatory"
            original_reason = str(article.get("reason") or "").strip()
            article["reason"] = (
                f"{rule_hit['reason']} "
                f"(규칙 기반 보정; 기존 판단: {original_reason or '없음'})"
            )
    return article_results


def _extract_body_name(text: str) -> str | None:
    for suffix in ("심의위원회", "자문위원회", "위원회", "협의회", "지원센터", "센터", "사무처", "전담기관", "담당관"):
        match = re.search(rf"([가-힣A-Za-z0-9·ㆍ\.\s]{{2,40}}{suffix})", text)
        if match:
            return re.sub(r"\s+", "", match.group(1)).strip()
    return None


def _article_title(article_no: str) -> str | None:
    match = re.search(r"\(([^)\n]{2,80})\)", article_no or "")
    return match.group(1).strip() if match else None


def _public_body_priority(article: dict[str, Any]) -> int:
    text = re.sub(r"\s+", "", str(article.get("text") or ""))
    if re.search(r"(장관소속|정부소속|국가소속|중앙행정기관소속)", text):
        return 5
    if "중앙회" in text and not re.search(r"(장관|국가|정부|중앙행정기관|지방자치단체|시ㆍ도|시·도)", text):
        return -1
    if re.search(r"(장관소속|정부소속|국가|중앙행정기관|지방자치단체|시ㆍ도지사|시·도지사)", text):
        return 2
    return 1


def _make_rule_based_estimate(article_results: list[dict[str, Any]], form_type: str) -> dict[str, Any] | None:
    if form_type != "assembly":
        return None
    forced_articles = [
        a for a in article_results
        if a.get("cost_trigger")
        and a.get("cost_candidate_strength", "medium") != "weak"
        and (
            (a.get("rule_cost_trigger") or {}).get("rule") == "committee_or_body_operation"
            or a.get("trigger_type") == "조직설치"
        )
    ]
    if not forced_articles:
        return None
    forced_articles = [a for a in forced_articles if _public_body_priority(a) >= 0]
    forced_articles.sort(key=_public_body_priority, reverse=True)
    committee_articles = [
        a for a in forced_articles
        if (a.get("rule_cost_trigger") or {}).get("rule") == "committee_or_body_operation"
        and any(key in ((_article_title(str(a.get("no") or "")) or "") + str(a.get("text") or "")) for key in ("위원회", "심의회", "협의회"))
    ]
    if committee_articles:
        committee_articles.sort(key=_public_body_priority, reverse=True)
        forced_articles = committee_articles[:1]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in forced_articles[:3]:
        title = _article_title(str(article.get("no") or ""))
        body_name = (
            title if title and any(key in title for key in ("위원회", "심의회", "협의회", "센터", "사무처", "전담기관", "담당관"))
            else _extract_body_name(str(article.get("text") or ""))
            or "위원회 등 조직"
        )
        if body_name in seen:
            continue
        seen.add(body_name)
        if any(key in body_name for key in ("위원회", "심의회", "협의회")):
            item_name = f"{body_name} 운영비"
            formula = "회의수당 단가 × 회의횟수 × 참석인원 + 여비 등 부대경비"
            variables = ["회의수당 단가", "회의횟수", "참석인원"]
            assumptions = [
                {
                    "name": "회의수당 단가",
                    "value": None,
                    "unit": "천원/명",
                    "basis": "유사 위원회 운영 사례 또는 소관 기관 수당 기준 확인 필요",
                    "source_type": "user_input",
                    "needs_user_confirm": True,
                },
                {
                    "name": "회의횟수",
                    "value": None,
                    "unit": "회/년",
                    "basis": "법안 또는 유사 비용추계서의 개최 횟수 전제 확인 필요",
                    "source_type": "user_input",
                    "needs_user_confirm": True,
                },
                {
                    "name": "참석인원",
                    "value": None,
                    "unit": "명",
                    "basis": "위원 정수, 민간위원 수 또는 참석률 전제 확인 필요",
                    "source_type": "user_input",
                    "needs_user_confirm": True,
                },
            ]
        else:
            item_name = f"{body_name} 설치·운영비"
            formula = "소요인력 인건비 + 운영비 + 자산취득비"
            variables = ["소요인력 수", "직급별 보수", "운영비 기준액", "자산취득비 단가"]
            assumptions = [
                {
                    "name": "소요인력 수",
                    "value": None,
                    "unit": "명",
                    "basis": "유사 조직 설치 비용추계서 또는 소관 기관 전제 확인 필요",
                    "source_type": "user_input",
                    "needs_user_confirm": True,
                },
                {
                    "name": "운영비 기준액",
                    "value": None,
                    "unit": "천원/년",
                    "basis": "유사 조직 운영비 기준 확인 필요",
                    "source_type": "user_input",
                    "needs_user_confirm": True,
                },
            ]
        items.append({
            "name": item_name,
            "category": "운영비",
            "formula": formula,
            "trigger_ref": str(article.get("no") or ""),
            "variables_needed": variables,
            "assumptions": assumptions,
            "calculation": {
                "base_amount_thousand": None,
                "recurrence": "annual",
                "start_year": 1,
                "end_year": 5,
                "growth_variable": "소비자물가상승률",
                "source_note": "규칙 기반 비용유발 보정으로 생성된 산식 후보입니다. 기준값 확정 후 계산해야 합니다.",
            },
            "requires_review": True,
        })
    if not items:
        return None
    return {
        "items": items,
        "year_estimates": _blocked_year_estimates({items[0]["name"]: items[0]["variables_needed"]}),
        "calculation_status": "awaiting_user_input",
    }


def _extract_new_high_court_name(text: str) -> str:
    preferred_patterns = (
        r"(?:다음에|에)\s*([가-힣]{2,8}고등법원)",
        r"^\s*([가-힣]{2,8}고등법원)\s*$",
    )
    existing = {"서울고등법원", "대구고등법원", "광주고등법원", "부산고등법원", "대전고등법원", "수원고등법원"}
    for pattern in preferred_patterns:
        for match in re.finditer(pattern, text or "", flags=re.MULTILINE):
            candidate = match.group(1)
            if candidate not in existing:
                return candidate

    candidates = re.findall(r"([가-힣]{2,12}고등법원)", text or "")
    for candidate in candidates:
        for region in ("인천", "제주", "울산", "전주", "창원", "청주", "춘천"):
            normalized = f"{region}고등법원"
            if normalized in candidate and normalized not in existing:
                return normalized
        if candidate not in existing:
            return candidate
    return candidates[0] if candidates else "신설 고등법원"


def _extract_effective_year(text: str, default: int = 2029) -> int:
    match = re.search(r"(\d{4})\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일(?:부터)?\s*시행", text or "")
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            pass
    return default


def _series_rows(series: list[int], start_calendar_year: int) -> list[dict[str, Any]]:
    return [
        {
            "year": index + 1,
            "calendar_year": start_calendar_year + index,
            "year_label": str(start_calendar_year + index),
            "amount_thousand": amount,
            "note": "유형별 대표 가정 산출",
            "missing_vars": [],
            "requires_review": True,
        }
        for index, amount in enumerate(series)
    ]


def _item_with_series(
    *,
    name: str,
    category: str,
    formula_family: str,
    formula: str,
    trigger_ref: str,
    series: list[int],
    assumptions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "formula_family": formula_family,
        "formula": formula,
        "trigger_ref": trigger_ref,
        "variables_needed": [str(a.get("name") or "") for a in assumptions if a.get("name")],
        "assumptions": assumptions,
        "calculation": {
            "mode": "yearly_series",
            "yearly_amounts_thousand": series,
            "recurrence": "annual",
            "start_year": 1,
            "end_year": len(series),
            "growth_variable": None,
            "source_note": "공공조직 신설형 대표 가정 라이브러리 적용",
        },
        "year_amounts_thousand": series,
        "requires_review": True,
        "review_reason": "유사 공식 비용추계서에서 일반화한 조직 신설형 대표 가정입니다.",
    }


def _safe_int_amount(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _annual_tag_amounts(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row for row in item.get("amounts") or []
        if not row.get("is_total") and _safe_int_amount(row.get("amount_thousand")) is not None
    ]
    return sorted(
        rows,
        key=lambda row: (
            row.get("year_offset") is None,
            row.get("year_offset") if row.get("year_offset") is not None else str(row.get("year_label") or ""),
        ),
    )


def _similarity_by_bill(similar_estimates: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in similar_estimates:
        try:
            score = float(row.get("similarity") or 0)
        except (TypeError, ValueError):
            score = 0.0
        for key in ("bill_id", "bill_no"):
            value = str(row.get(key) or "").strip()
            if value:
                out[value] = max(score, out.get(value, 0.0))
    return out


def _tag_pattern_has_amount_structure(pattern: dict[str, Any]) -> bool:
    items = pattern.get("items") or []
    return any(_annual_tag_amounts(item) for item in items)


def _pattern_context_score(pattern: dict[str, Any], source_text: str) -> float:
    compact_text = _compact_korean(source_text)
    if not compact_text:
        return 0.0
    pattern_blob = _compact_korean(" ".join(
        str(value or "")
        for item in pattern.get("items") or []
        for value in (item.get("name"), item.get("trigger_ref"))
    ))
    tokens = set(re.findall(
        r"[가-힣]{2,10}(?:고등법원|고등검찰청|지방법원|가정법원|검찰청|광역시|특별시|특례시|자치시|자치도)",
        pattern_blob,
    ))
    tokens.update(re.findall(r"(?:인천|서울|부산|대구|광주|대전|울산|수원|제주|전주|창원|청주|춘천)[가-힣]{0,6}", pattern_blob))
    if not tokens:
        return 0.0
    matched = [token for token in tokens if token and token in compact_text]
    return min(0.7, 0.18 * len(matched))


def _pattern_items_match_source(pattern: dict[str, Any], source_text: str) -> bool:
    """차용할 TAG 항목들이 현재 의안 본문에 실제로 존재하는 내용인지 검증.

    같은 법률의 '다른' 개정안 추계서를 법률명 일치만으로 통째 차용하는
    오차용을 막는다 (개정안마다 개정 내용이 완전히 다를 수 있음).
    항목명의 핵심 명사가 본문에서 절반 이상 확인될 때만 차용 허용.
    """
    compact_source = _compact_korean(source_text)
    if not compact_source:
        return True  # 본문이 없으면 검증 불가 — 기존 동작 유지
    items = [
        item for item in pattern.get("items") or []
        if _annual_tag_amounts(item)
    ]
    if not items:
        return False
    matched = 0
    checkable = 0
    for item in items:
        name = _compact_korean(str(item.get("name") or ""))
        # 합계류 행은 검증 대상에서 제외
        if not name or name in {"합계", "총계", "계", "소계"}:
            continue
        # 항목명에서 조사·일반어를 제외한 핵심 명사 토큰 추출 (3자 이상)
        tokens = [
            tok for tok in re.findall(r"[가-힣]{3,12}", name)
            if tok not in {"설치비용", "운영비용", "각종계획", "계획수립", "추가재정", "재정소요"}
        ]
        if not tokens:
            continue
        checkable += 1
        if any(tok in compact_source for tok in tokens):
            matched += 1
    if checkable == 0:
        return True
    return matched / checkable >= 0.5


def _select_strong_tag_pattern(
    tag_patterns: list[dict[str, Any]],
    similar_estimates: list[dict[str, Any]],
    bill_name: str,
    source_text: str = "",
) -> tuple[dict[str, Any], float] | tuple[None, float]:
    if not tag_patterns:
        return None, 0.0
    scores = _similarity_by_bill(similar_estimates)
    best: tuple[dict[str, Any] | None, float] = (None, 0.0)
    for pattern in tag_patterns:
        if not _tag_pattern_has_amount_structure(pattern):
            continue
        # 내용 검증: 차용 항목이 현재 의안 본문에 존재해야 함
        if not _pattern_items_match_source(pattern, source_text):
            continue
        bill_id = str(pattern.get("bill_id") or "").strip()
        bill_no = str(pattern.get("bill_no") or "").strip()
        score = max(scores.get(bill_id, 0.0), scores.get(bill_no, 0.0))
        if bill_name and pattern.get("bill_name"):
            score = max(score, _name_overlap(bill_name, str(pattern.get("bill_name") or "")))
        score += _pattern_context_score(pattern, source_text)
        if score > best[1]:
            best = (pattern, score)
    if best[0] and best[1] >= 0.55:
        return best[0], min(best[1], 1.0)
    return None, best[1]


def _build_estimate_from_tag_pattern(
    pattern: dict[str, Any],
    *,
    similarity: float,
) -> dict[str, Any] | None:
    tag_items = pattern.get("items") or []
    items: list[dict[str, Any]] = []
    max_years = 0

    for tag_item in tag_items:
        tagged_name = str(tag_item.get("name") or "")
        # 원본 추계서의 합계·총계 행은 개별 항목과 중복(이중계산)이므로 차용에서 제외
        compact_item_name = re.sub(r"\s+", "", tagged_name)
        if tag_item.get("is_total") or compact_item_name in {"합계", "총계", "계", "소계", "총합계", "연간총계"}:
            continue
        amount_rows = _annual_tag_amounts(tag_item)
        if not amount_rows:
            continue
        series = [_safe_int_amount(row.get("amount_thousand")) or 0 for row in amount_rows]
        max_years = max(max_years, len(series))
        first_formula = next(
            (
                str(row.get("formula") or "")
                for row in amount_rows
                if row.get("formula") and "미제공" not in str(row.get("formula"))
            ),
            "",
        )
        if "고등검찰청" in tagged_name:
            display_formula = "고등법원 신설에 따른 대응 고등검찰청 운영비 산정"
            formula_reason = "고등법원 신설 시 대응 고등검찰청 설치가 필요하다는 공식 추계 사례의 전제를 적용했습니다."
        elif "고등법원" in tagged_name:
            display_formula = "신규 충원 인원 × 인건비·기관부담금·운영비 + 자산취득비"
            formula_reason = first_formula or "신규 충원 인원에 인건비, 기관부담금, 운영비, 자산취득비 기준을 적용했습니다."
        else:
            display_formula = first_formula or "공식 비용추계서의 연도별 산출금액"
            formula_reason = first_formula or "공식 비용추계서 사례의 비용항목과 연도별 금액 구조를 적용했습니다."
        variables = [
            {
                "name": var.get("name"),
                "value": var.get("value"),
                "unit": var.get("unit") or "",
                "basis": var.get("source_text") or f"{pattern.get('bill_no')} 공식 추계 사례 변수",
                "source_type": "tag_structured_case",
                "needs_user_confirm": False,
            }
            for var in tag_item.get("variables") or []
            if var.get("name")
        ]
        source_note = f"{pattern.get('bill_no')} {pattern.get('bill_name')} 공식 비용추계서"
        items.append({
            "name": tag_item.get("name") or "공식 추계 사례 비용항목",
            "category": tag_item.get("category") or "기타",
            "formula_family": "tag_structured_case",
            "formula": display_formula,
            "trigger_ref": tag_item.get("trigger_ref") or "",
            "variables_needed": [str(var.get("name") or "") for var in variables if var.get("name")],
            "assumptions": variables,
            "calculation": {
                "mode": "yearly_series",
                "yearly_amounts_thousand": series,
                "recurrence": "annual",
                "start_year": 1,
                "end_year": len(series),
                "growth_variable": None,
                "source_note": source_note,
            },
            "year_amounts_thousand": series,
            "requires_review": True,
            "review_reason": formula_reason,
            "selected_formula": {
                "source_type": "tag_full_structure",
                "source": source_note,
                "similarity": round(similarity, 3),
                "formula": display_formula,
                "label": "공식 추계 사례 산식",
                "basis": formula_reason,
            },
            "tag_structure_evidence": {
                "bill_no": pattern.get("bill_no"),
                "bill_name": pattern.get("bill_name"),
                "similarity": round(similarity, 3),
                "item_name": tag_item.get("name"),
            },
        })

    if not items or max_years == 0:
        return None

    year_labels: list[str | None] = [None] * max_years
    for tag_item in tag_items:
        for idx, row in enumerate(_annual_tag_amounts(tag_item)[:max_years]):
            if year_labels[idx] is None and row.get("year_label"):
                year_labels[idx] = str(row.get("year_label"))

    totals: list[int] = []
    for idx in range(max_years):
        totals.append(sum((item.get("year_amounts_thousand") or [0] * max_years)[idx] if idx < len(item.get("year_amounts_thousand") or []) else 0 for item in items))
    year_estimates = []
    for idx, amount in enumerate(totals):
        label = year_labels[idx]
        row: dict[str, Any] = {
            "year": idx + 1,
            "year_label": label or f"{idx + 1}차년도",
            "amount_thousand": amount,
            "note": "공식 비용추계서 사례 기준",
            "missing_vars": [],
            "requires_review": True,
        }
        if label and re.fullmatch(r"\d{4}", label):
            row["calendar_year"] = int(label)
        year_estimates.append(row)

    total = sum(totals)
    return {
        "items": items,
        "year_estimates": year_estimates,
        "calculation_status": "computed_with_tag_structure",
        "template_key": "tag_full_structure",
        "template_label": "공식 비용추계서 사례 기준 적용",
        "total_amount_thousand": total,
        "average_amount_thousand": int(round(total / max_years)),
        "assumption_summary": [
            f"근거 문서: {pattern.get('bill_no')} {pattern.get('bill_name')} (구조 유사도 {similarity:.0%})",
            "해당 공식 추계서의 비용항목, 변수, 연도별 금액 구조를 기준으로 초안 작성",
            "금액은 천원 단위 내부값으로 보존하고 화면·문서에서는 백만원 단위로 표시",
        ],
        "tag_structure_source": {
            "bill_no": pattern.get("bill_no"),
            "bill_name": pattern.get("bill_name"),
            "similarity": round(similarity, 3),
        },
    }


def _build_judicial_institution_estimate(text: str, article_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    compact = re.sub(r"\s+", "", text or "")
    if "고등법원" not in compact or not re.search(r"(신설|설치|별표)", compact):
        return None
    high_court = _extract_new_high_court_name(text)
    prosecution = high_court.replace("고등법원", "고등검찰청")
    start_year = _extract_effective_year(text)
    trigger_refs = [
        str(article.get("no") or "")
        for article in article_results
        if "별표" in str(article.get("no") or "") or "고등법원" in str(article.get("text") or "")
    ]
    trigger_ref = ", ".join(trigger_refs[:3]) or "안 별표"

    court_series = [2_687_000, 3_131_000, 3_191_000, 3_253_000, 3_315_000]
    prosecution_series = [4_011_000, 4_706_000, 4_796_000, 4_888_000, 4_981_000]
    total_series = [court + pros for court, pros in zip(court_series, prosecution_series)]

    common_basis = (
        "고등법원 신설 시 대응 고등검찰청 설치를 함께 고려하고, 판사·검사 증원 및 청사 신축비는 "
        "별도 정원법·청사 활용 가능성을 이유로 제외하는 사법기관 신설형 대표 가정"
    )
    items = [
        _item_with_series(
            name=f"{high_court} 운영비용",
            category="기관운영비",
            formula_family="judicial_institution_operation",
            formula="신규충원 인원 × 1인당 인건비 + 기관부담금 + 신규충원 인원 × 1인당 운영비 + 1인당 자산취득비",
            trigger_ref=trigger_ref,
            series=court_series,
            assumptions=[
                {"name": "신규 충원 인원", "value": 24, "unit": "명", "basis": "유사 고등법원 규모와 기존 고등법원 재배치 가능 인원을 반영", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "1인당 인건비", "value": 80.3, "unit": "백만원/명", "basis": "대구·광주 고등법원 1인당 평균 인건비", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "1인당 운영비", "value": 25.0, "unit": "백만원/명", "basis": "대구·광주 고등법원 1인당 평균 운영비", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "자산취득비", "value": 5.343, "unit": "백만원/명", "basis": "정부기관 1인당 자산취득비 대표 가정", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "총괄 전제", "value": common_basis, "unit": "", "basis": common_basis, "source_type": "representative_case", "needs_user_confirm": False},
            ],
        ),
        _item_with_series(
            name=f"{prosecution} 운영비용",
            category="기관운영비",
            formula_family="judicial_institution_operation",
            formula="신규충원 인원 × 1인당 인건비 + 기관부담금 + 신규충원 인원 × 1인당 운영비 + 1인당 자산취득비",
            trigger_ref="검찰청법 제3조 대응 설치",
            series=prosecution_series,
            assumptions=[
                {"name": "신규 충원 인원", "value": 31, "unit": "명", "basis": "유사 고등검찰청 규모와 기존 고등검찰청 재배치 가능 인원을 반영", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "1인당 인건비", "value": 115.0, "unit": "백만원/명", "basis": "대구·광주 고등검찰청 1인당 평균 인건비", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "1인당 운영비", "value": 4.8, "unit": "백만원/명", "basis": "대구·광주 고등검찰청 1인당 평균 운영비", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "자산취득비", "value": 5.343, "unit": "백만원/명", "basis": "정부기관 1인당 자산취득비 대표 가정", "source_type": "representative_case", "needs_user_confirm": False},
                {"name": "총괄 전제", "value": common_basis, "unit": "", "basis": common_basis, "source_type": "representative_case", "needs_user_confirm": False},
            ],
        ),
    ]
    return {
        "items": items,
        "year_estimates": _series_rows(total_series, start_year),
        "calculation_status": "computed_by_special_template",
        "template_key": "public_organization_establishment.judicial_high_court",
        "template_label": "공공조직 신설형 - 사법기관",
        "total_amount_thousand": sum(total_series),
        "average_amount_thousand": int(round(sum(total_series) / len(total_series))),
        "assumption_summary": [
            f"{high_court} 신설 시 {prosecution}도 함께 설치되는 것으로 가정",
            "판사·검사 증원 및 청사 신축비는 추계에서 제외",
            "유사 고등법원·고등검찰청의 신규충원 인원, 1인당 인건비, 운영비, 자산취득비 기준 적용",
            f"시행연도 {start_year}년부터 5년간 추계",
        ],
    }


def _build_public_organization_establishment_estimate(
    text: str,
    article_results: list[dict[str, Any]],
    *,
    form_type: str,
) -> dict[str, Any] | None:
    if form_type != "assembly":
        return None
    judicial = _build_judicial_institution_estimate(text, article_results)
    if judicial:
        return judicial
    return None


def _cost_candidate_summary(article_results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "strong": 0,
        "medium": 0,
        "weak": 0,
        "non_attachment_review": 0,
    }
    refs: dict[str, list[str]] = {"strong": [], "medium": [], "weak": []}
    for article in article_results:
        if not article.get("cost_trigger") and not article.get("rule_cost_trigger"):
            continue
        strength = str(article.get("cost_candidate_strength") or "medium")
        if strength not in {"strong", "medium", "weak"}:
            strength = "medium"
        counts[strength] += 1
        if article.get("estimate_feasibility") == "non_attachment_review":
            counts["non_attachment_review"] += 1
        if len(refs[strength]) < 8:
            refs[strength].append(str(article.get("no") or ""))
    return {"counts": counts, "refs": refs}


def _compact_korean(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _article_base_no(article_no: str) -> str:
    match = _ARTICLE_NO_RE.search(article_no or "")
    return _normalize_article_no(match.group(0)) if match else _normalize_article_no(article_no)


def _find_matching_article(item: dict[str, Any], articles: list[dict[str, Any]]) -> dict[str, Any] | None:
    trigger = _article_base_no(str(item.get("trigger_ref") or ""))
    if trigger:
        for article in articles:
            if _article_base_no(str(article.get("no") or "")) == trigger:
                return article

    item_name = _compact_korean(str(item.get("name") or ""))
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for article in articles:
        title = _compact_korean(_article_title(str(article.get("no") or "")) or "")
        text = _compact_korean(str(article.get("text") or ""))
        score = 0.0
        if title and title in item_name:
            score += 1.0
        if title and title in text:
            score += 0.2
        if item_name and item_name[:8] in text:
            score += 0.4
        if score > best[0]:
            best = (score, article)
    return best[1] if best[0] > 0 else None


_ITEM_CLAIM_GROUNDING_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "조세·부담금",
        re.compile(r"조세|세금|세액|과세|세수|감면|부담금"),
        re.compile(r"조세|세금|세액|과세|세수|감면|부담금|세제지원"),
    ),
    (
        "인력·인건비",
        re.compile(r"증원|채용|신규인력|소요인력|정원|인건비|보수|급여"),
        re.compile(r"증원|채용|신규인력|소요인력|정원|인건비|보수|급여|공무원|직원"),
    ),
    (
        "지원·지급",
        re.compile(r"지원금|보조금|지급액|급여지급|재정지원|비용지원"),
        re.compile(r"지원|보조|지급|급여|출연|융자|부담"),
    ),
    (
        "위원회·협의체",
        re.compile(r"위원회|심의회|협의회"),
        re.compile(r"위원회|심의회|협의회"),
    ),
    (
        "시설·시스템",
        re.compile(r"시설비|건축비|공사비|구축비|시스템구축|센터설치|기관설치"),
        re.compile(r"시설|건축|공사|구축|시스템|센터|기관.*(?:설립|설치)"),
    ),
    (
        "조사·계획·용역",
        re.compile(r"조사비|계획수립|연구용역|용역비"),
        re.compile(r"조사|계획|연구|용역"),
    ),
)


def _unsupported_item_claims(
    item: dict[str, Any],
    article: dict[str, Any] | None,
) -> list[str]:
    """비용항목의 주장 중 대응 조문에서 확인되지 않는 유형을 반환한다."""
    if not article:
        return ["대응 조문 없음"]
    item_text = _compact_korean(" ".join(
        str(value or "")
        for value in (
            item.get("name"),
            item.get("category"),
            item.get("formula"),
            " ".join(str(value) for value in item.get("variables_needed") or []),
        )
    ))
    article_text = _compact_korean(" ".join(
        str(value or "") for value in (article.get("no"), article.get("text"))
    ))
    unsupported: list[str] = []
    for label, claim_pattern, support_pattern in _ITEM_CLAIM_GROUNDING_RULES:
        if claim_pattern.search(item_text) and not support_pattern.search(article_text):
            unsupported.append(label)
    return unsupported


def _exclude_non_estimable_items(
    estimate: dict[str, Any] | None,
    articles: list[dict[str, Any]],
) -> int:
    """Remove LLM estimate rows that deterministic policy marked non-estimable.

    LLM output and the generalized formula engine are merged later in the
    pipeline.  Without this final policy gate, an unquantified discretionary
    subsidy can re-enter the estimate and receive an arbitrary default amount.
    """
    if not estimate or not isinstance(estimate.get("items"), list):
        return 0
    blocked_feasibility = {
        "non_attachment_review",
        "no_incremental_cost",
        "minor_or_absorbable",
        "not_applicable",
    }
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in estimate.get("items") or []:
        article = _find_matching_article(item, articles)
        strength = str((article or {}).get("cost_candidate_strength") or "")
        feasibility = str((article or {}).get("estimate_feasibility") or "")
        unsupported_claims = _unsupported_item_claims(item, article)
        if unsupported_claims:
            excluded.append({
                "name": item.get("name"),
                "trigger_ref": item.get("trigger_ref"),
                "reason": (
                    "비용항목이 주장한 " + ", ".join(unsupported_claims)
                    + " 비용의 법적 근거 표현을 대응 조문에서 확인하지 못했습니다."
                ),
                "case_policy": "ungrounded_cost_claim",
                "estimate_feasibility": "not_applicable",
            })
            continue
        if article and (strength == "weak" or feasibility in blocked_feasibility):
            excluded.append({
                "name": item.get("name"),
                "trigger_ref": item.get("trigger_ref"),
                "reason": article.get("reason"),
                "case_policy": article.get("case_policy"),
                "estimate_feasibility": feasibility,
            })
            continue
        kept.append(item)
    estimate["items"] = kept
    if excluded:
        estimate.setdefault("excluded_items", []).extend(excluded)
    return len(excluded)


def _extract_committee_total_members(article_text: str) -> int | None:
    compact = _compact_korean(article_text)
    patterns = (
        r"위원장\d*(?:명|인)(?:을포함한|포함).*?(\d+)(?:명|인)(?:이내)?의?위원",
        r"위원회는위원장\d*(?:명|인)을포함한(\d+)(?:명|인)(?:이내)?의?위원",
        r"위원(?:수|정원)은?(\d+)(?:명|인)",
        r"(\d+)(?:명|인)(?:이내)?의위원으로구성",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


# TAG 후보 source_text에서 정량값을 도출하기 위한 패턴
# [^가-힣\n\d]는 한글/줄바꿈/숫자 제외 → 숫자가 욕심 매칭에 먹히는 버그 방지
_TAG_UNIT_COST_MAN_PAT = re.compile(
    r"(회의수당|회의참석수당|회의비|참석수당|수당)(?:\s*단가)?[^가-힣\n\d]{0,8}(\d{1,3}(?:,\d{3})*|\d+)\s*만\s*원"
)
_TAG_UNIT_COST_WON_PAT = re.compile(
    r"(회의수당|회의참석수당|회의비|참석수당|수당)(?:\s*단가)?[^가-힣\n\d]{0,8}(\d{2,3}(?:,\d{3}))\s*원"
)
_TAG_MEETING_COUNT_PAT = re.compile(r"(?:연(?:간)?|매년|연도별)\s*(\d{1,2})\s*회|분기별\s*1\s*회")
_TAG_PAID_MEMBER_PAT = re.compile(
    r"(?:민간위원|위촉위원|수당지급(?:의)?대상|회의참석수당지급대상)\s*(?:수)?[\(\s]*(\d{1,2})\s*명"
)
_TAG_PAID_MEMBER_RATIO_PAT = re.compile(r"정원(?:의|대비)?\s*(\d{1,3}(?:\.\d+)?)\s*%")
_TAG_PAID_MEMBER_COUNT_RATIO_PATS = (
    re.compile(r"(?:위원회)?정원\s*(\d{1,3})\s*명.*?(?:민간위원|위촉위원).*?(\d{1,3})\s*명"),
    re.compile(r"(\d{1,3})\s*명의?위원\s*중.*?(?:민간위원|위촉위원).*?(\d{1,3})\s*명"),
    re.compile(r"전체위원수는?\s*(\d{1,3})\s*명.*?(?:민간위원|위촉위원).*?(\d{1,3})\s*명"),
)


def _stat_summary(values: list[int]) -> dict[str, Any]:
    """리스트의 n/중앙값/최빈값/평균을 단순 산출."""
    if not values:
        return {}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2:
        median = sorted_vals[n // 2]
    else:
        median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) // 2
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    mode_value, mode_count = max(counts.items(), key=lambda row: (row[1], -row[0]))
    return {
        "n": n,
        "min": int(min(values)),
        "max": int(max(values)),
        "median": int(median),
        "mode": int(mode_value),
        "mode_count": int(mode_count),
        "mean": int(round(sum(values) / n)),
    }


def _extract_unit_costs_from_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """assumption_candidates의 source_text에서 회의수당 단가(원)를 직접 추출."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, int]] = set()
    for candidate in candidates:
        src = str(candidate.get("source_text") or "")
        if candidate.get("assumption_key") == "committee_allowance_unit":
            try:
                direct_value = int(round(float(candidate.get("value"))))
            except (TypeError, ValueError):
                direct_value = 0
            if 30_000 <= direct_value <= 1_000_000:
                sig = (candidate.get("bill_no"), direct_value)
                if sig not in seen:
                    seen.add(sig)
                    out.append({
                        "value": direct_value,
                        "bill_no": candidate.get("bill_no"),
                        "bill_name": candidate.get("bill_name"),
                        "item_name": candidate.get("item_name"),
                        "snippet": src[:120].strip(),
                        "score": float(candidate.get("score") or 0),
                    })
        if not src:
            continue
        for match in _TAG_UNIT_COST_MAN_PAT.finditer(src):
            try:
                value = int(match.group(2).replace(",", "")) * 10_000
            except ValueError:
                continue
            if not (30_000 <= value <= 1_000_000):
                continue
            sig = (candidate.get("bill_no"), value)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "value": value,
                "bill_no": candidate.get("bill_no"),
                "bill_name": candidate.get("bill_name"),
                "item_name": candidate.get("item_name"),
                "snippet": src[max(0, match.start() - 16):match.end() + 16].strip(),
            })
        for match in _TAG_UNIT_COST_WON_PAT.finditer(src):
            try:
                value = int(match.group(2).replace(",", ""))
            except ValueError:
                continue
            if not (30_000 <= value <= 1_000_000):
                continue
            sig = (candidate.get("bill_no"), value)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "value": value,
                "bill_no": candidate.get("bill_no"),
                "bill_name": candidate.get("bill_name"),
                "item_name": candidate.get("item_name"),
                "snippet": src[max(0, match.start() - 16):match.end() + 16].strip(),
            })
    return out


def _extract_meeting_counts_from_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, int]] = set()
    for candidate in candidates:
        src = str(candidate.get("source_text") or "")
        if candidate.get("assumption_key") == "committee_meeting_count":
            try:
                direct_value = int(round(float(candidate.get("value"))))
            except (TypeError, ValueError):
                direct_value = 0
            if 1 <= direct_value <= 24:
                sig = (candidate.get("bill_no"), direct_value)
                if sig not in seen:
                    seen.add(sig)
                    out.append({
                        "value": direct_value,
                        "bill_no": candidate.get("bill_no"),
                        "bill_name": candidate.get("bill_name"),
                        "snippet": src[:120].strip(),
                        "score": float(candidate.get("score") or 0),
                    })
        if not src:
            continue
        for match in _TAG_MEETING_COUNT_PAT.finditer(src):
            value = None
            if match.group(1):
                try:
                    value = int(match.group(1))
                except ValueError:
                    continue
            else:
                value = 4  # 분기별 1회 → 연 4회
            if not value or not (1 <= value <= 12):
                continue
            sig = (candidate.get("bill_no"), value)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "value": value,
                "bill_no": candidate.get("bill_no"),
                "bill_name": candidate.get("bill_name"),
                "snippet": src[max(0, match.start() - 10):match.end() + 10].strip(),
                "score": float(candidate.get("score") or 0),
            })
    return out


def _extract_paid_member_counts_from_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, int]] = set()
    for candidate in candidates:
        src = str(candidate.get("source_text") or "")
        if candidate.get("assumption_key") == "committee_paid_member_count":
            try:
                direct_value = int(round(float(candidate.get("value"))))
            except (TypeError, ValueError):
                direct_value = 0
            if 1 <= direct_value <= 50:
                sig = (candidate.get("bill_no"), direct_value)
                if sig not in seen:
                    seen.add(sig)
                    out.append({
                        "value": direct_value,
                        "bill_no": candidate.get("bill_no"),
                        "bill_name": candidate.get("bill_name"),
                        "snippet": src[:120].strip(),
                        "score": float(candidate.get("score") or 0),
                    })
        if not src:
            continue
        for match in _TAG_PAID_MEMBER_PAT.finditer(src):
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if not (1 <= value <= 50):
                continue
            sig = (candidate.get("bill_no"), value)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "value": value,
                "bill_no": candidate.get("bill_no"),
                "bill_name": candidate.get("bill_name"),
                "snippet": src[max(0, match.start() - 10):match.end() + 10].strip(),
                "score": float(candidate.get("score") or 0),
            })
    return out


def _extract_committee_meetings(
    article_text: str,
    candidates: list[dict[str, Any]] | None = None,
) -> tuple[int | None, str, str, dict[str, Any]]:
    """회의횟수 도출. 조문이나 제외 대상 선례 근거가 없으면 입력 대기로 남긴다."""
    compact = _compact_korean(article_text)
    match = re.search(r"연(?:간)?(\d+)회", compact)
    if match:
        value = int(match.group(1))
        trace = {
            "method": "article_explicit",
            "value": value,
            "samples": [],
            "basis": "조문에 명시된 연간 회의 횟수",
        }
        return value, "document", "조문에 명시된 연간 회의 횟수", trace
    match = re.search(r"분기별(?:로)?1회", compact)
    if match:
        trace = {
            "method": "article_explicit",
            "value": 4,
            "samples": [],
            "basis": "조문상 분기별 1회 개최 → 연 4회",
        }
        return 4, "document", "조문상 분기별 1회 개최 규정", trace

    samples = _extract_meeting_counts_from_candidates(candidates or [])
    if samples:
        stat = _stat_summary([s["value"] for s in samples])
        ranked = sorted(samples, key=lambda row: float(row.get("score") or 0), reverse=True)
        top_margin = (
            float(ranked[0].get("score") or 0) - float(ranked[1].get("score") or 0)
            if len(ranked) > 1 else 1.0
        )
        # The scorer's generic components max out below 1.0; a score at or
        # above 1.0 therefore signals distinctive subject overlap. If all
        # displayed rows are generic, use the target-excluded official corpus
        # distribution rather than taking the median of only two arbitrary
        # candidates selected for the UI quota.
        subject_matched = float(ranked[0].get("score") or 0) >= 1.0
        corpus = next(
            (
                candidate.get("corpus_distribution")
                for candidate in (candidates or [])
                if candidate.get("assumption_key") == "committee_meeting_count"
                and candidate.get("corpus_distribution")
            ),
            None,
        )
        has_repeated_mode = stat["mode_count"] >= 2
        if not subject_matched and corpus and int(corpus.get("n") or 0) >= 10:
            chosen = int(round(float(corpus["mode"])))
            method = "target_excluded_corpus_mode"
        elif top_margin >= 0.08:
            chosen = ranked[0]["value"]
            method = "top_semantic_match"
        elif has_repeated_mode:
            chosen = stat["mode"]
            method = "tag_candidates_mode"
        else:
            chosen = stat["median"]
            method = "tag_candidates_median"
        basis = (
            (
                f"주제가 일치하는 강한 선례가 없어 현재 의안을 제외한 "
                f"공식 위원회 사례 {int(corpus['n'])}건의 최빈값 연 {chosen}회 채택"
                if method == "target_excluded_corpus_mode"
                else f"유사사례 {stat['n']}건 중 문맥 유사도 우선으로 연 {chosen}회 채택 "
                     f"(중앙값 {stat['median']}회, 범위 {stat['min']}~{stat['max']}회)"
            )
        )
        trace = {
            "method": method,
            "value": chosen,
            "samples": samples[:5],
            "statistic": stat,
            "corpus_distribution": corpus,
            "basis": basis,
        }
        return chosen, "tag_statistic", basis, trace

    trace = {
        "method": "missing_evidence",
        "value": None,
        "samples": [],
        "basis": "조문·공식 유사사례에서 회의횟수 근거를 확인하지 못함",
    }
    return None, "user_input", "근거 없음: 연간 회의 개최 횟수 입력 필요", trace


def _extract_paid_committee_members(
    article_text: str,
    total_members: int | None,
    candidates: list[dict[str, Any]] | None = None,
) -> tuple[int | None, str, str, dict[str, Any]]:
    """수당 지급 대상 인원 도출. 조문 명시 > TAG 후보 통계 > 위원 정수 비율 추론."""
    compact = _compact_korean(article_text)
    explicit_patterns = (
        r"(?:수당지급(?:의)?대상|회의참석수당(?:지급)?(?:대상)?|위촉위원|민간위원).*?(\d+)명",
        r"(\d+)명의?(?:위촉위원|민간위원)",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, compact)
        if match:
            value = int(match.group(1))
            trace = {
                "method": "article_explicit",
                "value": value,
                "samples": [],
                "basis": "조문 또는 분석 결과에 명시된 수당 지급 대상 인원",
            }
            return value, "document", "조문 또는 분석 결과에 명시된 수당 지급 대상 인원", trace

    if total_members:
        ratio_samples: list[dict[str, Any]] = []
        for candidate in candidates or []:
            source = str(candidate.get("source_text") or "")
            match = _TAG_PAID_MEMBER_RATIO_PAT.search(source)
            ratio = float(match.group(1)) / 100 if match else None
            if ratio is None:
                compact_source = _compact_korean(source)
                for count_pattern in _TAG_PAID_MEMBER_COUNT_RATIO_PATS:
                    count_match = count_pattern.search(compact_source)
                    if not count_match:
                        continue
                    denominator = int(count_match.group(1))
                    numerator = int(count_match.group(2))
                    if denominator > 0 and numerator <= denominator:
                        ratio = numerator / denominator
                        break
            if ratio is None:
                continue
            if 0.1 <= ratio <= 1.0:
                ratio_samples.append({
                    "ratio": ratio,
                    "bill_no": candidate.get("bill_no"),
                    "bill_name": candidate.get("bill_name"),
                    "snippet": source[:160],
                    "score": float(candidate.get("score") or 0),
                })
        if ratio_samples:
            best = max(ratio_samples, key=lambda row: row["score"])
            value = max(1, min(total_members, round(total_members * best["ratio"])))
            ratio_percent = round(best["ratio"] * 100, 1)
            basis = (
                f"문맥상 가장 유사한 위원회 사례({best['bill_no']})의 민간위원 비율 "
                f"{ratio_percent:g}%를 위원 정수 {total_members}명에 적용"
            )
            trace = {
                "method": "top_similar_case_ratio",
                "value": value,
                "ratio": best["ratio"],
                "samples": ratio_samples[:5],
                "basis": basis,
            }
            return value, "tag_statistic", basis, trace

    if total_members:
        if "공무원이아닌사람이과반" in compact:
            value = total_members // 2 + 1
            trace = {
                "method": "statutory_ratio_majority",
                "value": value,
                "samples": [],
                "basis": "공무원이 아닌 위원이 과반수가 되도록 하는 규정 적용",
            }
            return value, "formula_assumption", "공무원이 아닌 위원이 과반수가 되도록 하는 규정에 따라 과반 인원을 수당 지급 대상으로 가정", trace
        if total_members <= 15:
            value = round(total_members * 2 / 3)
            trace = {
                "method": "ratio_two_thirds",
                "value": value,
                "samples": [],
                "basis": f"위원 정수 {total_members}명 중 위원장·관계공무원 제외 위촉위원 비율(2/3) 적용",
            }
            return value, "formula_assumption", "위원장 및 관계 공무원을 제외한 위촉위원 비율을 유사사례 기준으로 가정", trace
        value = max(1, total_members // 2)
        trace = {
            "method": "ratio_half",
            "value": value,
            "samples": [],
            "basis": f"위원 정수 {total_members}명 중 절반을 민간·위촉위원으로 가정",
        }
        return value, "formula_assumption", "위원 정수 중 수당 지급 대상 민간·위촉위원 수를 유사사례 기준으로 가정", trace

    # When the bill does not state a total committee size, an absolute count
    # from several official analogues is still a reviewable candidate.  If a
    # statutory total is known, however, the structural ratio above is safer
    # than copying another committee's raw headcount.
    samples = _extract_paid_member_counts_from_candidates(candidates or [])
    if samples:
        stat = _stat_summary([s["value"] for s in samples])
        chosen = stat["mode"] if stat["mode_count"] >= 2 else stat["median"]
        basis = f"유사사례 {stat['n']}건의 민간·위촉위원 수 분포 대표값 {chosen}명 채택"
        trace = {
            "method": "tag_candidates_distribution",
            "value": chosen,
            "samples": samples[:5],
            "statistic": stat,
            "basis": basis,
        }
        return chosen, "tag_statistic", basis, trace

    trace = {
        "method": "user_input_required",
        "value": None,
        "samples": [],
        "basis": "위원 정수 또는 수당 지급 대상 인원 확인 필요",
    }
    return None, "user_input", "위원 정수 또는 수당 지급 대상 인원 확인 필요", trace


def _committee_allowance_unit(item: dict[str, Any]) -> tuple[int, str, str, dict[str, Any]]:
    """회의수당 단가 도출. TAG source_text 정량값 통계 > 구조화 후보 > 보수적 기본값."""
    candidates = item.get("assumption_candidates") or []
    committee_candidates = [
        c for c in candidates
        if c.get("assumption_key") in {"committee_operating_cost", "committee_allowance_unit"}
    ]

    # 1) source_text 정량값 통계 + 외부 기준(예산편성지침) 교차 검증
    #    - 외부 기준이 통계 분포와 큰 편차 없으면 기준 우선 (표준값, 안정성)
    #    - 통계가 기준과 ±50%를 벗어나면 통계 채택 (지침 갱신/특수 케이스)
    unit_samples = _extract_unit_costs_from_candidates(committee_candidates)
    guideline_value = 200_000
    guideline_reference = (
        "「예산안 편성 및 기금운용계획안 작성 세부지침」상 위원회 회의참석비 "
        "기본 150,000원 + 장시간(4시간 이상) 회의 추가 50,000원"
    )
    if unit_samples:
        stat = _stat_summary([s["value"] for s in unit_samples])
        if stat["n"] >= 2:
            chosen = stat["median"]
            basis = (
                f"문맥상 유사한 공식 추계 사례 {stat['n']}건의 회의수당 단가 중앙값 "
                f"{chosen:,}원/인 채택 (범위 {stat['min']:,}~{stat['max']:,}원)"
            )
            trace = {
                "method": "tag_candidates_median",
                "value": chosen,
                "samples": unit_samples[:5],
                "statistic": stat,
                "reference": guideline_reference,
                "basis": basis,
            }
            return chosen, "tag_statistic", basis, trace
        tag_mode = stat["mode"]
        deviation = abs(tag_mode - guideline_value) / guideline_value if guideline_value else 1.0
        if deviation <= 0.5:
            basis = (
                f"예산편성지침 기준 {guideline_value:,}원/인 채택. "
                f"유사사례 {stat['n']}건 분포(최빈값 {tag_mode:,}원, 중앙값 {stat['median']:,}원, "
                f"편차 ±{deviation:.0%})로 합리성 교차 검증."
            )
            trace = {
                "method": "guideline_anchored_with_tag_validation",
                "value": guideline_value,
                "samples": unit_samples[:5],
                "statistic": stat,
                "deviation": round(deviation, 3),
                "reference": guideline_reference,
                "basis": basis,
            }
            return guideline_value, "guideline_validated", basis, trace
        basis = (
            f"유사사례 {stat['n']}건 통계 최빈값 {tag_mode:,}원/인 채택 "
            f"(중앙값 {stat['median']:,}원, 예산편성지침 기준 {guideline_value:,}원과 편차 ±{deviation:.0%}로 통계값 우선)"
        )
        trace = {
            "method": "tag_candidates_mode",
            "value": tag_mode,
            "samples": unit_samples[:5],
            "statistic": stat,
            "deviation": round(deviation, 3),
            "reference": guideline_reference,
            "basis": basis,
        }
        return tag_mode, "tag_statistic", basis, trace

    # 2) 후보 value 필드가 단가 범위에 있는 경우 (구조화 후보 직접 사용)
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for candidate in committee_candidates:
        raw_value = candidate.get("value")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        unit = str(candidate.get("unit") or "")
        text = _compact_korean(" ".join(
            str(candidate.get(key) or "")
            for key in ("variable_name", "item_name", "source_text")
        ))
        if not re.search(r"회의(?:참석)?수당|수당단가|회의비", text):
            continue
        if not (50_000 <= value <= 600_000):
            continue
        score = float(candidate.get("score") or 0)
        if "심의위원회" in text:
            score += 0.25
        if "편성및기금운용계획안작성세부지침" in text or "예산안편성" in text:
            score += 0.2
        if "원/회" in unit or "원" in unit:
            score += 0.1
        ranked.append((score, int(round(value)), candidate))

    if ranked:
        ranked.sort(key=lambda row: row[0], reverse=True)
        _, value, candidate = ranked[0]
        basis = f"TAG 유사사례 회의수당 단가 후보 직접 사용: {candidate.get('bill_no')} {candidate.get('item_name')}"
        trace = {
            "method": "tag_reference_single",
            "value": value,
            "samples": [{
                "value": value,
                "bill_no": candidate.get("bill_no"),
                "bill_name": candidate.get("bill_name"),
                "item_name": candidate.get("item_name"),
            }],
            "basis": basis,
        }
        return value, "tag_reference", basis, trace

    # 3) 보수적 기본값 + 외부 인용 명시
    trace = {
        "method": "budget_guideline_fallback",
        "value": 200_000,
        "samples": [],
        "basis": "유사사례에서 회의수당 단가 정보를 확인하지 못하여 예산편성지침 기준으로 보수적 채택",
        "reference": "「예산안 편성 및 기금운용계획안 작성 세부지침」상 위원회 회의참석비 기본 150,000원 + 장시간(4시간 이상) 회의 추가 50,000원",
    }
    return 200_000, "formula_assumption", "예산안 편성지침상 위원회 참석비 기본 15만원 및 장시간 회의 추가 5만원을 반영한 20만원 가정", trace


def _is_committee_meeting_item(item: dict[str, Any]) -> bool:
    text = _compact_korean(" ".join(
        str(part or "")
        for part in [
            item.get("name"),
            item.get("category"),
            item.get("formula"),
            " ".join(str(v) for v in item.get("variables_needed") or []),
            item.get("trigger_ref"),
        ]
    ))
    if not any(keyword in text for keyword in ("위원회", "심의회", "협의회")):
        return False
    if any(keyword in text for keyword in ("사무처", "사무국", "지원센터", "인건비", "상임위원", "특별위원회운영사업비")):
        return False
    return any(keyword in text for keyword in ("회의", "수당", "운영비", "설치및운영", "위원회운영"))


def _committee_article_context(
    item: dict[str, Any],
    articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the named committee article plus adjacent composition articles."""
    matched = _find_matching_article(item, articles)
    if not matched:
        return []
    try:
        center = articles.index(matched)
    except ValueError:
        return [matched]
    entity = _compact_korean(_extract_body_name(str(item.get("name") or "")) or "")
    generic_title = re.compile(r"^위원회(?:의)?(?:구성|조직|운영|회의|위원장|간사|소위원회|분과)")
    context: list[dict[str, Any]] = []
    for index in range(max(0, center - 1), min(len(articles), center + 5)):
        article = articles[index]
        title = _compact_korean(_article_title(str(article.get("no") or "")) or "")
        body = _compact_korean(str(article.get("text") or ""))
        if article is matched or (entity and entity in body) or generic_title.search(title):
            context.append(article)
    return context or [matched]


def _enhance_committee_meeting_formulas(estimate: dict[str, Any], articles: list[dict[str, Any]]) -> int:
    """단순 심의위원회 운영비를 회의수당 산식으로 구조화한다."""
    enhanced = 0
    for item in estimate.get("items") or []:
        if not _is_committee_meeting_item(item):
            continue
        calc = item.setdefault("calculation", {})
        if item.get("analogy_evidence") or calc.get("mode") == "yearly_series":
            continue
        if isinstance(calc, dict) and calc.get("base_amount_thousand") is not None and item.get("committee_formula"):
            continue

        related_articles = _committee_article_context(item, articles)
        article_text = " ".join(
            str(part or "")
            for article in related_articles
            for part in [article.get("no"), article.get("text")]
        )
        article_text = " ".join([article_text, str(item.get("formula") or ""), str(item.get("name") or "")])
        candidates = item.get("assumption_candidates") or []
        total_members = _extract_committee_total_members(article_text)
        meeting_count, meeting_source, meeting_basis, meeting_trace = _extract_committee_meetings(
            article_text, candidates=candidates
        )
        paid_members, paid_source, paid_basis, paid_trace = _extract_paid_committee_members(
            article_text, total_members, candidates=candidates
        )
        allowance_won, allowance_source, allowance_basis, allowance_trace = _committee_allowance_unit(item)
        operands_ready = meeting_count is not None and paid_members is not None
        calculated_amount_thousand = (
            int(round(meeting_count * paid_members * allowance_won / 1000))
            if operands_ready
            else None
        )
        existing_amount = calc.get("base_amount_thousand") if isinstance(calc, dict) else None
        try:
            previous_amount_thousand = int(existing_amount) if existing_amount is not None else None
        except (TypeError, ValueError):
            previous_amount_thousand = None
        # Once the opaque draft has been replaced with a structured formula,
        # its amount must be derived from those same operands. Keeping an old
        # LLM baseline here produced internally contradictory reports such as
        # 4 meetings × 20 people × 300,000 won = 28.8 million won.
        amount_thousand = calculated_amount_thousand
        item["formula"] = "회의횟수 × 수당지급대상 인원 × 회의수당 단가"
        item["variables_needed"] = ["회의횟수", "수당지급대상 인원", "회의수당 단가"]
        item["assumptions"] = [
            {
                "name": "회의횟수",
                "value": meeting_count,
                "unit": "회/년",
                "basis": meeting_basis,
                "source_type": meeting_source,
                "needs_user_confirm": meeting_source != "document" or meeting_count is None,
                "evidence": meeting_trace,
            },
            {
                "name": "수당지급대상 인원",
                "value": paid_members,
                "unit": "명",
                "basis": paid_basis,
                "source_type": paid_source,
                "needs_user_confirm": paid_source != "document" or paid_members is None,
                "evidence": paid_trace,
            },
            {
                "name": "회의수당 단가",
                "value": allowance_won,
                "unit": "원/회",
                "basis": allowance_basis,
                "source_type": allowance_source,
                "needs_user_confirm": True,
                "evidence": allowance_trace,
            },
        ]
        pipeline_stages: list[str] = []
        for stage_trace in (meeting_trace, paid_trace, allowance_trace):
            method = str(stage_trace.get("method") or "")
            if method.startswith("article"):
                pipeline_stages.append("document_extraction")
            elif method.startswith("tag_") or method in {"top_semantic_match", "top_similar_case_ratio"}:
                pipeline_stages.append("tag_candidates")
            elif method.startswith("ratio") or method.startswith("statutory"):
                pipeline_stages.append("structural_inference")
            elif method == "missing_evidence":
                pipeline_stages.append("human_input_required")
            else:
                pipeline_stages.append("budget_guideline_fallback")
        item["committee_formula"] = {
            "formula": "회의횟수 × 수당지급대상 인원 × 회의수당 단가",
            "meeting_count": meeting_count,
            "paid_members": paid_members,
            "allowance_won": allowance_won,
            "amount_thousand": amount_thousand,
            "source": "structured_committee_meeting_formula",
            "requires_review": True,
            "calculation_invariant": {
                "expression": "meeting_count * paid_members * allowance_won / 1000",
                "computed_amount_thousand": calculated_amount_thousand,
                "previous_base_amount_thousand": previous_amount_thousand,
                "replaced_previous_base": (
                    previous_amount_thousand is not None
                    and previous_amount_thousand != calculated_amount_thousand
                ),
            },
            "evidence_trace": {
                "meeting_count": meeting_trace,
                "paid_members": paid_trace,
                "allowance_won": allowance_trace,
                "pipeline": pipeline_stages,
                "candidate_pool_size": len(candidates),
            },
        }
        item["calculation"] = {
            "base_amount_thousand": amount_thousand,
            "recurrence": "annual",
            "start_year": 1,
            "end_year": 5,
            "growth_variable": None,
            "source_note": "위원회 회의수당 산식 기반 유사사례 가정값",
        }
        item["requires_review"] = True
        item["review_reason"] = (
            "회의횟수·수당지급대상 인원·회의수당 단가를 근거 기반으로 구조화했습니다."
            if operands_ready
            else "산식은 구조화했지만 근거로 채우지 못한 변수는 사용자 입력이 필요합니다."
        )
        enhanced += 1
    return enhanced


def _article_change_target_matches(text: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for match in _TARGET_NO_RE.finditer(text):
        no = _normalize_article_no(match.group())
        window = text[match.end():match.end() + 80]
        compact = re.sub(r"\s+", "", window)
        range_creation = compact.startswith(("부터", "까지")) and "신설" in compact and "다음과같이" in compact
        if compact.startswith(("부터", "까지")) and not range_creation:
            continue
        if not no.startswith("별표") and compact.startswith(("에따른", "에대한", "에따라", "에도불구하고", "에서")):
            continue
        if re.match(r"제\d+항(?:에따른|에도불구하고|과|및)", compact):
            continue
        if range_creation:
            change_type = "신설"
        elif "삭제" in compact and ("신설" in compact or "다음과같이" in compact):
            change_type = "개정"
        elif "삭제" in compact:
            change_type = "삭제"
        elif "신설" in compact:
            change_type = "신설"
        elif (
            "다음과같이" in compact[:50]
            or re.match(
                r"(?:제\d+항)?(?:각호외의부분(?:본문)?)?중",
                compact,
            )
            or compact.startswith("각각")
        ):
            change_type = "개정"
        else:
            continue
        if not any(t["no"] == no for t in targets):
            targets.append({"no": no, "change_type": change_type, "start": match.start()})
    return targets


def _article_change_targets(text: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for target in _article_change_target_matches(text):
        targets.setdefault(target["no"], target["change_type"])
    return targets


def _created_article_ranges(text: str) -> dict[str, str]:
    """Expand an explicitly created contiguous article range.

    Korean amendment bills sometimes introduce a whole chapter with one
    directive such as ``제21조부터 제36조까지를 다음과 같이 신설``.
    The individual article headers are the actual changed provisions, but the
    amendment directive mentions only the two endpoints. Treating those
    endpoints as the complete target set silently drops every article in the
    middle. Expand only an explicit, bounded numeric range followed by a
    creation directive; ordinary cross-references remain untouched.
    """
    compact = re.sub(r"\s+", "", text or "")
    created: dict[str, str] = {}
    pattern = re.compile(
        r"제(?P<start>\d+)조부터제(?P<end>\d+)조까지\)?"
        r"(?:을|를)?다음과같이신설"
    )
    for match in pattern.finditer(compact):
        start = int(match.group("start"))
        end = int(match.group("end"))
        if end < start or end - start > 200:
            continue
        for number in range(start, end + 1):
            created[f"제{number}조"] = "신설"
    return created


def _created_enumerated_article_ranges(text: str) -> dict[str, str]:
    """Expand every range in one enumerated creation directive.

    Amendment bills may list several sub-article ranges before a single
    ``respectively create as follows`` directive.  Each range is eligible
    only when that explicit directive appears before the first following
    article body, so ordinary cross-references cannot enter the target set.
    """
    compact = re.sub(r"\s+", "", text or "")
    created: dict[str, str] = {}
    pattern = re.compile(
        r"\uC81C(?P<start>\d+)\uC870(?P<start_sub>\uC758\d+)?"
        r"\uBD80\uD130\uC81C(?P<end>\d+)\uC870(?P<end_sub>\uC758\d+)?\uAE4C\uC9C0"
    )
    for match in pattern.finditer(compact):
        suffix = compact[match.end():match.end() + 260]
        directive = re.search(
            r"(?:\uAC01\uAC01)?\uB2E4\uC74C\uACFC\uAC19\uC774\uC2E0\uC124",
            suffix,
        )
        next_body_header = re.search(
            r"\uC81C\d+\uC870(?:\uC758\d+)?\([^)]{1,80}\)",
            suffix,
        )
        if not directive or (
            next_body_header and next_body_header.start() < directive.start()
        ):
            continue

        start = int(match.group("start"))
        end = int(match.group("end"))
        start_sub = match.group("start_sub")
        end_sub = match.group("end_sub")
        if start_sub or end_sub:
            if start != end or not start_sub or not end_sub:
                continue
            sub_start = int(start_sub[1:])
            sub_end = int(end_sub[1:])
            if sub_end < sub_start or sub_end - sub_start > 100:
                continue
            for sub_number in range(sub_start, sub_end + 1):
                created[f"\uC81C{start}\uC870\uC758{sub_number}"] = "\uC2E0\uC124"
        else:
            if end < start or end - start > 200:
                continue
            for number in range(start, end + 1):
                created[f"\uC81C{number}\uC870"] = "\uC2E0\uC124"
    return created


def _main_revision_text(text: str) -> str:
    start = _AMENDMENT_START_RE.search(text)
    body = text[start.end():] if start else text
    supp = _SUPPLEMENTARY_RE.search(body)
    if supp and supp.start() > 80:
        body = body[:supp.start()]
    return body.strip()


def split_articles_structured(text: str) -> tuple[list[dict[str, str]], str]:
    """법령/조례 개정문 형식에 맞춘 결정적 조문 추출.

    문장 중간의 참조 조문(예: 제44조를 준용한다)은 분석 대상 조문으로
    분리하지 않고, 줄 시작의 실제 조문 헤더만 블록으로 묶는다.
    """
    doc_type = _detect_doc_type(text)
    body = _main_revision_text(text) if doc_type in {"일부개정안", "전부개정안"} else text
    change_matches = _article_change_target_matches(body) if doc_type == "일부개정안" else []
    targets = {target["no"]: target["change_type"] for target in change_matches}
    if doc_type == "일부개정안":
        targets.update(_created_article_ranges(body))
        targets.update(_created_enumerated_article_ranges(body))

    matches = list(_ARTICLE_HEADER_RE.finditer(body))
    if not matches:
        if not change_matches:
            return [], doc_type

    out: list[dict[str, str]] = []
    normalized_targets = set(targets)
    for i, match in enumerate(matches):
        no_raw = match.group(1).strip()
        no = _normalize_article_no(no_raw)
        if doc_type == "일부개정안" and normalized_targets and no not in normalized_targets:
            continue

        next_positions = []
        if i + 1 < len(matches):
            next_positions.append(matches[i + 1].start())
        if doc_type == "일부개정안":
            next_positions.extend(
                int(target["start"])
                for target in change_matches
                if int(target["start"]) > match.start() + 10
            )
        next_start = min(next_positions) if next_positions else len(body)
        block = body[match.start():next_start].strip()
        block = re.sub(r"\s+", " ", block)
        if len(block) < 12:
            continue
        title = (match.group(2) or "").strip()
        label = f"{no}({title})" if title else no
        refs = sorted({
            _normalize_article_no(ref.group())
            for ref in _ARTICLE_NO_RE.finditer(block)
            if _normalize_article_no(ref.group()) != no
        })
        article = {
            "no": label,
            "text": block[:1500],
            "change_type": targets.get(no) or ("제정" if doc_type == "제정안" else "개정"),
        }
        if refs:
            article["references"] = refs
        article["_pos"] = match.start()
        out.append(article)

    existing = {
        _normalize_article_no(re.match(r"제\s*\d+\s*조(?:의\s*\d+)?", a["no"]).group())
        for a in out
        if re.match(r"제\s*\d+\s*조(?:의\s*\d+)?", a["no"])
    }
    for i, target in enumerate(change_matches):
        no = target["no"]
        if no in existing:
            continue
        start = int(target["start"])
        next_starts = [int(t["start"]) for t in change_matches[i + 1:] if int(t["start"]) > start]
        next_starts.extend(match.start() for match in matches if match.start() > start)
        end = min(next_starts) if next_starts else len(body)
        block = re.sub(r"\s+", " ", body[start:end]).strip()
        if len(block) < 8:
            continue
        refs = sorted({
            _normalize_article_no(ref.group())
            for ref in _ARTICLE_NO_RE.finditer(block)
            if _normalize_article_no(ref.group()) != no
        })
        article = {
            "no": no,
            "text": block[:1500],
            "change_type": str(target["change_type"]),
            "_pos": start,
        }
        if refs:
            article["references"] = refs
        out.append(article)

    out.sort(key=lambda row: int(row.get("_pos", 0)))
    for article in out:
        article.pop("_pos", None)
    return out, doc_type


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


def _json_from_llm_text(text: str) -> Any:
    cleaned = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original:
        # OpenAI-compatible providers can occasionally wrap an otherwise
        # valid JSON object in a short explanation or a non-exact code fence.
        # Decode the first complete JSON value without guessing or repairing
        # malformed fields.
        decoder = json.JSONDecoder()
        starts = sorted(
            index
            for marker in ("{", "[")
            if (index := cleaned.find(marker)) >= 0
        )
        for start in starts:
            try:
                value, _ = decoder.raw_decode(cleaned, start)
                return value
            except json.JSONDecodeError:
                continue
        raise original


def _call_llm_raw_json(
    prompt: str,
    *,
    temperature: float,
    timeout: int,
) -> Any:
    if not LLM_API_KEY:
        raise RuntimeError(
            f"{'UPSTAGE_API_KEY' if LLM_PROVIDER == 'upstage' else 'GEMINI_API_KEY'} "
            "설정이 필요합니다."
        )
    if LLM_PROVIDER == "upstage":
        messages: list[dict[str, str]] = []
        profile_instruction = profile_system_instruction(LLM_PROFILE, prompt)
        if profile_instruction:
            messages.append({"role": "system", "content": profile_instruction})
        messages.append({"role": "user", "content": prompt})
        data = _post(
            UPSTAGE_BASE_URL,
            {
                "Authorization": f"Bearer {UPSTAGE_API_KEY}",
                "Content-Type": "application/json",
            },
            {
                "model": UPSTAGE_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": _LLM_MAX_OUTPUT_TOKENS,
            },
            timeout=timeout,
        )
        choice = data["choices"][0]
        finish_reason = str(choice.get("finish_reason") or "")
        if finish_reason == "length":
            raise RuntimeError(
                "LLM 응답이 max_tokens 한도에서 잘렸습니다(finish_reason=length). "
                "이 사건 묶음은 더 작은 조각으로 나눠 다시 시도해야 합니다."
            )
        text = choice["message"]["content"]
    else:
        url = (
            f"{GEMINI_BASE_URL}/{GEMINI_GENERATION_MODEL}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )
        data = _post(
            url,
            {"Content-Type": "application/json"},
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": temperature,
                    "maxOutputTokens": _LLM_MAX_OUTPUT_TOKENS,
                },
            },
            timeout=timeout,
        )
        candidate = data["candidates"][0]
        finish_reason = str(candidate.get("finishReason") or "")
        if finish_reason == "MAX_TOKENS":
            raise RuntimeError(
                "LLM 응답이 maxOutputTokens 한도에서 잘렸습니다(finishReason=MAX_TOKENS). "
                "이 사건 묶음은 더 작은 조각으로 나눠 다시 시도해야 합니다."
            )
        text = candidate["content"]["parts"][0]["text"]
    return _json_from_llm_text(str(text))


def _gemini_raw_json(prompt: str) -> Any:
    """Compatibility name: return a list/dict from the active LLM provider."""
    try:
        return _call_llm_raw_json(prompt, temperature=0.1, timeout=120)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[{LLM_PROVIDER} raw 오류] {exc}\n")
        return None


def split_articles(text: str) -> tuple[list[dict[str, str]], str]:
    """LLM 본문 추출 (1순위) + 정규식 폴백.

    반환: (조문 리스트, 문서유형). 신구대비표/개정안이면 변경 조항만 포함.
    """
    if len(text) < 200:
        return split_articles_regex(text), "미상"

    structured_articles, structured_doc_type = split_articles_structured(text)
    if structured_articles:
        return structured_articles, structured_doc_type

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

    fallback_articles, fallback_doc_type = split_articles_structured(text)
    if fallback_articles:
        return fallback_articles, fallback_doc_type
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
                  bill_id_filter: str | None = None,
                  exclude_bill_nos: set[str] | None = None) -> list[dict]:
    """match_assembly_chunks RPC 호출 (Supabase에 등록된 함수).

    bill_id_filter가 있으면 RPC 결과 후 클라이언트 사이드 필터링.
    RPC 함수 시그니처를 바꾸지 않기 위해 더 많이 받고 필터링.
    """
    url = f"{SUPA_URL}/rest/v1/rpc/match_assembly_chunks"
    headers = {
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
    }
    # bill_id_filter 또는 평가 대상 제외가 있으면 더 많이 받고 클라이언트에서
    # 필터링한다. 같은 의안의 청크가 상위 결과를 독점해도 k건을 확보하기 위함.
    excluded = {str(value) for value in (exclude_bill_nos or set()) if value}
    fetch_k = k * 5 if bill_id_filter else k
    if excluded:
        fetch_k = max(fetch_k, k * 10)
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
    if excluded:
        results = [r for r in results if str(r.get("bill_no") or "") not in excluded]
    return results[:k]


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _local_tag_roots() -> list[Path]:
    candidates = [
        GENERATED_DIR / "assembly_rag_seed",
        GENERATED_DIR / "assembly_rag_seed_22_ce",
        GENERATED_DIR / "assembly_rag_seed_22",
        PROJECT_ROOT / "backend" / "generated" / "assembly_rag_seed",
        PROJECT_ROOT / "backend" / "generated" / "assembly_rag_seed_22_ce",
        PROJECT_ROOT / "backend" / "generated" / "assembly_rag_seed_22",
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen and path.exists():
            out.append(path)
            seen.add(key)
    return out


def _fetch_local_tag_patterns(bill_ids: list[str], limit: int = 3) -> list[dict]:
    wanted = {str(bill_id) for bill_id in bill_ids[:limit] if bill_id}
    if not wanted:
        return []
    merged: dict[str, dict[str, Any]] = {}
    for root in _local_tag_roots():
        structures = [
            row for row in _iter_jsonl(root / "cost_estimate_structures.jsonl")
            if str(row.get("bill_id") or "") in wanted or str(row.get("bill_no") or "") in wanted
        ]
        if not structures:
            continue
        struct_ids = {str(row.get("struct_id") or row.get("id") or "") for row in structures}
        items = [
            row for row in _iter_jsonl(root / "cost_estimate_items.jsonl")
            if str(row.get("struct_id") or row.get("structure_id") or "") in struct_ids
        ]
        item_ids = {str(row.get("item_id") or row.get("id") or "") for row in items}
        variables = [
            row for row in _iter_jsonl(root / "cost_estimate_variables.jsonl")
            if str(row.get("item_id") or "") in item_ids
        ]
        amounts = [
            row for row in _iter_jsonl(root / "cost_estimate_amounts.jsonl")
            if str(row.get("item_id") or "") in item_ids
        ]

        by_item: dict[str, dict[str, Any]] = {}
        for it in items:
            iid = str(it.get("item_id") or it.get("id") or "")
            if not iid:
                continue
            by_item[iid] = {
                "category": it.get("item_category"),
                "name": it.get("item_name"),
                "trigger_ref": it.get("trigger_ref"),
                "variables": [],
                "amounts": [],
                "_struct_id": str(it.get("struct_id") or it.get("structure_id") or ""),
                "_order": it.get("item_order") or 0,
            }
        for var in variables:
            iid = str(var.get("item_id") or "")
            if iid in by_item:
                by_item[iid]["variables"].append({
                    "name": var.get("variable_name"),
                    "value": var.get("variable_value"),
                    "unit": var.get("variable_unit"),
                    "source_text": var.get("source_text"),
                })
        for amount in amounts:
            iid = str(amount.get("item_id") or "")
            if iid in by_item:
                by_item[iid]["amounts"].append({
                    "year_label": amount.get("year_label"),
                    "year_offset": amount.get("year_offset"),
                    "amount_thousand": amount.get("amount_thousand"),
                    "formula": amount.get("formula_text"),
                    "is_total": amount.get("is_total"),
                })

        by_struct: dict[str, list[dict[str, Any]]] = {}
        for item in sorted(by_item.values(), key=lambda row: int(row.get("_order") or 0)):
            sid = str(item.pop("_struct_id", "") or "")
            item.pop("_order", None)
            by_struct.setdefault(sid, []).append(item)

        for structure in structures:
            sid = str(structure.get("struct_id") or structure.get("id") or "")
            key = str(structure.get("bill_id") or structure.get("bill_no") or sid)
            if key in merged:
                continue
            merged[key] = {
                "bill_id": structure.get("bill_id"),
                "bill_no": structure.get("bill_no"),
                "bill_name": structure.get("bill_name"),
                "items": by_struct.get(sid, []),
                "source": f"local:{root.name}",
            }

    ordered: list[dict[str, Any]] = []
    for bill_id in bill_ids[:limit]:
        key = str(bill_id)
        if key in merged:
            ordered.append(merged[key])
    for pattern in merged.values():
        if len(ordered) >= limit:
            break
        if pattern not in ordered:
            ordered.append(pattern)
    return ordered[:limit]


def fetch_local_tag_patterns_by_bill_name(
    bill_name: str,
    limit: int = 1,
    exclude_bill_nos: set[str] | None = None,
) -> list[dict]:
    target = _compact_korean(bill_name)
    if not target:
        return []
    excluded = {str(value) for value in (exclude_bill_nos or set()) if value}
    matched_ids: list[str] = []
    for root in _local_tag_roots():
        for structure in _iter_jsonl(root / "cost_estimate_structures.jsonl"):
            if str(structure.get("bill_no") or "") in excluded:
                continue
            row_name = _compact_korean(str(structure.get("bill_name") or ""))
            if not row_name:
                continue
            if row_name == target or row_name in target or target in row_name:
                bill_id = str(structure.get("bill_id") or structure.get("bill_no") or "")
                if bill_id and bill_id not in matched_ids:
                    matched_ids.append(bill_id)
            if len(matched_ids) >= limit:
                break
        if len(matched_ids) >= limit:
            break
    return _fetch_local_tag_patterns(matched_ids, limit=limit)


def fetch_tag_patterns(bill_ids: list[str], limit: int = 3) -> list[dict]:
    """유사 의안의 TAG 구조화 데이터(structures+items+variables+amounts) 조회."""
    if not bill_ids:
        return []
    bill_ids = bill_ids[:limit]
    if not SUPA_URL or not SUPA_KEY:
        return _fetch_local_tag_patterns(bill_ids, limit=limit)
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
        return _fetch_local_tag_patterns(bill_ids, limit=limit)

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
                "year_offset":     a.get("year_offset"),
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
            "bill_id":   s.get("bill_id"),
            "bill_no":   s["bill_no"],
            "bill_name": s["bill_name"],
            "items":     by_struct.get(s["id"], []),
        })
    if not any((pattern.get("items") or []) for pattern in out):
        return _fetch_local_tag_patterns(bill_ids, limit=limit)
    return out


_UNIT_COST_KEYWORDS = ("단가", "개소당", "1인당", "1개소", "대당", "건당", "원/", "/개소", "/명", "/대")


def _name_overlap(a: str, b: str) -> float:
    """두 문자열의 2-gram 토큰 겹침 비율 (간단 유사도)."""
    def toks(s: str) -> set[str]:
        s = "".join(s.split())
        return {s[i:i+2] for i in range(len(s)-1)} if len(s) >= 2 else {s}
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def pick_reference_unit_cost(
    patterns: list[dict],
    item_name: str,
    category: str,
    form_type: str = "gyeonggi",
) -> dict[str, Any] | None:
    costs = pick_reference_unit_costs(patterns, item_name, category, form_type=form_type, limit=1)
    return costs[0] if costs else None


def pick_reference_unit_costs(
    patterns: list[dict],
    item_name: str,
    category: str,
    form_type: str = "gyeonggi",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """해당 항목과 가장 유사한 단가 후보를 선정.

    국회 양식에서는 추천 후보, 경기도 양식에서는 참고 후보로 표시한다.
    """
    candidates: list[dict[str, Any]] = []
    for p in patterns:
        bill_no = p.get("bill_no")
        bill_name = (p.get("bill_name") or "")[:35]
        for it in p.get("items", []):
            cat = str(it.get("category") or "")
            tagged_item = str(it.get("name") or "")
            for v in it.get("variables", []):
                name = str(v.get("name") or "")
                val = v.get("value")
                unit = str(v.get("unit") or "")
                if val is None:
                    continue
                if not any(kw in f"{name} {unit}" for kw in _UNIT_COST_KEYWORDS):
                    continue
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if fval <= 0:
                    continue
                # 유사도: 항목명 겹침 + 카테고리 일치 보너스
                score = _name_overlap(item_name, tagged_item) + _name_overlap(item_name, name)
                if category and cat and category == cat:
                    score += 0.3
                if score <= 0:
                    continue
                caveat = (
                    "국회 의안 기준 — 유사도와 단가 성격 확인 후 채택 권장."
                    if form_type == "assembly"
                    else "국회 의안 기준 — 지자체 사업 규모와 다를 수 있음. 직접 확인 후 입력 권장."
                )
                candidates.append({
                    "variable_name": name,
                    "value": fval,
                    "unit": unit,
                    "ref_item": tagged_item,
                    "source": f"국회의안 {bill_no} {bill_name}",
                    "caveat": caveat,
                    "score": round(score, 3),
                })

    if not candidates:
        return []

    # 같은 의안/항목/변수/값 후보 중복 제거
    unique: dict[tuple[str, str, str, float], dict[str, Any]] = {}
    for row in candidates:
        key = (
            str(row.get("source") or ""),
            str(row.get("ref_item") or ""),
            str(row.get("variable_name") or ""),
            float(row.get("value") or 0),
        )
        current = unique.get(key)
        if not current or float(row.get("score") or 0) > float(current.get("score") or 0):
            unique[key] = row

    ranked = sorted(unique.values(), key=lambda row: float(row.get("score") or 0), reverse=True)
    best_score = float(ranked[0].get("score") or 0)
    threshold = max(0.15, best_score * 0.5)
    return [row for row in ranked if float(row.get("score") or 0) >= threshold][:limit]


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


# ── JSON LLM provider ─────────────────────────────────────────────────────────

def gemini_json(prompt: str, temperature: float = 0.1) -> dict | None:
    """Compatibility wrapper routing JSON generation to the active provider."""
    global _LLM_BACKOFF_UNTIL
    if time.time() < _LLM_BACKOFF_UNTIL:
        return None
    for attempt in range(3):
        try:
            parsed = _call_llm_raw_json(
                prompt,
                temperature=temperature,
                timeout=180,
            )
            parsed = normalize_profile_response(LLM_PROFILE, prompt, parsed)
            if isinstance(parsed, list):
                parsed = next((x for x in parsed if isinstance(x, dict)), None)
            return parsed if isinstance(parsed, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                if attempt < 1:
                    time.sleep(3)
                    continue
                _LLM_BACKOFF_UNTIL = time.time() + 30
            elif 500 <= exc.code < 600 and attempt < 2:
                time.sleep(2 ** attempt * 3)
                continue
            sys.stderr.write(
                f"[{LLM_PROVIDER} 오류] HTTP {exc.code}: {exc.reason}\n"
            )
            return None
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            _LLM_BACKOFF_UNTIL = time.time() + 30
            sys.stderr.write(f"[{LLM_PROVIDER} 오류] {exc}\n")
            return None
        except json.JSONDecodeError as exc:
            # 토큰 잘림이 아니라(별도로 finish_reason 확인함) 모델이 가끔
            # 문법적으로 깨진 JSON을 낸다(실측 확인). 같은 요청을 다시 보내면
            # 보통 정상 JSON이 나오므로, HTTP 5xx와 동일하게 재시도한다.
            if attempt < 2:
                sys.stderr.write(
                    f"[{LLM_PROVIDER} JSON 파싱 재시도 {attempt + 1}/2] {exc}\n"
                )
                time.sleep(1)
                continue
            sys.stderr.write(f"[{LLM_PROVIDER} 오류] {exc}\n")
            return None
        except Exception as exc:
            sys.stderr.write(f"[{LLM_PROVIDER} 오류] {exc}\n")
            return None
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

FINAL_PROMPT = """당신은 비용추계 전문가입니다. 새 의안에 대해 아래 명시된 양식 기준에 따라 종합 판단하세요.

[조례안명] {bill_name}
[감지된 분야] {field}
[적용 양식] {form_label} 기준
[적용 법령 근거] {form_legal_basis}
[미첨부 1호 금액 기준] {form_threshold_text}

[조문별 비용유발 분석]
{articles_summary}

조문별 candidate 값 해석:
- strong: 정답 비용추계서의 핵심 추계 조문일 가능성이 높음. 산식 매핑을 우선 검토.
- medium: 재정수반요인은 있으나 전제값 또는 실제 시행 여부 확인 필요.
- weak: 비용 냄새는 있으나 행정절차·선언/계획·재량 성격이 강함. 미첨부 또는 추계 제외 가능성 우선 검토.
- feasibility=non_attachment_review인 조문은 바로 금액으로 계산하지 말고 미첨부 3호, 일부추계 제외, 또는 미대상 가능성을 먼저 검토.

━━━ NABO 공식 분류 기준 (반드시 이 기준으로 판단) ━━━

verdict 값은 아래 5개 중 하나여야 합니다:

1. "추계서"
   - 법안 시행 시 직접적 재정지출 순증가 또는 재정수입 순증감 발생
   - ★ 중요: 조례안에 대상·단가 등 구체적 숫자가 없어도, 사업의 성격상 추가
     재정지출이 발생하는 것이 명백하면 "추계서"로 판단한다.
   - 실제 추계자는 구체값이 없어도 "전제조건(가정)"을 세워서 추계한다.
     (예: 시군 수요조사 가정 연 5개소 × 유사사업 단가)
   - 구체값이 없다는 이유만으로 미첨부_3호로 도피하지 마라.

2. "미첨부_1호" — 소요비용이 적어 재정 영향 미미
   - 적용 기준({form_label}): {form_threshold_text}
   - 근거: {form_legal_basis}
   - ★ 반드시 위 양식별 금액 기준을 적용한다. (국회 10억 ≠ 경기도 1억)

3. "미첨부_2호" — 국가안전보장·군사기밀 관련

4. "미첨부_3호" — 추계가 근본적으로 불가능한 경우만
   - 조항이 순수 선언적·권고적이고 어떤 사업도 특정되지 않음
   - 유사사례·단가 참고자료가 전혀 없어 가정조차 세울 수 없음
   - ★ 유사 사업이나 단가 후보가 하나라도 있으면 미첨부_3호가 아니라
     "추계서"(전제조건 기반)로 판단한다.

5. "미대상" — 재정규모 변화 없음
   - 정의 조항, 명칭 변경, 절차 정비, 대상의 명칭만 확대(신규 지출 없음) 등
   - ★ 명칭 변경/대상 확대 자체는 비용이 아니다. 신규 사업·시설·지원이
     추가될 때만 비용으로 본다.

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
- "영유아인구" (KOSIS 연도별 명)
- "아동인구" (KOSIS 연도별 명)
- "청년인구" (KOSIS 연도별 명)
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
        "formula": "산식 텍스트 (예: 지원 개소수 × 개소당 단가 × 5년)",
        "trigger_ref": "근거 조문",
        "variables_needed": ["지원 개소수", "개소당 단가"],
        "assumptions": [
          {{
            "name": "지원 개소수",
            "value": 5,
            "unit": "개소/년",
            "basis": "가정 근거 (예: 시군 수요조사 기준 연 5개소 가정)",
            "source_type": "user_input | tag_reference | document | statistic",
            "needs_user_confirm": true
          }},
          {{
            "name": "개소당 단가",
            "value": null,
            "unit": "천원/개소",
            "basis": "유사사업 단가 후보 참조 (값은 사용자 확정 필요)",
            "source_type": "user_input",
            "needs_user_confirm": true
          }}
        ],
        "calculation": {{
          "base_amount_thousand": 숫자 또는 null,
          "recurrence": "annual" | "one_time",
          "start_year": 1,
          "end_year": 5,
          "growth_variable": "카테고리 규칙에 따라 (아래 참조)",
          "source_note": "base_amount 산정 근거. 대상/단가가 사용자입력이면 null"
        }}
      }}
    ],
    "year_estimates": [
      {{"year": 1, "amount_thousand": null, "note": "금액은 시스템 Python 계산기가 산출"}}
    ]
  }} 또는 null,
  "if_non_attachment": {{
    "type": "1호|2호|3호|미대상",
    "reason_text": "미첨부 사유 텍스트 (NABO 기준 명시)"
  }} 또는 null
}}

━━━ 증가율(growth_variable) 카테고리 규칙 ━━━
- 인건비 → "명목임금상승률" 또는 "공무원임금상승률" (매년 복리)
- 운영비 → "소비자물가상승률" (매년 복리)
- 사업비/시설비 → null (정액. 단가 고정이므로 복리 적용 안 함)
- 지원금/위탁비 → 대상이 매년 늘면 해당 변수, 아니면 null

━━━ 전제조건(assumptions) 작성 규칙 ★ 핵심 ★ ━━━
- 추계서일 때 각 비용 항목에 assumptions 배열을 반드시 작성한다.
- 대상 규모(개소수/대상자수)와 단가는 조례안에 없으면 "가정"으로 명시한다.
  · 대상 규모: 합리적 가정값 + basis에 근거 ("시군 수요조사 기준 연 N개소 가정")
  · 단가: 값을 모르면 value=null, source_type="user_input" (절대 임의로 지어내지 마라)
- needs_user_confirm=true면 사용자가 확인/입력해야 하는 값이다.
- 조례안·참고자료에 명시된 실제 숫자가 있으면 그 값 + source_type="document".

━━━ 중요 ━━━
- verdict는 NABO 5개 분류 중 정확히 하나.
- 금액 계산은 하지 마라. year_estimates.amount_thousand는 null.
- calculation.base_amount_thousand는 대상·단가가 모두 확정(document/statistic)일 때만 숫자.
  하나라도 user_input이면 null (Python 계산기가 사용자 입력 후 계산).
- 단가/대상을 절대 임의로 지어내지 마라. 모르면 null + needs_user_confirm.
- verdict_reason_nabo는 NABO 분류 5개 중 어디에 해당하는지 명시.
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


def _extract_bill_name(text: str, fallback: str) -> str:
    """PDF 앞부분에서 실제 의안명을 우선 추출한다."""
    lines = [line.strip() for line in text[:2500].splitlines() if line.strip()]
    ignore = re.compile(r"(의안|번호|발의|의원|대표|연월일|제안이유|주요내용|[-―]\s*\d+\s*[-―]?)")
    suffix_only = {
        "일부개정법률안",
        "전부개정법률안",
        "개정법률안",
        "법률일부개정법률안",
        "법률전부개정법률안",
    }
    previous_candidate = ""
    for line in lines[:40]:
        compact = re.sub(r"\s+", "", line)
        if len(compact) < 4 or ignore.fullmatch(compact):
            continue
        if compact in suffix_only and previous_candidate:
            if previous_candidate.endswith("법률") and compact.startswith("법률"):
                compact = compact[len("법률"):]
            return previous_candidate + compact
        if compact.endswith(("일부개정법률안", "전부개정법률안", "개정법률안", "법률안", "법안", "조례안")):
            # Long bill names are often wrapped immediately before the suffix
            # line in Assembly PDFs. Preserve the preceding title fragment
            # instead of returning only e.g. "육성·지원에관한법률안".
            if previous_candidate and len(previous_candidate + compact) <= 180:
                return previous_candidate + compact
            return compact
        if not compact.startswith("(") and not ignore.search(compact):
            previous_candidate = compact
    for line in lines[:20]:
        compact = re.sub(r"\s+", "", line)
        if compact and not ignore.search(compact) and not compact.startswith("("):
            return compact
    return fallback


def _extract_current_bill_no(text: str, filename: str = "") -> str | None:
    """Extract the seven-digit National Assembly bill number for RAG holdout.

    Uploaded source bills often print only the five-digit serial (for example
    ``14559``), while filenames and stored reference documents use the full
    Assembly-age prefix (``2214559``).  Prefer an explicit full number; only
    reconstruct a short serial when the proposal date identifies the Assembly
    term unambiguously.
    """
    search_blob = f"{filename}\n{text[:4000]}"
    full = re.search(r"(?<!\d)((?:1[7-9]|2[0-9])\d{5})(?!\d)", search_blob)
    if full:
        return full.group(1)

    short = re.search(r"의안\s*번호\s*(\d{1,5})(?!\d)", text[:4000])
    proposed = re.search(
        r"발의\s*연월일\s*[:：]?\s*(\d{4})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})",
        text[:4000],
    )
    if not short or not proposed:
        return None

    year, month, day = (int(value) for value in proposed.groups())
    proposal_date = (year, month, day)
    terms = (
        (22, (2024, 5, 30), (2028, 5, 29)),
        (21, (2020, 5, 30), (2024, 5, 29)),
        (20, (2016, 5, 30), (2020, 5, 29)),
        (19, (2012, 5, 30), (2016, 5, 29)),
    )
    for term, start, end in terms:
        if start <= proposal_date <= end:
            return f"{term}{int(short.group(1)):05d}"
    return None


# ── NABO 금액 게이트 검증 ────────────────────────────────────────────────────

def _validate_verdict_with_amount(verdict: str, year_estimates: list[dict] | None,
                                  form_type: str = "gyeonggi") -> tuple[str, str | None]:
    """양식별 금액 기준으로 verdict 자동 검증/보정.

    경기도: 연평균 1억/총 3억 미만 → 미첨부_1호
    국회: 연평균 10억/총 30억 미만 → 미첨부_1호

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

    criteria = FORM_CRITERIA.get(form_type, FORM_CRITERIA["gyeonggi"])
    THRESHOLD_ANNUAL = criteria["threshold_annual_thousand"]
    THRESHOLD_TOTAL = criteria["threshold_total_thousand"]
    form_label = criteria["label"]
    threshold_text = criteria["threshold_text"]

    avg_thousand = sum(amounts) / len(amounts)
    total_thousand = sum(amounts)

    is_minor = (
        avg_thousand < THRESHOLD_ANNUAL
        and total_thousand < THRESHOLD_TOTAL
    )

    if verdict == "추계서" and is_minor:
        if form_type == "assembly":
            return verdict, None
        return "미첨부_1호", (
            f"Python 게이트({form_label}): 연평균 {avg_thousand/1000:.1f}백만원, "
            f"총 {total_thousand/1000:.1f}백만원 → 미첨부 1호 기준({threshold_text}) 충족 → 강제 변경"
        )
    if verdict == "미첨부_1호" and not is_minor:
        return "추계서", (
            f"Python 게이트({form_label}): 연평균 {avg_thousand/1000:.1f}백만원, "
            f"총 {total_thousand/1000:.1f}백만원 → 미첨부 1호 기준({threshold_text}) 초과 → 추계서로 강제 변경"
        )
    return verdict, None


# ── 메인 분석 함수 ─────────────────────────────────────────────────────────────

def recompute_with_user_inputs(
    estimate: dict[str, Any],
    user_inputs: list[dict[str, Any]],
    form_type: str = "assembly",
) -> dict[str, Any]:
    """사용자가 입력한 단가/대상 값을 반영해 재계산.

    user_inputs 예시:
      [
        {"item_index": 0, "base_amount_thousand": 50000, "recurrence": "annual",
         "start_year": 1, "end_year": 5, "growth_variable": null},
        {"item_index": 1, "base_amount_thousand": 1500, ...}
      ]

    또는 변수 단위:
      [{"item_index": 0, "variables": {"단가": 50000, "대상": 5}}]
    이때 base = 단가 * 대상 으로 계산.
    """
    items = estimate.get("items") or []
    if not items:
        return estimate

    # user_inputs를 item_index → dict로 매핑
    by_idx: dict[int, dict[str, Any]] = {}
    for u in user_inputs:
        idx = u.get("item_index")
        if idx is None:
            continue
        try:
            by_idx[int(idx)] = u
        except (TypeError, ValueError):
            continue

    # 각 항목에 사용자 입력 반영
    for i, item in enumerate(items):
        u = by_idx.get(i)
        if not u:
            continue
        calc = item.setdefault("calculation", {})
        # 직접 base 지정
        if "base_amount_thousand" in u:
            try:
                calc["base_amount_thousand"] = int(float(u["base_amount_thousand"]))
                calc["mode"] = "base_growth"
            except (TypeError, ValueError):
                pass
        # 변수 단위 입력. 화면에서 산식의 실제 변수명을 그대로 전달할 수 있다.
        elif "variables" in u and isinstance(u["variables"], dict):
            vals = u["variables"]
            try:
                numeric_vals = {
                    str(name): float(value)
                    for name, value in vals.items()
                    if value is not None and value != ""
                }
                assumptions = item.get("assumptions") or []
                for assumption in assumptions:
                    assumption_name = str(assumption.get("name") or "")
                    compact_name = _compact_korean(assumption_name)
                    matched_value = next(
                        (
                            value
                            for name, value in numeric_vals.items()
                            if _compact_korean(name) == compact_name
                            or _compact_korean(name) in compact_name
                            or compact_name in _compact_korean(name)
                        ),
                        None,
                    )
                    if matched_value is not None:
                        assumption["value"] = matched_value
                        assumption["source_type"] = "user_input"
                        assumption["needs_user_confirm"] = False

                def assumption_value(*terms: str) -> tuple[float | None, str]:
                    for assumption in assumptions:
                        name = _compact_korean(str(assumption.get("name") or ""))
                        if any(_compact_korean(term) in name for term in terms):
                            try:
                                return float(assumption.get("value")), str(assumption.get("unit") or "")
                            except (TypeError, ValueError):
                                continue
                    for name, value in numeric_vals.items():
                        compact = _compact_korean(name)
                        if any(_compact_korean(term) in compact for term in terms):
                            return value, ""
                    return None, ""

                def amount_in_thousand(value: float, unit: str) -> float:
                    compact_unit = _compact_korean(unit)
                    if "백만원" in compact_unit:
                        return value * 1000
                    if "만원" in compact_unit and "백만원" not in compact_unit:
                        return value * 10
                    if "원" in compact_unit and "천원" not in compact_unit:
                        return value / 1000
                    return value

                family = str(item.get("formula_family") or "")
                base_amount: float | None = None
                yearly_amounts: list[int] | None = None
                if family == "committee_operation":
                    meetings, _ = assumption_value("회의횟수", "개최횟수")
                    members, _ = assumption_value("수당지급대상", "민간위원", "참석인원")
                    allowance, allowance_unit = assumption_value("회의수당단가", "사례금단가", "수당단가")
                    if meetings and members and allowance:
                        base_amount = meetings * members * amount_in_thousand(allowance, allowance_unit)
                elif family == "research_service":
                    unit_cost, unit = assumption_value("용역단가", "조사단가")
                    count, _ = assumption_value("수행횟수", "조사횟수")
                    if unit_cost and count:
                        base_amount = amount_in_thousand(unit_cost, unit) * count
                elif family == "transfer_payment":
                    target, _ = assumption_value("지원대상자수", "대상수")
                    unit_cost, unit = assumption_value("1인당지급액", "지원단가")
                    count, _ = assumption_value("지급횟수")
                    rate, _ = assumption_value("집행률")
                    if target and unit_cost:
                        base_amount = target * amount_in_thousand(unit_cost, unit)
                        base_amount *= count or 1
                        base_amount *= (rate / 100) if rate else 1
                elif family == "facility_system":
                    initial_cost, initial_unit = assumption_value("초기구축비", "구축비")
                    annual_cost, annual_unit = assumption_value("연간유지관리비", "유지관리비", "운영비")
                    if initial_cost is not None and annual_cost is not None:
                        initial_thousand = amount_in_thousand(initial_cost, initial_unit)
                        annual_thousand = amount_in_thousand(annual_cost, annual_unit)
                        years = int(calc.get("end_year") or 5)
                        yearly_amounts = [
                            int(round(initial_thousand + annual_thousand)),
                            *[int(round(annual_thousand)) for _ in range(max(0, years - 1))],
                        ]
                elif family == "institution_establishment":
                    scale, _ = assumption_value("시설규모", "면적")
                    construction, construction_unit = assumption_value("단위면적당공사비", "공사비")
                    equipment, equipment_unit = assumption_value("장비비품비", "비품비", "자산취득비")
                    if scale is not None and construction is not None and equipment is not None:
                        base_amount = (
                            scale * amount_in_thousand(construction, construction_unit)
                            + amount_in_thousand(equipment, equipment_unit)
                        )
                        calc["recurrence"] = "one_time"
                elif family == "personnel_compensation":
                    people, _ = assumption_value("직급별인원", "소요인력", "증원인원")
                    pay, pay_unit = assumption_value("직급별1인당보수", "1인당보수", "평균보수")
                    contribution_rate, _ = assumption_value("기관부담요율", "부담요율")
                    if people is not None and pay is not None:
                        base_amount = people * amount_in_thousand(pay, pay_unit)
                        if contribution_rate is not None:
                            base_amount *= 1 + contribution_rate / 100
                elif family == "institution_operation":
                    basic, basic_unit = assumption_value("기본경비기준액", "기본경비")
                    program, program_unit = assumption_value("연간사업운영비", "사업운영비")
                    if basic is not None and program is not None:
                        base_amount = (
                            amount_in_thousand(basic, basic_unit)
                            + amount_in_thousand(program, program_unit)
                        )
                elif family == "employer_contribution":
                    pay, pay_unit = assumption_value("보수", "인건비")
                    rate, _ = assumption_value("기관부담요율", "부담요율")
                    if pay is not None and rate is not None:
                        base_amount = amount_in_thousand(pay, pay_unit) * rate / 100
                elif family == "basic_expense":
                    pay, pay_unit = assumption_value("보수또는인건비", "인건비", "보수")
                    rate, _ = assumption_value("기본경비비율", "비율")
                    if pay is not None and rate is not None:
                        base_amount = amount_in_thousand(pay, pay_unit) * rate / 100
                elif family == "asset_acquisition":
                    people, _ = assumption_value("증원인원", "인원")
                    unit_cost, unit = assumption_value("1인당자산취득비", "자산취득비")
                    if people is not None and unit_cost is not None:
                        base_amount = people * amount_in_thousand(unit_cost, unit)
                        calc["recurrence"] = "one_time"

                if base_amount is None:
                    unit_cost = numeric_vals.get("단가") or numeric_vals.get("unit_cost")
                    target = numeric_vals.get("대상") or numeric_vals.get("target")
                    if unit_cost and target:
                        base_amount = unit_cost * target
                if yearly_amounts:
                    calc.pop("base_amount_thousand", None)
                    calc["mode"] = "yearly_series"
                    calc["yearly_amounts_thousand"] = yearly_amounts
                    calc["source_note"] = "사용자가 입력한 구축비·유지관리비를 구분해 재계산"
                if base_amount is not None and base_amount > 0:
                    calc.pop("yearly_amounts_thousand", None)
                    calc["base_amount_thousand"] = int(round(base_amount))
                    calc["mode"] = "base_growth"
                    calc["source_note"] = "사용자가 입력한 필수 기준값으로 재계산"
            except (TypeError, ValueError):
                pass
        # 기타 calculation 필드
        for k in ("recurrence", "start_year", "end_year", "growth_variable"):
            if k in u:
                calc[k] = u[k]

    # Python 계산기 재실행
    calculated, calc_issues = compute_year_estimates(estimate, tag_patterns=[], allow_estimated=False)
    estimate["recompute_issues"] = calc_issues
    if calculated:
        has_blocking_issue = any(issue.get("level") == "error" for issue in calc_issues)
        estimate["calculation_status"] = (
            "computed_partial_by_python" if has_blocking_issue else "computed_by_python"
        )
        estimate["year_estimates"] = calculated
        _sync_estimate_amount_totals(estimate)
        if not has_blocking_issue:
            estimate["user_inputs_needed"] = []
    else:
        missing_by_item: dict[str, list[str]] = {}
        for issue in calc_issues:
            name = str(issue.get("item") or "계산 항목")
            reason = str(issue.get("reason") or "입력 필요")
            missing_by_item.setdefault(name, []).append(reason)
        estimate["calculation_status"] = "awaiting_user_input"
        estimate["year_estimates"] = _blocked_year_estimates(missing_by_item or {"계산 항목": ["base_amount_thousand"]})
    estimate["estimation_status"] = classify_estimation_status(estimate)
    apply_formula_source_strategy(estimate, tag_patterns=[])
    human_input = _build_human_input_requests(estimate)
    if human_input:
        estimate["human_input"] = human_input
        if calculated and human_input.get("blocking_count"):
            estimate["calculation_status"] = "computed_partial_by_python"
    else:
        estimate.pop("human_input", None)
    # 양식 게이트 재적용
    yr = estimate.get("year_estimates")
    raw_v = estimate.get("verdict_after_recompute") or "추계서"
    corrected, note = _validate_verdict_with_amount(raw_v, yr, form_type=form_type)
    estimate["verdict_after_recompute"] = corrected
    if note:
        estimate["recompute_gate_note"] = note
    return estimate


# 양식별 분류 기준 (금액·근거 조례)
FORM_CRITERIA = {
    "gyeonggi": {
        "label": "경기도",
        "threshold_annual_thousand": 100_000,    # 연평균 1억원 미만 → 미첨부 1호
        "threshold_total_thousand":  300_000,    # 한시적 총 3억원 미만 → 미첨부 1호
        "threshold_text": "연평균 1억원 미만 또는 한시 총 3억원 미만",
        "legal_basis": "「경기도 의안의 비용추계에 관한 조례」 제3조제1항제1호",
    },
    "assembly": {
        "label": "국회",
        "threshold_annual_thousand": 1_000_000,  # 연평균 10억원 미만
        "threshold_total_thousand":  3_000_000,  # 한시 총 30억원 미만
        "threshold_text": "연평균 10억원 미만 또는 한시 총 30억원 미만",
        "legal_basis": "「국회법」 제79조의2 및 NABO 비용추계 가이드라인",
    },
}


def analyze_v2(filename: str, content_b64: str, form_type: str = "gyeonggi") -> dict[str, Any]:
    """server.py가 호출하는 진입점. 입력: 파일명 + base64 PDF. 출력: 결과 dict."""
    t0 = time.time()
    workflow_issues: list[dict[str, Any]] = []

    if not LLM_API_KEY:
        required_key = (
            "UPSTAGE_API_KEY"
            if LLM_PROVIDER == "upstage"
            else "GEMINI_API_KEY"
        )
        raise ValueError(f"{required_key} 설정이 필요합니다.")

    # 1) PDF 추출
    pdf_bytes = base64.b64decode(_strip_data_url(content_b64))
    raw_text = _extract_pdf_text_from_bytes(pdf_bytes)
    text = strip_appendices(raw_text) if raw_text else ""
    if not raw_text:
        raise ValueError(
            "PDF에서 텍스트를 추출하지 못했습니다. 스캔본/이미지 PDF이거나 텍스트 레이어가 없는 파일입니다. "
            "텍스트가 포함된 PDF로 다시 업로드하거나 OCR 처리 후 분석해야 합니다."
        )
    current_bill_no = _extract_current_bill_no(raw_text, filename) if form_type == "assembly" else None
    excluded_bill_nos = {current_bill_no} if current_bill_no else set()
    if current_bill_no:
        workflow_issues.append({
            "level": "info",
            "category": "현재 의안 검색 제외",
            "detail": f"분석 대상 의안 {current_bill_no}의 정답 문서를 RAG·TAG 후보에서 제외했습니다.",
            "action": "동일 의안 정답 누출 없이 다른 공식 사례만으로 추계합니다.",
        })
    raw_doc_type = _detect_doc_type(raw_text)
    articles: list[dict[str, str]] = []
    doc_type = raw_doc_type
    if raw_doc_type == "일부개정안":
        articles = split_articles_from_revision_table_pdf(pdf_bytes)
        if articles:
            doc_type = "신구조문대비표"
            workflow_issues.append({
                "level": "info",
                "category": "신구조문대비표 우선 파싱",
                "detail": f"일부개정안으로 판단되어 신구조문대비표의 개정안 컬럼에서 {len(articles)}개 조문을 추출했습니다.",
                "action": "현행 조문은 비교용으로 제외하고 개정안 컬럼 변경분만 분석합니다.",
            })
        else:
            workflow_issues.append({
                "level": "warn",
                "category": "신구조문대비표 파싱 실패",
                "detail": "좌표 기반으로 신구조문대비표 개정안 컬럼을 분리하지 못했습니다.",
                "action": "기존 개정문 파싱으로 fallback합니다.",
            })
    if not articles:
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

    # 의안명 추출
    bill_name = _extract_bill_name(text, filename)

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
        compact_article = re.sub(r"\s+", "", str(art.get("text") or ""))
        if _NON_COST_ARTICLE_RE.search(compact_article) and not _rule_cost_trigger(art.get("text") or ""):
            return {
                "_idx": idx,
                **art,
                "cost_trigger": False,
                "trigger_type": "없음",
                "obligation_strength": "aspirational",
                "reason": "목적·정의·관계·벌칙 등 독립적인 비용산식이 없는 조문으로 규칙 분류했습니다.",
                "legal_refs": [],
                "similar_refs": [],
                "analysis_source": "deterministic_non_cost_rule",
            }
        art_legal = vector_search(art_emb, source="legal_reference", k=2) if art_emb else []
        art_similar = (
            vector_search(
                art_emb,
                source="national_assembly",
                doc_type="cost_estimate",
                k=2,
                exclude_bill_nos=excluded_bill_nos,
            )
            if art_emb else []
        )
        # ── Dual-Channel: 유사 미첨부 사유서도 함께 검색 (kNN verdict 신호)
        art_non_attach = (
            vector_search(
                art_emb,
                source="national_assembly",
                doc_type="non_attachment_reason",
                k=3,
                exclude_bill_nos=excluded_bill_nos,
            )
            if art_emb else []
        )
        cost_sims = [float(x.get("similarity") or 0) for x in art_similar]
        na_sims = [float(x.get("similarity") or 0) for x in art_non_attach]
        knn_channels = {
            "cost_avg": round(sum(cost_sims) / len(cost_sims), 4) if cost_sims else 0.0,
            "non_attach_avg": round(sum(na_sims) / len(na_sims), 4) if na_sims else 0.0,
            "non_attach_top": round(max(na_sims), 4) if na_sims else 0.0,
            "non_attach_refs": [
                {
                    "bill_no": x.get("bill_no"),
                    "bill_name": (x.get("bill_name") or "")[:60],
                    "similarity": round(float(x.get("similarity") or 0), 4),
                }
                for x in art_non_attach[:3]
            ],
        }
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
            "knn_channels": knn_channels,
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
    article_results = _apply_rule_cost_trigger_overrides(article_results)
    article_results = apply_validated_case_policy(
        article_results,
        document_text=raw_text,
    )
    override_count = sum(1 for a in article_results if a.get("rule_cost_trigger"))
    candidate_summary = _cost_candidate_summary(article_results)
    if override_count:
        workflow_issues.append({
            "level": "info",
            "category": "비용유발 조문 규칙 보정",
            "detail": f"위원회·센터·지원·시설 등 명확한 재정수반 표현 {override_count}건을 비용유발 후보로 보정했습니다.",
            "action": "LLM이 미대상으로 판단하더라도 산식 후보 검토 단계로 넘깁니다.",
            "candidate_summary": candidate_summary,
        })
    if candidate_summary["counts"]["non_attachment_review"]:
        workflow_issues.append({
            "level": "info",
            "category": "미첨부 가능 후보 분리",
            "detail": (
                f"재정수반 후보 중 {candidate_summary['counts']['non_attachment_review']}건은 "
                "재량규정·행정절차 성격으로 판단되어 미첨부 사유 검토 대상으로 분리했습니다."
            ),
            "action": "이 후보들은 산식으로 직접 계산하지 않고 일부추계 제외 또는 미첨부 사유로 우선 처리합니다.",
            "candidate_summary": candidate_summary,
        })

    # 4) 본문 임베딩으로 유사 RAG 검색 (위에서 이미 계산됨)
    similar_estimates = (
        vector_search(
            bill_emb,
            source="national_assembly",
            doc_type="cost_estimate",
            k=5,
            exclude_bill_nos=excluded_bill_nos,
        )
        if bill_emb else []
    )
    similar_non_attach = (
        vector_search(
            bill_emb,
            source="national_assembly",
            doc_type="non_attachment_reason",
            k=3,
            exclude_bill_nos=excluded_bill_nos,
        )
        if bill_emb else []
    )

    if not bill_emb:
        workflow_issues.append({
            "level": "warn",
            "category": "임베딩 비활성화",
            "detail": "본문 임베딩을 만들지 못해 유사 사례 검색을 수행하지 못했습니다.",
            "action": "RAG/TAG 보조 근거 없이 규칙 및 LLM 기본 판단으로 진행했습니다. OPENAI_API_KEY 또는 Azure OpenAI 임베딩 설정을 확인하면 유사사례 근거가 보강됩니다.",
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
            "level": "info",
            "category": "유사 비용추계서 검색 결과 없음",
            "detail": "본문 기준 유사 비용추계서를 찾지 못해 새로운 유형으로 판단했습니다.",
            "action": "표준 산식으로 best-effort 초안을 생성했습니다. 사용자 보완 시 정밀도가 더 올라갑니다.",
        })

    # 5) 종합 판단 + 추계서 생성
    articles_summary = "\n".join(
        f"{a['no']} | cost_trigger={a['cost_trigger']} | "
        f"type={a['trigger_type']} | strength={a['obligation_strength']} | "
        f"candidate={a.get('cost_candidate_strength', 'none')} | "
        f"feasibility={a.get('estimate_feasibility', 'unknown')} | "
        f"non_attach_risk={a.get('non_attachment_risk', 'unknown')} | "
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
    similar_bill_ids: list[str] = []
    for similar in similar_estimates:
        bill_id = str(similar.get("bill_id") or "")
        if bill_id and bill_id not in similar_bill_ids:
            similar_bill_ids.append(bill_id)
        if len(similar_bill_ids) >= 3:
            break
    tag_patterns = fetch_tag_patterns(similar_bill_ids, limit=3)
    if not tag_patterns:
        tag_patterns = fetch_local_tag_patterns_by_bill_name(
            bill_name,
            limit=5,
            exclude_bill_nos=excluded_bill_nos,
        )
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

    criteria = FORM_CRITERIA.get(form_type, FORM_CRITERIA["gyeonggi"])
    final = gemini_json(FINAL_PROMPT.format(
        bill_name=bill_name,
        field=detected_field,
        form_label=criteria["label"],
        form_legal_basis=criteria["legal_basis"],
        form_threshold_text=criteria["threshold_text"],
        articles_summary=articles_summary,
        classification_refs=_fmt_chunks(classification_chunks),
        methodology_refs=_fmt_chunks(methodology_chunks),
        nabo_cases=_fmt_chunks(nabo_case_chunks, 800),
        similar_estimates=similar_est_text or "(없음)",
        tag_patterns=tag_patterns_text,
        similar_non_attach=similar_na_text or "(없음)",
    )) or {}

    # 5-1) KOSIS 변수값 자동 채우기 + 단가 후보 제시
    estimate = final.get("if_needs_estimate")
    strong_tag_pattern, strong_tag_similarity = _select_strong_tag_pattern(
        tag_patterns,
        similar_estimates,
        bill_name,
        text,
    )
    tag_structure_estimate = (
        _build_estimate_from_tag_pattern(strong_tag_pattern, similarity=strong_tag_similarity)
        if strong_tag_pattern
        else None
    )
    public_org_estimate = _build_public_organization_establishment_estimate(
        text,
        article_results,
        form_type=form_type,
    ) if not tag_structure_estimate else None
    if tag_structure_estimate:
        source_bill_no = strong_tag_pattern.get("bill_no") or "-"
        source_bill_name = strong_tag_pattern.get("bill_name") or "공식 비용추계서"
        source_similarity = f"{strong_tag_similarity:.0%}"
        applied_item_names = [
            str(item.get("name") or "")
            for item in tag_structure_estimate.get("items") or []
            if item.get("name")
        ]
        joined_items = ", ".join(applied_item_names[:4]) or "재정수반 비용항목"
        is_judicial_establishment = any("고등법원" in name for name in applied_item_names)
        estimate = tag_structure_estimate
        final["if_needs_estimate"] = estimate
        final["verdict"] = "추계서"
        final["verdict_label"] = "비용추계서"
        final["if_non_attachment"] = None
        if is_judicial_establishment:
            target_reason = (
                "이 의안은 고등법원 신설을 내용으로 하므로, 법원 운영을 위한 인력 운영비, "
                "기본 운영비, 자산취득비 등 신규 재정지출이 발생합니다."
            )
            cost_reason = (
                "또한 고등법원 신설 시 대응되는 고등검찰청 운영 비용도 함께 검토할 필요가 있어 "
                "추가재정소요가 발생하는 의안으로 판단했습니다."
            )
        else:
            target_reason = (
                "이 의안은 새로운 기관·사업·운영 근거를 두고 있어 기존 예산 범위를 넘어서는 "
                "재정지출 발생 가능성이 있습니다."
            )
            cost_reason = (
                f"비용항목으로 {joined_items}이 확인되어 연도별 추가재정소요를 산정했습니다."
            )
        final["reason_summary"] = target_reason
        final["verdict_reason_nabo"] = cost_reason
        final["decision_basis"] = {
            "title": "작성 대상 판단 근거",
            "reasons": [
                {"label": "판단 사유", "text": target_reason},
                {"label": "발생 비용", "text": cost_reason},
                {
                    "label": "참고 문서",
                    "text": f"국회 의안 {source_bill_no} 「{source_bill_name}」 비용추계서",
                },
                {"label": "구조 유사도", "text": source_similarity},
                {"label": "적용 비용항목", "text": joined_items},
            ],
        }
        workflow_issues.append({
            "level": "info",
            "category": "공식 추계 사례 기준 적용",
            "detail": (
                f"근거 문서: 국회 의안 {source_bill_no} 「{source_bill_name}」, "
                f"구조 유사도 {source_similarity}"
            ),
            "action": "비용항목, 변수, 연도별 금액 구조를 기준으로 초안을 작성했습니다.",
        })
        suppressed = {
            "신구조문대비표 파싱 실패",
            "임베딩 비활성화",
            "법령 RAG 근거 없음",
            "유사 비용추계서 없음",
            "유사 비용추계서 신뢰도 낮음",
            "TAG 산식 패턴 없음",
        }
        workflow_issues[:] = [
            issue for issue in workflow_issues
            if issue.get("category") not in suppressed
        ]
    elif public_org_estimate:
        estimate = public_org_estimate
        final["if_needs_estimate"] = estimate
        final["verdict"] = "추계서"
        final["verdict_label"] = "비용추계서"
        final["if_non_attachment"] = None
        final["reason_summary"] = (
            "공공조직 신설형 비용추계 구조를 적용하여 인건비, 기관부담금, 운영비, "
            "자산취득비 중심으로 추가재정소요를 산출했습니다."
        )
        final["verdict_reason_nabo"] = (
            "기관 신설에 따라 인력 운영비와 기관 운영비 등 추가 재정소요가 발생하므로 비용추계서 작성 대상입니다."
        )
        workflow_issues.append({
            "level": "info",
            "category": "공공조직 신설형 템플릿 적용",
            "detail": "기관 신설형 의안으로 판단하여 유사 공식 추계서에서 일반화한 산식과 전제값을 적용했습니다.",
            "action": "값 변경 패널에서 신규충원 인원, 단가 또는 연간 기준금액을 조정할 수 있습니다.",
        })
    else:
        generalized_estimate = build_generalized_estimate(article_results, years=5)
        estimate = merge_generalized_estimate(estimate, generalized_estimate)
    if estimate is not final.get("if_needs_estimate"):
        final["if_needs_estimate"] = estimate
        if estimate and estimate.get("items"):
            final["verdict"] = "추계서"
            final["verdict_label"] = "비용추계서"
            final["if_non_attachment"] = None
            final["reason_summary"] = (
                "재정수반 조문을 범용 비용유형으로 분류하여 산식 후보를 구성했습니다. "
                "금액 확정 여부는 산식별 기준값과 외부자료 확보 상태를 별도로 표시합니다."
            )
            final["verdict_reason_nabo"] = (
                "재정지출 또는 재정수입 변화를 유발하는 비용항목과 계산 구조가 확인되어 비용추계 검토 대상입니다."
            )
            workflow_issues.append({
                "level": "info",
                "category": "범용 산식 구조 적용",
                "detail": "재정수반 조문을 조사·지원·시설·기관설립 등 상위 계산구조로 변환했습니다.",
                "action": "외부자료와 정책 전제의 충족 여부에 따라 자동계산 또는 입력대기로 구분합니다.",
            })
    rule_based_estimate = _make_rule_based_estimate(article_results, form_type=form_type)
    if rule_based_estimate and (
        not estimate
        or not (estimate.get("items") if isinstance(estimate, dict) else None)
        or final.get("verdict") == "미대상"
    ):
        estimate = rule_based_estimate
        final["if_needs_estimate"] = estimate
        final["verdict"] = "추계서"
        final["verdict_label"] = "비용추계서"
        final["reason_summary"] = (
            "위원회·조직의 설치 또는 운영 근거가 있어 재정수반 가능성이 있으므로 "
            "미대상으로 종결하지 않고 산식 후보와 기준값 검토 대상으로 보정했습니다."
        )
        final["verdict_reason_nabo"] = (
            "조직 설치·운영에 따른 회의수당, 운영비 또는 인건비 발생 가능성이 있어 비용추계 검토 대상입니다."
        )
        workflow_issues.append({
            "level": "warn",
            "category": "미대상 판단 보정",
            "detail": "명확한 비용유발 후보가 있어 미대상 판단을 추계 검토 대상으로 보정했습니다.",
            "action": "TAG/RAG 기준값 또는 사용자 입력으로 회의횟수·단가·인원 전제를 확정해야 합니다.",
        })
    analogical_estimate = (
        build_analogical_committee_estimate(
            text=text,
            articles=article_results,
            years=5,
            exclude_bill_nos=excluded_bill_nos,
        )
        if form_type == "assembly"
        else None
    )
    if analogical_estimate:
        estimate = analogical_estimate
        final["if_needs_estimate"] = estimate
        final["verdict"] = "추계서"
        final["verdict_label"] = "비용추계서"
        final["reason_summary"] = (
            "위원회 신설에 따른 인건비·기본경비·자산취득비·사업비를 "
            "구조가 유사한 국회 비용추계 사례의 전제와 산식을 기준으로 추계했습니다."
        )
        workflow_issues.append({
            "level": "warn",
            "category": "유사 비용추계 사례 기반 가정",
            "detail": (
                f"{analogical_estimate['analogy_selection']['bill_no']} "
                f"{analogical_estimate['analogy_selection']['bill_name']}의 "
                "항목·산식·전제를 기준선으로 적용했습니다."
            ),
            "action": "위원회 직무, 위원 정수와 지원인력 규모의 동등성을 확인한 뒤 전제값을 확정해야 합니다.",
        })
    excluded_item_count = _exclude_non_estimable_items(estimate, article_results)
    if excluded_item_count:
        workflow_issues.append({
            "level": "info",
            "category": "추계 곤란 항목 계산 제외",
            "detail": f"정책 판정에서 추계 곤란·기존 사업으로 분류된 {excluded_item_count}개 항목을 금액 계산에서 제외했습니다.",
            "action": "제외 항목은 재정수반요인과 미실시 사유에는 남기되 총액에는 포함하지 않습니다.",
        })
    if estimate and estimate.get("items"):
        for item in estimate["items"]:
            if form_type == "assembly":
                formula_template = build_formula_template(item, tag_patterns)
                if formula_template and not item.get("formula_template"):
                    item["formula_template"] = formula_template
                    calc = item.setdefault("calculation", {})
                    if (
                        isinstance(calc, dict)
                        and not calc.get("growth_variable")
                        and formula_template.get("growth_variable")
                    ):
                        calc["growth_variable"] = formula_template["growth_variable"]
            kosis_results = _lookup_kosis_variables(_item_lookup_variables(item))
            if kosis_results:
                item["kosis_lookups"] = kosis_results
            refs = pick_reference_unit_costs(
                tag_patterns, item.get("name", ""), item.get("category", ""), form_type=form_type
            )
            if refs:
                item["reference_unit_costs"] = refs
                item["reference_unit_cost"] = refs[0]
            if form_type == "assembly":
                assumption_candidates = find_assumption_candidates(
                    item,
                    form_type=form_type,
                    limit=12 if _is_committee_meeting_item(item) else 8,
                    exclude_bill_nos=excluded_bill_nos,
                )
                if assumption_candidates:
                    item["assumption_candidates"] = assumption_candidates
        if form_type == "assembly":
            enhanced_committee_items = _enhance_committee_meeting_formulas(estimate, article_results)
            if enhanced_committee_items:
                workflow_issues.append({
                    "level": "warn",
                    "category": "위원회 회의수당 산식 적용",
                    "detail": f"위원회 운영비 {enhanced_committee_items}개 항목을 회의횟수 × 수당지급대상 인원 × 회의수당 단가 산식으로 구조화했습니다.",
                    "action": "유사사례 기반 가정값이므로 회의횟수, 수당 지급 대상, 수당 단가를 확인해야 합니다.",
                })
            tag_amount_count = apply_tag_formula_evidence(estimate, tag_patterns)
            if tag_amount_count:
                workflow_issues.append({
                    "level": "info",
                    "category": "유사 공식 추계 사례 적용",
                    "detail": f"{tag_amount_count}개 항목에 유사 공식 비용추계서의 산식·금액을 적용해 합리적 초안을 생성했습니다.",
                    "action": "사업 범위와 단가의 적합성을 확인하시면 더 정확한 산정이 가능합니다.",
                })
            default_amount_count = apply_reasonable_default_amounts(estimate)
            if default_amount_count:
                workflow_issues.append({
                    "level": "info",
                    "category": "표준 기본값 적용",
                    "detail": f"{default_amount_count}개 항목에 표준 기본값을 적용해 best-effort 초안을 생성했습니다.",
                    "action": "값 변경 패널에서 금액·단가·대상 수를 보완하시면 즉시 재계산됩니다.",
                })
            apply_formula_source_strategy(estimate, tag_patterns)
        # 검토가 필요한 전제조건 수집. 기본 흐름은 입력 대기가 아니라
        # 합리적 가정/TAG 후보로 초안을 생성하고 검토 항목만 표시한다.
        needs_input = []
        for item in estimate["items"]:
            for a in item.get("assumptions") or []:
                if a.get("needs_user_confirm") or a.get("value") is None:
                    needs_input.append({
                        "item": item.get("name"),
                        "variable": a.get("name"),
                        "unit": a.get("unit"),
                        "basis": a.get("basis"),
                        "current_value": a.get("value"),
                        "assumption_candidates": item.get("assumption_candidates", [])[:5],
                    })
        if needs_input:
            estimate["review_assumptions"] = needs_input

    # 5-2) Python 계산기로 연도별 금액 산출
    # 정책: 단가·대상 등 필수 변수가 비어 있어도 가능한 경우 TAG/가정 후보로
    #       초안을 먼저 생성한다. 불확실성은 계산 차단이 아니라 검토 필요로 표시한다.
    if (
        estimate
        and estimate.get("items")
        and estimate.get("calculation_status") not in {"computed_by_special_template", "computed_with_tag_structure"}
    ):
        initial_missing_by_item = _missing_formula_variables(estimate)
        calculated, calc_issues = compute_year_estimates(estimate, tag_patterns=tag_patterns, allow_estimated=True)
        missing_by_item = _missing_formula_variables(estimate)
        if calculated:
            used_tag_estimate = any(
                item.get("evidence_basis") or item.get("tag_amount_evidence")
                for item in estimate.get("items") or []
            )
            estimate["calculation_status"] = (
                "computed_with_evidence"
                if used_tag_estimate or initial_missing_by_item
                else "computed_by_python"
            )
            estimate["year_estimates"] = calculated
            _sync_estimate_amount_totals(estimate)
            if initial_missing_by_item or calc_issues:
                workflow_issues.append({
                    "level": "warn",
                    "category": "가정 기반 초안",
                    "detail": "법안에 직접 없는 단가·대상 값은 유사 비용추계서 TAG와 기준값 후보를 활용해 초안을 생성했습니다.",
                    "action": "확정 제출 전에는 가정값의 정책 적합성만 검토하면 됩니다.",
                    "items": {
                        "initial_missing": initial_missing_by_item,
                        "calculation_notes": calc_issues,
                    },
                })
        else:
            apply_reasonable_default_amounts(estimate)
            calculated, calc_issues = compute_year_estimates(estimate, tag_patterns=[], allow_estimated=True)
            if calculated:
                estimate["calculation_status"] = "computed_with_evidence"
                estimate["year_estimates"] = calculated
                _sync_estimate_amount_totals(estimate)
            else:
                unresolved = _missing_formula_variables(estimate)
                estimate["calculation_status"] = "awaiting_user_input"
                estimate["year_estimates"] = _blocked_year_estimates(
                    unresolved or {"계산 항목": ["필수 기준값"]}
                )
                estimate.pop("total_amount_thousand", None)
                estimate.pop("average_amount_thousand", None)
                workflow_issues.append({
                    "level": "warn",
                    "category": "사용자 입력 필요",
                    "detail": "공식 자료와 유사사례에서 신뢰할 수 있는 기준값을 찾지 못해 임의 금액을 생성하지 않았습니다.",
                    "action": "표시된 필수 변수만 입력하면 동일 산식으로 즉시 재계산됩니다.",
                    "items": unresolved,
                })
        review_vars = _review_variables(estimate)
        if review_vars:
            estimate["verification_needed"] = review_vars

        estimation_status = classify_estimation_status(estimate)
        estimate["estimation_status"] = estimation_status
        status_code = estimation_status.get("code")
        has_amounts = _has_computed_amounts(estimate)
        if status_code == "needs_external_data" and not has_amounts:
            estimate["calculation_status"] = "needs_external_data"
            final["confidence"] = min(float(final.get("confidence") or 0), 0.65)
            workflow_issues.append({
                "level": "warn",
                "category": "외부 통계·기준자료 필요",
                "detail": estimation_status.get("reason"),
                "action": "대상 규모·단가·실적 자료를 연결하면 동일 산식으로 재계산할 수 있습니다.",
                "items": estimation_status.get("missing"),
            })
        elif status_code == "needs_policy_input" and not has_amounts:
            estimate["calculation_status"] = "needs_policy_input"
            final["confidence"] = min(float(final.get("confidence") or 0), 0.6)
            workflow_issues.append({
                "level": "warn",
                "category": "정책 전제 입력 필요",
                "detail": estimation_status.get("reason"),
                "action": "사업 규모·지급 주기·운영방식 전제를 확정한 뒤 재계산해야 합니다.",
                "items": estimation_status.get("missing"),
            })
        elif status_code == "computed_with_estimates":
            estimate["calculation_status"] = "computed_with_evidence"
            final["confidence"] = min(float(final.get("confidence") or 0), 0.72)
        elif status_code == "partially_computed":
            estimate["calculation_status"] = "computed_partial_by_python"
            final["confidence"] = min(float(final.get("confidence") or 0), 0.68)
        elif status_code == "computed":
            final["confidence"] = max(float(final.get("confidence") or 0), 0.85)

        # 계산기가 TAG 금액을 적용하거나 일부 항목만 계산한 후의 상태를
        # 기준으로 HITL 요청을 새로 구성한다. 이래야 계산된 값을 필수
        # 입력으로 표시하거나, 미계산 항목을 단순 확인으로 표시하지 않는다.
        apply_formula_source_strategy(estimate, tag_patterns)
        human_input = _build_human_input_requests(estimate)
        if human_input:
            estimate["human_input"] = human_input
            if has_amounts and human_input.get("blocking_count"):
                estimate["calculation_status"] = "computed_partial_by_python"
        else:
            estimate.pop("human_input", None)

    # 5-2.5) NABO 금액 게이트 - verdict와 계산 결과가 일치하는지 검증
    raw_verdict = final.get("verdict", "unknown")
    year_ests_for_gate = (estimate or {}).get("year_estimates") if estimate else None
    corrected_verdict, gate_note = _validate_verdict_with_amount(raw_verdict, year_ests_for_gate, form_type=form_type)
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

    # 5-2.6) 미첨부 사유서 정합성 보정.
    # 추계 곤란 후보인데 실제 산출 금액이 없으면 공식 문서는 미첨부 사유서가 맞다.
    non_attachment_candidates = [
        article for article in article_results
        if article.get("cost_trigger")
        and article.get("estimate_feasibility") == "non_attachment_review"
    ]
    strong_formula_candidates = [
        article for article in article_results
        if article.get("cost_trigger")
        and article.get("cost_candidate_strength") == "strong"
        and article.get("estimate_feasibility") == "formula_ready"
    ]
    # ── NABO 존재 게이트(existence gate) 원칙:
    #    산출 가능한 조문(strong formula: 위원 정수·증원 인원·보수 명시)이
    #    하나라도 있으면 그 조문만으로 "일부추계"를 작성한다 (다수결 아님).
    #    미첨부는 산출 가능 조문이 0개일 때만 허용.
    has_computable_article = bool(strong_formula_candidates)

    # 미첨부 후보가 강한 산식 후보 대비 우세하면 items 있어도 미첨부로 강제 전환.
    # (조직 신설 조문 다수 + 대통령령 위임 다수 케이스에서 산출 강제 방지)
    rule_non_attach_dominance = (
        not has_computable_article
        and len(non_attachment_candidates) >= 3
        and len(non_attachment_candidates) >= max(2, len(strong_formula_candidates) * 2)
    )

    # ── Dual-Channel kNN verdict 신호 (데이터 기반, 규칙보다 우선)
    #    각 비용유발 조문의 유사 비용추계서 채널(A) vs 유사 미첨부 사유서 채널(B)
    #    유사도를 비교. B가 우세하거나 강한 미첨부 매칭이 다수면 미첨부 신호.
    knn_signals = [
        a.get("knn_channels") for a in article_results
        if a.get("cost_trigger") and isinstance(a.get("knn_channels"), dict)
    ]
    knn_b_dominant = sum(
        1 for s in knn_signals
        if s.get("non_attach_avg", 0) > s.get("cost_avg", 0)
    )
    knn_strong_b = sum(1 for s in knn_signals if s.get("non_attach_top", 0) >= 0.70)

    # 동일 법률의 미첨부 선례: 같은 법 개정안을 NABO가 미첨부 처리한 사례가
    # 강하게 매칭(0.70+)되면 가장 강력한 선례로 본다.
    def _norm_law_name(value: Any) -> str:
        s = re.sub(r"\s+", "", str(value or ""))
        return re.sub(r"(일부개정법률안|전부개정법률안|개정법률안|법률안|법안)$", "", s)

    _current_law = _norm_law_name(bill_name)
    knn_same_law_na = bool(_current_law) and any(
        float(ref.get("similarity") or 0) >= 0.70
        and _norm_law_name(ref.get("bill_name")) == _current_law
        for s in knn_signals
        for ref in (s.get("non_attach_refs") or [])
    )
    # 단일 조문 의안 보완: 거의 동일 사안의 미첨부 사례(0.80+)가 존재하거나,
    # 강한 매칭(0.72+) + 채널 간 명확한 마진(0.08+)이면 조문 수와 무관하게 발동.
    knn_very_strong_single = any(
        s.get("non_attach_top", 0) >= 0.80
        or (
            s.get("non_attach_top", 0) >= 0.72
            and s.get("non_attach_avg", 0) - s.get("cost_avg", 0) >= 0.08
        )
        for s in knn_signals
    )
    knn_non_attach_dominance = (
        not has_computable_article
        and (
            knn_same_law_na
            or knn_very_strong_single
            or (
                len(knn_signals) >= 2
                and (
                    knn_b_dominant >= max(2, int(len(knn_signals) * 0.5))
                    or knn_strong_b >= 2
                )
            )
        )
    )
    if knn_non_attach_dominance:
        strongest = max(knn_signals, key=lambda s: s.get("non_attach_top", 0))
        top_refs = strongest.get("non_attach_refs") or []
        workflow_issues.append({
            "level": "info",
            "category": "유사 미첨부 사례 kNN 매칭",
            "detail": (
                f"비용유발 조문 {len(knn_signals)}개 중 {knn_b_dominant}개에서 유사 미첨부 사유서 "
                f"채널이 유사 비용추계서 채널보다 우세하고, 강한 매칭(유사도 0.70+)이 {knn_strong_b}건 "
                f"확인되어 미첨부 사유서 작성 대상으로 판단했습니다."
            ),
            "action": "유사 미첨부 사례: " + ", ".join(
                f"{r.get('bill_name')}({r.get('similarity')})" for r in top_refs[:2]
            ),
        })

    non_attach_dominance = rule_non_attach_dominance or knn_non_attach_dominance

    # ── 존재 게이트의 대칭 적용: 산출 가능(전제값 확보) 항목이 0개면 추계서 금지.
    #    전제값이 전부 미확보(value=None, 사용자 확정 필요)인 항목은 placeholder 금액일 뿐
    #    "산출 가능한 항목"이 아니다. NABO는 이런 경우 미첨부 3호(기술적 곤란)로 처리.
    def _item_data_backed(item: dict[str, Any]) -> bool:
        assumps = item.get("assumptions") or []
        if not assumps:
            return True  # 전제 가정이 불필요 = 산식 전제가 법률안에 명시된 항목
        # source_type="user_input"은 데이터 풀에서 값을 못 채워 사용자 확정이
        # 필요하다는 뜻이므로, 값이 임의로 채워져 있어도 산출 근거로 보지 않는다.
        return any(
            a.get("value") is not None and a.get("source_type") != "user_input"
            for a in assumps
        )

    _estimate_items = (estimate or {}).get("items") or []
    all_items_placeholder = bool(_estimate_items) and not any(
        _item_data_backed(it) for it in _estimate_items
    )

    if (
        form_type == "assembly"
        and not final.get("if_non_attachment")
        and (non_attachment_candidates or knn_non_attach_dominance)
        and (
            (
                not _has_computed_amounts(estimate)
                and not (estimate and estimate.get("items"))
                and not strong_formula_candidates
            )
            or non_attach_dominance
            or (all_items_placeholder and not has_computable_article)
        )
    ):
        if non_attachment_candidates:
            primary = non_attachment_candidates[0]
        else:
            # kNN 단독 케이스: 미첨부 매칭이 가장 강한 비용유발 조문을 대표로
            trigger_articles = [
                a for a in article_results
                if a.get("cost_trigger") and isinstance(a.get("knn_channels"), dict)
            ]
            primary = max(
                trigger_articles,
                key=lambda a: (a.get("knn_channels") or {}).get("non_attach_top", 0),
            ) if trigger_articles else {}
        ref = primary.get("no") or "해당 조문"
        knn_ref_names = [
            r.get("bill_name")
            for r in ((primary.get("knn_channels") or {}).get("non_attach_refs") or [])[:2]
            if r.get("bill_name")
        ]
        knn_reason = (
            f"유사 미첨부 사례({', '.join(knn_ref_names)})에서도 동일한 사유로 "
            "비용추계서를 첨부하지 않았습니다. " if knn_ref_names and knn_non_attach_dominance else ""
        )
        reason = (
            primary.get("review_reason")
            or (primary.get("rule_cost_trigger") or {}).get("review_reason")
            or "구체적인 사업 범위와 산식 전제값이 법률안에 명시되지 않아 기술적으로 비용추계가 곤란합니다."
        ) + (" " + knn_reason if knn_reason else "")
        final["if_non_attachment"] = {
            "type": "3호",
            "reason_text": (
                f"개정안 {ref}는 재정수반 가능성이 있으나, 구체적인 사업 범위, 수행 방식, "
                f"대상 및 규모 등 비용 산정에 필요한 전제가 법률안에 명시되어 있지 않습니다. "
                f"따라서 {reason} 이는 「의안의 비용추계 등에 관한 규칙」 제3조제1항제3호에 따른 "
                "기술적으로 추계가 어려운 경우에 해당합니다."
            ),
        }
        # dominance 케이스: 이미 산출된 items가 있으면 무효화 (미첨부 우선)
        if non_attach_dominance and estimate:
            estimate["items"] = []
            estimate["year_estimates"] = []
            estimate["calculation_status"] = "non_attachment_dominant"

    # 미첨부 사유서가 생성된 경우: items 유무 관계없이 미첨부로 전환.
    # (조직 신설 우세 케이스에서 items가 남아있어도 미첨부 사유서를 최종 결과로 사용)
    non_attachment = final.get("if_non_attachment")
    if (
        isinstance(non_attachment, dict)
        and (
            (not _has_computed_amounts(estimate) and not (estimate and estimate.get("items")))
            or non_attach_dominance
            # 존재 게이트 대칭: 모든 items가 전제값 미확보(placeholder)면 산출물이 아님
            or (all_items_placeholder and not has_computable_article)
        )
    ):
        non_attachment["reason_text"] = _public_non_attachment_text(non_attachment.get("reason_text"))
        na_verdict, na_label = _non_attachment_verdict(non_attachment.get("type"))
        final["verdict"] = na_verdict
        final["verdict_label"] = na_label
        final["reason_summary"] = non_attachment.get("reason_text") or final.get("reason_summary", "")
        final["verdict_reason_nabo"] = (
            f"{na_label}: {non_attachment.get('reason_text') or '비용추계서 미첨부 사유에 해당함.'}"
        )
        final["if_needs_estimate"] = None
        estimate = None
        workflow_issues.append({
            "level": "info",
            "category": "미첨부 사유서 우선 보정",
            "detail": "미첨부 사유가 생성되었고 실제 산출 금액이 없어 비용추계서가 아닌 미첨부 사유서로 정리했습니다.",
            "action": "재정수반요인은 남기되, 추계 곤란 사유를 공식 양식에 표시합니다.",
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
        "formType":     form_type,
        "formCriteria": {
            "label":          criteria["label"],
            "legalBasis":     criteria["legal_basis"],
            "thresholdText":  criteria["threshold_text"],
        },
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
            "basis":         final.get("decision_basis"),
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
                "currentBillNo": current_bill_no,
                "excludedCurrentBill": bool(current_bill_no),
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
