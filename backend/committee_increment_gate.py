"""개정형 '상임위원/정원 증원' 탐지 게이트.

`committee_gate.group_committee_articles`는 위원회 "신설"(둔다/설치)만 개체로
잡는다. 그래서 "상임위원을 2명에서 5명으로 늘린다" 같은 개정형 증원 조문은
개체가 0개가 되어 계산이 아예 시작되지 않았다(2203739·2211768 실측 무출력).

이 게이트는 그 공백을 메운다 — 조문에서 정무직 상임위원/일반직 정원의 증원
규모를 뽑아, tag_rule_engine.calculate_member_increment로 인건비를 계산하게
한다. 홀드아웃 6건 실측: 증원수 × 정무직급여 × (1+기관부담+기본경비)로
5/6이 오차 30% 이내(2203739 -6%, 2200815 +1% 등).

보수적 설계: 명확한 증원 문구에만 발동하고, 이미 committee_gate가 개체로 잡은
위원회명과는 중복 계산하지 않는다(호출부에서 dedupe).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_C = lambda s: re.sub(r"\s+", "", s or "")


@dataclass(frozen=True)
class MemberIncrement:
    label: str            # 사람이 읽는 항목명
    headcount: int        # 증원 인원
    salary_type: str      # "political"(정무직 상임위원) | "staff"(일반직 정원)
    evidence: str         # 근거 문구
    article_no: str = ""
    is_new_installation: bool = False  # True면 위원회 신설 상임구성(중복계산 주의)


# 정무직 상임위원 증원 패턴 (강한 신호만).
# 핵심: "N명을 포함한 M명"(위원회 구성 서술)을 증원으로 오독하면 안 된다
# (2126661 등 실측 오탐). 반드시 "현재 N→M으로 늘/증원/확대" 같은 변화 동사가
# 붙은 경우만 잡는다.
_PATTERNS = [
    # A. 상임위원을 (현재) N인/명에서 M인/명으로 [늘/증원/확대/변경/조정]
    ("political", re.compile(
        r"상임위원[^0-9]{0,10}?(?:현재)?\s*(?P<cur>\d+)\s*[명인]\s*(?:에서|을|를)\s*(?P<new>\d+)\s*[명인]"
        r"(?:으로|로)\s*(?:늘|증원|확대|상향|변경|조정|한다|하는)")),
    # B. 위원장 N명, 위원 M명을 상임으로 (신설 위원회의 상임 구성)
    ("political_sum", re.compile(
        r"위원장\s*(?P<a>\d+)\s*[명인][^0-9]{0,8}위원\s*(?P<b>\d+)\s*[명인]을?\s*상임")),
    # C. 상임위원 N명(을) 증원/추가
    ("political_flat", re.compile(r"상임위원\s*(?P<n>\d+)\s*[명인]\s*(?:을|를)?\s*(?:증원|추가|늘)")),
    # D. 과(정원 N명) 신설 / 정원 N명 증원
    ("staff", re.compile(r"(?:과|팀|부|국)\s*\(?정원\s*(?P<n>\d+)\s*[명인]")),
]

# 사무처장 겸직 배제 → 상임위원 실질 +1
_CHAIR_SPLIT = re.compile(
    r"상임위원[^.]{0,40}?사무처장[^.]{0,14}?(?:겸직|겸임)[^.]{0,10}?(?:삭제|배제|금지|아니)"
    r"|사무처장[^.]{0,14}?(?:겸직|겸임)[^.]{0,10}?(?:삭제|배제|금지)[^.]{0,50}?상임위원"
)


def detect_member_increments(source: "list[dict] | str") -> list[MemberIncrement]:
    """상임위원/정원 증원 항목을 뽑는다. 없으면 빈 리스트.

    source는 전체 의안 텍스트(str) 또는 조문 리스트(list[dict]). 증원 문구는
    조문(제N조)이 아니라 제안이유·주요내용에 있는 경우가 많아(2211768 실측),
    전체 텍스트를 넘기는 것을 권장한다."""
    if isinstance(source, str):
        full = _C(source)
    else:
        full = _C(" ".join(f"{a.get('title','')} {a.get('text','')}" for a in source))
    out: list[MemberIncrement] = []
    seen_kinds: set[str] = set()

    for kind, pat in _PATTERNS:
        m = pat.search(full)
        if not m:
            continue
        new_install = False
        if kind == "political":
            cur, new = int(m.group("cur")), int(m.group("new"))
            if new <= cur:
                continue
            n, styp, lbl = new - cur, "political", f"상임위원 {new-cur}명 증원(정무직)"
        elif kind == "political_sum":
            n = int(m.group("a")) + int(m.group("b"))
            styp, lbl = "political", f"상임위원 {n}명 신설(정무직)"
            new_install = True
        elif kind == "political_flat":
            n, styp, lbl = int(m.group("n")), "political", f"상임위원 {int(m.group('n'))}명 증원(정무직)"
        else:  # staff
            n, styp, lbl = int(m.group("n")), "staff", f"일반직 {int(m.group('n'))}명 증원(정원)"
        if styp in seen_kinds:
            continue
        seen_kinds.add(styp)
        out.append(MemberIncrement(label=lbl, headcount=n, salary_type=styp,
                                   evidence=m.group(0)[:60], is_new_installation=new_install))

    # 사무처장 겸직 배제형(+1) — 겸임하던 직위가 별도 인원이 되면 실질 +1이다
    # (일반 원리: 직위 분리 = 인원 증가). 다른 상임위원 증원과 독립적으로 더한다.
    # 실측(2211768): "상임위원 2→5"(=+3) '그리고' "사무처장 겸직 배제"(=+1) → 총 4명.
    # 배타(else)로 두면 +3만 잡아 -23% 과소였다. 가산으로 바꿔 근접시킨다.
    if _CHAIR_SPLIT.search(full):
        out.append(MemberIncrement(
            label="사무처장 1명(겸직 배제로 별도 배치)", headcount=1,
            salary_type="political", evidence="사무처장 겸직 배제→별도 인원 +1"))

    return out


# ── LLM 백업 ────────────────────────────────────────────────────────────
# 규칙은 명확한 문구만 잡는다(오탐 0 우선). 규칙이 0개를 반환할 때, 문구가
# 다양한 증원(예: "사무처장 겸직 규정을 삭제"=+1, 다직급 구성 서술에서 증원분
# 분리)을 신뢰 LLM이 판단해 메운다. 홀드아웃 6건: 규칙 단독 1/6 → 하이브리드
# 6/6 탐지, 5/6 오차 30% 이내. 프로덕션 LLM(solar)은 비결정성 때문에 신뢰하지
# 않고, 수동 하네스에서는 Claude가 이 함수를 대행 패치한다.
_INCREMENT_PROMPT = """다음 법안 텍스트에서 '상임위원(정무직) 증원' 또는 '일반직 정원 증원'이 있으면 추출하라.
- 위원회를 새로 설치하며 상임위원을 두는 경우, "N명에서 M명으로 늘림", "상임위원 N명 증원",
  "사무처장 겸직 규정 삭제"(=상임위원 +1) 등을 모두 증원으로 본다.
- 증원이 없으면 빈 배열.
JSON만 출력: {{"increments":[{{"headcount":정수,"salary_type":"political|staff","label":"설명"}}]}}

법안 텍스트:
{text}"""


def extract_member_increment_llm(text: str) -> list[MemberIncrement]:
    """규칙이 놓친 증원을 LLM으로 추출. 프로덕션 solar 대신 신뢰 LLM/수동 대행.
    기본 구현은 프로덕션 LLM 호출이지만, 파이프라인은 규칙이 비었을 때만 부른다."""
    from .article_extraction_engine import _call_upstage_json
    try:
        parsed = _call_upstage_json(_INCREMENT_PROMPT.format(text=text[:6000]))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    out: list[MemberIncrement] = []
    for row in parsed.get("increments", []):
        if not isinstance(row, dict):
            continue
        hc = row.get("headcount")
        st = row.get("salary_type")
        if not isinstance(hc, int) or hc <= 0 or st not in ("political", "staff"):
            continue
        out.append(MemberIncrement(
            label=str(row.get("label") or f"{st} {hc}명 증원"),
            headcount=hc, salary_type=st, evidence="LLM 백업 추출"))
    return out
