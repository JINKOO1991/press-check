"""자체 검증기(contracts.schema_errors)와 jsonschema Draft202012Validator(format 검사 포함)의 판정·오류 위치 일치."""
import copy
import json

import pytest

import refschema
from press_review import contracts

import pdr_engine_paths as paths

SCHEMAS = {n: contracts.load_schema(n) for n in ("article", "review", "source-record", "case-record")}


def test_reference_format_checker_is_not_a_noop():
    v = refschema.ref_validator({"type": "string", "format": "date-time"})
    assert list(v.iter_errors("2026-10-05T12:00:00+09:00")) == []
    assert list(v.iter_errors("2026-10-05 12:00:00")) != []
    v = refschema.ref_validator({"type": "string", "format": "uri"})
    assert list(v.iter_errors("https://example.invalid/a?b=1")) == []
    assert list(v.iter_errors("not a uri")) != []
    v = refschema.ref_validator({"type": "string", "format": "date"})
    assert list(v.iter_errors("2026-02-30")) != []


def mutate(base, fn):
    obj = copy.deepcopy(base)
    fn(obj)
    return obj


@pytest.fixture(scope="module")
def art():
    return json.loads((paths.ARTICLE_FIXTURE).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rev():
    return json.loads((paths.CONTRACTS / "review.example.json").read_text(encoding="utf-8"))


def article_cases(art):
    valid = {
        "example": art,
        "opinion_genre": mutate(art, lambda o: o["context"].update(genre="opinion")),
        "election_string": mutate(art, lambda o: o["context"].update(election_context="2026 지방선거")),
        "sensitive_true": mutate(art, lambda o: o["context"].update(sensitive_material=True)),
        "caption_block": mutate(art, lambda o: o["blocks"].append({"block_id": "c1", "section": "caption", "text": "사진=가상"})),
        "empty_notes": mutate(art, lambda o: o["context"].update(verification_notes=[])),
        "date_leap": mutate(art, lambda o: o.update(publication_date="2024-02-29")),
    }
    invalid = {
        "missing_context": mutate(art, lambda o: o.pop("context")),
        "bad_section": mutate(art, lambda o: o["blocks"][0].update(section="headline")),
        "bad_month": mutate(art, lambda o: o.update(publication_date="2026-13-01")),
        "short_date": mutate(art, lambda o: o.update(publication_date="2026-10-5")),
        "feb30": mutate(art, lambda o: o.update(publication_date="2026-02-30")),
        "extra_prop": mutate(art, lambda o: o.update(foo=1)),
        "empty_text": mutate(art, lambda o: o["blocks"][1].update(text="")),
        "no_blocks": mutate(art, lambda o: o.update(blocks=[])),
        "version": mutate(art, lambda o: o.update(schema_version="0.2")),
        "block_id_int": mutate(art, lambda o: o["blocks"][0].update(block_id=7)),
        "date_int": mutate(art, lambda o: o.update(publication_date=20261005)),
        "election_int": mutate(art, lambda o: o["context"].update(election_context=5)),
        "sensitive_str": mutate(art, lambda o: o["context"].update(sensitive_material="false")),
        "block_extra": mutate(art, lambda o: o["blocks"][0].update(x=1)),
        "notes_not_array": mutate(art, lambda o: o["context"].update(verification_notes="x")),
    }
    return valid, invalid


def review_cases(rev):
    def f0(o):
        return o["findings"][0]

    valid = {
        "example": rev,
        "expires_set": mutate(rev, lambda o: o["retention"].update(expires_at="2026-10-05T12:30:00+09:00")),
        "expires_z": mutate(rev, lambda o: o["retention"].update(expires_at="2026-10-05T03:30:00Z")),
        "no_findings": mutate(rev, lambda o: o.update(findings=[], overall_status="partial")),
        "no_evidence": mutate(rev, lambda o: f0(o).update(evidence=[], validation_status="needs_information", review_confidence="low")),
        "verify_first": mutate(rev, lambda o: f0(o)["suggested_edit"].update(mode="verify_first", replacement=None)),
        "mode_none": mutate(rev, lambda o: f0(o)["suggested_edit"].update(mode="none", replacement=None)),
        "reason_100": mutate(rev, lambda o: f0(o).update(reason_short="가" * 100)),
        "stars_1": mutate(rev, lambda o: f0(o).update(priority_stars=1)),
        "stars_float_int": mutate(rev, lambda o: f0(o).update(priority_stars=2.0)),
        "live": mutate(rev, lambda o: o.update(execution_mode="live")),
        "related_edit": mutate(rev, lambda o: f0(o)["suggested_edit"].update(related_edits=[{"span": {"block_id": "subtitle", "start": 0, "end": 2, "quote": "경찰"}, "replacement": "수사당국"}])),
    }
    invalid = {
        "stars_4": mutate(rev, lambda o: f0(o).update(priority_stars=4)),
        "stars_0": mutate(rev, lambda o: f0(o).update(priority_stars=0)),
        "stars_bool": mutate(rev, lambda o: f0(o).update(priority_stars=True)),
        "stars_float": mutate(rev, lambda o: f0(o).update(priority_stars=2.5)),
        "stars_str": mutate(rev, lambda o: f0(o).update(priority_stars="3")),
        "reason_101": mutate(rev, lambda o: f0(o).update(reason_short="가" * 101)),
        "reason_empty": mutate(rev, lambda o: f0(o).update(reason_short="")),
        "replace_null": mutate(rev, lambda o: f0(o)["suggested_edit"].update(replacement=None)),
        "replace_empty": mutate(rev, lambda o: f0(o)["suggested_edit"].update(replacement="")),
        "verify_with_text": mutate(rev, lambda o: f0(o)["suggested_edit"].update(mode="verify_first")),
        "mode_missing": mutate(rev, lambda o: f0(o)["suggested_edit"].pop("mode")),
        "verified_false": mutate(rev, lambda o: f0(o)["evidence"][0].update(verified=False)),
        "verified_one": mutate(rev, lambda o: f0(o)["evidence"][0].update(verified=1)),
        "verified_missing": mutate(rev, lambda o: f0(o)["evidence"][0].pop("verified")),
        "url_space": mutate(rev, lambda o: f0(o)["evidence"][0].update(url="not a uri")),
        "url_no_scheme": mutate(rev, lambda o: f0(o)["evidence"][0].update(url="www.law.go.kr/x")),
        "analyzed_space": mutate(rev, lambda o: o.update(analyzed_at="2026-10-05 12:00:00")),
        "analyzed_no_tz": mutate(rev, lambda o: o.update(analyzed_at="2026-10-05T12:00:00")),
        "analyzed_month13": mutate(rev, lambda o: o.update(analyzed_at="2026-13-05T12:00:00Z")),
        "expires_bad": mutate(rev, lambda o: o["retention"].update(expires_at="tomorrow")),
        "legal_feb30": mutate(rev, lambda o: o.update(legal_as_of="2026-02-30")),
        "sha_upper": mutate(rev, lambda o: o.update(article_sha256=o["article_sha256"].upper())),
        "sha_short": mutate(rev, lambda o: o.update(article_sha256="abc")),
        "span_start_neg": mutate(rev, lambda o: f0(o)["span"].update(start=-1)),
        "span_end_zero": mutate(rev, lambda o: f0(o)["span"].update(end=0)),
        "span_end_float": mutate(rev, lambda o: f0(o)["span"].update(end=18.5)),
        "hidden_neg": mutate(rev, lambda o: o.update(hidden_finding_count=-1)),
        "retention_mode": mutate(rev, lambda o: o["retention"].update(mode="disk")),
        "exec_mode": mutate(rev, lambda o: o.update(execution_mode="dry")),
        "probability": mutate(rev, lambda o: f0(o).update(probability=0.4)),
        "bad_channel": mutate(rev, lambda o: f0(o).update(risk_channels=["lawsuit"])),
        "bad_authority": mutate(rev, lambda o: f0(o)["evidence"][0].update(authority_type="blog")),
        "bad_locator_kind": mutate(rev, lambda o: f0(o)["evidence"][0]["locator"].update(kind="line")),
        "missing_findings": mutate(rev, lambda o: o.pop("findings")),
        "status_bad": mutate(rev, lambda o: o.update(overall_status="safe")),
        "related_missing_replacement": mutate(rev, lambda o: f0(o)["suggested_edit"].update(related_edits=[{"span": {"block_id": "t", "start": 0, "end": 1, "quote": "a"}}])),
    }
    return valid, invalid


def source_cases():
    base = {
        "schema_version": "0.1", "source_id": "s1", "source_family": "law_go_kr",
        "canonical_url": "https://example.invalid/s1", "document_title": "t", "publication_date": None, "decision_date": None,
        "checked_at": "2026-10-05T12:00:00+09:00", "access_status": "available", "collection_enabled": False,
        "robots": {"fetch_status": "checked", "purpose_status": "unknown", "policy_url": "https://example.invalid/robots.txt", "notes": ""},
        "rights": {k: "unknown" for k in ("read", "store_raw", "create_derivatives", "external_api", "redistribute_raw", "redistribute_derivatives", "fine_tune")},
        "rights_basis": [], "raw_sha256": None,
        "extraction": {"status": "not_started", "version": None, "pages_expected": None, "pages_extracted": None},
        "outcome_type": "unknown", "before_after_status": "not_applicable", "notes": [],
    }
    valid = {
        "base": base,
        "basis": mutate(base, lambda o: o["rights_basis"].append({"url": "https://example.invalid/b", "checked_at": "2026-10-05T12:00:00Z", "scope": "read", "review_status": "pending"})),
        "pages": mutate(base, lambda o: o["extraction"].update(status="extracted", pages_expected=3, pages_extracted=3)),
        "sha": mutate(base, lambda o: o.update(raw_sha256="a" * 64)),
        "dates": mutate(base, lambda o: o.update(publication_date="2026-10-05", decision_date="anything goes")),
    }
    invalid = {
        "family": mutate(base, lambda o: o.update(source_family="blog")),
        "url": mutate(base, lambda o: o.update(canonical_url="nope")),
        "sha": mutate(base, lambda o: o.update(raw_sha256="xyz")),
        "pages_neg": mutate(base, lambda o: o["extraction"].update(pages_expected=-1)),
        "right_enum": mutate(base, lambda o: o["rights"].update(read="yes")),
        "collection_str": mutate(base, lambda o: o.update(collection_enabled="no")),
        "robots_missing": mutate(base, lambda o: o["robots"].pop("notes")),
        "basis_status": mutate(base, lambda o: o["rights_basis"].append({"url": "https://example.invalid/b", "checked_at": "2026-10-05T12:00:00Z", "scope": "read", "review_status": "ok"})),
    }
    return valid, invalid


def case_cases():
    from case_record_fixture import base_record, mutated, q
    base = base_record()
    valid = {
        "base": base,
        "null_outcome": mutated(lambda o: o.update(outcome=None, outcome_type="unknown", settlement_or_adjudication="unknown")),
        "dates_null": mutated(lambda o: o.update(decision_date=None, case_number=None, institution_name=None, att_id=None)),
        "law_version": mutated(lambda o: o["law_versions"].append({"law": "언론중재법", "effective_from": "2005-07-28", "effective_to": None,
                                                                    "relation": "applied_at_time", "quote": None})),
        "history": mutated(lambda o: o["appeal_or_later_history"].append({"relation": "appeal", "case_number": None, "note": "항소", "quote": None})),
        "verified": mutated(lambda o: o.update(verification_status="verified", verified_by=[{"by": "reviewer", "method": "human", "at": "2026-10-09T09:00:00+09:00"}])),
        "provision": mutated(lambda o: o["legal_provisions"].append({"citation": "언론중재법 제14조", "quote": q("정정보도를 청구하였다")})),
    }
    invalid = {
        "version": mutated(lambda o: o.update(schema_version="0.2")),
        "missing_case_id": mutated(lambda o: o.pop("case_id")),
        "extra": mutated(lambda o: o.update(probability=0.3)),
        "institution": mutated(lambda o: o.update(institution="blog")),
        "authority": mutated(lambda o: o.update(authority_type="blog")),
        "outcome_type": mutated(lambda o: o.update(outcome_type="illegal")),
        "scope": mutated(lambda o: o.update(outcome_scope="merits")),
        "speaker": mutated(lambda o: o["factual_background"][0].update(speaker="judge")),
        "quote_missing_end": mutated(lambda o: o["factual_background"][0]["quote"].pop("end")),
        "quote_start_neg": mutated(lambda o: o["factual_background"][0]["quote"].update(start=-1)),
        "quote_end_zero": mutated(lambda o: o["factual_background"][0]["quote"].update(end=0)),
        "quote_empty": mutated(lambda o: o["factual_background"][0]["quote"].update(quote="")),
        "quote_start_float": mutated(lambda o: o["factual_background"][0]["quote"].update(start=1.5)),
        "quote_extra": mutated(lambda o: o["factual_background"][0]["quote"].update(x=1)),
        "date_bad": mutated(lambda o: o.update(decision_date="2001-02-30")),
        "date_int": mutated(lambda o: o.update(decision_date=20010504)),
        "no_support": mutated(lambda o: o.update(supporting_passage_ids=[])),
        "locator_kind": mutated(lambda o: o["source_locators"][0].update(kind="line")),
        "pair": mutated(lambda o: o.update(correction_pair_confidence="verified")),
        "orig_status": mutated(lambda o: o["available_original_article"].update(status="seen")),
        "orig_quote_missing": mutated(lambda o: o["available_original_article"].pop("quote")),
        "verified_by_at": mutated(lambda o: o["verified_by"].append({"by": "r", "method": "human", "at": "yesterday"})),
        "verified_method": mutated(lambda o: o["verified_by"].append({"by": "r", "method": "gut", "at": "2026-10-09T09:00:00Z"})),
        "verification": mutated(lambda o: o.update(verification_status="ok")),
        "qc_neg": mutated(lambda o: o["quote_check"].update(matched=-1)),
        "sha": mutated(lambda o: o["structurer"].update(input_sha256="xyz")),
        "party_role": mutated(lambda o: o["parties_redacted"][0].update(role="judge")),
        "text_too_long": mutated(lambda o: o["factual_background"][0].update(text="가" * 601)),
        "history_rel": mutated(lambda o: o.update(appeal_or_later_history=[{"relation": "x", "case_number": None, "note": "n", "quote": None}])),
        "law_rel": mutated(lambda o: o.update(law_versions=[{"law": "x", "effective_from": None, "effective_to": None, "relation": "now", "quote": None}])),
    }
    return valid, invalid


def _check(schema, valid, invalid):
    assert len(valid) >= 5 and len(invalid) >= 5
    for name, inst in valid.items():
        assert contracts.schema_errors(inst, schema) == [], name
        assert refschema.ref_paths(schema, inst) == set(), name
    for name, inst in invalid.items():
        mine = {p for p, _ in contracts.schema_error_items(inst, schema)}
        ref = refschema.ref_paths(schema, inst)
        assert ref, f"reference accepts invalid case {name}"
        assert mine == ref, f"{name}: self={sorted(mine)} ref={sorted(ref)}"


def test_article_parity(art):
    _check(SCHEMAS["article"], *article_cases(art))


def test_review_parity(rev):
    _check(SCHEMAS["review"], *review_cases(rev))


def test_source_record_parity():
    _check(SCHEMAS["source-record"], *source_cases())


def test_case_record_parity():
    _check(SCHEMAS["case-record"], *case_cases())


def test_contract_examples_validate_under_both(rev, art):
    assert contracts.validate_article(art) == []
    assert contracts.schema_errors(rev, SCHEMAS["review"]) == []
    assert refschema.ref_paths(SCHEMAS["review"], rev) == set()


def test_bool_is_not_a_number_or_const_match():
    assert contracts.schema_errors(True, {"type": "integer"}) != []
    assert contracts.schema_errors(1, {"const": True}) != []
    assert contracts.schema_errors(True, {"const": True}) == []
    assert contracts.schema_errors(1.0, {"type": "integer"}) == []
    assert contracts.schema_errors(True, {"enum": [1]}) != []
    assert contracts.schema_errors(None, {"type": "string", "format": "date-time", "minLength": 3}) != []
    assert contracts.schema_errors(None, {"type": ["string", "null"], "format": "date-time", "pattern": "x"}) == []


def test_unknown_keyword_is_rejected_not_ignored():
    with pytest.raises(contracts.UnsupportedSchemaKeyword):
        contracts.schema_errors("x", {"oneOf": []})


def test_load_schema_rejects_traversal():
    with pytest.raises(FileNotFoundError):
        contracts.load_schema("../fixtures/corpus.json")
