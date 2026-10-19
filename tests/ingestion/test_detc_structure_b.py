"""U3b: 병합 번호가 색인에 닿는 경로(case_number 쉼표 연결)와 인쇄 표기 처분 가드. 합성 fixture, 네트워크 없음."""
import datetime as dt
import json

import pytest

import pdr_ing_paths  # noqa: F401
from press_review import corpus_sqlite as cs
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from test_detc_structure import LEDGER, TXT, assemble, base_case, draft, full_text, reply, row, run_structurer, seed_detc

NOW = dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
MERGED_PARTY = "\n2020헌마1\n【주"


def merged_text():
    return full_text().replace(MERGED_PARTY, "\n2020헌마1\n2020헌마2, 2020헌마3 (병합)\n【주", 1)


# ------------------------------------------------------------------ 1. 병합 번호
def test_merged_case_number_is_comma_joined_case_id_unchanged_and_index_finds_each_token(manifest, corpus, tmp_path):
    doc_id = seed_detc(manifest, corpus, full=merged_text())
    led = S.ledger_values(manifest, doc_id, detc=True)
    assert led["case_number"] == "2020헌마1, 2020헌마2, 2020헌마3" and led["case_id_number"] == "2020헌마1"
    run_structurer(manifest, corpus, reply(base_case(manifest, doc_id, case_number="2020헌마9")))
    case = manifest.get_case("lawgo_detc:1:2020헌마1")                       # case_id 는 대표 번호 compact 그대로
    assert case["record"]["case_number"] == "2020헌마1, 2020헌마2, 2020헌마3" and case["record"]["verification_status"] == "unverified"
    assert cs.case_numbers(case["record"]["case_number"]) == ["2020헌마1", "2020헌마2", "2020헌마3"]
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built" and res["passages"] >= 1
    idx = cs.open_current(tmp_path / "index")
    try:
        want = {h["passage_id"] for h in idx.search("2020헌마1", limit=5)}
        assert want and want == {h["passage_id"] for h in idx.search("2020헌마2", limit=5)} == {h["passage_id"] for h in idx.search("2020헌마3", limit=5)}
    finally:
        idx.close()


def test_single_number_document_and_precedent_ledger_unchanged(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    assert S.ledger_values(manifest, doc_id, detc=True)["case_number"] == "2020헌마1"
    manifest.ensure_source("lawgo_prec_supreme", "law_go_kr", "판례", "https://www.law.go.kr/x")
    manifest.upsert_document("lawgo_prec:7", "lawgo_prec_supreme", title="p", decision_date="2020-01-02")
    manifest.update_document_fields("lawgo_prec:7", extra_merge={"body": {"body_case_no": "2020다1", "case_nos": ["2020다1", "2020다2"]}})
    assert S.ledger_values(manifest, "lawgo_prec:7")["case_number"] == "2020다1"      # 판례는 인쇄 표기 그대로


# ------------------------------------------------------------------ 2. 인쇄 표기 처분 매핑
@pytest.mark.parametrize("text,want", [
    ("각 헌법에 위반된다", {"위헌"}), ("헌법에 합치하지 아니한다", {"헌법불합치"}), ("헌법에 합치되지 아니한다", {"헌법불합치"}),
    ("모두 기각한다", {"기각"}), ("모두 각하한다", {"각하"}), ("헌법에 위반되지 아니한다", {"합헌"}),
    ("헌법에 위반된다, 각하", {"위헌", "각하"}), ("위헌 각하", {"위헌", "각하"}), ("합헌결정", {"합헌"}),
    ("헌법에 위반된다고 할 수 없다", {"위헌불선언"}),
])
def test_map_decision_type_printed_phrases(text, want):
    assert sr.map_decision_type(text) == (want, [])


def test_map_decision_type_unmappable_pieces_go_to_rest():
    assert sr.map_decision_type("기타 결정") == (set(), ["기타", "결정"])
    assert sr.map_decision_type("일부 인용") == ({"인용"}, ["일부"])


@pytest.mark.parametrize("decision_type,dispositions,quarantined", [
    ("헌법에 위반되지 아니한다", ["위헌"], True),       # 인쇄 표기가 장부와 모순
    ("헌법에 위반된다", ["위헌"], False),
    ("모두 기각한다", ["각하"], True),
    ("헌법에 합치하지 아니한다", ["헌법불합치"], False),
    ("기타 결정", ["위헌"], False),                      # 대응 불가는 note 만
    ("헌법에 위반되지 아니한다", ["unknown"], False),     # 전부 unknown 이면 생략
])
def test_disposition_guard_with_printed_phrases(decision_type, dispositions, quarantined):
    rows = {"p": row("reasons", "majority"), "p_dis": row("opinion", "dissent"), "p_ord": row("order")}
    co = assemble("constitutional_decision", rows, dict(TXT, p_ord="주문 문장이다.\n"), draft("p", "court_majority", decision_type=decision_type),
                  dict(LEDGER, dispositions=dispositions))
    assert (co.record["verification_status"] == "quarantined") is quarantined
