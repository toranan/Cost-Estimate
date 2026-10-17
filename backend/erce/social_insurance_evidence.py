"""Reviewed social-insurance inputs learned from development cost-estimate pairs.

These rows are deliberately narrow precedents, not a universal social-insurance
rate table.  Values quoted by a cost estimate from an official source remain
``precedent_assumption`` until the underlying official source is independently
verified.
"""
from backend.erce.reviewed_variable_rows import evidence_row


def social_insurance_rows():
    rows = []

    # National Pension military-service credit: the estimate applies 50% of the
    # projected A-value and the 9% contribution rate.  The A-value series is a
    # model output specific to the cited estimate period, so it is not promoted
    # to an official standard.
    common = dict(
        source_bill_no="2203723",
        available_at="2024-09-05",
        agency="국민연금공단",
        policy_domain="국민연금",
        scope="군복무크레딧 보험료 지원; 국민연금 A값의 50%를 기준소득으로 적용",
        service_function="군복무크레딧 연금보험료 지원",
        reuse_policy="comparable",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        allowed_routes=["social_insurance_premium_support"],
        limitation="비용추계서에서 인용·산출한 값이며 공식 원문을 독립 확인한 표준값이 아님. 새 법안의 보험료율·지원비율·A값 적용 규정을 다시 확인해야 함.",
    )
    rows.append(evidence_row(
        evidence_key="2203723:pension_A_value:2026-2030",
        variable_key="premium_base_amount",
        value=[3_348_000, 3_479_000, 3_621_000, 3_769_000, 3_924_000],
        unit="KRW/person/month",
        source_ref="의안 2203723 비용추계서 PDF p.6 표3: 국민연금 A값",
        variable_role="premium_base",
        value_years=[2026, 2027, 2028, 2029, 2030],
        **common,
    ))
    rows.append(evidence_row(
        evidence_key="2203723:pension_contribution_rate:2024",
        variable_key="insurance_rate",
        value=0.09,
        unit="ratio",
        source_ref="의안 2203723 비용추계서 PDF p.6 표3: 보험료율 9% 적용",
        variable_role="rate",
        **common,
    ))
    rows.append(evidence_row(
        evidence_key="2203723:military_credit_income_share:2024",
        variable_key="support_rate",
        value=0.50,
        unit="ratio",
        source_ref="의안 2203723 비용추계서 PDF p.6 표3: A값의 50% 적용",
        variable_role="rate",
        **common,
    ))

    # Employment-insurance leave benefits.  This is a cited cap/precedent in the
    # estimate, not an independently verified notice value.
    rows.append(evidence_row(
        evidence_key="2203719:spouse_childbirth_daily_benefit:2024",
        variable_key="benefit_unit_amount",
        value=80_382,
        unit="KRW/person/day",
        source_bill_no="2203719",
        available_at="2024-09-05",
        source_ref="의안 2203719 비용추계서 PDF p.9 표6: 1일 지원단가 80,382원",
        agency="고용노동부",
        policy_domain="고용보험",
        scope="배우자출산휴가·난임치료휴가 급여; 2024년 배우자출산휴가 급여 상한액 준용",
        service_function="고용보험 휴가급여",
        variable_role="unit_rate",
        reuse_policy="same_scope",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        allowed_routes=["social_insurance_benefit_duration"],
        price_year=2024,
        limitation="2024년 고시 상한액을 비용추계서가 준용한 값. 다른 급여종류나 이후 연도에 자동 적용하지 않음.",
    ))

    # Adoption leave estimate: 2025 plan amount rolled forward by the estimate's
    # nominal-wage assumptions.  Keep the whole period attached to the precedent.
    rows.append(evidence_row(
        evidence_key="2204126:adoption_leave_daily_benefit:2026-2030",
        variable_key="benefit_unit_amount",
        value=[83_500, 86_600, 89_900, 93_300, 96_900],
        unit="KRW/person/day",
        source_bill_no="2204126",
        available_at="2024-09-20",
        source_ref="의안 2204126 비용추계서 PDF p.6 표3: 지원단가 8.35~9.69만원/일",
        agency="고용노동부",
        policy_domain="고용보험",
        scope="예비양부모 양자 입양휴가 급여; 배우자출산휴가 급여 수준 준용",
        service_function="고용보험 입양휴가급여",
        variable_role="unit_rate",
        reuse_policy="same_scope",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        allowed_routes=["social_insurance_benefit_duration"],
        value_years=[2026, 2027, 2028, 2029, 2030],
        limitation="입양휴가 세부기준이 확정되지 않은 상태에서 배우자출산휴가 수준을 준용한 선례이며 다른 휴가에 자동 전용 금지.",
    ))

    # Industrial-accident advance-payment recovery assumptions.  Two versions are
    # retained instead of overwriting the older observation.
    for bill, available, year, rate, page in (
        ("2206812", "2024-12-20", 2023, 0.673, 13),
        ("2213106", "2025-09-18", 2024, 0.718, 15),
    ):
        rows.append(evidence_row(
            evidence_key=f"{bill}:industrial_accident_recovery_rate:{year}",
            variable_key="recovery_rate",
            value=rate,
            unit="ratio",
            source_bill_no=bill,
            available_at=available,
            source_ref=f"의안 {bill} 비용추계서 PDF p.{page}: 징수결정액 대비 환수액 비율 {rate*100:.1f}% 준용",
            agency="근로복지공단",
            policy_domain="산재보험",
            scope="산재보험급여 우선지급 후 불승인·부당지급 환수 시나리오",
            service_function="산재보험 우선지급 미환수액 산정",
            variable_role="recovery_rate",
            reuse_policy="same_scope",
            source_class="precedent_assumption",
            obtained_from="development_review",
            available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
            allowed_routes=["social_insurance_unrecovered_payment"],
            observation_year=year,
            limitation="해당 연도 누적 환수실적을 장래에도 유지한다고 둔 선례 가정. 환수 절차·시점·제도 변경 시 재사용 금지.",
        ))

    # Health-insurance support share used by bill 2201075.  The estimate itself
    # describes 14% as a recent-actual average, not a fixed statutory rate.
    rows.append(evidence_row(
        evidence_key="2201075:health_insurance_government_support_rate:2021-2023avg",
        variable_key="government_support_rate",
        value=0.14,
        unit="ratio",
        source_bill_no="2201075",
        available_at="2024-06-27",
        source_ref="의안 2201075 비용추계서 PDF p.7 표3: 최근 3년 실제수입액 평균 국고지원율 14% 준용",
        agency="국민건강보험공단",
        policy_domain="건강보험",
        scope="건강보험 추가지출에 연동되는 국고지원금 추계",
        service_function="건강보험 국고지원",
        variable_role="government_share",
        reuse_policy="comparable",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        allowed_routes=["social_insurance_government_share"],
        observation_years=[2021, 2022, 2023],
        limitation="법정 고정률이 아니라 비용추계서가 최근 결산 실적 평균을 준용한 값. 새 추계 시 최신 결산·법률 기준을 다시 확인해야 함.",
    ))

    rows.append(evidence_row(
        evidence_key="2215623:long_term_care_government_support_rate:2025",
        variable_key="government_support_rate",
        value=0.20,
        unit="ratio",
        source_bill_no="2215623",
        available_at="2025-12-24",
        source_ref="의안 2215623 비용추계서 PDF p.7: 노인장기요양보험료 예상수입액의 20% 국고지원 규정 인용",
        agency="국민건강보험공단",
        policy_domain="노인장기요양보험",
        scope="노인장기요양보험 추가지출에 연동되는 일반회계 국고지원금",
        service_function="노인장기요양보험 국고지원",
        variable_role="government_share",
        reuse_policy="same_scope",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        allowed_routes=["social_insurance_government_share"],
        limitation="비용추계서가 법 규정을 인용한 값이나 공식 시행본을 독립 확인하지 않았으므로 official_standard로 승격하지 않음.",
    ))

    # A narrow industrial-accident benefit-rate observation; useful only for the
    # same voluntary-enrolment expansion scope.
    rows.append(evidence_row(
        evidence_key="2204606:small_business_owner_benefit_rate:2023",
        variable_key="benefit_recipient_rate",
        value=0.017,
        unit="ratio",
        source_bill_no="2204606",
        available_at="2024-10-08",
        source_ref="의안 2204606 비용추계서 PDF p.7 표5: 가입자 대비 보험급여 지급인원 비중 1.7%",
        agency="근로복지공단",
        policy_domain="산재보험",
        scope="중소사업주 산재보험 임의가입 승인요건 삭제에 따른 추가 급여지급인원 산정",
        service_function="산재보험 임의가입 확대",
        variable_role="participation_rate",
        reuse_policy="same_scope",
        source_class="precedent_assumption",
        obtained_from="development_review",
        available_at_basis="bill proposal date from training metadata; exact cost-estimate publication date not separately provided",
        limitation="2023년 중소사업주 가입자·급여지급 실적에 한정. 가족종사자·일반근로자 또는 다른 연도에 자동 적용하지 않음.",
    ))

    return rows


__all__ = ["social_insurance_rows"]
