"""소스 -> 처리 순위(P2_DESIGN §8 A/B/C) 상수. 뒤 순위는 생략이 아니라 나중에 처리한다.

family 가 아니라 source_id 로 매긴다: ``pac_standards_*`` 는 family 가 ``pac_other`` 지만 순위는 A 다.
첨부가 없는 소스(법제처 판례 ``lawgo_prec_supreme``)는 여기서 다루지 않는다(판례 순위 P-T1~T3 은 lawapi 쪽 몫).
매핑에 없는 source_id 는 조용히 버리지 않고 실행 결과의 ``unmapped_sources`` 로 보고한다.
"""
from __future__ import annotations

TIER_A = (
    "pac_adjustment_casebooks", "pac_election_whitepapers", "pac_correction_casebooks", "pac_board_correction_status",
    "pac_standards_correction_page", "pac_standards_election_page", "pac_board_committee_rules",
    "iendc_review_results", "pac_research_judgment_analysis",
)
TIER_B = (
    "pac_research_quarterly", "pac_research_seminars", "pac_research_journal", "pac_research_foreign_law",
    "pac_research_issue_reports", "pac_research_commissioned",
    "pac_other_annual_reports", "pac_other_promo_publications", "pac_other_misc_reports", "pac_other_mediation_persuasion",
)
TIER_C: tuple = ()

SOURCE_TIER = {**{s: "A" for s in TIER_A}, **{s: "B" for s in TIER_B}, **{s: "C" for s in TIER_C}}
TIER_ORDER = ("A", "B", "C")


def tier_of(source_id: str) -> str | None:
    return SOURCE_TIER.get(source_id)


def parse_tiers(spec: str | None) -> tuple:
    """'A' / 'A,B' / None(전부) -> 순위 튜플."""
    if not spec:
        return TIER_ORDER
    out = tuple(t.strip().upper() for t in spec.split(",") if t.strip())
    bad = [t for t in out if t not in TIER_ORDER]
    if bad:
        raise ValueError(f"unknown tier {bad} (A, B, C)")
    return out


def sort_key(source_id: str, att_id: str) -> tuple:
    t = SOURCE_TIER.get(source_id)
    return (TIER_ORDER.index(t) if t else len(TIER_ORDER), source_id, att_id)
