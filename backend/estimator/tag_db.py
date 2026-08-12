"""원본 TAG 데이터 1차 조회 — 실제 추계서의 항목/변수/금액 전체.

12키 파생 풀이 아니라 raw TAG(항목 3천+, 변수 9천+, 금액 1.7만+)를 직접 쓴다.
비용항목(이름·성격)으로 유사 선례 항목을 찾아, 그 항목들의 변수값(unit_cost/target_count/
frequency/rate)과 연간 금액을 대표값으로 뽑는다. 커버리지가 12키에 갇히지 않는다.
"""
from __future__ import annotations

import glob
import json
import re
import statistics
from functools import lru_cache
from typing import Any

_GENERATED = "backend/generated"
_STOP = {"및", "등", "의", "에", "관한", "위한", "대한", "따른", "지원", "사업비", "비용", "운영"}


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[가-힣]{2,}", str(text or ""))
    return {t for t in toks if t not in _STOP}


# 비용 구조 family — 산식 구조가 같은 것끼리 묶어 매칭(구조 다른 것 섞임 방지).
_FAMILY_RULES = (
    ("committee", re.compile(r"위원회|심의회|협의회|위원단|자문단|회의수당|참석수당")),
    ("research", re.compile(r"용역|실태조사|연구|계획수립|종합계획|기본계획|시행계획|조사")),
    ("personnel", re.compile(r"인건비|보수|봉급|급여|정원|인력|직원|공무원|증원")),
    ("subsidy", re.compile(r"지원금|보조금|출연금|급여지급|수당지급|지급액|장려금")),
    ("facility", re.compile(r"시설|설비|구축|전산|시스템|장비|건립|정보망")),
)


def _family(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    for fam, pat in _FAMILY_RULES:
        if pat.search(compact):
            return fam
    return "other"


# 변수의 계산 역할 추론 — variable_type 필드가 부실해서 이름에서도 유추(범용).
_ROLE_NAME_RULES = (
    ("unit_price", re.compile(r"수당|단가|보수|봉급|1인당|인당|급여|사례금|여비")),
    ("frequency", re.compile(r"횟수|회의|주기|빈도|개최")),
    ("count", re.compile(r"인원|위원|정원|대상|명수|건수|인력")),
    ("rate", re.compile(r"율|률|비율|집행")),
)


def _infer_role(vtype: str, vname: str) -> str | None:
    """variable_type 우선, 없으면 변수 이름에서 역할 추론."""
    base = {"unit_cost": "unit_price", "frequency": "frequency",
            "target_count": "count", "rate": "rate"}.get(vtype)
    if base:
        return base
    compact = re.sub(r"\s+", "", vname or "")
    for role, pat in _ROLE_NAME_RULES:
        if pat.search(compact):
            return role
    return None


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    """항목 → {name, bill_id, tokens}, 항목 → 변수목록, 항목 → 연간금액(합계행 제외 중앙값)."""
    def iter_jsonl(fname: str):
        seen = set()
        for p in glob.glob(f"{_GENERATED}/**/{fname}.jsonl", recursive=True):
            for line in open(p, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield r

    # struct_id → 의안번호/법률명/제안일 (홀드아웃용)
    struct_meta: dict[str, dict[str, str]] = {}
    for r in iter_jsonl("cost_estimate_structures"):
        sid = str(r.get("struct_id") or "")
        if sid:
            struct_meta[sid] = {
                "bill_no": str(r.get("bill_no") or ""),
                "bill_name": str(r.get("bill_name") or ""),
                "propose_date": str(r.get("propose_date") or ""),
            }

    items: dict[str, dict[str, Any]] = {}
    for r in iter_jsonl("cost_estimate_items"):
        iid = str(r.get("item_id") or "")
        if not iid:
            continue
        name = str(r.get("item_name") or "")
        meta = struct_meta.get(str(r.get("struct_id") or ""), {})
        cat = str(r.get("item_category") or "")
        # item_category가 이미 "지원금"으로 명확히 분류돼 있으면 그 신호를
        # 최우선으로 신뢰한다. 이름 텍스트 재분류(_family)는 "부모급여"처럼
        # personnel 규칙의 "급여" 키워드에 먼저 걸려 지원금 항목을 personnel/
        # research로 오분류하는 경우가 있었다(실측: 965건 중 81건 오분류).
        family = "subsidy" if cat == "지원금" else _family(name + " " + cat)
        items[iid] = {
            "name": name,
            "bill_id": str(r.get("bill_id") or ""),
            "bill_no": meta.get("bill_no", ""),
            "bill_name": meta.get("bill_name", ""),
            "propose_date": meta.get("propose_date", ""),
            "category": cat,
            "family": family,
            "tokens": _tokens(name + " " + cat),
        }
    vars_by_item: dict[str, list[dict[str, Any]]] = {}
    for r in iter_jsonl("cost_estimate_variables"):
        iid = str(r.get("item_id") or "")
        if iid in items:
            vars_by_item.setdefault(iid, []).append({
                "type": str(r.get("variable_type") or ""),
                "name": str(r.get("variable_name") or ""),
                "value": r.get("variable_value"),
                "unit": str(r.get("variable_unit") or ""),
                # Keep the exact sentence/table fragment that justified the
                # operand.  The estimator uses this provenance when it must
                # fill one missing operand from a different precedent.
                "source_text": str(r.get("source_text") or ""),
            })
    amounts_by_item: dict[str, list[int]] = {}
    for r in iter_jsonl("cost_estimate_amounts"):
        iid = str(r.get("item_id") or "")
        if iid in items and not r.get("is_total"):
            a = r.get("amount_thousand")
            try:
                if a is not None:
                    amounts_by_item.setdefault(iid, []).append(int(a))
            except (TypeError, ValueError):
                pass
    annual_by_item = {
        iid: int(statistics.median([v for v in amts if v > 0]))
        for iid, amts in amounts_by_item.items()
        if any(v > 0 for v in amts)
    }
    return {"items": items, "vars": vars_by_item, "annual": annual_by_item}


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


@lru_cache(maxsize=1)
def _load_embeddings() -> dict[str, list[float]]:
    """미리 계산한 TAG 항목 임베딩 캐시. 없으면 빈 dict(→ 토큰 매칭 폴백)."""
    out: dict[str, list[float]] = {}
    for p in glob.glob(f"{_GENERATED}/**/tag_item_embeddings.jsonl", recursive=True):
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
                if r.get("emb"):
                    out[str(r["item_id"])] = r["emb"]
            except json.JSONDecodeError:
                continue
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _robust_median(values: list[int]) -> int:
    """이상치(IQR 밖) 제거 후 중앙값. 엉뚱한 오매칭이 대표값을 왜곡하는 것을 막는다."""
    if len(values) < 4:
        return int(statistics.median(values))
    s = sorted(values)
    q1 = s[len(s) // 4]
    q3 = s[(len(s) * 3) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [v for v in s if lo <= v <= hi]
    return int(statistics.median(kept or s))


def find_similar_items(
    query: str,
    *,
    family: str = "",
    exclude_bill_nos: set[str] | None = None,
    exclude_bill_name: str = "",
    cutoff_date: str | None = None,
    k: int = 30,
) -> list[str]:
    """이름 토큰 겹침으로 유사 선례 항목 id 상위 k개.

    family가 주어지면 같은 비용 구조(위원회/용역/인건비 등)끼리만 매칭 → 구조 다른 것 섞임 방지.
    홀드아웃: 본인 의안번호 제외 + 같은 법률명(쌍둥이) 제외 + 제안일 이후(미래) 제외.
    """
    data = _load()
    qtok = _tokens(query)
    if not qtok and not family:
        return []
    excluded = {str(b) for b in (exclude_bill_nos or set()) if b}
    self_name = _norm_name(exclude_bill_name)
    # 임베딩 캐시가 있으면 의미(코사인) 검색, 없으면 토큰 겹침으로 폴백.
    embs = _load_embeddings()
    qvec = None
    from ..analyzer_v2 import try_embed
    if embs:
        qvec = try_embed(query[:2000])
    scored: list[tuple[float, str]] = []
    for iid, meta in data["items"].items():
        if family and meta.get("family") != family:
            continue  # 같은 구조 family만
        if meta.get("bill_no") and meta["bill_no"] in excluded:
            continue
        if self_name and _norm_name(meta.get("bill_name", "")) == self_name:
            continue  # 같은 법률의 다른 발의버전(쌍둥이) 제외
        if cutoff_date and meta.get("propose_date") and meta["propose_date"] > cutoff_date:
            continue  # 분석 대상보다 미래 선례 제외
        if qvec is not None and iid in embs:
            score = _cosine(qvec, embs[iid])  # 의미 유사도
        else:
            overlap = len(qtok & meta["tokens"])
            if overlap == 0 and not family:
                continue
            score = overlap / (len(qtok) ** 0.5) if qtok else 0.0
        scored.append((score, iid))
    scored.sort(reverse=True)
    return [iid for _, iid in scored[:k]]


# role → TAG variable_type 후보
_ROLE_TO_TYPES = {
    "unit_price": {"unit_cost"},
    "count": {"target_count", "frequency"},
    "rate": {"rate"},
}


def representative_variable(query: str, role: str, *, family: str = "", exclude_bill_nos: set[str] | None = None, exclude_bill_name: str = "", cutoff_date: str | None = None):
    """유사 선례 항목들에서 role에 맞는 변수값을 모아 대표값(중앙값) 반환. (value, n) 또는 None."""
    data = _load()
    types = _ROLE_TO_TYPES.get(role, set())
    if not types:
        return None
    ids = find_similar_items(query, family=family, exclude_bill_nos=exclude_bill_nos, exclude_bill_name=exclude_bill_name, cutoff_date=cutoff_date)
    vals: list[float] = []
    for iid in ids:
        for v in data["vars"].get(iid, []):
            if v["type"] in types and v["value"] is not None:
                try:
                    vals.append(float(v["value"]))
                except (TypeError, ValueError):
                    pass
    if not vals:
        return None
    return statistics.median(vals), len(vals)


def representative_annual_amount(query: str, *, family: str = "", exclude_bill_nos: set[str] | None = None, exclude_bill_name: str = "", cutoff_date: str | None = None):
    """총액 준용: 유사 선례 항목들의 연간 금액(천원) 대표값(중앙값). (thousand, n) 또는 None."""
    data = _load()
    ids = find_similar_items(query, family=family, exclude_bill_nos=exclude_bill_nos, exclude_bill_name=exclude_bill_name, cutoff_date=cutoff_date)
    vals = [data["annual"][iid] for iid in ids if iid in data["annual"]]
    if not vals:
        return None
    return int(statistics.median(vals)), len(vals)


@lru_cache(maxsize=1)
def _formula_by_item() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in glob.glob(f"{_GENERATED}/**/cost_estimate_amounts.jsonl", recursive=True):
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("is_total"):
                continue
            iid = str(r.get("item_id") or "")
            ft = str(r.get("formula_text") or "").strip()
            if iid and ft and iid not in out:
                out[iid] = ft
    return out


def estimate_structural(query: str, *, family: str = "", exclude_bill_nos: set[str] | None = None,
                        exclude_bill_name: str = "", cutoff_date: str | None = None) -> dict[str, Any] | None:
    """유사 선례들의 변수 구조를 뽑아 계산(총액 준용보다 정밀).

    같은 유형(위원회 등)의 선례에서 frequency(회의횟수)·target_count(인원)·unit_cost(단가)·rate를
    각각 대표값(이상치 제거 중앙값)으로 뽑아 곱한다. 산식이 TAG에 있는 걸 그대로 활용하는 것.
    unit_cost가 없으면(구조가 총액형) None → 호출측이 총액 준용으로 폴백.
    """
    data = _load()
    ids = find_similar_items(query, family=family, exclude_bill_nos=exclude_bill_nos,
                             exclude_bill_name=exclude_bill_name, cutoff_date=cutoff_date)
    by_role: dict[str, list[int]] = {"frequency": [], "count": [], "unit_price": [], "rate": []}
    for iid in ids:
        for v in data["vars"].get(iid, []):
            if v["value"] is None:
                continue
            role = _infer_role(v["type"], v["name"])
            if role is None:
                continue
            try:
                by_role[role].append(int(round(float(v["value"]))))
            except (TypeError, ValueError):
                pass
    reps = {r: _robust_median(vals) for r, vals in by_role.items() if len(vals) >= 3}
    if "unit_price" not in reps:
        return None  # 단가 구조가 없으면(총액형) → 총액 준용으로 폴백
    base = float(reps["unit_price"])
    parts = [f"단가={reps['unit_price']:,}"]
    for r in ("frequency", "count"):
        if r in reps and reps[r] > 0:
            base *= reps[r]
            parts.append(f"{'횟수' if r == 'frequency' else '인원'}={reps[r]}")
    if "rate" in reps:
        rr = reps["rate"] / 100.0 if reps["rate"] > 2 else reps["rate"]
        if 0 < rr < 1:  # 진짜 분수(집행률 등)만; 2 같은 노이즈 배제
            base *= rr
            parts.append(f"비율={rr:g}")
    return {"annual_thousand": int(round(base / 1000)), "formula": " × ".join(parts), "n": len(ids)}


def estimate_by_analogy(query: str, *, family: str = "", exclude_bill_nos: set[str] | None = None, exclude_bill_name: str = "", cutoff_date: str | None = None) -> dict[str, Any] | None:
    """유사 선례 항목에서 '연간 금액 대표값 + 근거 산식(formula_text) + 선례 이름'을 함께 반환.

    산식·값 모두 TAG에 고정돼 있어 같은 query면 항상 같은 결과(결정적).
    """
    data = _load()
    ids = find_similar_items(query, family=family, exclude_bill_nos=exclude_bill_nos, exclude_bill_name=exclude_bill_name, cutoff_date=cutoff_date)
    priced = [iid for iid in ids if iid in data["annual"]]
    # 신뢰도 게이트: 근거가 너무 적으면(5건 미만) 준용 불신 → None(외부데이터로)
    if len(priced) < 5:
        return None
    vals = [data["annual"][iid] for iid in priced]
    med = _robust_median(vals)
    # 대표값에 가장 가까운 선례를 근거로
    anchor = min(priced, key=lambda iid: abs(data["annual"][iid] - med))
    formulas = _formula_by_item()
    return {
        "annual_thousand": med,
        "n": len(priced),
        "anchor_name": data["items"][anchor]["name"],
        "anchor_formula": formulas.get(anchor, ""),
        "examples": [data["items"][i]["name"][:30] for i in priced[:4]],
    }
