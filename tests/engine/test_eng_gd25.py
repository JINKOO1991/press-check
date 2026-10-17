"""G-D25(docs/P2_DESIGN.md §11): 연도만 아는 근거의 기준일은 그해 12월 31일(상한)이다. 합성 데이터, 네트워크 없음.

우선순위: 결정일 > 수록 간행물 발행일 > 연도(YYYY-12-31, basis=year_upper_bound) > date_unknown.
연도는 장부 year, 없으면 제목 머리의 연도, 선거백서는 선거 회차 -> 연도 대응표(상수)에서 읽는다.
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdr_engine_paths as paths  # noqa: F401,E402

from press_review import corpus, corpus_sqlite as cs, pubyear  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
WHITEPAPERS = "pac_election_whitepapers"


# ------------------------------------------------------------------ reference_date
def test_gd25_year_only_gives_dec_31_as_an_upper_bound_with_its_own_basis():
    r = corpus.reference_date(None, None, 2022)
    assert r == {"reference_date": "2022-12-31", "reference_date_basis": "year_upper_bound", "date_unknown": False}


def test_gd25_precedence_decision_then_publication_then_year_then_unknown():
    assert corpus.reference_date("2021-05-04", "2021-09-01", 2021)["reference_date_basis"] == "decision_date"
    assert corpus.reference_date(None, "2021-09-01", 2021) == {"reference_date": "2021-09-01", "reference_date_basis": "publication_date", "date_unknown": False}
    assert corpus.reference_date("garbage", None, 2020)["reference_date"] == "2020-12-31"
    assert corpus.reference_date(None, None, None) == {"reference_date": None, "reference_date_basis": None, "date_unknown": True}


@pytest.mark.parametrize("bad", [0, 1899, 2101, "2022", 2022.0, True, None, -5])
def test_gd25_only_a_plausible_integer_year_counts(bad):
    assert corpus.reference_date(None, None, bad) == {"reference_date": None, "reference_date_basis": None, "date_unknown": True}


def test_gd25_old_two_argument_calls_keep_working():
    assert corpus.reference_date("2019-03-14") == {"reference_date": "2019-03-14", "reference_date_basis": "decision_date", "date_unknown": False}
    assert corpus.reference_date(None, "2019-03-01")["reference_date_basis"] == "publication_date"


# ------------------------------------------------------------------ 연도 읽기
@pytest.mark.parametrize("title,year", [
    ("2024 시정권고 사례집", 2024),
    ("2022 시정권고 사례집", 2022),
    ("2012년도 조정중재·시정권고 사례집", 2012),
    ("  2017년도", 2017),
    ("2019년 제5차 시정권고소위원회 결과", 2019),
])
def test_gd25_leading_year_of_the_title_is_read_when_the_ledger_year_is_missing(title, year):
    assert pubyear.document_year("pac_correction_casebooks", title, None) == year


@pytest.mark.parametrize("title", ["국내언론관계판결집 제13집", "시정권고 심의기준(고정 페이지)", "2012~2013 연구", "2012-2013 합본", "제2024-12호 결정", "", None])
def test_gd25_no_year_when_the_title_has_no_leading_year_or_it_is_a_range(title):
    assert pubyear.document_year("pac_research_judgment_analysis", title, None) is None


def test_gd25_ledger_year_wins_over_the_title():
    assert pubyear.document_year("pac_adjustment_casebooks", "2012년도 조정중재·시정권고 사례집", 2013) == 2013
    assert pubyear.document_year("pac_adjustment_casebooks", "제목 없음", "2013") == 2013         # 장부 값이 문자열이어도 받는다
    assert pubyear.document_year("pac_adjustment_casebooks", "2012년도 사례집", 1700) == 2012      # 말이 안 되는 장부 값은 건너뛴다


@pytest.mark.parametrize("title,year", [
    ("16대 국회의원 선거기사심의백서", 2000),
    ("제17대 국회의원선거", 2004),
    ("제18대 국회의원선거", 2008),
    ("제19대 국회의원선거", 2012),
    ("제20대 국회의원선거", 2016),
    ("제21대 국회의원선거", 2020),
    ("제22대 국회의원선거", 2024),
    ("제17대 대통령선거", 2008),        # 12월 선거: 선거기사심의위원회 운영이 이듬해 1월 18일까지(백서 본문) -> 12월 31일이 상한이 아니다
    ("제18대 대통령선거", 2013),
    ("제19대 대통령선거", 2017),
    ("제20대 대통령선거", 2022),
    ("제21대 대통령선거", 2025),
    ("5.31 지방선거", 2006),
    ("제3회 전국동시지방선거", 2002),
    ("제5회 전국동시지방선거", 2010),
    ("제6회 전국동시지방선거", 2014),
    ("제7회 전국동시지방선거", 2018),
    ("제8회 전국동시지방선거", 2022),
    ("제9회 전국동시지방선거", 2026),
])
def test_gd25_election_whitepaper_titles_map_to_the_upper_bound_year(title, year):
    assert pubyear.document_year(WHITEPAPERS, title, None) == year


def test_gd25_unknown_election_is_unknown_not_guessed():
    assert pubyear.document_year(WHITEPAPERS, "제23대 국회의원선거", None) is None
    assert pubyear.document_year(WHITEPAPERS, "제10회 전국동시지방선거", None) is None
    assert pubyear.document_year(WHITEPAPERS, "선거기사심의백서", None) is None


def test_gd25_election_table_is_only_used_for_the_whitepaper_source():
    assert pubyear.document_year("pac_other_misc_reports", "제17대 대통령선거 보고", None) is None


def test_gd25_election_table_rows_carry_date_bound_year_and_a_check_flag():
    assert pubyear.ELECTIONS, "table must not be empty"
    for key, row in pubyear.ELECTIONS.items():
        kind, no = key
        assert kind in ("assembly", "president", "local") and isinstance(no, int)
        assert dt.date.fromisoformat(row["date"]).year in (row["bound_year"], row["bound_year"] - 1)
        assert isinstance(row["needs_check"], bool)
        assert row["needs_check"] == (not row["evidence"]), key                  # 근거가 없는 행만 '확인 필요'
    # 12월 대선은 이듬해가 상한(백서 본문의 운영기간: 2008-01-18, 2013-01-18)
    for no, y in ((16, 2003), (17, 2008), (18, 2013)):
        assert pubyear.ELECTIONS[("president", no)]["bound_year"] == y
        assert pubyear.ELECTIONS[("president", no)]["date"].startswith(str(y - 1) + "-12")
    # 4월·5월·6월 선거는 선거 연도
    for key, row in pubyear.ELECTIONS.items():
        if not row["date"].startswith(("1987-12", "1992-12", "1997-12", "2002-12", "2007-12", "2012-12")):
            assert row["bound_year"] == int(row["date"][:4]), key


# ------------------------------------------------------------------ 색인 통합
def _ledger(tmp_path):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir(parents=True)
    m = Manifest(corpus_dir / "manifest.sqlite")
    ids = {}
    try:
        for sid, fam in (("pac_correction_casebooks", "pac_cases"), ("pac_election_whitepapers", "pac_election"),
                         ("pac_research_judgment_analysis", "pac_other"), ("pac_adjustment_casebooks", "pac_cases")):
            m.ensure_source(sid, fam, "합성")

        def add(name, source, title, *, pub=None, year=None, decision=None):
            fam = "pac_election" if source == WHITEPAPERS else ("pac_other" if "research" in source else "pac_cases")
            doc = f"{fam}:{3000 + len(ids)}"
            m.upsert_document(doc, source, canonical_url=f"https://www.pac.or.kr/kor/pages/?p=207&idx={3000 + len(ids)}&m=view",
                              title=title, publication_date=pub, year=year)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:c"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": ["정정보도"], "outcome": None, "legal_reasoning": [],
                                     "institution": "pac", "case_number": None, "decision_date": None, "publication_date": None,
                                     "authority_type": "pac_settlement", "outcome_type": "settlement"}, verification_status="unverified")
            pid = f"{cid}#c1"
            m.upsert_chunk(pid, doc, f"정정보도 조정 합성 사례 {name}.", "text_range", f"{doc}#s006:0-20", case_id=cid,
                           authority_type="pac_settlement", decision_date=decision)
            ids[name] = (doc, pid)
        add("title_year", "pac_correction_casebooks", "2024 시정권고 사례집")
        add("ledger_year", "pac_adjustment_casebooks", "제목에 연도 없음", year=2012)
        add("election_dec", WHITEPAPERS, "제17대 대통령선거")
        add("election_apr", WHITEPAPERS, "제22대 국회의원선거")
        add("no_year", "pac_research_judgment_analysis", "국내언론관계판결집 제13집")
        add("pub_wins", "pac_correction_casebooks", "2020 시정권고 사례집", pub="2020-03-01")
        add("decision_wins", "pac_correction_casebooks", "2020 시정권고 사례집", pub="2020-03-01", decision="2019-11-02")
    finally:
        m.close()
    assert cs.build_index(corpus_dir / "manifest.sqlite", tmp_path / "index", now=NOW)["status"] == "built"
    return cs.open_current(tmp_path / "index"), ids


def test_gd25_index_uses_the_year_upper_bound_and_the_as_of_filter_is_conservative(tmp_path):
    idx, ids = _ledger(tmp_path)
    try:
        got = {n: (p["reference_date"], p["reference_date_basis"], p["date_unknown"]) for n, p in ((n, idx.get_passage(*v)) for n, v in ids.items())}
        assert got["title_year"] == ("2024-12-31", "year_upper_bound", False)
        assert got["ledger_year"] == ("2012-12-31", "year_upper_bound", False)
        assert got["election_dec"] == ("2008-12-31", "year_upper_bound", False)
        assert got["election_apr"] == ("2024-12-31", "year_upper_bound", False)
        assert got["no_year"] == (None, None, True)
        assert got["pub_wins"] == ("2020-03-01", "publication_date", False)
        assert got["decision_wins"] == ("2019-11-02", "decision_date", False)
        hits = lambda as_of: {h["passage_id"] for h in idx.search("정정보도 조정", as_of=as_of, limit=10)}  # noqa: E731
        assert ids["title_year"][1] not in hits("2024-06-30")          # 같은 해 기사보다 늦은 자료로 취급된다(보수적)
        assert ids["title_year"][1] in hits("2024-12-31")
        assert ids["no_year"][1] in hits("2001-01-01")                # date_unknown 은 필터에서 빠지지 않는다(G-D23)
    finally:
        idx.close()


def test_gd25_index_stats_count_year_bounded_passages_as_dated(tmp_path):
    idx, ids = _ledger(tmp_path)
    try:
        assert idx.status()["date_unknown_count"] == 1
    finally:
        idx.close()
