"""MockProvider -> build_report -> 유효한 보고서, 그리고 MCP 도구 함수(prepare/submit/explain) 흐름."""
import copy
import datetime as dt
import json

import pytest

import refschema
from press_review import contracts, review
from press_review.article_io import parse_plaintext
from press_review.providers import MockProvider
from press_review.store import EphemeralStore

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def run(article, corpus, raw=None, **kw):
    findings, expl = MockProvider().analyze(article, corpus)
    return review.build_report_ex(article, findings, expl, corpus, now=NOW, raw_bytes=raw, **kw)


def test_mock_on_example_article_gives_valid_report(article, article_bytes, corpus):
    res = run(article, corpus, article_bytes)
    assert res.errors == [] and res.report is not None
    r = res.report
    assert contracts.validate_report(r, article, corpus, raw_bytes=article_bytes) == []
    schema = contracts.load_schema("review")
    assert list(refschema.ref_validator(schema).iter_errors(r)) == []        # jsonschema도 통과
    assert r["article_sha256"].startswith("ac38fd55")
    assert r["execution_mode"] == "synthetic_example" and r["overall_status"] == "findings"
    assert r["retention"]["expires_at"] == "2026-10-08T12:30:00+09:00" and r["analyzed_at"] == "2026-10-08T12:00:00+09:00"
    assert r["hidden_finding_count"] == 0 and r["legal_as_of"] == "2026-10-05"            # 기사 발행(예정)일(P3_DESIGN §2). 합성 코퍼스 기준일(2026-10-08)이 아니다
    assert any("file_bytes" in n for n in r["coverage_notes"]) and any("합성" in n for n in r["coverage_notes"])
    assert "finding-001" in res.explanations


def test_mock_finding_equals_contract_example(article, article_bytes, corpus, example_review_obj):
    r = run(article, corpus, article_bytes, report_id="example-report-001").report
    got, want = r["findings"][0], example_review_obj["findings"][0]
    assert got == want                                                     # 서버가 verified/ref를 채운 결과가 예제와 동일


def test_object_input_uses_cjson_hash(article, corpus):
    r = run(article, corpus).report
    assert r["article_sha256"] != "ac38fd558f61269d67393bec0f1705bf1ef0739dfec84488a5e3de10043c7869"
    assert any("cjson-1" in n for n in r["coverage_notes"])
    assert contracts.validate_report(r, article, corpus) == []


def test_benign_article_is_partial_not_all_clear(corpus, mk_article):
    art = mk_article([("title", "title", "시청, 새 도서관 개관"), ("p1", "body", "시는 8일 새 도서관을 열었다.")])
    r = run(art, corpus).report
    assert r["findings"] == [] and r["overall_status"] == "partial"
    assert any("합성 코퍼스" in n for n in r["coverage_notes"])


def test_hedged_or_unmatched_title_not_flagged(corpus, mk_article):
    art = mk_article([("title", "title", "대표 횡령 혐의 수사"), ("p1", "body", "경찰은 횡령 혐의를 수사 중이다.")])
    assert run(art, corpus).report["findings"] == []
    art = mk_article([("title", "title", "회사 돈 빼돌린 대표"), ("p1", "body", "경찰은 횡령 혐의를 수사 중이다.")])
    f = run(art, corpus).report["findings"][0]
    assert f["suggested_edit"]["mode"] == "verify_first" and f["suggested_edit"]["replacement"] is None


def test_plaintext_to_report_end_to_end(corpus):
    txt = ("회사 돈 빼돌린 대표, 경찰 수사\n\n경찰, 횡령 혐의로 사실관계 확인\n\n경찰은 A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다.\n")
    art = parse_plaintext(txt, publication_date="2026-10-05")
    r = run(art, corpus).report
    assert r and r["findings"][0]["suggested_edit"]["replacement"] == "회사 돈 빼돌린 혐의…경찰, 대표 수사"


def test_build_report_returns_pair_and_none_on_failure(article, corpus):
    findings, expl = MockProvider().analyze(article, corpus)
    report, errors = review.build_report(article, findings, expl, corpus, now=NOW)
    assert errors == [] and report["findings"]
    bad = copy.deepcopy(findings)
    bad[0]["span"]["quote"] = "틀린 인용"
    report, errors = review.build_report(article, bad, expl, corpus, now=NOW)
    assert report is None and any("quote does not equal" in e for e in errors)


def test_naive_now_rejected(article, corpus):
    r = review.build_report_ex(article, [], {}, corpus, now=dt.datetime(2026, 10, 8, 12, 0))
    assert r.report is None and "timezone-aware" in r.errors[0]


def test_invalid_article_rejected(corpus, mk_article):
    r = review.build_report_ex(mk_article([("t", "title", "제목")]), [], {}, corpus, now=NOW)
    assert r.report is None and r.errors[0].startswith("article:")


# ---- 근거 처리: 서버만 verified를 붙인다 -------------------------------------------------------

def _finding(article, corpus):
    return copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])


def test_model_supplied_verified_is_ignored(article, corpus):
    f = _finding(article, corpus)
    f["evidence"] = [{"source_id": "law-prec-194652", "passage_id": "summary-2", "verified": False}]
    r = review.build_report_ex(article, [f], {}, corpus, now=NOW).report
    assert r["findings"][0]["evidence"][0]["verified"] is True


def test_invented_evidence_is_an_error_not_silently_dropped(article, corpus):
    f = _finding(article, corpus)
    f["evidence"] = [{"source_id": "law-prec-000", "passage_id": "x", "url": "https://www.law.go.kr/made-up", "verified": True}]
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is None and any("not in the corpus" in e for e in res.errors)


def test_model_supplied_url_must_equal_corpus(article, corpus):
    f = _finding(article, corpus)
    f["evidence"] = [{"source_id": "law-prec-194652", "passage_id": "summary-2", "url": "https://www.law.go.kr/LSW/precInfoP.do?precSeq=1"}]
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is None and any("url differs" in e for e in res.errors)
    f["evidence"][0]["url"] = "https://www.law.go.kr/LSW/precInfoP.do?precSeq=194652"
    assert review.build_report_ex(article, [f], {}, corpus, now=NOW).report is not None


def test_unknown_evidence_keys_rejected(article, corpus):
    f = _finding(article, corpus)
    f["evidence"][0]["probability"] = 0.9
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is None and any("unexpected keys" in e for e in res.errors)


def test_empty_evidence_is_coerced_low_and_needs_information(article, corpus):
    f = _finding(article, corpus)
    f.update(evidence=[], review_confidence="high", validation_status="verified")
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    out = res.report["findings"][0]
    assert (out["review_confidence"], out["validation_status"], out["evidence"]) == ("low", "needs_information", [])
    assert any("no evidence" in w for w in res.warnings)
    assert "[근거 미확보]" in review.render.render_table(res.report)


# ---- 수정안 검사가 보고서 조립에 연결돼 있다 ------------------------------------------------------

def test_new_number_in_replacement_blocks_report(article, corpus):
    f = _finding(article, corpus)
    f["suggested_edit"]["replacement"] = "회사 돈 3억 빼돌린 혐의…경찰, 대표 수사"
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is None and any("NEW_NUMBER" in e for e in res.errors)


def test_warning_goes_to_required_verification(article, corpus):
    f = _finding(article, corpus)
    f["suggested_edit"]["replacement"] = "회사 돈 빼돌린 혐의…경찰, 박씨 수사"
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is not None
    rv = res.report["findings"][0]["suggested_edit"]["required_verification"]
    assert any("NEW_PROPER_NOUN" in x for x in rv) and any("NEW_PROPER_NOUN" in w for w in res.warnings)


def test_direct_quote_edit_blocks_report(article, corpus):
    f = _finding(article, corpus)
    q = "횡령한 사실이 없다"
    s = article["blocks"][3]["text"].index(q)
    f["span"] = {"block_id": "p2", "start": s, "end": s + len(q), "quote": q}
    f["suggested_edit"]["replacement"] = "횡령한 사실이 있다"
    res = review.build_report_ex(article, [f], {}, corpus, now=NOW)
    assert res.report is None and any("DIRECT_QUOTE_CHANGED" in e for e in res.errors)


def test_overlapping_replace_findings_conflict(article, corpus):
    f1 = _finding(article, corpus)
    f2 = copy.deepcopy(f1)
    f2["finding_id"] = "finding-002"
    res = review.build_report_ex(article, [f1, f2], {}, corpus, now=NOW)
    assert res.report is None and any("edit conflict" in e for e in res.errors)


def test_findings_sorted_by_stars_then_position_and_hidden_count(mk_article, corpus):
    text = " ".join(f"문장{i}번입니다." for i in range(8))
    art = mk_article([("title", "title", "제목"), ("p1", "body", text)])
    fs = []
    for i in range(8):
        q = f"문장{i}번입니다."
        s = text.index(q)
        fs.append({
            "finding_id": f"f-{i}", "span": {"block_id": "p1", "start": s, "end": s + len(q), "quote": q},
            "priority_stars": 3 if i in (5, 6) else 1, "risk_channels": [], "issue_category": "c", "review_confidence": "low",
            "reason_short": "사유입니다.", "suggested_edit": {"mode": "none", "replacement": None},
            "evidence": [], "counter_considerations": [],
        })
    res = review.build_report_ex(art, list(reversed(fs)), {}, corpus, now=NOW)
    r = res.report
    assert [f["finding_id"] for f in r["findings"]] == ["f-5", "f-6", "f-0", "f-1", "f-2", "f-3", "f-4", "f-7"]
    assert r["hidden_finding_count"] == 3
    assert len(r["findings"]) == 8                                           # 전체는 버리지 않는다
    assert review.render.render_table(r).count("[f-") == 5


def test_unknown_explanation_ids_warn(article, corpus):
    findings, expl = MockProvider().analyze(article, corpus)
    expl["finding-099"] = {"x": "y"}
    res = review.build_report_ex(article, findings, expl, corpus, now=NOW)
    assert res.report and any("finding-099" in w for w in res.warnings) and "finding-099" not in res.explanations
    expl2 = {"finding-001": {"x": 5}}
    assert review.build_report_ex(article, findings, expl2, corpus, now=NOW).report is None


def test_percentage_env_is_ignored_with_note(article, corpus, monkeypatch):
    monkeypatch.setenv("PRESS_DISPUTE_ENABLE_PERCENTAGE", "1")
    r = run(article, corpus).report
    assert any("PRESS_DISPUTE_ENABLE_PERCENTAGE" in n for n in r["coverage_notes"])
    assert "%" not in review.render.render_table(r).replace("PRESS_DISPUTE_ENABLE_PERCENTAGE", "")


# ---- MCP 도구 함수 흐름 -------------------------------------------------------------------

class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_tool_flow_plaintext_to_explanation(corpus):
    clock = Clock()
    store = EphemeralStore(ttl_seconds=1800, clock=clock)
    txt = ("회사 돈 빼돌린 대표, 경찰 수사\n\n경찰, 횡령 혐의로 사실관계 확인\n\n경찰은 A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다.\n\n"
           "김씨는 본지에 “횡령한 사실이 없다”고 말했다.\n")
    prep = review.prepare_article(store, text=txt, publication_date="2026-10-05", genre="news")
    assert prep["ok"] and prep["hash_mode"] == "cjson-1" and len(prep["article_sha256"]) == 64
    assert [b["block_id"] for b in prep["blocks"]] == ["title", "subtitle", "p1", "p2"]
    assert all(b["length_cp"] == len(b["text"]) for b in prep["blocks"])
    assert prep["expires_in_seconds"] == 1800

    art = store.get(f"article:{prep['article_handle']}")["article"]
    findings, expl = MockProvider().analyze(art, corpus)
    out = review.submit_report(store, corpus, article_handle=prep["article_handle"], findings=findings, explanations=expl, now=NOW)
    assert out["ok"] and out["report_id"].startswith("rpt-") and "별(★)은 실제 분쟁 발생 확률이 아니라" in out["table"]
    assert out["hidden_finding_count"] == 0 and out["expires_at"] == "2026-10-08T12:30:00+09:00"

    exp = review.explain_finding(store, report_id=out["report_id"], finding_id="finding-001")
    assert exp["ok"] and "적용 쟁점" in exp["markdown"]
    assert review.explain_finding(store, report_id=out["report_id"], finding_id="finding-009")["ok"] is False
    clock.t += 1801
    gone = review.explain_finding(store, report_id=out["report_id"], finding_id="finding-001")
    assert gone["ok"] is False and "다시 제출" in gone["message"]
    again = review.submit_report(store, corpus, article_handle=prep["article_handle"], findings=findings, now=NOW)
    assert again["ok"] is False and "expired" in again["errors"][0]


def test_prepare_article_object_and_errors(article):
    store = EphemeralStore()
    ok = review.prepare_article(store, article=article, genre="opinion", publication_date="2026-10-06")
    assert ok["ok"]
    stored = store.get(f"article:{ok['article_handle']}")["article"]
    assert stored["context"]["genre"] == "opinion" and stored["publication_date"] == "2026-10-06"
    assert article["context"]["genre"] == "news"                           # 입력 객체는 건드리지 않는다
    assert review.prepare_article(store)["ok"] is False
    assert review.prepare_article(store, text="a", article=article)["ok"] is False
    assert review.prepare_article(store, article={"blocks": []})["ok"] is False
    assert review.prepare_article(store, text="  ")["ok"] is False
    assert review.prepare_article(store, text="제목만")["ok"] is False      # 본문 없음


def test_submit_failure_lists_errors_and_stores_nothing(article, corpus):
    store = EphemeralStore()
    h = review.prepare_article(store, article=article)["article_handle"]
    findings, _ = MockProvider().analyze(article, corpus)
    findings[0]["span"]["end"] = 999
    out = review.submit_report(store, corpus, article_handle=h, findings=findings, now=NOW)
    assert out["ok"] is False and any("findings[0]" in e for e in out["errors"])
    assert len(store) == 1                                                  # 기사만 있고 보고서는 저장되지 않음


def test_report_json_roundtrips_through_json(article, corpus):
    r = run(article, corpus).report
    assert json.loads(json.dumps(r, ensure_ascii=False)) == r
