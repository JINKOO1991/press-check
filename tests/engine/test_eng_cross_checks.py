"""contracts/README.md 교차검증 1~7 각각의 실패 사례와 통과 사례."""
import copy
import datetime as dt
import hashlib
import json
import unicodedata

import pytest

from press_review import contracts, corpus as corpus_mod, hashing

import pdr_engine_paths as paths


def errs(report, article, corpus, raw=None):
    return contracts.validate_report(report, article, corpus, raw_bytes=raw)


def has(errors, needle):
    return any(needle in e for e in errors)


# ---- 불변식: 예제 정합 -------------------------------------------------------------------

def test_example_hash_equals_actual_file_bytes(article_bytes, example_review_obj):
    assert hashlib.sha256(article_bytes).hexdigest() == example_review_obj["article_sha256"]
    assert example_review_obj["article_sha256"].startswith("ac38fd55")
    on_disk = (paths.CONTRACTS / "article.example.json").read_bytes()
    assert on_disk == article_bytes                                    # fixtures 사본이 원본과 바이트 동일


def test_example_spans_match_article_text(article, example_review_obj):
    blocks = {b["block_id"]: b["text"] for b in article["blocks"]}
    f = example_review_obj["findings"][0]
    sp = f["span"]
    assert blocks[sp["block_id"]][sp["start"]:sp["end"]] == sp["quote"]
    assert len(sp["quote"]) == sp["end"] - sp["start"] == 18


def test_example_review_passes_all_checks(article, article_bytes, example_review, corpus):
    assert errs(example_review, article, corpus, article_bytes) == []


def test_corpus_fixture_holds_example_evidence(example_review_obj, corpus):
    ev = example_review_obj["findings"][0]["evidence"][0]
    p = corpus.get_passage(ev["source_id"], ev["passage_id"])
    assert (p["url"], p["locator"], p["authority_type"]) == (ev["url"], ev["locator"], ev["authority_type"])


def test_schema_errors_short_circuit(article, corpus, example_review):
    example_review["priority"] = 1
    assert has(errs(example_review, article, corpus), "additionalProperties")


# ---- 1. span ---------------------------------------------------------------------------

def test_check1_quote_mismatch(article, article_bytes, example_review, corpus):
    example_review["findings"][0]["span"]["quote"] = "회사 돈 빼돌린 대표, 경찰 수삿"
    assert has(errs(example_review, article, corpus, article_bytes), "quote does not equal")


def test_check1_offset_shifted(article, article_bytes, example_review, corpus):
    sp = example_review["findings"][0]["span"]
    sp["start"], sp["end"] = 0, 17
    assert has(errs(example_review, article, corpus, article_bytes), "quote does not equal")


def test_check1_end_beyond_block_and_unknown_block(article, article_bytes, example_review, corpus):
    example_review["findings"][0]["span"]["end"] = 400
    assert has(errs(example_review, article, corpus, article_bytes), "outside block")
    example_review["findings"][0]["span"].update(end=18, block_id="nope")
    assert has(errs(example_review, article, corpus, article_bytes), "not in article")


def test_check1_float_offsets_rejected(article, article_bytes, example_review, corpus):
    example_review["findings"][0]["span"]["end"] = 18.0   # 스키마는 허용(2020-12)하지만 교차검증은 정수만
    assert has(errs(example_review, article, corpus, article_bytes), "must be integers")


def test_check1_utf16_offsets_fail_codepoint_offsets_pass(mk_article, example_review, corpus):
    text = "😀 서울 시장 발언"
    art = mk_article([("title", "title", "제목"), ("p1", "body", text)])
    quote = "서울 시장"
    cp_start = text.index(quote)
    utf16_start = cp_start + 1          # 😀 하나가 UTF-16에서 2 code unit
    rep = _single_finding_report(art, corpus, "p1", cp_start, cp_start + len(quote), quote)
    assert errs(rep, art, corpus) == []
    rep["findings"][0]["span"]["start"] = utf16_start
    rep["findings"][0]["span"]["end"] = utf16_start + len(quote)
    assert has(errs(rep, art, corpus), "quote does not equal")


def test_check1_nfc_nfd_not_normalized(mk_article, corpus):
    nfd = unicodedata.normalize("NFD", "횡령 혐의")
    art = mk_article([("title", "title", "제목"), ("p1", "body", nfd)])
    rep = _single_finding_report(art, corpus, "p1", 0, len(nfd), unicodedata.normalize("NFC", "횡령 혐의"))
    assert has(errs(rep, art, corpus), "quote does not equal")
    rep["findings"][0]["span"]["quote"] = nfd
    assert errs(rep, art, corpus) == []


def test_check1_related_edit_span_checked(article, article_bytes, example_review, corpus):
    example_review["findings"][0]["suggested_edit"]["related_edits"] = [
        {"span": {"block_id": "subtitle", "start": 0, "end": 2, "quote": "검찰"}, "replacement": "수사당국"}
    ]
    assert has(errs(example_review, article, corpus, article_bytes), "related_edits[0].span: quote does not equal")


def test_check1_repeated_phrase_distinguished_by_position(mk_article, corpus):
    art = mk_article([("title", "title", "제목"), ("p1", "body", "횡령 혐의. 횡령 혐의.")])
    rep = _single_finding_report(art, corpus, "p1", 7, 12, "횡령 혐의")
    assert errs(rep, art, corpus) == []
    rep["findings"][0]["span"]["start"], rep["findings"][0]["span"]["end"] = 3, 8
    assert has(errs(rep, art, corpus), "quote does not equal")


# ---- 2. 해시 ---------------------------------------------------------------------------

def test_check2_file_hash_requires_raw_bytes(article, article_bytes, example_review, corpus):
    # 예제 해시는 파일 바이트 기준이다. raw_bytes 없이 객체 해시와 비교하면 불일치.
    assert has(errs(example_review, article, corpus), "article_sha256 mismatch")
    assert has(errs(example_review, article, corpus, article_bytes + b" "), "article_sha256 mismatch")


def test_check2_object_mode_hash(article, example_review, corpus):
    example_review["article_sha256"] = hashing.article_sha256(article)[0]
    assert errs(example_review, article, corpus) == []
    article["blocks"][0]["text"] += "!"
    assert has(errs(example_review, article, corpus), "article_sha256 mismatch")


def test_check2_stale_article_id(article, article_bytes, example_review, corpus):
    example_review["article_id"] = "other"
    assert has(errs(example_review, article, corpus, article_bytes), "article_id differs")


# ---- 3. 수정안 --------------------------------------------------------------------------

def test_check3_mode_replacement_pairing(article, article_bytes, example_review, corpus):
    se = example_review["findings"][0]["suggested_edit"]
    se["replacement"] = None
    assert errs(example_review, article, corpus, article_bytes)         # replace인데 null
    se.update(mode="verify_first", replacement="문장")
    assert errs(example_review, article, corpus, article_bytes)         # verify_first인데 문장
    se.update(replacement=None)
    assert errs(example_review, article, corpus, article_bytes) == []   # 추가 확인은 required_verification에


def test_check3_related_edits_need_replace_mode(article, article_bytes, example_review, corpus):
    se = example_review["findings"][0]["suggested_edit"]
    se.update(mode="verify_first", replacement=None,
              related_edits=[{"span": {"block_id": "subtitle", "start": 0, "end": 2, "quote": "경찰"}, "replacement": "x"}])
    assert has(errs(example_review, article, corpus, article_bytes), "requires mode 'replace'")


def test_check3_overlapping_edits_conflict(article, article_bytes, example_review, corpus):
    title = article["blocks"][0]["text"]
    example_review["findings"][0]["suggested_edit"]["related_edits"] = [
        {"span": {"block_id": "title", "start": 10, "end": 14, "quote": title[10:14]}, "replacement": "x"}
    ]
    assert has(errs(example_review, article, corpus, article_bytes), "edit conflict")


def test_check3_adjacent_edits_are_fine_and_second_finding_overlap_conflicts(article, article_bytes, example_review, corpus):
    f1 = example_review["findings"][0]
    title = article["blocks"][0]["text"]
    f1["span"].update(start=0, end=5, quote=title[:5])
    f2 = copy.deepcopy(f1)
    f2.update(finding_id="finding-002", detailed_explanation_ref="example-report-001/finding-002")
    f2["span"].update(start=5, end=9, quote=title[5:9])
    example_review["findings"].append(f2)
    assert errs(example_review, article, corpus, article_bytes) == []          # 맞닿음은 충돌 아님
    f2["span"].update(start=4, end=9, quote=title[4:9])
    assert has(errs(example_review, article, corpus, article_bytes), "edit conflict")


def test_check3_non_replace_spans_may_overlap(article, article_bytes, example_review, corpus):
    f1 = example_review["findings"][0]
    f1["suggested_edit"].update(mode="verify_first", replacement=None)
    f2 = copy.deepcopy(f1)
    f2.update(finding_id="finding-002", detailed_explanation_ref="example-report-001/finding-002")
    example_review["findings"].append(f2)
    assert errs(example_review, article, corpus, article_bytes) == []


def test_duplicate_finding_id(article, article_bytes, example_review, corpus):
    f1 = example_review["findings"][0]
    f1["suggested_edit"].update(mode="none", replacement=None)
    example_review["findings"].append(copy.deepcopy(f1))
    assert has(errs(example_review, article, corpus, article_bytes), "duplicate finding_id")


# ---- 4. 근거 ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value,needle", [
    ("passage_id", "summary-99", "not found in corpus"),
    ("source_id", "law-prec-000000", "not found in corpus"),
    ("url", "https://www.law.go.kr/LSW/precInfoP.do?precSeq=999999", "url differs"),
    ("locator", {"kind": "html_section", "value": "판결요지 [1]"}, "locator differs"),
    ("authority_type", "statute", "authority_type differs"),
])
def test_check4_evidence_must_match_corpus(article, article_bytes, example_review, corpus, field, value, needle):
    example_review["findings"][0]["evidence"][0][field] = value
    assert has(errs(example_review, article, corpus, article_bytes), needle)


def test_check4_no_corpus_cannot_verify(article, article_bytes, example_review):
    assert has(errs(example_review, article, None, article_bytes), "corpus required")


def test_no_evidence_forces_low_confidence_and_needs_information(article, article_bytes, example_review, corpus):
    f = example_review["findings"][0]
    f["evidence"] = []
    assert has(errs(example_review, article, corpus, article_bytes), "needs_information")
    f["validation_status"] = "needs_information"
    assert has(errs(example_review, article, corpus, article_bytes), "review_confidence 'low'")
    f["review_confidence"] = "low"
    assert errs(example_review, article, corpus, article_bytes) == []


def test_check4_fixture_evidence_not_allowed_in_live_report(article, article_bytes, example_review, corpus):
    example_review["execution_mode"] = "live"
    example_review["retention"]["expires_at"] = "2026-10-05T12:30:00+09:00"
    e = errs(example_review, article, corpus, article_bytes)
    assert has(e, "fixture passage cited") and has(e, "fixture corpus")


def test_check4_decision_after_legal_as_of(article, article_bytes, example_review, corpus):
    ev = {"source_id": "fixture-pac-decision-001", "passage_id": "p1"}
    p = corpus.get_passage(**ev)
    example_review["findings"][0]["evidence"] = [{**ev, "url": p["url"], "locator": p["locator"], "authority_type": p["authority_type"], "verified": True}]
    example_review["legal_as_of"] = "2025-01-01"
    assert has(errs(example_review, article, corpus, article_bytes), "after legal_as_of")


# ---- 5. 권한(source record) ---------------------------------------------------------------

def source_record():
    from test_eng_schema_parity import source_cases
    return copy.deepcopy(source_cases()[0]["base"])


def test_check5_defaults_are_not_permission():
    assert contracts.validate_source_record(source_record()) == []


def test_check5_collection_requires_allowed_purpose_read_and_access():
    r = source_record()
    r["collection_enabled"] = True
    e = contracts.validate_source_record(r)
    assert has(e, "purpose_status") and has(e, "rights.read")
    r["robots"]["purpose_status"] = "disallowed"                   # 네이버 사례: 목적 금지
    r["rights"]["read"] = "permission_required"
    assert has(contracts.validate_source_record(r), "'disallowed'")
    r["access_status"] = "restricted"
    assert has(contracts.validate_source_record(r), "access_status")


def test_check5_allowed_needs_a_recorded_basis_and_rights_are_independent():
    r = source_record()
    r["collection_enabled"] = True
    r["robots"]["purpose_status"] = "allowed"
    r["rights"]["read"] = "allowed"
    assert has(contracts.validate_source_record(r), "without any rights_basis")
    # docs/DECISIONS.md UDEC-5: 사용자 결정으로 allowed + 근거 review_status pending 은 유효한 기록이다
    r["rights_basis"] = [{"url": "https://example.invalid/b", "checked_at": "2026-10-05T12:00:00Z", "scope": "read", "review_status": "pending"}]
    assert contracts.validate_source_record(r) == []
    r["rights"]["external_api"] = "unknown"                  # read만 allowed: 다른 이용행위는 독립
    r["rights"]["redistribute_raw"] = "permission_required"
    assert contracts.validate_source_record(r) == []


def test_check5_udec5_shape_is_valid():
    r = source_record()
    r["rights"].update(store_raw="allowed", create_derivatives="allowed", external_api="allowed",
                       redistribute_raw="permission_required", redistribute_derivatives="permission_required", fine_tune="permission_required")
    r["rights_basis"] = [{"url": "https://example.invalid/udec-5", "checked_at": "2026-10-08T00:00:00+09:00", "scope": "사용자 결정 2026-10-08", "review_status": "pending"}]
    assert contracts.validate_source_record(r) == []


def test_check5_raw_hash_requires_store_raw_and_pages_bound():
    r = source_record()
    r["raw_sha256"] = "a" * 64
    assert has(contracts.validate_source_record(r), "store_raw")
    r = source_record()
    r["extraction"].update(status="extracted", pages_expected=2, pages_extracted=3)
    assert has(contracts.validate_source_record(r), "pages_extracted exceeds")


# ---- 6. 상태 ---------------------------------------------------------------------------

def test_check6_findings_status_needs_findings(article, article_bytes, example_review, corpus):
    example_review["findings"] = []
    assert has(errs(example_review, article, corpus, article_bytes), "'findings' with no findings")


def test_check6_no_actionable_is_not_a_guarantee(article, article_bytes, example_review, corpus):
    example_review["overall_status"] = "no_actionable_findings"
    e = errs(example_review, article, corpus, article_bytes)
    assert has(e, "with findings present")
    example_review["findings"] = []
    e = errs(example_review, article, corpus, article_bytes)
    assert has(e, "non-fixture, non-empty corpus")
    example_review["coverage_notes"] = []
    assert has(errs(example_review, article, corpus, article_bytes), "requires coverage_notes")


@pytest.mark.parametrize("status", ["partial", "insufficient_information"])
def test_check6_insufficient_needs_coverage_notes(article, article_bytes, example_review, corpus, status):
    example_review["findings"] = []
    example_review["overall_status"] = status
    assert errs(example_review, article, corpus, article_bytes) == []
    example_review["coverage_notes"] = []
    assert has(errs(example_review, article, corpus, article_bytes), "requires coverage_notes")


def test_check6_hidden_count_separates_total_from_displayed(mk_article, corpus):
    art = mk_article([("title", "title", "제목"), ("p1", "body", "".join(f"문장{i:02d}입니다. " for i in range(8)))])
    rep = _single_finding_report(art, corpus, "p1", 0, 6, art["blocks"][1]["text"][:6])
    template = rep["findings"][0]
    rep["findings"] = []
    for i in range(7):
        f = copy.deepcopy(template)
        f.update(finding_id=f"finding-{i + 1:03d}", detailed_explanation_ref=f"{rep['report_id']}/finding-{i + 1:03d}")
        f["suggested_edit"].update(mode="none", replacement=None)
        rep["findings"].append(f)
    rep["hidden_finding_count"] = 2
    assert errs(rep, art, corpus) == []
    rep["hidden_finding_count"] = 0
    assert has(errs(rep, art, corpus), "hidden_finding_count")
    rep["hidden_finding_count"] = 7
    assert has(errs(rep, art, corpus), "hidden_finding_count")


# ---- 7. 보관 ---------------------------------------------------------------------------

def _live(rep):
    rep["execution_mode"] = "live"
    rep["findings"][0]["evidence"] = []
    rep["findings"][0]["validation_status"] = "needs_information"
    rep["findings"][0]["review_confidence"] = "low"
    return rep


class _LiveCorpus(corpus_mod.FixtureCorpus):
    is_fixture = False  # 검사 전용: fixture 아닌 척 하는 코퍼스

    @property
    def snapshot_id(self):
        return "live-test-1"


def test_check7_live_report_needs_expiry(article, article_bytes, example_review):
    rep = _live(example_review)
    rep["corpus_snapshot_id"] = "live-test-1"
    c = _LiveCorpus()
    assert has(errs(rep, article, c, article_bytes), "expires_at must be set")
    rep["retention"]["expires_at"] = "2026-10-05T12:30:00+09:00"
    assert errs(rep, article, c, article_bytes) == []


def test_check7_expiry_after_analysis_and_bounded(article, article_bytes, example_review, corpus):
    r = example_review
    r["retention"]["expires_at"] = "2026-10-05T11:59:59+09:00"
    assert has(errs(r, article, corpus, article_bytes), "after analyzed_at")
    r["retention"]["expires_at"] = "2026-10-08T12:00:00+09:00"
    assert has(errs(r, article, corpus, article_bytes), "exceeds")
    r["retention"]["expires_at"] = "2026-10-05T12:30:00+09:00"
    assert errs(r, article, corpus, article_bytes) == []


def test_check7_no_disk_retention_in_p0(article, article_bytes, example_review, corpus):
    example_review["retention"]["mode"] = "local_encrypted"
    assert has(errs(example_review, article, corpus, article_bytes), "not supported")


def test_check7_snapshot_must_match_for_live(article, article_bytes, example_review):
    rep = _live(example_review)
    rep["retention"]["expires_at"] = "2026-10-05T12:30:00+09:00"
    rep["corpus_snapshot_id"] = "something-else"
    assert has(errs(rep, article, _LiveCorpus(), article_bytes), "corpus_snapshot_id differs")


# ---- article 검증 -----------------------------------------------------------------------

def test_validate_article_extra_rules(article, mk_article):
    assert contracts.validate_article(article) == []
    a = copy.deepcopy(article)
    a["blocks"].append({"block_id": "title", "section": "body", "text": "x"})
    assert has(contracts.validate_article(a), "duplicate")
    a = mk_article([("t", "title", "제목")])
    assert has(contracts.validate_article(a), "body block required")
    a = mk_article([("p", "body", "본문")])
    assert has(contracts.validate_article(a), "title block required")
    a = mk_article([("t", "title", "제\ud83d목"), ("p", "body", "본문")])
    assert has(contracts.validate_article(a), "surrogate")
    a = mk_article([("t", "title", "제목"), ("p", "body", "가" * (contracts.MAX_ARTICLE_CP + 1))])
    assert has(contracts.validate_article(a), "longer than")


def test_single_sentence_rule():
    assert contracts.is_single_sentence("제목이 본문보다 범행을 확정합니다.")
    assert contracts.is_single_sentence("물음표로 끝납니까?")
    assert not contracts.is_single_sentence("첫 문장입니다. 둘째 문장입니다.")
    assert not contracts.is_single_sentence("줄바꿈\n포함")
    assert not contracts.is_single_sentence("   ")


# ---- 헬퍼 ------------------------------------------------------------------------------

def _single_finding_report(art, corpus, block_id, start, end, quote):
    digest, _ = hashing.article_sha256(art)
    return {
        "schema_version": "0.1", "execution_mode": "synthetic_example", "report_id": "rpt-test",
        "article_id": art["article_id"], "article_sha256": digest, "policy_version": "draft-0.1",
        "corpus_snapshot_id": corpus.snapshot_id, "analyzed_at": "2026-10-08T12:00:00+09:00", "legal_as_of": "2026-10-08",
        "overall_status": "findings", "coverage_notes": [],
        "retention": {"mode": "ephemeral", "expires_at": "2026-10-08T12:30:00+09:00"},
        "findings": [{
            "finding_id": "finding-001", "span": {"block_id": block_id, "start": start, "end": end, "quote": quote},
            "priority_stars": 2, "risk_channels": ["civil"], "issue_category": "test", "review_confidence": "low",
            "reason_short": "시험용 사유입니다.",
            "suggested_edit": {"mode": "verify_first", "replacement": None, "related_edits": [], "required_verification": ["확인"]},
            "evidence": [], "counter_considerations": [], "detailed_explanation_ref": "rpt-test/finding-001",
            "validation_status": "needs_information",
        }],
        "hidden_finding_count": 0,
    }
