"""실(實)LLM end-to-end 성능검증 러너.

evaluate_tag_e2e_manual_llm.py는 Phase 2 파싱을 Claude(사람 대행)가 채웠다.
이 스크립트는 그 대신 **실제 프로덕션처럼** 최신 프런티어 LLM(gpt-5.6-sol)을
파서로 붙여 tag_pipeline.run_pipeline()을 PDF부터 리포트까지 통째로 돌린다.

목적: "LLM이 약해서 틀렸다"는 변명을 제거한다. 최고 모델로 돌려도 같은
review_required/오차가 나오면, 병목은 파서가 아니라 룰엔진·문제구조라는 게
증명된다. 반대로 확 좋아지면 파서가 병목이었다는 뜻.

주의: 이 스크립트는 실제 OpenAI API를 호출한다(과금). 정답 PDF는 열지 않고,
계산 결과(committee item)만 뽑아 사람이 정답과 대조한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.article_extraction_engine import _json_from_llm_text  # noqa: E402

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.6-sol"


def _load_openai_key() -> str:
    for line in (ROOT / "backend" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("OPENAI_API_KEY를 backend/.env에서 찾지 못했습니다.")


_KEY = _load_openai_key()
_CALL_LOG: list[dict] = []


def make_openai_json(model: str):
    """_call_upstage_json과 동일한 인터페이스(prompt -> parsed JSON)의 OpenAI 버전.
    GPT-5.x는 temperature 미지원·max_completion_tokens 사용이라 파라미터를 맞춘다."""

    def _openai_json(prompt: str, *, temperature: float = 0.1, timeout: int = 180):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 16000,
        }
        req = urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"},
        )
        t0 = time.monotonic()
        last_err = None
        for attempt in range(3):
            try:
                data = json.load(urllib.request.urlopen(req, timeout=timeout))
                choice = data["choices"][0]
                usage = data.get("usage", {})
                _CALL_LOG.append({
                    "model": model,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "seconds": round(time.monotonic() - t0, 1),
                    "finish_reason": choice.get("finish_reason"),
                })
                if str(choice.get("finish_reason") or "") == "length":
                    raise RuntimeError("LLM 응답이 토큰 한도에서 잘렸습니다.")
                return _json_from_llm_text(str(choice["message"]["content"]))
            except urllib.error.HTTPError as e:  # noqa: PERF203
                last_err = f"HTTP {e.code}: {e.read().decode()[:300]}"
                time.sleep(2 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"OpenAI 호출 실패(3회): {last_err}")

    return _openai_json


def run_bill(pdf_path: str, model: str, base_year: int = 2027) -> dict:
    from backend import tag_pipeline

    pdf_bytes = Path(pdf_path).read_bytes()
    openai_json = make_openai_json(model)

    # 파이프라인 전체에서 LLM을 호출하는 4개 바인딩을 모두 OpenAI로 교체.
    # (같은 _call_upstage_json 함수가 4개 모듈에 각각 import돼 있어 각각 패치)
    patches = [
        patch("backend.article_extraction_engine._call_upstage_json", side_effect=openai_json),
        patch("backend.tag_parsers._call_upstage_json", side_effect=openai_json),
        patch("backend.committee_assembly_internal_gate._call_upstage_json", side_effect=openai_json),
        patch("backend.committee_activity_duration_gate._call_upstage_json", side_effect=openai_json),
    ]
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = tag_pipeline.run_pipeline(
            pdf_bytes, base_year=base_year, filename=Path(pdf_path).name,
            use_precedent_fallback=False,  # 선례 fallback은 별도 데이터계층 — 순수 룰엔진 성능만 측정
        )
    return result


def summarize(result: dict) -> dict:
    committees = []
    for item in result.get("items", []):
        calc = item.get("calc_result")
        if not isinstance(calc, dict) or "committee_type" not in calc:
            continue
        committees.append({
            "name": calc.get("name"),
            "committee_type": calc.get("committee_type"),
            "status": calc.get("status"),
            "annual_cost_won": calc.get("annual_cost_won"),
            "annual_cost_won_range": calc.get("annual_cost_won_range"),
            "one_time_cost_won": calc.get("one_time_cost_won"),
            "year_amounts": item.get("year_amounts"),
            "year_amounts_range": item.get("year_amounts_range"),
            "trace": calc.get("trace"),
            "reason": calc.get("reason"),
            "standing_member_cost": calc.get("standing_member_cost"),
        })
    return {
        "bill_no": result.get("bill_no"),
        "bill_title": result.get("bill_title"),
        "committee_count": result.get("committee_count"),
        "committees": committees,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="의안 원문 PDF 경로")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-year", type=int, default=2027)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    t0 = time.monotonic()
    result = run_bill(args.pdf, args.model, args.base_year)
    summary = summarize(result)
    summary["_meta"] = {
        "model": args.model,
        "llm_calls": len(_CALL_LOG),
        "total_prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in _CALL_LOG),
        "total_completion_tokens": sum(c.get("completion_tokens") or 0 for c in _CALL_LOG),
        "wall_seconds": round(time.monotonic() - t0, 1),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
