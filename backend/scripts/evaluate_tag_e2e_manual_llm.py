"""Source-only TAG end-to-end regression with a frozen manual LLM adapter.

The online model is intentionally not measured here.  A reviewer supplies only
values visible in the target bill articles; deterministic extraction, entity
grouping, rule calculation, precedent routing, and time-series expansion remain
the production code paths.  Answer PDFs are not opened by this script.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import re
from unittest.mock import patch

from backend.article_extraction_engine import extract_pdf_text, split_articles_regex
from backend.tag_parsers import (
    CapitalExpenditureVars,
    CommitteeVars,
    PersonnelVars,
    TransferPaymentVars,
)
from backend.tag_pipeline import run_pipeline


ROOT = Path(__file__).resolve().parents[2]

# is_assembly_internal_committee()는 이 파이프라인에서 몇 안 되게 진짜
# 프로덕션 LLM(solar-pro3)을 직접 호출하는 함수다 — 실측(2211757, 윤리특별
# 위원회) 결과 같은 조문·같은 코드를 3번 돌렸는데 판정이 True/False/False로
# 흔들렸다(범위가 5.4억~7.2억 vs 400만~1200만원으로 80배 이상 차이). 이
# 하네스에선 LLM 호출 대신 Claude가 직접 조문을 읽고 판단한 결과를 고정값으로
# 둔다 — "국회법 개정으로 국회 자체 위원회(위원=국회의원)를 신설/상설화"하는
# 의안만 True.
ASSEMBLY_INTERNAL_OVERRIDE = {
    "2126636": True,  # 헌법특별위원회(국회법 개정)
    "2211757": True,  # 윤리특별위원회(국회법 개정, 상설화)
    "2215199": True,  # 미래전략특별위원회(국회 자체, "본회의에서 선거")
}


CASES = {
    "2216065": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2216065/2216065_의사국 의안과_의안원문.pdf",
        "committees": {
            "보호심의위원회": {"total_members": 7, "ex_officio": None, "meetings": None},
        },
    },
    "2217718": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2217718/2217718_의사국 의안과_의안원문.pdf",
        "committees": {
            "국가인공지능데이터센터진흥전문위원회": {
                "total_members": 20, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2126145": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2126145/2126145_의사국 의안과_의안원문.pdf",
        "committees": {
            "지역금융위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
        },
    },
    "2126635": {
        "source": "backend/generated/committee_v5_fresh5_f/source/2126635_의안원문.pdf",
        "committees": {
            "자문위원회": {"total_members": 30, "ex_officio": 0, "meetings": None},
        },
    },
    "2126661": {
        "source": "backend/generated/committee_v5_fresh5_f/source/2126661_의안원문.pdf",
        "committees": {
            "10ㆍ29이태원참사진상규명과재발방지를위한특별조사위원회": {
                # 안 제17조: "직원의 정원은 60명 이내에서...대통령령으로 정한다"
                # (전에는 "조사위원회"라는 안 쓰이는 별칭 키에 들어있어서 실제
                # 매칭 엔티티에 반영이 안 됐었다 — 이번에 실제 키로 옮김)
                "total_members": 9, "ex_officio": 0, "meetings": None,
                "standing_member_headcount": 3, "staff_headcount": 60,
            },
            "징계위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "10ㆍ29이태원참사피해구제심의위원회": {
                "total_members": 9, "ex_officio": None, "meetings": None,
            },
            "10ㆍ29이태원참사희생자추모위원회": {
                "total_members": 9, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2126659": {
        "source": "backend/generated/committee_v5_holdout5_d/source/2126659_의안원문.pdf",
        "committees": {
            "가족돌봄아동ㆍ청소년ㆍ청년정책심의위원회": {
                "total_members": 15, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2126679": {
        "source": "backend/generated/committee_v5_holdout5_d/source/2126679_의안원문.pdf",
        "committees": {
            "돌봄근로자처우개선위원회": {"total_members": 20, "ex_officio": None, "meetings": 4},
            "분과위원회": {"total_members": 5, "ex_officio": None, "meetings": None},
            "시ㆍ도위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
            "시ㆍ도돌봄근로자처우개선위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
            "시ㆍ군ㆍ구돌봄근로자처우개선위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
            "지역위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
        },
    },
    "2214559": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2214559/2214559_의사국 의안과_의안원문.pdf",
        "committees": {
            "친환경농어업발전위원회": {"total_members": 25, "ex_officio": 1, "meetings": None},
        },
    },
    "2213188": {
        "source": "backend/generated/committee_bills_fresh_22/files/2213188/2213188_의사국 의안과_의안원문.pdf",
        "committees": {
            "퇴직연금기금운용위원회": {"total_members": 12, "ex_officio": 4, "meetings": 4},
        },
    },
    "2213116": {
        "source": "backend/generated/personnel_bills_10/files/2213116/2213116_의사국 의안과_의안원문.pdf",
        "committees": {
            "전담재판부후보추천위원회": {
                "total_members": 9,
                "ex_officio": 0,
                "meetings": None,
                "temporal_mode": "event_driven",
                "finite_event_count": 2,
                "temporal_evidence_quotes": [
                    "제17조제5항제1호및제2호에따른후보자추천위원회는이법시행후2주이내에",
                    "제17조제5항제3호에따른후보자추천위원회는제1심판결선고일부터2주이내에",
                ],
            },
        },
    },
    "2211774": {
        "source": "backend/generated/committee_bills_fresh_22/files/2211774/2211774_의사국 의안과_의안원문.pdf",
        "committees": {
            "법관평가위원회": {
                "total_members": 15,
                # 원문은 외부추천 10명·법원내부 5명이라고만 하고
                # '당연직' 수를 명시하지 않으므로 임의로 5를 넣지 않는다.
                "ex_officio": None,
                "meetings": None,
                "temporal_mode": "unknown",
                "finite_event_count": None,
                "temporal_evidence_quotes": None,
                "total_members_min": 15,
                "total_members_max": 15,
                "member_groups": [
                    {"label": "국회교섭단체 추천", "count": 5, "allocation": "fixed", "kind": "private_external", "evidence_quote": "국회교섭단체가의석수비율에따라추천하는사람5명"},
                    {"label": "법률가단체 추천", "count": 5, "allocation": "fixed", "kind": "private_external", "evidence_quote": "법률가단체가추천하는사람5명"},
                    {"label": "법원 내부 구성원", "count": 5, "allocation": "fixed", "kind": "internal_public_organization", "evidence_quote": "법원내부구성원5명"},
                ],
            },
        },
    },
    "2213574": {
        "source": "backend/generated/personnel_bills_10/files/2213574/2213574_의사국 의안과_의안원문.pdf",
        "committees": {
            "공무직위원회": {
                # 제4조제1항: "위원장 1명을 포함하여 30명 이내의 위원으로 구성한다"
                "total_members": 30,
                # 제4조제2항제1호: 기획재정부장관·교육부장관·행정안전부장관·
                # 고용노동부장관·국무조정실장·인사혁신처장 6명(숫자로 명시된 당연직)
                "ex_officio": 6,
                # 회의 개최 주기는 조문에 명시 안 됨
                "meetings": None,
            },
            # 발전협의회·분야별협의회: 위원 자격요건(노동조합 추천/전문가/공무원)만
            # 나열하고 정원 숫자는 조문에 없음.
            "공무직발전협의회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "지방공공기관등공무직근로자등이종사하는부문에따라분야별협의회": {
                "total_members": 0, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2216353": {
        "source": "backend/generated/committee_v5_holdout5_b/source/2216353_의안원문.pdf",
        "committees": {
            "국가회계위원회": {
                # 위원장 1명 포함 25명 이상 30명 이하(구간) — 상한 30으로 취급
                "total_members": 30,
                "ex_officio": None,
                "meetings": None,
                "total_members_min": 25,
                "total_members_max": 30,
            },
            # 실무위원회: "위원회에 실무위원회를 둔다"만 있고 정원 숫자는 없음
            "에실무위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2212322": {
        "source": "backend/generated/committee_v4_holdout5/source/2212322_의안원문.pdf",
        "committees": {
            "지원협의회": {
                # 정원 관련 정보가 조문에 없음(협의회 구성만 언급, 인원수 미명시)
                "total_members": 0,
                "ex_officio": None,
                "meetings": None,
            },
        },
    },
    "2126685": {
        "source": "backend/generated/committee_v5_holdout5_d/source/2126685_의안원문.pdf",
        "committees": {
            # "위원장 1명을 포함한 3명의 상임위원과 12명 이내의 비상임위원으로 구성"
            "공공기관운영위원회": {
                "total_members": 15, "ex_officio": None, "meetings": None,
                "standing_member_headcount": 3,
                "total_members_min": 3, "total_members_max": 15,
            },
            # 나머지 4개 위원회/소위원회는 정원 숫자가 조문에 없음(대통령령 위임 등)
            "공공기관노정위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "기금운용심의회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "인사검증소위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "임원추천위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2215457": {
        "source": "backend/generated/personnel_bills_10/files/2215457/2215457_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원장을 포함하여 9명의 위원으로 구성하며 위원장 및 위원 1명은 상임으로 함"
            "공익위원회": {
                "total_members": 9, "ex_officio": None, "meetings": None,
                "standing_member_headcount": 2,
            },
        },
    },
    "2213320": {
        "source": "backend/generated/committee_v5_all35/source/2213320_의안원문.pdf",
        "committees": {
            # 제7조: "위원장 1명을 포함한 20명 이내의 위원으로 구성" —
            # 위원은 차관급 공무원(수 미상, 명단만 나열)+지자체장+전문가
            "국가인공지능데이터센터진흥위원회": {
                "total_members": 20, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2212282": {
        "source": "backend/generated/committee_v5_holdout5_c/source/2212282_의안원문.pdf",
        "committees": {
            # 제8조: "위원장 및 부위원장 각 1명을 포함하여...21명 이내의
            # 위원으로 구성"(노조위원 7명이내+정부위원 7명이내+전문가위원
            # 7명이내, 전부 위촉직 — 숫자로 명시된 당연직 없음)
            "공무원임금위원회": {"total_members": 21, "ex_officio": None, "meetings": None},
        },
    },
    "2212092": {
        "source": "backend/generated/committee_v5_holdout5_b/source/2212092_의안원문.pdf",
        "committees": {
            # 제9조: "위원장2명과 부위원장2명, 간사위원1명을 포함하여
            # 40명이내의 위원으로 구성"
            "시민사회위원회": {"total_members": 40, "ex_officio": None, "meetings": None},
            # 제10조: "구성 및 운영에 필요한 사항은...조례로 정한다" — 정원 미상
            "시ㆍ도시민사회위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2216417": {
        "source": "backend/generated/committee_v5_all35/source/2216417_의안원문.pdf",
        "committees": {
            # "추천위원장 1명을 포함하여 7명의 추천위원으로 구성" — 이번
            # 마라톤 #46에서 _SIZE_EXACT 정규식 버그를 발견·수정한 그 조문
            "위원후보추천위원회": {"total_members": 7, "ex_officio": None, "meetings": None},
            "위원회에위원후보추천위원회": {"total_members": 7, "ex_officio": None, "meetings": None},
        },
    },
    "2216503": {
        "source": "backend/generated/committee_v5_all35/source/2216503_의안원문.pdf",
        "committees": {
            # "추천위원회위원장 1명을 포함한 10명의 추천위원회위원으로
            # 구성" — 2216417과 경쟁하는 유사 법안(다른 발의자)
            "위원후보추천위원회": {"total_members": 10, "ex_officio": None, "meetings": None},
            "위각선출ㆍ지명기관에인권위원후보추천위원회": {
                "total_members": 10, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2211745": {
        "source": "backend/generated/committee_v5_holdout5_b/source/2211745_의안원문.pdf",
        "committees": {},  # group_committee_articles가 엔티티를 찾지 못함(신설 vs 증분 게이트 케이스)
    },
    "2211944": {
        "source": "backend/generated/committee_v4_holdout5/source/2211944_의안원문.pdf",
        "committees": {},  # 위와 동일 — 엔티티 미검출
    },
    "2215935": {
        "source": "backend/generated/committee_v5_regression5_e/source/2215935_의사국 의안과_의안원문.pdf",
        "committees": {},  # 위와 동일 — 엔티티 미검출
    },
    "2124118": {
        "source": "backend/generated/committee_v5_holdout5_c/source/2124118_의안원문.pdf",
        "committees": {
            # 제22조: "위원장1명, 상임위원1명을 포함한 9명 이내의 위원으로
            # 구성" — 사무기구를 둔다는 조문은 있으나 인원 미상
            "공익법인위원회": {
                "total_members": 9, "ex_officio": None, "meetings": None,
                "standing_member_headcount": 1,
            },
        },
    },
    "2124970": {
        "source": "backend/generated/committee_v5_regression5_e/source/2124970_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원장1명을 포함하여 25명이상 30명이하의 위원으로 구성"
            "대전특별자치시지원위원회": {
                "total_members": 30, "ex_officio": None, "meetings": None,
                "total_members_min": 25, "total_members_max": 30,
            },
            "실무위원회": {"total_members": 0, "ex_officio": None, "meetings": None},  # 대통령령 위임, 정원 미상
            # "감사위원장1명을 포함한 7명이내의 위원으로 구성"
            "감사위원회": {"total_members": 7, "ex_officio": None, "meetings": None},
            "연구개발위원회": {"total_members": 0, "ex_officio": None, "meetings": None},  # 시조례 위임
            "외국교육기관설립운영심의위원회": {"total_members": 0, "ex_officio": None, "meetings": None},  # 시조례 위임
        },
    },
    "2124966": {
        "source": "backend/generated/committee_v5_holdout5_d/source/2124966_의안원문.pdf",
        "committees": {
            # "위원장1명과 부위원장2명을 포함한 20명 이내의 위원으로 구성" —
            # 당연직/위촉위원으로 구분한다고만 하고 당연직 수는 안 나옴
            # (부교육감 1명만 당연직으로 명시, 전체 당연직 수는 도조례 위임)
            "글로벌생명경제도시종합계획심의회": {
                "total_members": 20, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2125736": {
        "source": "backend/generated/committee_v5_fresh5_f/source/2125736_의안원문.pdf",
        "committees": {
            # "위원장 및 부위원장 각1명을 포함하여...위원 27명으로 구성"
            # (공무원대표9+국가지자체대표9+공익대표9, 위원장/부위원장은 공익대표 중 겸임)
            "공무원보수위원회": {"total_members": 27, "ex_officio": None, "meetings": None},
        },
    },
    "2126334": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2126334/2126334_의사국 의안과_의안원문.pdf",
        "committees": {
            # "공동위원장 2인을 포함한 25인 이내의 위원으로 구성"
            "국립대학병원발전위원회": {"total_members": 25, "ex_officio": None, "meetings": None},
        },
    },
    "2126636": {
        "source": "backend/generated/committee_v5_fresh5_f/source/2126636_의안원문.pdf",
        "committees": {
            # "헌법특별위원회의 위원수는 30명으로 한다" — 국회법 개정(제40조·
            # 제41조·제44조·제48조 준용), 국회 자체 특별위원회로 보임
            "헌법특별위원회": {"total_members": 30, "ex_officio": None, "meetings": None},
        },
    },
    "2211757": {
        "source": "backend/generated/committee_bills_fresh_22/files/2211757/2211757_의사국 의안과_의안원문.pdf",
        "committees": {
            # "윤리특별위원회는 위원장1명을 포함한 15명의 위원으로 구성한다"
            # — 국회법 개정(기존 "제44조제1항에따라 구성"에서 "둔다"로 상설화)
            "제44조제1항에따라윤리특별위원회": {"total_members": 15, "ex_officio": None, "meetings": None},
            "윤리특별위원회": {"total_members": 15, "ex_officio": None, "meetings": None},
        },
    },
    "2212433": {
        "source": "backend/generated/committee_v5_fresh5_f/source/2212433_의안원문.pdf",
        "committees": {
            # 2216417/2216503과 동일 패턴("선출ㆍ지명할 때마다") — 국가인권위
            # 위원후보추천위원회, "추천위원회위원장1명을 포함한 10명"
            "위원후보추천위원회": {"total_members": 10, "ex_officio": None, "meetings": None},
            "위원회에위원후보추천위원회": {"total_members": 10, "ex_officio": None, "meetings": None},
        },
    },
    "2212535": {
        "source": "backend/generated/personnel_bills_10/files/2212535/2212535_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원회는 상임위원3명을 포함한 11명의 위원으로 구성" —
            # "위원장 및 상임위원은 정무직으로 보함", 한시조직(조사개시 후
            # 1년 이내 활동 완료)
            "반헌법행위조사특별위원회": {
                "total_members": 11, "ex_officio": None, "meetings": None,
                "standing_member_headcount": 3,
            },
            "징계위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2212678": {
        "source": "backend/generated/committee_bills_fresh_22/files/2212678/2212678_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원장을 포함한 28명 이내의 위원" — 전부 위촉 전문가, 당연직 없음
            "기후과학위원회": {"total_members": 28, "ex_officio": None, "meetings": None},
        },
    },
    "2212912": {
        "source": "backend/generated/standard_value_verify/files/2212912/2212912_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원장1명과 부위원장1명을 포함하여 30명이내의 위원으로 구성"
            "농어촌기본소득위원회": {"total_members": 30, "ex_officio": None, "meetings": None},
            "위원회에농어촌기본소득정책조정실무위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "농어촌기본소득지급대상자심의위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2213848": {
        "source": "backend/generated/subsidy_bills_diverse10/files/2213848/2213848_의사국 의안과_의안원문.pdf",
        "committees": {
            # "위원장1명을 포함한 30명 이내의 위원으로 구성"
            "재생에너지자립단지위원회": {"total_members": 30, "ex_officio": None, "meetings": None},
            "재생에너지자립단지실무위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2213873": {
        "source": "backend/generated/committee_v5_holdout5_b/source/2213873_의안원문.pdf",
        "committees": {
            "과학기술국제협력전략위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
            "연구안보특별위원회": {"total_members": 15, "ex_officio": None, "meetings": None},
        },
    },
    "2213937": {
        "source": "backend/generated/personnel_bills_10/files/2213937/2213937_의사국 의안과_의안원문.pdf",
        "committees": {
            "광주전남특별지방자치단체지원협의회": {"total_members": 30, "ex_officio": None, "meetings": None},
            "광주전남특별지방자치단체설치준비위원회": {"total_members": 30, "ex_officio": None, "meetings": None},
            "이법이공포된날부터1개월이내에광주전남특별지방자치단체추진위원회": {
                "total_members": 30, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2214537": {
        "source": "backend/generated/subsidy_bills_fresh_22_nonchild/files/2214537/2214537_의사국 의안과_의안원문.pdf",
        "committees": {
            "필수어업용기자재심의위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2215078": {
        "source": "backend/generated/committee_v4_holdout5/source/2215078_의안원문.pdf",
        "committees": {
            "화재조사심의위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2215198": {
        "source": "backend/generated/committee_v4_holdout5/source/2215198_의안원문.pdf",
        "committees": {
            "국가미래위원회": {"total_members": 20, "ex_officio": None, "meetings": None},
        },
    },
    "2215199": {
        "source": "backend/generated/committee_v5_holdout5_b/source/2215199_의안원문.pdf",
        "committees": {
            # 국회 자체 특위: "위원수는 15명으로 한다"
            "미래전략특별위원회": {"total_members": 15, "ex_officio": None, "meetings": None},
        },
    },
    "2215954": {
        "source": "backend/generated/personnel_bills_10/files/2215954/2215954_의사국 의안과_의안원문.pdf",
        "committees": {
            "교원인사위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
            "학사위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2126167": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2126167/2126167_의사국 의안과_의안원문.pdf",
        "committees": {
            # "설립준비위원장을 포함하여 3명 이내의 설립준비위원으로 구성"
            "교육부장관또는교육감은학교복합시설지원센터를설립할경우설립에관한사무를관장하도록설립준비위원회": {
                "total_members": 3, "ex_officio": None, "meetings": None,
            },
        },
    },
    "2126638": {
        "source": "backend/generated/assembly_rag_seed_age21_50/files/2126638/bill_text_의안원문.pdf",
        "committees": {
            # "위원장1명을 포함하여 45명 이내로 구성"
            "국가교육과정전문위원회": {"total_members": 45, "ex_officio": None, "meetings": None},
            # "구성과 운영에 필요한 사항은 대통령령으로 정한다" — 정원 미상
            "국가교육과정현장검토위원회": {"total_members": 0, "ex_officio": None, "meetings": None},
        },
    },
    "2214561": {
        "source": "backend/generated/subsidy_bills_diverse10/files/2214561/2214561_의사국 의안과_의안원문.pdf",
        "committees": {
            "탈석탄위원회": {
                # 조문은 30명 이상 40명 이하라고 명시한다. 현재
                # CommitteeVars는 구간이 아닌 단일 정수만 받으므로 상한을 넘긴다.
                "total_members": 40,
                # 3개 그룹을 동수로 구성하지만 30~40 중 실제 정원이
                # 확정되지 않아 공무원 정확인원도 확정할 수 없다.
                "ex_officio": None,
                "meetings": None,
                # 6개월 내 목표연도 설정 외에 계획·보상·이행감독 업무가
                # 계속되므로 유한 사건 위원회로 단정하지 않는다.
                "temporal_mode": "unknown",
                "finite_event_count": None,
                "temporal_evidence_quotes": None,
                "total_members_min": 30,
                "total_members_max": 40,
                "member_groups": [
                    {"label": "공무원", "count": None, "allocation": "equal_share", "kind": "government_official", "evidence_quote": "기획예산처장관,재정경제부장관,기후에너지환경부장관,산업통상부장관,고용노동부장관,석탄화력발전소폐쇄지역의광역및기초지방자치단체의장등대통령령으로정하는공무원"},
                    {"label": "이해관계자", "count": None, "allocation": "equal_share", "kind": "private_external", "evidence_quote": "석탄화력발전산업종사노동자대표,석탄화력발전소폐쇄지역주민대표,석탄화력발전사업자대표,전국적규모의노동단체추천을받은사람"},
                    {"label": "전문가", "count": None, "allocation": "equal_share", "kind": "private_external", "evidence_quote": "기후과학,온실가스감축,재생에너지,탈석탄,정의로운전환등의관련분야에학식이나경험이풍부한사람으로서대통령이위촉하는사람"},
                ],
            },
        },
    },
}

_TABLE_MARKER = re.compile(r"신\s*[ㆍ·]?\s*구\s*조\s*문\s*대\s*비\s*표")


def _source_articles(text: str) -> tuple[list[dict], str]:
    """Manual-LLM substitute: keep the proposal body and remove comparison tables."""
    marker = _TABLE_MARKER.search(text)
    if marker:
        text = text[: marker.start()]
    return split_articles_regex(text), "manual_source_only"


def _lookup_committee(case: dict, article_text: str) -> CommitteeVars:
    compact = re.sub(r"[^0-9A-Za-z가-힣]", "", article_text)
    matches = []
    for name, values in case["committees"].items():
        key = re.sub(r"[^0-9A-Za-z가-힣]", "", name)
        if key and key in compact:
            matches.append((len(key), name, values))
    if not matches:
        raise ValueError("수동 LLM fixture에 없는 위원회 개체")
    _, name, values = max(matches)
    return CommitteeVars(name=name, extraction_mismatches=None, **values)


def _blocked_personnel(_: str) -> PersonnelVars:
    return PersonnelVars(target_grade="", headcount=0)


def _blocked_transfer(_: str) -> TransferPaymentVars:
    return TransferPaymentVars(target_demographic="조문상 대상", subsidy_amount_per_person=None, payment_cycle="년")


def _blocked_capital(article_text: str) -> CapitalExpenditureVars:
    return CapitalExpenditureVars(system_type="조문상 시설·시스템", scale="중")


def evaluate_case(bill_no: str, case: dict, *, use_precedent: bool) -> dict:
    source = ROOT / case["source"]
    with ExitStack() as stack:
        stack.enter_context(patch("backend.tag_pipeline.split_articles", side_effect=_source_articles))
        stack.enter_context(
            patch(
                "backend.tag_pipeline.extract_committee_vars",
                side_effect=lambda text: _lookup_committee(case, text),
            )
        )
        stack.enter_context(patch("backend.tag_pipeline.extract_personnel_vars", side_effect=_blocked_personnel))
        stack.enter_context(patch("backend.tag_pipeline.extract_transfer_payment_vars", side_effect=_blocked_transfer))
        stack.enter_context(patch("backend.tag_pipeline.extract_capital_expenditure_vars", side_effect=_blocked_capital))
        stack.enter_context(patch("backend.tag_rule_engine.extract_activity_duration_months_llm", return_value=None))
        stack.enter_context(
            patch(
                "backend.tag_pipeline.is_assembly_internal_committee",
                side_effect=lambda text: ASSEMBLY_INTERNAL_OVERRIDE.get(bill_no, False),
            )
        )
        result = run_pipeline(
            source.read_bytes(), filename=source.name,
            use_precedent_fallback=use_precedent,
        )
    committee_items = [row for row in result["items"] if row.get("entity_name")]
    return {
        "bill_no": bill_no,
        "source_pdf": case["source"],
        "evaluation_scope": "post_llm_tag_pipeline",
        "committee_count": result["committee_count"],
        "committees": [
            {
                "name": row.get("entity_name"),
                "articles": row.get("article_no"),
                "status": row["calc_result"].get("status"),
                "annual_cost_won": row["calc_result"].get("annual_cost_won"),
                "annual_cost_won_range": row["calc_result"].get("annual_cost_won_range"),
                "year_amounts": row.get("year_amounts"),
                "year_amounts_range": row.get("year_amounts_range"),
                "formula": row["calc_result"].get("trace"),
                "evidence_route": row["calc_result"].get("evidence_route"),
                "reason": row["calc_result"].get("reason"),
            }
            for row in committee_items
        ],
        "committee_total_won": sum(sum(row.get("year_amounts") or []) for row in committee_items),
        "committee_unallocated_total_won": sum(
            int((row.get("calc_result") or {}).get("finite_event_total_cost_won") or 0)
            for row in committee_items
            if (row.get("calc_result") or {}).get("event_year_allocation_unresolved")
        ),
        "pipeline_grand_total_won": result["aggregated"]["grand_total_won"],
        "pipeline_grand_total_including_unallocated_won": result["aggregated"].get(
            "grand_total_including_unallocated_won"
        ),
        "pipeline_grand_total_won_range": result["aggregated"].get(
            "grand_total_won_range"
        ),
        "evidence_status": (result.get("evidence_workflow") or {}).get("status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-precedent", action="store_true")
    parser.add_argument("--bill", action="append", choices=sorted(CASES))
    args = parser.parse_args()
    bills = args.bill or list(CASES)
    payload = {
        "schema_version": "tag-e2e-manual-llm-v1",
        "answer_usage": "none",
        "online_llm_measured": False,
        "cases": [evaluate_case(bill, CASES[bill], use_precedent=not args.no_precedent) for bill in bills],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in payload["cases"]:
        print(row["bill_no"], "entities=", row["committee_count"], "committee_total=", row["committee_total_won"])
        for committee in row["committees"]:
            print(" ", committee["name"], committee["status"], committee["annual_cost_won"], committee["annual_cost_won_range"])


if __name__ == "__main__":
    main()
