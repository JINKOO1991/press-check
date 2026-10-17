"""contracts.validate_case_record: 스키마 + 인용 대조 + 라벨 분리 교차검증."""
import pytest

import pdr_engine_paths as paths
from case_record_fixture import PAGE_ID, PAGES, base_record, mutated, q
from press_review import contracts


def errs(rec, pages=PAGES):
    return contracts.validate_case_record(rec, pages)


def test_base_record_is_valid_with_and_without_pages():
    assert errs(base_record()) == []
    assert contracts.validate_case_record(base_record()) == []                   # 페이지 없이도 스키마·라벨 검사는 한다
    assert contracts.validate_case_record(base_record(), lambda pid: PAGES.get(pid)) == []   # 함수형 페이지 공급


def test_schema_copies_are_byte_identical():
    for f in ("article", "review", "source-record", "case-record"):
        a = (paths.CONTRACTS / f"{f}.schema.json").read_bytes()
        b = (paths.ENGINE / "press_review" / "schemas" / f"{f}.schema.json").read_bytes()
        assert a == b, f


def test_schema_errors_are_returned_before_cross_checks():
    r = base_record()
    r["schema_version"] = "9"
    out = errs(r)
    assert out and all(e.startswith("$") for e in out)


@pytest.mark.parametrize("name, fn, needle", [
    ("quote_text_differs", lambda o: o["disputed_spans"][0]["quote"].update(quote="가상시 시장이 공사비를 지켰다"), "quote does not match"),
    ("start_shift", lambda o: o["disputed_spans"][0]["quote"].update(start=o["disputed_spans"][0]["quote"]["start"] - 1,
                                                                       end=o["disputed_spans"][0]["quote"]["end"] - 1), "quote does not match"),
    ("end_beyond_page", lambda o: o["disputed_spans"][0]["quote"].update(end=99999), "outside page"),
    ("start_equals_end_impossible_by_schema_but_start_gt_len", lambda o: o["disputed_spans"][0]["quote"].update(start=500, end=510), "outside page"),
    ("unknown_page", lambda o: o["disputed_spans"][0]["quote"].update(page_id="nope@p1"), "not a known page"),
    ("support_missing_cited", lambda o: o["disputed_spans"][0]["quote"].update(page_id="pac_adjustment_casebooks:1#1@p4", start=0, end=3, quote="조정은"),
     "supporting_passage_ids does not contain"),
    ("support_unknown", lambda o: o["supporting_passage_ids"].append("ghost@p9"), "is not a known page"),
])
def test_quote_cross_checks(name, fn, needle):
    out = errs(mutated(fn))
    assert any(needle in e for e in out), (name, out)


def test_quote_comparison_collapses_only_whitespace():
    r = mutated(lambda o: o["outcome"].update(quote=q("양 당사자가  조정합의에 이르렀음을 확인한다.")))
    r["outcome"]["quote"]["quote"] = "양 당사자가 조정합의에 이르렀음을 확인한다."       # 원문은 공백 2칸, quote는 1칸
    assert errs(r) == []
    r["outcome"]["quote"]["quote"] = "양 당사자가 조정합의에 이르렀음을 확인합니다."
    assert any("outcome.quote" in e for e in errs(r))


def test_offsets_are_code_points_not_utf16():
    pages = {"p@1": "😀가나다 본문 " + "정정보도" + " 이후 문장"}
    start = pages["p@1"].index("정정보도")
    r = base_record()
    r["supporting_passage_ids"] = ["p@1"]
    for f in ("factual_background", "disputed_spans", "claims_or_requested_remedy"):
        r[f] = []
    r["outcome"] = {"text": "x", "speaker": "institution", "quote": {"quote": "정정보도", "page_id": "p@1", "start": start, "end": start + 4}}
    r["available_original_article"] = {"status": "absent", "quote": None}
    assert contracts.validate_case_record(r, pages) == []
    r["outcome"]["quote"].update(start=start + 1, end=start + 5)                  # UTF-16 위치를 쓴 경우처럼 한 칸 밀림
    assert any("quote does not match" in e for e in contracts.validate_case_record(r, pages))


@pytest.mark.parametrize("name, fn, needle", [
    ("settlement_as_judgment", lambda o: o.update(outcome_type="judgment", authority_type="judgment"), "cannot be recorded as outcome_type"),
    ("settlement_outcome_without_value", lambda o: o.update(settlement_or_adjudication="adjudication"), "requires settlement_or_adjudication"),
    ("pac_settlement_with_ruling", lambda o: o.update(outcome_type="decision", settlement_or_adjudication="adjudication"), "cannot carry outcome_type"),
    ("procedural_dismissal_with_merits_scope", lambda o: o.update(authority_type="judgment", outcome_type="dismissal_procedural",
                                                                   settlement_or_adjudication="adjudication", outcome_scope="whole"), "requires outcome_scope 'procedural'"),
    ("procedural_scope_with_merits_type", lambda o: o.update(authority_type="judgment", outcome_type="judgment",
                                                              settlement_or_adjudication="adjudication", outcome_scope="procedural"), "outcome_scope 'procedural' cannot"),
    ("withdrawal_without_value", lambda o: o.update(outcome_type="withdrawal"), "requires settlement_or_adjudication 'withdrawal'"),
    ("court_speaker_in_pac_record", lambda o: o["legal_reasoning"].append({"text": "반대", "speaker": "court_dissenting", "quote": q("조정합의")}), "requires authority_type 'judgment'"),
    ("claimant_in_reasoning", lambda o: o["legal_reasoning"].append({"text": "주장", "speaker": "claimant", "quote": q("조정합의")}), "not allowed here"),
    ("dissent_as_outcome", lambda o: o["outcome"].update(speaker="court_dissenting"), "not allowed here"),
    ("claim_by_institution", lambda o: o["claims_or_requested_remedy"][0].update(speaker="institution"), "not allowed here"),
    ("article_field_wrong_speaker", lambda o: o["disputed_spans"][0].update(speaker="claimant"), "not allowed here"),
    ("quoted_status_without_quote", lambda o: o["available_original_article"].update(quote=None), "quote is required exactly"),
    ("absent_status_with_quote", lambda o: o["available_corrected_article"].update(quote=q("조정합의")), "quote is required exactly"),
    ("full_pair_without_corrected", lambda o: o.update(correction_pair_confidence="full_pair_verified"), "requires both articles"),
    ("before_missing_but_quoted", lambda o: o.update(correction_pair_confidence="before_missing"), "contradicts"),
    ("verified_without_by", lambda o: o.update(verification_status="verified"), "requires verified_by"),
    ("by_without_verified", lambda o: o.update(verified_by=[{"by": "r", "method": "human", "at": "2026-10-09T09:00:00Z"}]), "requires verified_by"),
    ("matched_gt_total", lambda o: o["quote_check"].update(matched=9, total=5), "exceeds total"),
    ("law_dates_reversed", lambda o: o["law_versions"].append({"law": "x", "effective_from": "2010-01-01", "effective_to": "2005-01-01",
                                                               "relation": "unknown", "quote": None}), "after effective_to"),
])
def test_label_separation_rules(name, fn, needle):
    out = errs(mutated(fn))
    assert any(needle in e for e in out), (name, out)


def test_court_record_with_majority_and_dissent_is_valid():
    def f(o):
        o.update(authority_type="judgment", outcome_type="judgment", settlement_or_adjudication="adjudication", institution="supreme_court",
                 outcome_scope="partial")
        o["outcome"]["speaker"] = "court_majority"
        o["legal_reasoning"] = [{"text": "다수의견", "speaker": "court_majority", "quote": q("조정합의")},
                                {"text": "반대의견", "speaker": "court_dissenting", "quote": q("확인한다")}]
    assert errs(mutated(f)) == []


def test_start_not_before_end_is_rejected_even_without_pages():
    r = mutated(lambda o: None)
    r["outcome"]["quote"].update(start=10, end=5)
    assert any("end must be greater than start" in e for e in contracts.validate_case_record(r))
    r["outcome"]["quote"].update(start=7, end=7)
    assert any("end must be greater than start" in e for e in contracts.validate_case_record(r))
