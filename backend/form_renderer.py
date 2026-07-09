"""form_renderer.py

analyzer_v2 결과를 경기도/국회 비용추계서 양식 HTML로 렌더링.

사용:
    from .form_renderer import render_form
    html = render_form(result, format="gyeonggi")
    html = render_form(result, format="assembly")
"""
from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Any

# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

def _fmt_amount(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def _safe(text: Any) -> str:
    return escape(str(text or ""))


def _display_bill_name(value: Any) -> str:
    name = str(value or "")
    replacements = {
        "기반조성및": "기반조성 및 ",
        "국방정보자원관리에관한": "국방정보자원관리에 관한 ",
        "일부개정법률안": " 일부개정법률안",
        "전부개정법률안": " 전부개정법률안",
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    return " ".join(name.split())


def _public_reason_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"유사\s*입법사례\([^)]*\)에서도\s*", "유사 입법사례에서도 ", text)
    text = re.sub(r"유사\s*입법사례\s*\d+\s*", "유사 입법사례 ", text)
    return " ".join(text.split())


# ── 경기도 별지 제1호서식 ─────────────────────────────────────────────────────

def render_gyeonggi(result: dict[str, Any]) -> str:
    """경기도 의안의 비용 추계에 관한 조례 [별지 제1호서식]."""
    bill_name = _safe(result.get("billName"))
    today = datetime.now().strftime("%Y년 %m월 %d일")

    estimate = result.get("estimate") or {}
    items    = estimate.get("items") or []
    years    = estimate.get("year_estimates") or []
    non_at   = result.get("nonAttachment")
    verdict  = result.get("verdict", {})
    articles = result.get("articles", [])

    # 비용유발 조문만
    triggers = [a for a in articles if a.get("cost_trigger")]
    trigger_rows = "".join(
        f"<tr><td>{_safe(a.get('no'))}</td>"
        f"<td>{_safe(a.get('trigger_type'))}</td>"
        f"<td>{_safe(a.get('reason'))}</td></tr>"
        for a in triggers
    ) or "<tr><td colspan='3' class='empty'>해당 조문 없음</td></tr>"

    # 항목별 + KOSIS 자동조회값
    def _kosis_html(it):
        lookups = it.get("kosis_lookups") or []
        if not lookups:
            return ""
        rows = []
        for k in lookups:
            yvs = " · ".join(
                f"{yv.get('year')}: {yv.get('value')}{k.get('unit','')}"
                for yv in (k.get("year_values") or [])
            )
            rows.append(
                f"<div style='margin-top:4px'><b>{_safe(k.get('variable'))}</b> "
                f"<span style='color:#666;font-size:9pt'>({_safe(k.get('source'))})</span>"
                f"<div style='font-size:9pt;color:#333'>{_safe(yvs)}</div></div>"
            )
        return "<div style='margin-top:6px;padding:6px;background:#f0fdf4;border-left:3px solid #16a34a;font-size:9pt'><b>📊 KOSIS</b>" + "".join(rows) + "</div>"

    item_rows = "".join(
        f"<tr>"
        f"<td>{i+1}</td>"
        f"<td>{_safe(it.get('name'))}{_kosis_html(it)}</td>"
        f"<td>{_safe(it.get('category'))}</td>"
        f"<td>{_safe(it.get('formula'))}</td>"
        f"<td>{_safe(it.get('trigger_ref'))}</td>"
        f"</tr>"
        for i, it in enumerate(items)
    ) or "<tr><td colspan='5' class='empty'>비용 항목 없음</td></tr>"

    # 연도별
    year_rows = "".join(
        f"<tr><td>{_safe(y.get('year'))}차년도</td>"
        f"<td>{_fmt_amount(y.get('amount_thousand'))}</td>"
        f"<td>{_safe(y.get('note'))}</td></tr>"
        for y in years
    ) or "<tr><td colspan='3' class='empty'>—</td></tr>"

    # 미첨부 사유서 섹션
    non_attach_block = ""
    if non_at:
        non_attach_block = f"""
        <section class="block">
          <h3>비용추계서 미첨부 사유</h3>
          <p><strong>{_safe(non_at.get('type'))}유형</strong></p>
          <p>{_safe(non_at.get('reason_text'))}</p>
        </section>
        """

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{bill_name} - 비용추계서 (경기도)</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: 'Noto Sans KR', sans-serif; color:#111; line-height:1.6; font-size:11pt; }}
  .header {{ text-align:center; margin-bottom: 20px; }}
  .header h1 {{ font-size: 16pt; margin: 0; letter-spacing: -0.02em; }}
  .header .subtitle {{ color: #555; font-size: 9pt; margin-top: 4px; }}
  .meta {{ display: flex; justify-content: space-between; margin-bottom: 16px; font-size: 10pt; }}
  .block {{ margin: 18px 0; }}
  .block h3 {{ font-size: 12pt; border-bottom: 2px solid #333; padding-bottom: 4px; margin-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 10pt; }}
  th, td {{ border: 1px solid #999; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 700; }}
  td.amount {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.empty {{ text-align: center; color: #999; padding: 12px; }}
  .verdict-box {{ background: #f9fafb; border-left: 4px solid #4f46e5; padding: 12px 16px; margin: 12px 0; }}
  .verdict-box .label {{ font-weight: 700; font-size: 11pt; }}
  .signature {{ margin-top: 40px; text-align: right; font-size: 10pt; }}
  .footnote {{ font-size: 8.5pt; color: #666; margin-top: 4px; }}
</style></head>
<body>

<div class="header">
  <div class="subtitle">[별지 제1호서식] 경기도 의안의 비용 추계에 관한 조례</div>
  <h1>비 용 추 계 서</h1>
</div>

<div class="meta">
  <div><strong>의안명:</strong> {bill_name}</div>
  <div>{today}</div>
</div>

<section class="block">
  <h3>1. 종합 판단</h3>
  <div class="verdict-box">
    <div class="label">{_safe(verdict.get('label'))}</div>
    <div>{_safe(verdict.get('summary'))}</div>
    <div class="footnote">AI 분석 신뢰도: {round((verdict.get('confidence') or 0) * 100)}%</div>
  </div>
</section>

<section class="block">
  <h3>2. 재정수반요인 (비용 유발 조문)</h3>
  <table>
    <thead><tr><th style="width:18%">조항</th><th style="width:22%">유형</th><th>판단 근거</th></tr></thead>
    <tbody>{trigger_rows}</tbody>
  </table>
</section>

<section class="block">
  <h3>3. 비용항목 산출 근거</h3>
  <table>
    <thead><tr><th style="width:6%">번호</th><th>항목명</th><th style="width:12%">분류</th><th>산식</th><th style="width:18%">근거 조문</th></tr></thead>
    <tbody>{item_rows}</tbody>
  </table>
</section>

<section class="block">
  <h3>4. 5개년 추계 (단위: 천원)</h3>
  <table>
    <thead><tr><th style="width:18%">연도</th><th style="width:25%">금액</th><th>비고</th></tr></thead>
    <tbody>{year_rows}</tbody>
  </table>
</section>

{non_attach_block}

<div class="signature">
  경 기 도 의 회<br/>
  {today}
</div>

</body></html>"""


# ── 국회 별지 제2호서식 ─────────────────────────────────────────────────────────

def _render_assembly_non_attachment(result: dict[str, Any]) -> str:
    bill_name = _safe(_display_bill_name(result.get("billName")))
    today = datetime.now().strftime("%Y년 %m월 %d일")
    non_at = result.get("nonAttachment") or {}
    articles = result.get("articles") or []
    triggers = [article for article in articles if article.get("cost_trigger")]

    def article_ref(value: Any) -> str:
        raw = " ".join(str(value or "").split())
        return raw if raw.startswith("안 ") else f"안 {raw}" if raw.startswith("제") else raw

    def plain(value: Any, limit: int = 420) -> str:
        return _safe(" ".join(str(value or "").split())[:limit])

    type_text = str(non_at.get("type") or "3호")
    if "1" in type_text:
        legal_reason = "제1호: 예상되는 비용이 비용추계서 첨부 기준 미만인 경우"
    elif "2" in type_text:
        legal_reason = "제2호: 국가안전보장 또는 군사기밀에 관한 사항으로 비용추계서 첨부가 곤란한 경우"
    else:
        legal_reason = "제3호: 의안의 내용이 선언적·권고적인 형식으로 규정되는 등 기술적으로 추계가 어려운 경우"

    factor_rows = []
    reason_rows = []
    for idx, article in enumerate(triggers, 1):
        ref = article_ref(article.get("no"))
        factor_rows.append(
            f"<tr><td>{idx}</td><td>{_safe(ref)}</td>"
            f"<td>{plain(article.get('text'))}</td><td>의무규정</td></tr>"
        )
        reason_rows.append(
            f"<tr><td>{idx}</td><td>{_safe(ref)}</td><td>{_safe(legal_reason)}</td></tr>"
        )
    factor_html = "".join(factor_rows) or "<tr><td colspan='4' class='empty'>해당 없음</td></tr>"
    reason_html = "".join(reason_rows) or (
        f"<tr><td>1</td><td>재정수반요인</td><td>{_safe(legal_reason)}</td></tr>"
    )
    primary_ref = article_ref(triggers[0].get("no")) if triggers else "재정수반요인"
    primary_name = primary_ref
    if "(" in primary_ref and ")" in primary_ref:
        primary_name = primary_ref.split("(", 1)[1].rsplit(")", 1)[0]

    reason_text = _safe(_public_reason_text(non_at.get("reason_text")))

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{bill_name} - 비용추계서 미첨부 사유서</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: 'Noto Sans KR', 'Apple SD Gothic Neo', sans-serif; color:#111; line-height:1.5; font-size:9.8pt; }}
  .doc-title {{ text-align:center; margin: 12px 0 22px; }}
  .doc-title .bill {{ font-size:14pt; font-weight:700; margin-bottom:5px; }}
  .doc-title .label {{ font-size:12pt; font-weight:700; }}
  h3 {{ font-size:11.5pt; margin:18px 0 7px; }}
  h4 {{ font-size:10pt; margin:12px 0 5px; }}
  table {{ width:100%; border-collapse:collapse; table-layout:fixed; font-size:8.9pt; }}
  th, td {{ border:1px solid #555; padding:5px 6px; vertical-align:top; word-break:keep-all; overflow-wrap:break-word; }}
  th {{ background:#f1f5f9; text-align:center; }}
  td.center, td:first-child {{ text-align:center; }}
  td.empty {{ text-align:center; color:#888; }}
  .detail {{ margin-top:8px; padding:10px 12px; border-top:1px solid #555; border-bottom:1px solid #555; }}
  .detail-title {{ font-weight:700; margin-bottom:5px; }}
  .detail p {{ margin:0; line-height:1.65; }}
  .signature {{ margin-top:34px; text-align:center; font-size:9pt; }}
</style></head>
<body>
<div class="doc-title">
  <div class="bill">{bill_name}</div>
  <div class="label">【비용추계서 미첨부 사유서】</div>
</div>

<h3>Ⅰ. 재정수반요인</h3>
<table>
  <colgroup><col width="7%"><col width="25%"><col width="56%"><col width="12%"></colgroup>
  <thead><tr><th>연번</th><th>조·항(조제목)</th><th>주요내용</th><th>비고</th></tr></thead>
  <tbody>{factor_html}</tbody>
</table>

<h3>Ⅱ. 미첨부 근거 규정 및 상세 사유</h3>
<h4>1. 근거 규정</h4>
<table>
  <colgroup><col width="7%"><col width="28%"><col width="65%"></colgroup>
  <thead><tr><th>연번</th><th>조·항(조제목)</th><th>미첨부 근거 규정</th></tr></thead>
  <tbody>{reason_html}</tbody>
</table>

<h4>2. 상세 사유</h4>
<div class="detail">
  <div class="detail-title">□ {primary_name}({_safe(primary_ref)})</div>
  <p>○ {reason_text}</p>
</div>

<div class="signature">
  국회예산정책처 작성 형식 참고<br>
  {today}
</div>
</body></html>"""


def render_assembly(result: dict[str, Any]) -> str:
    """국회 비용추계서 원문 흐름에 가깝게 렌더링한다."""
    bill_name = _safe(result.get("billName"))
    today = datetime.now().strftime("%Y년 %m월 %d일")

    estimate = result.get("estimate") or {}
    items    = estimate.get("items") or []
    years    = estimate.get("year_estimates") or []
    non_at   = result.get("nonAttachment")
    articles = result.get("articles", [])
    workflow = ((result.get("workflow") or {}).get("issues") or [])
    verdict_type = str((result.get("verdict") or {}).get("type") or "")
    has_amount = any(
        isinstance(row, dict) and row.get("amount_thousand") is not None
        for row in years
    )
    if non_at and (verdict_type.startswith("미첨부") or not items or not has_amount):
        return _render_assembly_non_attachment(result)

    def _million(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(round(float(value) / 1000))
        except (TypeError, ValueError):
            return None

    def _fmt_million(value: Any) -> str:
        amount = _million(value)
        return "—" if amount is None else f"{amount:,}"

    def _year_label(row: dict[str, Any]) -> str:
        return str(row.get("year_label") or row.get("calendar_year") or row.get("year") or "")

    def _article_ref(text: Any) -> str:
        raw = str(text or "")
        if raw.startswith("안 "):
            return raw
        if raw.startswith("제"):
            return f"안 {raw}"
        return raw

    def _display_article_ref(text: Any) -> str:
        raw = _article_ref(text)
        replacements = (
            ("국가및지방자치단체", "국가 및 지방자치단체"),
            ("간호정책종합계획", "간호정책 종합계획"),
            ("연도별시행계획", "연도별 시행계획"),
            ("간호정책심의위원회", "간호정책심의위원회"),
            ("간호ㆍ간병통합서비스", "간호·간병통합서비스"),
            ("간호인력지원센터", "간호인력 지원센터"),
            ("설치및운영", "설치 및 운영"),
            ("수립ㆍ시행", "수립·시행"),
            ("경비보조등", "경비 보조 등"),
            ("의수립등", "의 수립 등"),
            ("의제공등", "의 제공 등"),
            ("의책무등", "의 책무 등"),
        )
        for old, new in replacements:
            raw = raw.replace(old, new)
        return raw

    def _plain(text: Any, limit: int = 240) -> str:
        cleaned = " ".join(str(text or "").split())
        return _safe(cleaned[:limit])

    def _trigger_ref_key(text: Any) -> str:
        raw = str(text or "")
        return raw.split("(")[0].replace("안", "").strip()

    def _ref_tokens(text: Any) -> set[str]:
        """텍스트(trigger_ref / article.no)에서 별표 N, 제 N 조 같은
        개별 참조 토큰을 모두 추출. 묶음 trigger_ref(별표1, 별표3, 별표5)도
        개별 항목 매칭이 가능해진다."""
        raw = str(text or "")
        out: set[str] = set()
        for m in re.finditer(r"별표\s*\d+(?:의\s*\d+)?|제\s*\d+\s*조(?:의\s*\d+)?", raw):
            out.add(re.sub(r"\s+", "", m.group(0)))
        base = _trigger_ref_key(raw)
        if base:
            normalized = base.replace(" ", "").replace("[", "").replace("]", "").rstrip(",")
            if normalized:
                out.add(normalized)
        return out

    def _cleanup_source_note(text: Any) -> str:
        """추계 근거 텍스트에서 부정 표현 정리."""
        s = str(text or "")
        s = re.sub(r"산출\s*근거\s*및\s*상세\s*내역\s*미제공\.?\s*", "", s)
        s = re.sub(r"\(?상세\s*산식\s*미제공\)?\s*", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _article_reason(article: dict[str, Any], *, estimated: bool) -> str:
        rule = article.get("rule_cost_trigger") or {}
        policy = str(article.get("case_policy") or article.get("incremental_cost_status") or "")
        if estimated:
            if article.get("estimate_feasibility") == "formula_ready":
                return "유사사례를 참고하여 추계"
            return (
                rule.get("review_reason")
                or rule.get("reason")
                or article.get("reason")
                or "산식과 전제값을 적용할 수 있어 비용추계 대상으로 보았습니다."
            )
        if policy in {
            "transferred_existing_provision",
            "referenced_existing_program",
            "existing_program_continuation",
            "linked_existing_survey",
        }:
            return "기시행 사업으로 추가 비용을 수반하지 않음"
        if policy == "integrated_plan_basic_expense":
            return "각 기관의 기본경비로 수행할 수 있을 것으로 예상되어 추계 대상에서 제외"
        if policy == "declarative_unquantified":
            return "제3호: 의안의 내용이 선언적·권고적인 형식으로 규정되는 등 기술적으로 추계가 어려운 경우"
        if policy == "discretionary_unquantified":
            return "제3호: 재량적 규정으로 지원 여부 및 규모를 합리적으로 예측하기 어려운 경우"
        policy_reason = _public_reason_text(article.get("reason"))
        if policy_reason:
            return policy_reason
        if article.get("estimate_feasibility") == "non_attachment_review":
            return (
                rule.get("review_reason")
                or "재량규정, 자료 부족 또는 구체 사업계획 미확정으로 현 단계에서 합리적 추계가 곤란합니다."
            )
        if article.get("cost_candidate_strength") == "weak":
            return "계획·조사·행정절차 성격이 강해 직접적인 추가재정소요 산정 대상에서 제외 검토합니다."
        return rule.get("review_reason") or rule.get("reason") or "추가 전제값 확인이 필요합니다."

    def _article_summary(article: dict[str, Any]) -> str:
        supplied = article.get("summary") or article.get("content_summary")
        if supplied:
            return _public_reason_text(supplied)
        title = str(article.get("no") or "")
        title_text = title.split("(", 1)[1].rsplit(")", 1)[0] if "(" in title and ")" in title else title
        policy = str(article.get("case_policy") or article.get("incremental_cost_status") or "")
        trigger_type = str(article.get("trigger_type") or "")
        compact_text = re.sub(r"\s+", "", str(article.get("text") or ""))
        if policy == "integrated_plan_basic_expense":
            if "시행계획" in title_text:
                return f"{title_text}을 매년 수립·시행하고 추진실적을 평가하도록 규정함"
            return f"{title_text}을 수립하고 기존 법정계획과 연계하도록 규정함"
        if policy == "linked_existing_survey" or "실태조사" in title_text:
            return f"{title_text}를 정기적으로 실시하고 그 결과를 정책계획에 반영하도록 규정함"
        if policy in {"transferred_existing_provision", "referenced_existing_program"}:
            return f"현행 법률에서 시행 중인 {title_text} 관련 제도와 지원 근거를 이 법에 규정함"
        if policy == "existing_program_continuation":
            return f"기존 {title_text} 제도를 승계하여 설치·운영 및 비용 지원 근거를 규정함"
        if policy == "declarative_unquantified":
            return f"국가와 지방자치단체가 {title_text} 관련 정책과 필요한 지원을 마련하도록 규정함"
        if policy == "discretionary_unquantified":
            return f"{title_text}에 필요한 시설비·운영비·조사연구비 등을 보조할 수 있도록 규정함"
        if trigger_type == "조직설치":
            # 별표/기관 신설 케이스: 위원회 표현 대신 실제 기관명 사용
            article_no_raw = str(article.get("no") or "")
            is_annex = "별표" in article_no_raw
            institution_hits = re.findall(r"(인천|서울|부산|대구|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)?\s*(고등법원|고등검찰청|지방법원|지방검찰청|가정법원|행정법원)", compact_text)
            if institution_hits:
                names = []
                for region, kind in institution_hits:
                    label = (region + kind).strip() if region else kind
                    if label not in names:
                        names.append(label)
                if len(names) >= 2:
                    return f"{names[0]} 및 {names[1]} 등 신규 사법조직을 설치함"
                return f"{names[0]}을(를) 신설함"
            if is_annex:
                return f"{title_text or '별표 개정'}에 따라 신규 기관·조직을 설치함"
            member_match = re.search(r"(\d+)명이내", compact_text)
            member_text = f" {member_match.group(1)}명 이내로 구성되는" if member_match else ""
            return f"{title_text} 관련 사항을 심의하기 위하여{member_text} 위원회 또는 조직을 설치·운영하도록 규정함"
        if trigger_type == "직접지원":
            return f"{title_text}의 시행에 필요한 비용의 전부 또는 일부를 지원할 수 있도록 규정함"
        return f"{title_text}에 관한 의무와 지원 근거를 규정함"

    def _article_rule_note(article: dict[str, Any]) -> str:
        text = re.sub(r"\s+", "", str(article.get("text") or ""))
        has_mandatory = bool(re.search(r"(하여야한다|두어야한다|실시하여야한다|수립하여야한다)", text))
        has_discretionary = bool(re.search(r"(할수있다|지원할수있다|보조할수있다)", text))
        if has_mandatory and has_discretionary:
            return "의무·재량규정"
        if has_discretionary:
            return "재량규정"
        return "의무규정"

    def _item_formula_reason(item: dict[str, Any]) -> str:
        committee = item.get("committee_formula") or {}
        if committee:
            return (
                f"회의횟수 {committee.get('meeting_count')}회 × "
                f"수당지급대상 {committee.get('paid_members')}명 × "
                f"회의수당 단가 {int(committee.get('allowance_won') or 0):,}원 = "
                f"{_fmt_million(committee.get('amount_thousand'))}백만원"
            )
        calc = item.get("calculation") or {}
        source_note = calc.get("source_note")
        if source_note:
            cleaned = _cleanup_source_note(source_note)
            if cleaned:
                return cleaned
            return "유사 공식 비용추계 사례의 산식·금액을 적용해 합리적 초안을 생성"
        selected = item.get("selected_formula") or {}
        if selected.get("basis"):
            return str(selected.get("basis"))
        template = item.get("formula_template") or {}
        return template.get("notes") or "구조화 산식과 전제값을 적용했습니다."

    def _korean_object_particle(value: Any) -> str:
        text = re.sub(r"[^가-힣]", "", str(value or ""))
        if not text:
            return "을"
        code = ord(text[-1]) - 0xAC00
        return "을" if 0 <= code <= 11171 and code % 28 else "를"

    def _fmt_result_amount(value: int | None) -> str:
        if value is None:
            return "산정 불가"
        if abs(value) < 100:
            return f"{value * 100:,}만원"
        return f"{value:,}백만원"

    def _assumption_value(item: dict[str, Any], name: str) -> Any:
        for assumption in item.get("assumptions") or []:
            if assumption.get("name") == name:
                return assumption.get("value")
        return None

    def _general_assumption_paragraphs() -> list[str]:
        if not items:
            return []
        paragraphs: list[str] = []
        analogy = estimate.get("analogy_selection") or {}
        if analogy:
            paragraphs.append(
                "법률안만으로 조직의 실제 업무량과 지원인력 규모를 확정하기 어려우므로, "
                "조직 유형과 직무가 유사한 국회 비용추계 사례를 기준선으로 적용함."
            )
            paragraphs.append(
                f"유사사례는 {analogy.get('bill_no') or ''} "
                f"{analogy.get('bill_name') or '국회 비용추계서'}이며, "
                "해당 사례의 항목별 산식과 전제값은 적용 적합성을 확인할 필요가 있음."
            )
            paragraphs.append(f"추계기간은 {first_year_text}부터 {last_year_text}까지 5년으로 함.")
            return paragraphs

        committee_item = next((item for item in items if item.get("committee_formula")), None)
        if committee_item:
            committee = committee_item.get("committee_formula") or {}
            meeting_count = committee.get("meeting_count")
            paid_members = committee.get("paid_members")
            allowance = committee.get("allowance_won")
            trace = committee.get("evidence_trace") or {}
            paragraphs.append(
                "재정수반요인 중 추가재정소요 산출이 어렵거나 기시행 사업으로 추가 비용이 수반되지 않는 "
                f"경우를 제외하고, 추계가 가능한 {_article_ref(committee_item.get('trigger_ref'))}의 비용을 산출함."
            )
            paragraphs.append(
                "위원회 설치 및 운영 비용은 기능과 구성 방식이 유사한 위원회 사례를 준용하며, "
                "별도의 사무조직 운영비가 확인되지 않는 경우 회의수당만 반영함."
            )
            paragraphs.append(
                f"위원회는 연 {meeting_count}회 개최하고, 위원장과 관계 공무원을 제외한 "
                f"수당 지급 대상 위촉위원은 {paid_members}명으로 가정함."
            )
            paid_trace = trace.get("paid_members") or {}
            if paid_trace.get("method") == "tag_candidates_mode":
                paid_stat = paid_trace.get("statistic") or {}
                paragraphs.append(
                    f"수당 지급 대상 인원은 유사 비용추계서 {paid_stat.get('n')}건의 민간·위촉위원 수 분포 "
                    f"(최빈값 {paid_stat.get('mode')}명, 중앙값 {paid_stat.get('median')}명)에서 도출함."
                )
            elif paid_trace.get("method", "").startswith("ratio"):
                paragraphs.append(
                    f"수당 지급 대상 인원은 위원 정수 중 위원장·관계공무원을 제외한 위촉위원 비율을 적용하여 산출함."
                )
            paragraphs.append(
                f"회의수당 단가는 1인당 {int(allowance or 0):,}원으로 하고 추계기간 중 동일하게 유지되는 것으로 가정함."
            )
            allowance_trace = trace.get("allowance_won") or {}
            if allowance_trace.get("method") == "guideline_anchored_with_tag_validation":
                stat = allowance_trace.get("statistic") or {}
                paragraphs.append(
                    f"단가는 「예산안 편성 및 기금운용계획안 작성 세부지침」 위원회 회의참석비 기준액을 채택하며, "
                    f"유사 비용추계서 {stat.get('n')}건의 회의수당 단가 분포"
                    f"(중앙값 {int(stat.get('median') or 0):,}원, 최빈값 {int(stat.get('mode') or 0):,}원)"
                    f"와 교차 비교하여 합리성을 검증함."
                )
            elif allowance_trace.get("method") == "tag_candidates_mode":
                stat = allowance_trace.get("statistic") or {}
                paragraphs.append(
                    f"단가는 유사 비용추계서 {stat.get('n')}건의 회의수당 분포에서 최빈값을 적용함."
                )
            elif allowance_trace.get("method") == "budget_guideline_fallback":
                paragraphs.append(
                    "단가는 「예산안 편성 및 기금운용계획안 작성 세부지침」상 위원회 회의참석비 "
                    "기본 150,000원에 장시간 회의 추가 50,000원을 더한 보수적 기준액을 적용함."
                )
            paragraphs.append(f"추계기간은 {first_year_text}부터 {last_year_text}까지 5년으로 함.")
            return paragraphs

        paragraphs.append(f"추계기간은 {first_year_text}부터 {last_year_text}까지 5년으로 함.")
        return paragraphs

    triggers = [a for a in articles if a.get("cost_trigger")]
    # 묶음 trigger_ref ("안 [별표1], [별표3], [별표5]") 도 개별 토큰까지 펼침
    estimated_refs: set[str] = set()
    for _it in items:
        estimated_refs |= _ref_tokens(_it.get("trigger_ref"))
    year_labels = [_year_label(y) for y in years]
    year_labels = [label if len(label) == 4 else str(2025 + idx) for idx, label in enumerate(year_labels)]
    year_amounts = [_million(y.get("amount_thousand")) for y in years]
    total_million = _million(estimate.get("total_amount_thousand"))
    if total_million is None and year_amounts:
        total_million = sum(v for v in year_amounts if v is not None)
    average_million = _million(estimate.get("average_amount_thousand"))
    if average_million is None and year_amounts:
        available = [v for v in year_amounts if v is not None]
        average_million = int(round(sum(available) / len(available))) if available else None

    first_year = year_labels[0] if year_labels else "1차년도"
    last_year = year_labels[-1] if year_labels else "5차년도"
    first_year_text = f"{first_year}년" if str(first_year).isdigit() else str(first_year)
    last_year_text = f"{last_year}년" if str(last_year).isdigit() else str(last_year)
    first_amount = _fmt_result_amount(year_amounts[0] if year_amounts else None)
    last_amount = _fmt_result_amount(year_amounts[-1] if year_amounts else None)
    total_text = _fmt_result_amount(total_million)
    avg_text = _fmt_result_amount(average_million)

    primary_trigger = next((a for a in triggers if a.get("cost_candidate_strength") == "strong"), triggers[0] if triggers else {})
    primary_title = str(primary_trigger.get("no") or items[0].get("trigger_ref") if items else "")
    primary_name = primary_title.split("(")[1].split(")")[0] if "(" in primary_title and ")" in primary_title else (items[0].get("name") if items else "추가재정소요")
    partial = any(
        issue.get("category") == "미첨부 가능 후보 분리"
        for issue in workflow
    )
    prefix = "(일부추계) " if partial else ""
    doc_type = str(result.get("docType") or "의안")
    action_text = "운영할 경우" if any(item.get("committee_formula") for item in items) else "시행할 경우"
    object_particle = _korean_object_particle(primary_name)
    result_sentence = (
        f"❑{prefix}{doc_type}에 따라 {primary_name}{object_particle} {action_text} 추가재정소요는 "
        f"{first_year_text} {first_amount}, {last_year_text} {last_amount} 등 "
        f"{first_year_text}부터 {last_year_text}까지 총 {total_text}(연평균 {avg_text})으로 추계됨"
    )
    committee_item = next((item for item in items if item.get("committee_formula")), None)
    if committee_item:
        meeting_count = (committee_item.get("committee_formula") or {}).get("meeting_count")
        result_reason = f"<p class='reason'>◦연 {int(meeting_count or 0)}회 위원회를 개최하는 것으로 가정</p>"
    else:
        result_reason = (
            f"<p class='reason'>◦{_safe(_article_ref(primary_trigger.get('no') or (items[0].get('trigger_ref') if items else '')))}는 "
            "구조화 산식과 전제값을 적용할 수 있어 추계 대상으로 봄.</p>"
            if primary_trigger or items else ""
        )
    result_notes = [
        "본 추계는 유사사례와 구조화 산식을 준용한 결과로, 실제 사업 규모와 운영 방법 등에 따라 추가재정소요가 달라질 수 있음"
    ]
    if partial:
        result_notes.append(
            "다른 재정수반요인은 추계가 곤란하거나 기시행 사업으로 판단하여 일부 재정수반요인만 추계한 것으로, 전체 재정소요액은 추계된 금액을 상회할 수 있음"
        )
    result_note_html = "".join(
        f"<p class='footnote'>주: {idx}. {_safe(note)}</p>"
        for idx, note in enumerate(result_notes, 1)
    )

    cost_row_label = (
        f"{_safe(_display_article_ref(_trigger_ref_key(items[0].get('trigger_ref') if items else primary_trigger.get('no'))))}에 따른 추가재정소요"
        if items or primary_trigger else "추가재정소요"
    )
    cost_table_header = "".join(f"<th>{_safe(label)}</th>" for label in year_labels)
    cost_table_cells = "".join(
        f"<td class='amount'>{'—' if value is None else f'{value:,}'}</td>"
        for value in year_amounts
    )

    trigger_rows = []
    for idx, article in enumerate(triggers, 1):
        note = _article_rule_note(article)
        trigger_rows.append(
            f"<tr><td>{idx}</td>"
            f"<td>{_safe(_display_article_ref(article.get('no')))}</td>"
            f"<td>{_safe(_article_summary(article))}</td>"
            f"<td>{_safe(note)}</td></tr>"
        )
    trigger_html = "".join(trigger_rows) or "<tr><td colspan='4' class='empty'>해당 없음</td></tr>"

    # ── 추계 여부 표: items 기준으로 그룹 1행, 묶음 trigger_ref도 그대로 표시
    #    items에 매칭 안 된 trigger article만 × 행으로 추가 → 모순(× + 산출) 자동 제거
    estimate_review_rows: list[str] = []
    covered_tokens: set[str] = set()
    row_idx = 0

    for item in items:
        row_idx += 1
        item_ref_raw = str(item.get("trigger_ref") or "").strip()
        if not item_ref_raw:
            item_ref_raw = str(item.get("name") or "")
        item_tokens = _ref_tokens(item_ref_raw)
        covered_tokens |= item_tokens
        # 대응 article의 reason 우선 사용 (기시행·기본경비 등 정책 분류 텍스트)
        matched_article = next(
            (a for a in triggers if _ref_tokens(a.get("no")) & item_tokens),
            None,
        )
        if matched_article is not None:
            reason = _article_reason(matched_article, estimated=True)
        else:
            reason = "유사사례를 참고하여 추계"
        estimate_review_rows.append(
            f"<tr><td>{row_idx}</td>"
            f"<td>{_safe(_display_article_ref(item_ref_raw))}</td>"
            f"<td class='center'>○</td>"
            f"<td>{_safe(reason)}</td></tr>"
        )

    for article in triggers:
        if _ref_tokens(article.get("no")) & covered_tokens:
            continue
        row_idx += 1
        reason = _article_reason(article, estimated=False)
        estimate_review_rows.append(
            f"<tr><td>{row_idx}</td>"
            f"<td>{_safe(_display_article_ref(article.get('no')))}</td>"
            f"<td class='center'>×</td>"
            f"<td>{_safe(reason)}</td></tr>"
        )

    estimate_review_html = "".join(estimate_review_rows) or "<tr><td colspan='4' class='empty'>해당 없음</td></tr>"

    # 미실시 사유 상세 블록은 items에 잡히지 않은 trigger article만 (× 행과 일치)
    exclusion_detail_html = "".join(
        f"<p class='case-detail'>❑({_safe(_display_article_ref(_trigger_ref_key(article.get('no'))))}) "
        f"{_safe(_public_reason_text(article.get('reason') or _article_reason(article, estimated=False)))}</p>"
        for article in triggers
        if not (_ref_tokens(article.get("no")) & covered_tokens)
    )
    general_assumptions = "".join(
        f"<p class='bullet'>❑{_safe(paragraph)}</p>"
        for paragraph in _general_assumption_paragraphs()
    )

    detail_blocks = []
    for item in items:
        item_years = item.get("year_amounts_thousand") or []
        item_cells = "".join(f"<td class='amount'>{_fmt_million(v)}</td>" for v in item_years[:len(year_labels)])
        item_total = sum((_million(v) or 0) for v in item_years[:len(year_labels)]) if item_years else None
        formula_text = _cleanup_source_note(item.get("formula")) or "유사 공식 비용추계 사례의 산식을 적용"
        block = [
            f"<h4>{_safe(item.get('name'))} <span class='small'>({_safe(_article_ref(item.get('trigger_ref')) )})</span></h4>",
            f"<p class='formula'>산식: {_safe(formula_text)}</p>",
            f"<p class='reason'>추계 근거: {_safe(_item_formula_reason(item))}</p>",
            "<table><thead><tr>"
            + cost_table_header
            + "<th>합계</th></tr></thead><tbody><tr>"
            + item_cells
            + f"<td class='amount'>{'—' if item_total is None else f'{item_total:,}'}</td></tr></tbody></table>",
        ]
        detail_amounts = item.get("detail_amounts") or {}
        if detail_amounts:
            rows = []
            for name, values in detail_amounts.items():
                cells = "".join(f"<td class='amount'>{_safe(values.get(label, '—'))}</td>" for label in year_labels)
                rows.append(f"<tr><td>{_safe(name)}</td>{cells}</tr>")
            block.append(
                "<table class='subtable'><thead><tr><th>구분</th>"
                + cost_table_header
                + "</tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )
        detail_blocks.append("<div class='detail-block'>" + "".join(block) + "</div>")
    detail_html = "".join(detail_blocks) or "<p class='empty'>비용 항목 없음</p>"

    non_attach_block = ""
    if non_at:
        non_attach_block = f"""
        <section class="block">
          <h3>비용추계서 미첨부 사유</h3>
          <p><strong>{_safe(non_at.get('type'))}유형</strong></p>
          <p>{_safe(non_at.get('reason_text'))}</p>
        </section>
        """

    side_opinion_html = ""
    if partial:
        side_opinion_html = (
            '<section class="block"><h3>Ⅳ. 부대의견</h3>'
            "<p class='case-detail'>❑본 비용추계서의 추가재정소요액은 유사사례를 준용하여 추계한 결과로, "
            "향후 실제 추가재정소요액은 달라질 수 있음.</p>"
            "<p class='case-detail'>❑다른 재정수반요인은 추계가 곤란하거나 기존 사업으로 판단하여 제외한 것이므로, "
            "전체 재정소요액은 추계된 금액을 상회할 수 있음.</p></section>"
        )

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{bill_name} - 비용추계서 (국회)</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: 'Noto Sans KR', 'Apple SD Gothic Neo', sans-serif; color:#111; line-height:1.45; font-size:9.7pt; }}
  p {{ margin: 4px 0; }}
  .doc-title {{ text-align:center; margin: 8px 0 14px; }}
  .doc-title .bill {{ font-size: 14pt; font-weight: 700; margin-bottom: 4px; }}
  .doc-title .label {{ font-size: 12pt; font-weight: 700; }}
  .block {{ margin: 14px 0 16px; }}
  h3 {{ font-size: 11.5pt; margin: 0 0 6px; }}
  h4 {{ font-size: 10pt; margin: 9px 0 3px; }}
  .result {{ margin: 6px 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 8.8pt; margin-top: 5px; }}
  th, td {{ border: 1px solid #555; padding: 4px 5px; text-align: left; vertical-align: top; }}
  th {{ background: #f1f5f9; font-weight: 700; text-align: center; }}
  .fixed-table {{ table-layout: fixed; }}
  .fixed-table th, .fixed-table td {{ word-break: keep-all; overflow-wrap: break-word; }}
  td.amount {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.center {{ text-align: center; }}
  td.empty {{ text-align: center; color: #999; padding: 12px; }}
  .unit {{ text-align:right; font-size:8.5pt; color:#444; margin-top: 2px; }}
  .small {{ font-size: 8.5pt; color:#444; font-weight: 400; }}
  .formula {{ margin: 3px 0 6px; }}
  .bullet {{ margin: 3px 0; }}
  .assumption-list {{ margin-top: 7px; }}
  table.assumption-item {{ margin: 0 0 5px; page-break-inside: avoid; font-size: 8.8pt; }}
  .assumption-item .assumption-detail {{ width: 76%; background: #f8fafc; }}
  .assumption-item .assumption-value {{ text-align: right; font-weight: 700; white-space: nowrap; }}
  .assumption-item .assumption-detail {{ color: #444; font-size: 8.3pt; }}
  .assumption-item .assumption-detail strong {{ color: #111; font-size: 8.8pt; }}
  .assumption-item .assumption-detail span {{ color: #555; }}
  .detail-block {{ margin-top: 10px; }}
  .subtable {{ margin-top: 6px; font-size: 9pt; }}
  .signature {{ margin-top: 26px; text-align: center; font-size: 9pt; }}
  .footnote {{ font-size: 8.5pt; color: #555; margin-top: 4px; }}
  .case-detail {{ margin: 7px 0; line-height: 1.55; }}
</style></head>
<body>

<div class="doc-title">
  <div class="bill">{bill_name}</div>
  <div class="label">【비용추계서】</div>
</div>

<section class="block">
  <h3>Ⅰ. 비용추계 결과</h3>
  <p class="result">{result_sentence}</p>
  {result_reason}
  <div class="unit">(단위: 백만원)</div>
  <table>
    <thead><tr><th>구분</th>{cost_table_header}<th>합계</th><th>연평균</th></tr></thead>
    <tbody>
      <tr><td>{cost_row_label}</td>{cost_table_cells}<td class="amount">{'—' if total_million is None else f'{total_million:,}'}</td><td class="amount">{'—' if average_million is None else f'{average_million:,}'}</td></tr>
    </tbody>
  </table>
  {result_note_html}
</section>

<section class="block">
  <h3>Ⅱ. 재정수반요인</h3>
  <table class="fixed-table">
    <colgroup><col width="7%"><col width="23%"><col width="58%"><col width="12%"></colgroup>
    <thead><tr><th>연번</th><th>조·항(조제목)</th><th>주요내용</th><th>비고</th></tr></thead>
    <tbody>{trigger_html}</tbody>
  </table>
</section>

<section class="block">
  <h3>Ⅲ. 비용추계의 전제와 상세내역</h3>
  <h4>1. 재정수반요인별 추계 여부</h4>
  <table class="fixed-table">
    <colgroup><col width="7%"><col width="24%"><col width="12%"><col width="57%"></colgroup>
    <thead><tr><th>연번</th><th>조·항(조제목)</th><th>추계여부</th><th>비고(추계 미실시 사유)</th></tr></thead>
    <tbody>{estimate_review_html}</tbody>
  </table>
  {exclusion_detail_html}

  <h4>2. 비용추계의 총괄적 전제</h4>
  {general_assumptions}

  <h4>3. 재정수반요인별 상세 추계내역</h4>
  <div class="unit">(단위: 백만원)</div>
  {detail_html}
</section>

{non_attach_block}
{side_opinion_html}

<div class="signature">
  국회예산정책처 작성 형식 참고<br/>
  {today}
</div>

</body></html>"""


# ── 진입점 ────────────────────────────────────────────────────────────────────

def render_form(result: dict[str, Any], format: str = "gyeonggi") -> str:
    """양식 선택해서 HTML 반환."""
    if format == "assembly":
        return render_assembly(result)
    return render_gyeonggi(result)
