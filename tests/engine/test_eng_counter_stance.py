"""stance=counter: 청구가 배척된 결과를 우선하되 어느 쪽이 졌는지 모르는 처분 표지를 신호로 쓰지 않고, 후보 상한 앞에서 잘리지 않는다."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: F401,E402

from press_review import corpus_sqlite as cs  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def build(tmp_path, rows, monkeypatch=None, fts=True):
    """rows: (name, authority_type, outcome_type, outcome_text, text). 판결은 supreme_court, 그 밖은 pac."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source("lawgo_prec_supreme", "law_go_kr", "합성")
        ids = {}
        for i, (name, auth, otype, otext, text) in enumerate(rows):
            doc = f"lawgo_prec:80{i:04d}"
            m.upsert_document(doc, "lawgo_prec_supreme", canonical_url=f"https://www.law.go.kr/LSW/precInfoP.do?precSeq=80{i:04d}", title=name)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:2020다{i + 1:05d}"
            rec = {"case_id": cid, "doc_id": doc, "issue_tags": [], "legal_reasoning": [], "institution": "supreme_court" if auth == "judgment" else "pac",
                   "case_number": f"2020다{i + 1:05d}", "decision_date": "2021-01-01", "authority_type": auth, "outcome_type": otype,
                   "outcome": {"text": otext, "speaker": "court_majority"} if otext else None}
            m.upsert_case(cid, doc, rec, verification_status="unverified")
            pid = f"{doc}:2020다{i + 1:05d}#c1"
            m.upsert_chunk(pid, doc, text, "text_range", f"{doc}#s006:0-90", case_id=cid, authority_type=auth, decision_date="2021-01-01")
            ids[name] = pid
    finally:
        m.close()
    if monkeypatch is not None and not fts:
        monkeypatch.setattr(cs, "fts_trigram_available", lambda: False)
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built"
    return cs.open_current(tmp_path / "index"), ids


# ---- V-COUNTER-1
def test_counter_signal_requires_the_claimant_side_to_lose():
    f = cs.counter_signals
    assert f("judgment", [], "피고의 상고를 기각한다") == []                       # 진 쪽이 피고(언론사 쪽)다
    assert f("judgment", [], "원고의 나머지 청구를 기각한다") == []                 # 일부 승소는 배척이 아니다
    assert f("judgment", [], "상고를 모두 기각한다") == []                          # 어느 쪽이 졌는지 문장만으로 알 수 없다
    assert f("judgment", [], "원고의 청구를 기각한다")                              # 청구 배척
    assert f("judgment", [], "원고의 상고를 기각한다")
    assert f("judgment", [], "원고 패소")


def test_counter_does_not_rank_press_losses_or_unmarked_judgments_first(tmp_path):
    rows = [("p01", "judgment", "judgment", "피고의 상고를 기각한다", "정정보도 청구 사건에서 피고가 상고하였다."),
            ("p02", "judgment", "judgment", "원고의 청구를 기각한다", "정정보도 청구 사건에서 법원이 판단하였다."),
            ("p03", "judgment", "judgment", "원고 승소", "정정보도 청구 사건에서 법원이 판단하였다.")]
    idx, ids = build(tmp_path, rows)
    try:
        got = [h["passage_id"] for h in idx.search("정정보도 청구", stance="counter")]
        assert got[0] == ids["p02"], got
    finally:
        idx.close()


def test_counter_does_not_put_unmarked_judgment_before_decision(tmp_path):
    rows = [("p01", "pac_decision", "decision", "정정보도를 결정한다", "정정보도 청구 정정보도 청구 정정보도 청구 사건이다."),
            ("p02", "judgment", "judgment", "원고 승소", "정정보도 청구 사건에서 법원이 판단하였다.")]
    idx, ids = build(tmp_path, rows)
    try:
        both = [h["passage_id"] for h in idx.search("정정보도 청구", stance="both")]
        counter = [h["passage_id"] for h in idx.search("정정보도 청구", stance="counter")]
        assert counter == both, (counter, both)
    finally:
        idx.close()


# ---- V-COUNTER-2
def _heavy_rows():
    rows = [(f"p{i:02d}", "judgment", "judgment", "원고 승소", "보도 " * 30 + "정정보도 보도 보도") for i in range(1, 7)]
    rows.append(("p07", "judgment", "judgment", "원고의 청구를 기각한다", "정정보도 사건. 보도"))
    return rows


def test_counter_two_char_query_is_not_cut_by_bigram_cap(tmp_path):
    idx, ids = build(tmp_path, _heavy_rows())
    try:
        got = [h["passage_id"] for h in idx.search("보도", stance="counter", limit=5)]
        assert ids["p07"] in got, got
        assert got[0] == ids["p07"]
    finally:
        idx.close()


def test_counter_bigram_only_index_is_not_cut_by_bigram_cap(tmp_path, monkeypatch):
    idx, ids = build(tmp_path, _heavy_rows(), monkeypatch, fts=False)
    try:
        assert idx.status()["search_mode"] == "bigram_only"
        got = [h["passage_id"] for h in idx.search("정정보도", stance="counter", limit=5)]
        assert got[0] == ids["p07"], got
    finally:
        idx.close()
