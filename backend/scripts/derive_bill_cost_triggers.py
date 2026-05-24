"""derive_bill_cost_triggers.py

cost_estimate_items의 trigger_ref(근거 조문)를 역매핑하여
bill_cost_triggers 테이블을 채운다.

신뢰도 100%: 이미 NABO/지방의회 전문가가 추계서에 명시한 조문이므로 가정/추정 없음.

사용법:
    python -m backend.scripts.derive_bill_cost_triggers
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import get_env

SUPA_URL = get_env("SUPABASE_URL").rstrip("/")
SUPA_KEY = get_env("SUPABASE_SERVICE_ROLE_KEY")
OPENAI_KEY = get_env("OPENAI_API_KEY")
EMBED_MODEL = get_env("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


def supabase_get(path: str, params: str = "") -> list[dict]:
    url = f"{SUPA_URL}/rest/v1/{path}?{params}"
    req = urllib.request.Request(
        url, headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def supabase_post(path: str, payload: list[dict], prefer: str = "return=minimal") -> None:
    url = f"{SUPA_URL}/rest/v1/{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "apikey": SUPA_KEY,
            "Authorization": f"Bearer {SUPA_KEY}",
            "Content-Type": "application/json",
            "Prefer": prefer,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def embed_batch(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]


def fetch_all_items() -> list[dict]:
    """모든 cost_estimate_items + structures의 bill_no/name 조인."""
    print("cost_estimate_items 전체 조회 중...", flush=True)
    items: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        rows = supabase_get(
            "cost_estimate_items",
            f"select=id,structure_id,bill_id,item_category,item_name,trigger_ref"
            f"&trigger_ref=not.is.null&limit={limit}&offset={offset}",
        )
        if not rows:
            break
        items.extend(rows)
        print(f"  → {len(items)}건 조회", flush=True)
        if len(rows) < limit:
            break
        offset += limit
    return items


def fetch_existing_triggers() -> set[tuple[str, str]]:
    """이미 있는 (bill_id, article_no) 쌍 — 중복 방지."""
    existing: set[tuple[str, str]] = set()
    offset = 0
    while True:
        rows = supabase_get(
            "bill_cost_triggers",
            f"select=bill_id,article_no&limit=1000&offset={offset}",
        )
        if not rows:
            break
        for r in rows:
            existing.add((r["bill_id"], r["article_no"]))
        if len(rows) < 1000:
            break
        offset += 1000
    return existing


def main() -> None:
    items = fetch_all_items()
    print(f"\n총 {len(items)}개 items 로드", flush=True)

    # (bill_id, article_no) 단위로 그룹핑
    triggers_map: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        bill_id = item["bill_id"]
        trigger_ref = (item.get("trigger_ref") or "").strip()
        if not trigger_ref:
            continue
        key = (bill_id, trigger_ref)
        if key not in triggers_map:
            triggers_map[key] = {
                "bill_id":      bill_id,
                "article_no":   trigger_ref,
                "article_text": "",  # items에는 조문 본문이 없음 → null
                "cost_trigger": True,
                "trigger_type": item.get("item_category", "사업비"),
                "obligation_strength": "mandatory",  # 추계서가 작성된 의안이므로 사실상 mandatory
                "cost_items":   [item["item_name"]],
                "confidence":   1.0,
                "status":       "confirmed",
                "reason":       f"추계서에 명시된 조문 (역매핑)",
            }
        else:
            # 같은 조문에 여러 비용 항목 → cost_items에 추가
            triggers_map[key]["cost_items"].append(item["item_name"])

    print(f"고유 (bill_id, 조문) 쌍: {len(triggers_map)}건", flush=True)

    # 기존 데이터 확인
    existing = fetch_existing_triggers()
    print(f"기존 bill_cost_triggers: {len(existing)}건", flush=True)

    new_triggers = [t for k, t in triggers_map.items() if k not in existing]
    print(f"신규 적재 대상: {len(new_triggers)}건", flush=True)

    if not new_triggers:
        print("적재할 신규 데이터 없음. 종료.", flush=True)
        return

    # 임베딩은 article_text가 있어야 의미 있는데, 우리는 item_name으로 대체
    # → bill_id + 조문 + 비용항목명들을 합쳐서 임베딩 텍스트로 사용
    print("\n임베딩 생성 중 (article_embedding)...", flush=True)
    embed_texts = []
    for t in new_triggers:
        text = f"{t['article_no']} {' '.join(t['cost_items'])}"[:8000]
        embed_texts.append(text)

    batch_size = 100
    embeddings: list[list[float]] = []
    for i in range(0, len(embed_texts), batch_size):
        batch = embed_texts[i:i + batch_size]
        try:
            vecs = embed_batch(batch)
            embeddings.extend(vecs)
            print(f"  배치 {i//batch_size + 1}: {len(batch)}건 임베딩 완료", flush=True)
        except Exception as exc:
            print(f"  [WARN] 배치 {i//batch_size + 1} 실패: {exc}", flush=True)
            embeddings.extend([None] * len(batch))

    # article_embedding 부여
    for t, vec in zip(new_triggers, embeddings):
        t["article_embedding"] = vec
        # cost_items는 jsonb 필드라 그대로 둠

    # Supabase 업로드 (배치 10건씩)
    print(f"\nSupabase 업로드 시작...", flush=True)
    upload_batch = 10
    uploaded = 0
    for i in range(0, len(new_triggers), upload_batch):
        batch = new_triggers[i:i + upload_batch]
        try:
            supabase_post("bill_cost_triggers", batch)
            uploaded += len(batch)
            print(f"  {uploaded}/{len(new_triggers)}건 완료", flush=True)
        except urllib.error.HTTPError as exc:
            err_text = exc.read().decode("utf-8", errors="ignore")[:200]
            print(f"  [ERROR] 배치 실패: {exc.code} {err_text}", flush=True)

    print(f"\n✅ 완료: {uploaded}/{len(new_triggers)}건 bill_cost_triggers 적재", flush=True)


if __name__ == "__main__":
    main()
