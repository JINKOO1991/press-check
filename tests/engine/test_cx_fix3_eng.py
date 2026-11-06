"""코드 검토 3회 지적 A1(사건번호 색인·질의 정규화)·A2(발췌 위치의 NFKC 역매핑) 시험. 합성 fixture 만 쓴다."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: F401,E402

from press_review import corpus_sqlite as cs, review  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
FAMILY = "pac_adjustment_casebooks"


def build(tmp_path, cases):
    """cases: (name, case_number, text). 구조화 사건 passage 하나씩."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        doc = "pac_adj:9001"
        m.upsert_document(doc, FAMILY, canonical_url="https://www.pac.or.kr/synthetic/9001", title="합성 사례집", year=2012)
        m.set_document_status(doc, "structured")
        for i, (name, number, text) in enumerate(cases):
            cid = f"{doc}:case{i:03d}"
            rec = {"case_id": cid, "doc_id": doc, "issue_tags": [], "legal_reasoning": [], "institution": "pac", "case_number": number,
                   "decision_date": "2012-05-01", "authority_type": "pac_decision", "outcome_type": "decision"}
            m.upsert_case(cid, doc, rec, verification_status="unverified")
            m.upsert_chunk(f"{cid}#c1", doc, text, "pdf_page", f"p{10 + i}", case_id=cid, authority_type="pac_decision", decision_date="2012-05-01")
            ids[name] = f"{cid}#c1"
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built"
    return cs.open_current(tmp_path / "index"), ids


def hits(idx, q):
    return [h["passage_id"] for h in idx.search(q, limit=5) if h["match"] == "case_number"]


def test_a1_two_digit_and_four_digit_queries_find_both_notations(tmp_path):
    idx, ids = build(tmp_path, [("short", "97도158", "합성 본문 가나다라마바사."), ("long", "1998도77", "합성 본문 아자차카타파하."), ("merged", "2018다1 및 2018다2", "합성 본문 거너더러머버서.")])
    try:
        for q in ("97도158", "1997도158"):
            assert hits(idx, q) == [ids["short"]], q
        for q in ("98도77", "1998도77"):
            assert hits(idx, q) == [ids["long"]], q
        assert hits(idx, "2018다2") == [ids["merged"]]
        assert cs.case_number_keys("97도158") == ["97도158", "1997도158"] and cs.case_number_keys("1997도158") == ["1997도158"]
        assert cs.case_numbers("99다6203") == ["99다6203"]                               # 기존 함수의 반환은 그대로다
    finally:
        idx.close()


def test_a1_rules_are_in_the_snapshot_input_and_meta(tmp_path):
    idx, _ = build(tmp_path, [("a", "97도158", "합성 본문이다.")])
    try:
        assert idx._meta["case_number_rules"] == cs.case_number_rules() and idx._meta["format"] == cs.INDEX_FORMAT
    finally:
        idx.close()
    src = Path(cs.__file__).read_text(encoding="utf-8")
    assert "case_number_rules()" in src.split("h.update(ne_stem_rules()")[1].split("by_auth")[0]


def test_a1_query_variants_cover_an_older_index_that_has_only_the_original_notation(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "case_number_keys", cs.case_numbers, raising=False)        # 원 표기만 넣던 이전 색인을 흉내 낸다
    idx, ids = build(tmp_path, [("short", "97도158", "합성 본문 가나다라마바사.")])
    try:
        assert hits(idx, "1997도158") == [ids["short"]] and hits(idx, "97도158") == [ids["short"]]
    finally:
        idx.close()


LONG_FW = "합성 문장이다. " * 200 + "ＡＢＣ 표적이다. " + "끝 문장이다. " * 200


def test_a2_excerpt_window_is_found_through_nfkc_and_stays_an_exact_substring(tmp_path):
    idx, ids = build(tmp_path, [("long", None, LONG_FW)])
    try:
        out = review.search_cases(idx, query="ABC", limit=6)
        p = {x["passage_id"]: x for x in out["passages"]}[ids["long"]]
        ex = p["excerpts"]
        for e in ex:
            assert e["text"] == LONG_FW[e["start"]:e["end"]]
        assert any("ＡＢＣ" in e["text"] for e in ex)
    finally:
        idx.close()


def test_a2_expanding_characters_keep_offsets_in_the_original_text():
    text = "ｘ" * 10 + "㈜" * 3 + "합성 " * 500 + "ＡＢＣ 표적" + "끝 " * 500        # ㈜ 는 NFKC 에서 세 글자(( 주 ))로 늘어난다
    spans = review._excerpt_spans(text, ["abc"])
    pos = text.index("ＡＢＣ")
    assert any(s <= pos < e for s, e in spans) and all(0 <= s < e <= len(text) for s, e in spans)
    assert review._excerpt_spans("합성 " * 500 + "plain 표적", ["plain"]) != [(0, 300)]       # 정규화가 없는 본문의 기존 동작
