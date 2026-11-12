"""submit_report 의 구간 좌표 보정(UDEC-33): quote 가 그 블록에 정확히 한 번 나오면 start/end 를 고치고, 아니면 기존 거절 경로."""
import copy
import datetime as dt

import pdr_engine_paths  # noqa: F401
from press_review import review
from press_review.providers import MockProvider
from press_review.store import EphemeralStore

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
TAG = "[좌표보정]"


def _setup(article, corpus, p1_text, quote):
    """p1 블록 text 를 바꾼 사본 기사를 올리고, span 이 p1 의 quote 를 가리키는 finding 을 돌려준다(좌표는 호출자가 정한다)."""
    art = copy.deepcopy(article)
    for b in art["blocks"]:
        if b["block_id"] == "p1":
            b["text"] = p1_text
    store = EphemeralStore()
    h = review.prepare_article(store, article=art)["article_handle"]
    f = copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])
    f["span"] = {"block_id": "p1", "start": 0, "end": len(quote), "quote": quote}
    f["suggested_edit"]["related_edits"] = []
    return store, h, f


def _submit(store, corpus, h, f):
    return review.submit_report(store, corpus, article_handle=h, findings=[f], now=NOW)


def _stored_span(store, out):
    return store.get(f"report:{out['report_id']}")["report"]["findings"][0]["span"]


def _first_quote(article, corpus):
    return MockProvider().analyze(article, corpus)[0][0]["span"]["quote"]


def test_wrong_coordinates_single_occurrence_is_repaired(article, corpus):
    q = _first_quote(article, corpus)
    text = "앞 문장이다. " + q + " 뒤 문장이다."
    store, h, f = _setup(article, corpus, text, q)
    f["span"].update(start=148, end=153)
    out = _submit(store, corpus, h, f)
    pos = text.index(q)
    assert out["ok"] is True
    assert _stored_span(store, out)["start"] == pos and _stored_span(store, out)["end"] == pos + len(q)
    assert any(TAG in w and f"[148,153)" in w and f"[{pos},{pos + len(q)})" in w and f["finding_id"] in w for w in out["warnings"])
    assert not any(q in w for w in out["warnings"])
    assert f["span"]["start"] == 148             # 호출자 입력은 바뀌지 않는다


def test_repair_warning_does_not_echo_manuscript_text_in_finding_id(article, corpus):
    """호스트가 finding_id 에 원고 구절을 넣어도 보정 알림에는 원문이 나오지 않는다(서버가 만든 findings[i] 로 가리킨다)."""
    q = _first_quote(article, corpus)
    text = "앞 문장이다. " + q + " 뒤 문장이다."
    store, h, f = _setup(article, corpus, text, q)
    f["finding_id"] = q
    f["span"].update(start=148, end=153)
    out = _submit(store, corpus, h, f)
    assert out["ok"] is True
    fixes = [w for w in out["warnings"] if TAG in w]
    assert fixes and all("findings[0]" in w for w in fixes)
    assert not any(q in w for w in out["warnings"])


def test_quote_twice_keeps_original_rejection(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, q + " / " + q, q)
    f["span"].update(start=1, end=1 + len(q))
    out = _submit(store, corpus, h, f)
    assert out["ok"] is False and any("quote does not equal block text" in e for e in out["errors"])


def test_quote_absent_keeps_original_rejection(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, "전혀 다른 문장이 길게 이어진다. " * 3, q)
    out = _submit(store, corpus, h, f)
    assert out["ok"] is False and any("quote does not equal block text" in e for e in out["errors"])


def test_overlapping_occurrences_count_as_ambiguous(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, "가가가가", "가가가")
    f["span"].update(start=0, end=2)
    out = _submit(store, corpus, h, f)
    assert out["ok"] is False and any("quote does not equal block text" in e for e in out["errors"])


def test_astral_prefix_uses_code_point_offsets(article, corpus):
    q = _first_quote(article, corpus)
    text = "😀😀" + q + " 끝."
    store, h, f = _setup(article, corpus, text, q)
    f["span"].update(start=4, end=4 + len(q))        # UTF-16 단위로 센 값
    out = _submit(store, corpus, h, f)
    assert out["ok"] is True
    assert (_stored_span(store, out)["start"], _stored_span(store, out)["end"]) == (2, 2 + len(q))


def test_related_edit_span_is_repaired(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, "앞 " + q + " 뒤", q)
    f["span"].update(start=2, end=2 + len(q))
    sub = {"block_id": "p2", "start": 0, "end": 1, "quote": "김씨"}
    f["suggested_edit"]["related_edits"] = [{"span": sub, "replacement": "A씨"}]
    out = _submit(store, corpus, h, f)
    assert out["ok"] is True
    rel = store.get(f"report:{out['report_id']}")["report"]["findings"][0]["suggested_edit"]["related_edits"][0]["span"]
    assert (rel["start"], rel["end"]) == (0, 2)
    assert any(TAG in w and "related_edits[0]" in w and "[0,1)" in w and "[0,2)" in w for w in out["warnings"])


def test_correct_coordinates_produce_no_notice(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, "앞 " + q + " 뒤", q)
    f["span"].update(start=2, end=2 + len(q))
    out = _submit(store, corpus, h, f)
    assert out["ok"] is True and not any(TAG in w for w in out["warnings"])


def test_non_integer_coordinates_are_not_repaired(article, corpus):
    q = _first_quote(article, corpus)
    for bad in ("2", 2.0, True, None):
        store, h, f = _setup(article, corpus, "앞 " + q + " 뒤", q)
        f["span"].update(start=bad, end=2 + len(q))
        out = _submit(store, corpus, h, f)
        assert out["ok"] is False, bad
        assert not any(TAG in w for w in out.get("warnings", [])), bad


def test_end_zero_is_repaired(article, corpus):
    q = _first_quote(article, corpus)
    store, h, f = _setup(article, corpus, "앞 " + q + " 뒤", q)
    f["span"].update(start=0, end=0)
    out = _submit(store, corpus, h, f)
    assert out["ok"] is True
    assert (_stored_span(store, out)["start"], _stored_span(store, out)["end"]) == (2, 2 + len(q))
