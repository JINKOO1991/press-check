"""재게이트 뒤 검수 결함 V-consistency-02(G-D23 기준일) 시험. 합성 데이터."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdr_engine_paths as paths  # noqa: F401,E402

from press_review import corpus_sqlite as cs  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def _ledger(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source("pac_adjustment_casebooks", "pac_cases", "합성")

        def add(name, *, doc_pub, rec_pub, decision=None):
            doc = f"pac_cases:8{len(ids):03d}"
            m.upsert_document(doc, "pac_adjustment_casebooks", canonical_url=f"https://www.pac.or.kr/kor/pages/?p=207&idx=8{len(ids):03d}&m=view",
                              title=name, publication_date=doc_pub)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:c"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": ["정정보도"], "outcome": None, "legal_reasoning": [],
                                     "institution": "pac", "case_number": None, "decision_date": None, "publication_date": rec_pub,
                                     "authority_type": "pac_settlement", "outcome_type": "settlement"}, verification_status="unverified")
            pid = f"{cid}#c1"
            m.upsert_chunk(pid, doc, f"정정보도 조정 합성 사례 {name}.", "text_range", f"{doc}#s006:0-20", case_id=cid,
                           authority_type="pac_settlement", decision_date=decision)
            ids[name] = (doc, pid)
        add("both", doc_pub="2022-06-01", rec_pub="2021-10-27")          # 기사 날짜(사건 기록)는 수록 간행물 발행일보다 앞선다
        add("recordonly", doc_pub=None, rec_pub="2021-10-27")
        add("docfirst", doc_pub="2019-03-01", rec_pub=None)
    finally:
        m.close()
    assert cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)["status"] == "built"
    return cs.open_current(tmp_path / "index"), ids


def test_vcon02_reference_date_is_the_volume_publication_date_not_the_reviewed_articles_date(tmp_path):
    idx, ids = _ledger(tmp_path)
    try:
        p = idx.get_passage(*ids["both"])
        assert (p["reference_date"], p["reference_date_basis"], p["date_unknown"]) == ("2022-06-01", "publication_date", False)
        assert ids["both"][1] not in {h["passage_id"] for h in idx.search("정정보도 조정", as_of="2021-11-01", limit=10)}
        assert ids["both"][1] in {h["passage_id"] for h in idx.search("정정보도 조정", as_of="2022-06-01", limit=10)}
    finally:
        idx.close()


def test_vcon02_a_case_record_publication_date_alone_is_not_a_reference_date(tmp_path):
    idx, ids = _ledger(tmp_path)
    try:
        p = idx.get_passage(*ids["recordonly"])
        assert (p["reference_date"], p["reference_date_basis"], p["date_unknown"]) == (None, None, True)
        assert (idx.get_passage(*ids["docfirst"])["reference_date"]) == "2019-03-01"
    finally:
        idx.close()
