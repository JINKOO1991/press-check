"""적대 검증에서 확인된 결함의 회귀 테스트(V1 직접인용 전체 교체, V3 별 점수 float, V4 block_id 비문자열, V5 전각 따옴표)."""
import copy
import datetime as dt

import pytest

import pdr_engine_paths  # noqa: F401
from press_review import edit_checks as ec, review
from press_review.providers import MockProvider
from press_review.store import EphemeralStore

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
BODY = "검찰은 김 씨가 돈을 받았다고 밝혔다. 김 씨는 “나는 돈을 받지 않았다”고 반박했다."


def make_article(blocks, article_id="t-001", date="2026-10-05"):
    return {
        "schema_version": "0.1", "article_id": article_id, "publication_date": date,
        "blocks": [{"block_id": b, "section": s, "text": t} for b, s, t in blocks],
        "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False},
    }


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


def _art(body=BODY):
    return make_article([("title", "title", "제목"), ("p1", "body", body)])


def _check(quote, rep, body=BODY):
    return ec.check_replacement(quote, rep, _art(body), block_id="p1", start=body.index(quote))


# ---- V1: 수정 구간이 직접인용 전체를 덮는 경우 --------------------------------------------------

def test_v1_whole_quote_replaced_with_narration_text_is_error():
    issues = _check("“나는 돈을 받지 않았다”", "“돈을 받았다”")
    assert "DIRECT_QUOTE_CHANGED" in codes(issues, "error")


def test_v1_whole_quote_replaced_with_unpaired_open_mark_is_error():
    issues = _check("“나는 돈을 받지 않았다”", "“나는 돈을 받았다")
    assert "DIRECT_QUOTE_CHANGED" in codes(issues, "error")


def test_v1_whole_quote_kept_verbatim_inside_longer_replacement_is_ok():
    issues = _check("“나는 돈을 받지 않았다”고 반박했다", "“나는 돈을 받지 않았다”며 반박했다")
    assert codes(issues, "error") == []


def test_v1_whole_quote_dropped_is_ok():
    issues = _check("“나는 돈을 받지 않았다”고 반박했다", "반박했다")
    assert "DIRECT_QUOTE_CHANGED" not in codes(issues, "error")


def test_v1_same_inner_text_with_different_marks_is_ok():
    issues = _check("“나는 돈을 받지 않았다”", "「나는 돈을 받지 않았다」")
    assert codes(issues, "error") == []


def test_v1_whole_quote_with_inner_text_changed_is_error():
    issues = _check("“나는 돈을 받지 않았다”", "“나는 돈을 받지 않았다고 했다”")
    assert "DIRECT_QUOTE_CHANGED" in codes(issues, "error")


# ---- V5: 전각·각종 따옴표 -------------------------------------------------------------------

@pytest.mark.parametrize("o,c", [("＂", "＂"), ("〝", "〞"), ("«", "»")])
def test_v5_quote_regions_recognize_marks(o, c):
    t = f"그는 {o}가나다{c}라고 했다"
    assert [(s, e, k) for s, e, k in ec.quote_regions(t)] == [(3, 8, "direct")]


@pytest.mark.parametrize("o,c", [("＂", "＂"), ("〝", "〞"), ("«", "»")])
def test_v5_made_up_quote_in_fullwidth_marks_is_new_quote_error(o, c):
    body = "검찰은 김 씨가 돈을 받았다고 밝혔다. 김 씨는 반박했다."
    issues = _check("반박했다", f"{o}나는 횡령했다{c}며 반박했다", body)
    assert "NEW_QUOTE" in codes(issues, "error")


# ---- V3 / V4: submit_report 가 내부 오류 없이 처리한다 ---------------------------------------------

def _prepared(article, corpus):
    store = EphemeralStore()
    h = review.prepare_article(store, article=article)["article_handle"]
    return store, h, copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])


def test_v3_integral_float_stars_do_not_crash_or_orphan(article, corpus):
    store, h, f = _prepared(article, corpus)
    f["priority_stars"] = 2.0
    n0 = len(store)
    out = review.submit_report(store, corpus, article_handle=h, findings=[f], now=NOW)
    assert out["ok"] is True and "★★☆" in out["table"]
    assert len(store) == n0 + 1


def test_v3_non_integral_stars_rejected_cleanly(article, corpus):
    store, h, f = _prepared(article, corpus)
    f["priority_stars"] = 2.5
    n0 = len(store)
    out = review.submit_report(store, corpus, article_handle=h, findings=[f], now=NOW)
    assert out["ok"] is False and len(store) == n0


def test_v4_non_string_block_id_is_finding_level_error(article, corpus):
    store, h, f = _prepared(article, corpus)
    f["span"]["block_id"] = ["p1"]
    out = review.submit_report(store, corpus, article_handle=h, findings=[f], now=NOW)
    assert out["ok"] is False and any("findings[0]" in e for e in out["errors"])


def test_v4_non_string_block_id_in_related_edit_is_finding_level_error(article, corpus):
    store, h, f = _prepared(article, corpus)
    f["suggested_edit"]["related_edits"] = [{"span": {"block_id": {"x": 1}, "start": 0, "end": 2, "quote": "검찰"}, "replacement": "경찰"}]
    out = review.submit_report(store, corpus, article_handle=h, findings=[f], now=NOW)
    assert out["ok"] is False and any("findings[0]" in e for e in out["errors"])
