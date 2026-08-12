"""위원 정수를 조문 원문에서 직접 뽑는다 (격리 모듈, additive).

실측(2213848): 선례(11명)를 그대로 쓰면 오차 -52.2%였는데, 조문 원문의
"30명 이내"를 대신 쓰면 오차가 +30.4%로 줄었다(회의횟수·단가는 선례값이
정확히 맞았음 — 4회, 25만원 그대로). 즉 위원정수는 선례에서 빌리는 것보다
조문 원문에 있으면 그걸 우선하는 게 더 정확하다.

한계(실측 확인): "OO명 이내"는 총원 상한이지 유급 민간위원 수가 아닐 수
있다(2213848: 30명 상한 중 정부위원 6명은 무급). "OO명 이상 OO명 이하"
(2211944)처럼 범위인 경우, 그리고 "정원 증가분"처럼 신구 비교 델타가
필요한 경우는 이 모듈로 못 푼다 — 그런 경우 None을 반환해 선례값 폴백을
그대로 쓰게 한다.

실측(2126334): "공동위원장 2인을 포함한 25인 이내의 위원"처럼 인원을
"명" 대신 "인"으로 세는 조문도 있다(구식/한자어 표기, "명"과 동일한
의미) — extraction_mismatches 진단으로 처음 확인됨. 세 패턴 모두 "명"과
"인"을 동등하게 인정한다.
"""
from __future__ import annotations

import re

COMMITTEE_SIZE_EXTRACT_VERSION = "committee-size-extract-v2"

_CNT = r"(?:명|인)"
# 실측(2215954, 국립의학전문대학원법 부칙 "10명 이내의 설립위원"): _SIZE_CAP이
# "위원" 직전 글자를 정확히 요구해 "설립위원"·"준비위원"·"추천위원"류 복합명사를
# 놓치고 있었다(설립준비위원회 자체는 깨끗한 TYPE_3 케이스라 잡았으면 9명×20만
# ×4회 폴백=900만원까지 근접했을 텐데, 못 잡아 review_required로 완전히 빗나감).
# _SIZE_MIN엔 이미 [가-힣]{0,8}위원 접두 허용이 있었는데 _SIZE_CAP/_SIZE_RANGE엔
# 빠져있던 걸 발견 — 셋 다 같은 방식으로 통일한다.
_SIZE_RANGE = re.compile(rf"(\d+)\s*{_CNT}\s*이상\s*(\d+)\s*{_CNT}\s*이하의?\s*[가-힣]{{0,8}}위원")
_SIZE_CAP = re.compile(rf"(\d+)\s*{_CNT}\s*이내의?\s*[가-힣]{{0,8}}위원")
# 실측(2216353, 회계기본법안): "위원장 및 부위원장 각 1명을 포함한 9명의
# 위원"처럼 위원장 외에 부위원장 등 두 번째 직위가 "및 OO 각"으로 끼어드는
# 표현을 못 잡고 있었다 — "위원장 N명 포함" 단일 직위 표현만 상정한 패턴이라.
# 부위원장을 두는 위원회 자체가 드물지 않은 구조라 일반화해서 고친다.
# 실측(2216417, 국가인권위원회법): "추천위원장 1명을 포함하여 7명의
# 추천위원으로 구성"처럼 "위원장"·"위원" 둘 다 앞에 복합명사가 붙는 경우를
# 놓치고 있었다 — _SIZE_CAP/_SIZE_RANGE/_SIZE_MIN엔 이미 있는 [가-힣]{0,8}
# 접두 허용이 정작 _SIZE_EXACT엔 두 "위원" 자리 모두 빠져있었다(양쪽 다
# 하드코딩된 리터럴). 같은 원리를 동일하게 적용해 통일한다.
# 주의: 접두 허용을 넣자마자 "…포함한 15명 이내의 위원"(2216520, cap 케이스)
# 까지 삼켜서 exact로 오분류하는 회귀가 생겼다 — "이내"는 상한(cap) 표지라
# exact의 복합명사 접두로 흡수되면 안 된다. 뒤쪽 접두에서만 "이내" 시작을
# 명시적으로 차단한다.
_SIZE_EXACT = re.compile(
    rf"[가-힣]{{0,8}}위원장(?:및[가-힣]{{0,10}}각)?\s*\d*\s*{_CNT}을?\s*포함(?:한|하여)\s*(\d+)\s*{_CNT}의?\s*(?!이내)[가-힣]{{0,8}}위원"
)
# 실측(2126635): "30명 이상의 위원", "500명 이상의 국민참여위원"처럼 상한 없이
# 하한만 있는 표현 — 이전엔 _SIZE_RANGE("이상...이하" 둘 다 필요)도
# _SIZE_CAP("이내")도 못 잡았다. NABO 실제 관행(2126635 정답지 확인)은 이
# 경우 명시된 최솟값을 그대로 점추정치로 쓴다("현 시점에서는 구성을 알 수
# 없으므로 최소인원으로 가정함") — 단, "이내"(cap)가 과대추정 위험이라면
# "이상"(min)은 반대로 과소추정 위험이 있다는 걸 명시해야 한다.
_SIZE_MIN = re.compile(rf"(\d+)\s*{_CNT}\s*이상의?\s*[가-힣]{{0,8}}위원")

# 실측(2215954, 국립의학전문대학원법 부칙 설립준비위원회): 당연직은 지금까지
# "N명 중 M명은 공무원" 식 열거형만 다뤘는데, "위원장은 보건복지부차관으로
# 한다"처럼 위원장을 정부 직위로 못박는 지정형 문구도 있다 — 이미 공무원인
# 사람이 위원장이면 무급(당연직)이라는 원칙 자체는 새 게 아니다(2211774
# 법원내부구성원, 2213188 당연직 위원장 고용노동부장관 등에서 이미 확인됨).
# 새로운 건 표현 형태(지정형)일 뿐이라 규칙화하되, "위원장은 위원 중에서
# 호선한다"처럼 정부 직위가 아닌 지정(민간 위원장 오탐)까지 잡지 않도록
# 정부 직위 명칭 화이트리스트로 좁힌다.
_GOV_TITLE_RE = r"(?:차관|장관|청장|처장|실장|국무총리|시장|도지사|지사|원장|본부장|이사장)"
_DESIGNATED_CHAIR_EX_OFFICIO_RE = re.compile(rf"위원장은[가-힣]*(?:{_GOV_TITLE_RE})(?:으)?로한다")


def extract_designated_ex_officio_chair(article_text: str) -> int | None:
    """"위원장은 OO(정부 직위)로 한다"류 지정형 문구에서 당연직 위원장 1명을
    감지한다. 못 찾으면 None(LLM 판단에 맡김)."""
    text = re.sub(r"\s+", "", article_text or "")
    return 1 if _DESIGNATED_CHAIR_EX_OFFICIO_RE.search(text) else None


# 실측(2124118): 위원 정수와 별도로 "상임위원 N명"이 언급되는 경우가 있다 —
# 이 사람들은 회의수당이 아니라 상근 인건비 대상(다른 산식)이라 총원/당연직과
# 구분해서 뽑아야 한다.
_STANDING_MEMBER_RE = re.compile(r"상임위원\s*(\d+)\s*명")


def extract_standing_member_count(article_text: str) -> int | None:
    """조문에서 "상임위원 N명"을 뽑는다. 없으면 None(상임위원 없는 게 기본값)."""
    text = re.sub(r"\s+", "", article_text or "")
    match = _STANDING_MEMBER_RE.search(text)
    return int(match.group(1)) if match else None


# 실측(2216065): "위촉하는 사람 5명"처럼 유급 위원 수(paid_members)가 당연직을
# 빼는 계산 없이 조문에 그대로 명시되는 경우가 있다. 당연직(ex_officio)은
# "대통령령으로 정하는 관계 중앙행정기관의 장"처럼 숫자 없이 위임되는 경우가
# 흔해 LLM 판단에 의존해야 하지만(비결정적), 위촉 인원은 이 정규식으로 바로
# 확정할 수 있어 우선한다 — ex_officio가 없거나 틀려도 회의수당 대상 인원은
# 정확해진다.
_PAID_MEMBER_RE = re.compile(r"위촉(?:하는|한)?\s*(?:사람|위원)\s*(\d+)\s*명")


# 실측(2026 웹서치, 2126334 검증 과정에서 확인): 정부입법지원센터 「법령입안
# 심사기준」의 표준 문구 — "민간위원(또는 공무원이 아닌 위원)이 과반수가
# 되도록 하여야 한다"를 위원정수 조항 후단에 두는 경우가 있다. 이건 법적
# 최소비율(하한)이 조문에 "숫자가 아닌 문구로" 명시된 경우라, 정확한 유급
# 인원수는 못 뽑아도 하한(과반수=50%)은 확정적으로 뽑을 수 있다. 조건부
# 관행이라 모든 위원회에 있는 건 아니다 — 없으면 None.
# 주의: "여성인 위원이 OO퍼센트" 같은 성별 균형 조항은 별개 목적(양성평등)이라
# 제외한다.
# 실측(2126679 돌봄근로자법): "공무원이 아닌 사람이 과반수가 되도록"처럼
# "위원" 대신 "사람"을 쓰는 표현도 있다 — 처음엔 "위원"만 허용해서 놓쳤다.
_PAID_RATIO_MAJORITY_RE = re.compile(
    r"(?:민간위원|위촉위원|위촉된?\s*위원|공무원이\s*아닌\s*(?:위원|사람))(?:은|이)?\s*과반수가?\s*되도록"
)


def extract_paid_member_ratio_floor(article_text: str) -> float | None:
    """조문에 "민간위원이 과반수가 되도록 하여야 한다"류 법정 최소비율 조항이
    있으면 0.5를 반환(하한만 확정, 정확한 값은 아님). 없으면 None."""
    text = re.sub(r"\s+", "", article_text or "")
    if _PAID_RATIO_MAJORITY_RE.search(text):
        return 0.5
    return None


def extract_paid_member_count(article_text: str) -> int | None:
    """조문에서 "위촉(하는) 사람/위원 N명"을 뽑는다. 없으면 None(ex_officio 기반
    계산으로 폴백)."""
    text = re.sub(r"\s+", "", article_text or "")
    match = _PAID_MEMBER_RE.search(text)
    return int(match.group(1)) if match else None


_MEETING_EXACT = re.compile(r"연\s*(\d+)\s*회")
_MEETING_QUARTERLY = re.compile(r"분기(?:별|마다|당)")
_MEETING_SEMIANNUAL = re.compile(r"반기(?:별|마다|당)")
_MEETING_MONTHLY = re.compile(r"매월|월별|월\s*1\s*회")
_MEETING_BIMONTHLY = re.compile(r"격월")


def extract_meeting_frequency(article_text: str) -> int | None:
    """조문에서 연간 회의 횟수를 뽑는다. 숫자 명시 우선, 주기 표현은 환산.

    못 찾으면 None(호출부가 관행값 폴백을 쓰게 한다) — 여기서 추측하지 않는다.
    """
    text = re.sub(r"\s+", "", article_text or "")
    exact_match = _MEETING_EXACT.search(text)
    if exact_match:
        return int(exact_match.group(1))
    if _MEETING_MONTHLY.search(text):
        return 12
    if _MEETING_BIMONTHLY.search(text):
        return 6
    if _MEETING_QUARTERLY.search(text):
        return 4
    if _MEETING_SEMIANNUAL.search(text):
        return 2
    return None


# 실측(2126661, 10·29이태원참사 특별법): "조사위원회는... 1년 이내에 활동을
# 완료하여야 한다"처럼 활동기간이 법에 명시된 한시(限時) 위원회가 있다 —
# 이런 위원회는 영구 상설이 아니라서 5개년 시계열에 매년 반복 적용하면
# 안 된다(실측: 원래 15개월짜리 총액을 5년 매년 반복치로 계산하면 +413%).
# 연장 조항이 "한 차례만 M개월 이내에서 연장할 수 있다"처럼 여러 번 나올 수
# 있어 전부 합산한다 — 단, 이건 "연장이 전부 실제로 쓰였을 때의 상한"이지
# NABO가 실제로 가정하는 값(예: 연장 1회만 사용)과는 다를 수 있다(정직한
# 상한으로만 취급).
# 실측(2126661): 기본기간은 "활동기간은 N년 이내로 하고"(요약문 스타일)
# 또는 "OO일부터 N년 이내에 활동을 완료하여야 한다"(조문 원문 스타일) 두
# 가지로 표현된다.
_ACTIVITY_BASE_YEAR_RE = re.compile(r"활동기간은?\s*(\d+)\s*년\s*이내|(\d+)\s*년\s*이내에\s*활동을?\s*완료")
_ACTIVITY_BASE_MONTH_RE = re.compile(r"활동기간은?\s*(\d+)\s*개월\s*이내|(\d+)\s*개월\s*이내에\s*활동을?\s*완료")
_ACTIVITY_EXTENSION_RE = re.compile(r"활동기간을?\s*(?:추가로\s*)?(\d+)\s*개월\s*이내에서\s*연장")


def extract_activity_duration_months(article_text: str) -> int | None:
    """조문에서 한시위원회의 최대 활동기간(개월)을 뽑는다. 기본기간 + 연장
    조항(여러 번 나오면 전부 합산)의 상한. 한시 조항 자체가 없으면 None
    (영구 상설로 취급)."""
    text = re.sub(r"\s+", "", article_text or "")
    base_months = None
    year_match = _ACTIVITY_BASE_YEAR_RE.search(text)
    if year_match:
        value = year_match.group(1) or year_match.group(2)
        base_months = int(value) * 12
    else:
        month_match = _ACTIVITY_BASE_MONTH_RE.search(text)
        if month_match:
            value = month_match.group(1) or month_match.group(2)
            base_months = int(value)
    if base_months is None:
        return None
    extension_months = sum(int(m) for m in _ACTIVITY_EXTENSION_RE.findall(text))
    return base_months + extension_months


def extract_committee_size(article_text: str) -> dict | None:
    """조문에서 위원 정수를 뽑는다.

    반환: {"kind": "exact"|"cap"|"min", "value": int} 또는
          {"kind": "range", "low": int, "high": int}, 못 찾으면 None.
    "exact"/"cap"/"min"은 곧바로 쓸 수 있고(단, "cap"은 과대추정, "min"은
    과소추정 위험을 내포), "range"는 사람 확인이 필요하다는 신호로만 쓴다.
    """
    text = re.sub(r"\s+", "", article_text or "")
    range_match = _SIZE_RANGE.search(text)
    if range_match:
        return {
            "kind": "range",
            "low": int(range_match.group(1)),
            "high": int(range_match.group(2)),
        }
    exact_match = _SIZE_EXACT.search(text)
    if exact_match:
        return {"kind": "exact", "value": int(exact_match.group(1))}
    cap_match = _SIZE_CAP.search(text)
    if cap_match:
        return {"kind": "cap", "value": int(cap_match.group(1))}
    min_match = _SIZE_MIN.search(text)
    if min_match:
        return {"kind": "min", "value": int(min_match.group(1))}
    return None
