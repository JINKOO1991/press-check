"""Codex 1회차(gpt-6-astra) 지적 CX1-2·3·7·8·9 회귀 테스트. 합성 데이터, 네트워크 없음."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_eng_counter_stance import build  # noqa: E402

from press_review import corpus_sqlite as cs  # noqa: E402
from press_review import edit_checks as ec  # noqa: E402
from press_review import review  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def err_codes(issues):
    return [i.code for i in issues if i.level == "error"]


def run_edit(article, bid, old, new):
    t = {b["block_id"]: b["text"] for b in article["blocks"]}[bid]
    s = t.index(old)
    return ec.check_replacement(old, new, article, block_id=bid, start=s)


# ---- CX1-2: 직접인용 원문 그대로(공백 포함)
def test_cx1_2_whole_quote_with_changed_spacing_is_an_error(mk_article):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "그는 “아버지가 방에 들어갔다.”라고 말했다.")])
    issues = run_edit(a, "p1", "“아버지가 방에 들어갔다.”", "“아버지 가방에 들어갔다.”")
    assert "DIRECT_QUOTE_CHANGED" in err_codes(issues) or "NEW_QUOTE" in err_codes(issues)
    # 인용 바깥만 바꾸면 오류 없음
    assert err_codes(run_edit(a, "p1", "“아버지가 방에 들어갔다.”라고", "“아버지가 방에 들어갔다.”고")) == []
    # 인용을 그대로 두고 앞 문구만 바꾼 수정도 오류 없음
    assert err_codes(run_edit(a, "p1", "그는 “아버지가 방에 들어갔다.”", "그가 “아버지가 방에 들어갔다.”")) == []


def test_cx1_2_new_quote_check_is_exact_not_whitespace_insensitive(mk_article):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "그는 “아버지가 방에 들어갔다”고 했고, 어머니가 웃었다.")])
    assert err_codes(run_edit(a, "p1", "어머니가 웃었다", "“아버지 가방에 들어갔다”")) == ["NEW_QUOTE"]


# ---- CX1-3: 블록을 넘는 인용, 중첩 인용
def test_cx1_3_quote_spanning_blocks_is_tracked(mk_article):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "그는 “조사를 받았다."), ("p2", "body", "돈을 받은 적은 없다.”라고 말했다.")])
    assert "DIRECT_QUOTE_CHANGED" in err_codes(run_edit(a, "p2", "없다", "있다"))
    assert "DIRECT_QUOTE_CHANGED" in err_codes(run_edit(a, "p1", "조사를", "수사를"))
    assert err_codes(run_edit(a, "p2", "라고 말했다", "라고 밝혔다")) == []          # 인용 바깥은 그대로 허용


def test_cx1_3_unclosed_quote_does_not_swallow_later_blocks(mk_article):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "그는 “끝나지 않은 말"), ("p2", "body", "중간 문단이다."),
                    ("p3", "body", "그는 “무죄”라고 말했다.")])
    assert err_codes(run_edit(a, "p2", "중간", "다음")) == []


def test_cx1_3_direct_quote_nested_in_single_quote(mk_article):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "‘그가 “무죄”라고 말했다’는 보도가 나왔다.")])
    assert "DIRECT_QUOTE_CHANGED" in err_codes(run_edit(a, "p1", "무죄", "유죄"))


def test_cx1_3_scan_quotes_nesting():
    assert (0, 15, "single") in ec.scan_quotes("‘그가 “무죄”라고 말했다’ 끝")
    assert ec.quote_regions("“바깥 ‘안’ 바깥”") == [(0, 11, "direct")]          # 기존 계약(바깥 구간 하나) 유지


# ---- CX1-7: submit_report 구조 검증
def _finding(**se):
    return {"finding_id": "finding-001", "span": {"block_id": "p1", "start": 0, "end": 2, "quote": "경찰"}, "priority_stars": 2,
            "risk_channels": ["civil"], "issue_category": "unsupported_assertion", "review_confidence": "medium", "reason_short": "확인하세요.",
            "suggested_edit": dict({"mode": "replace", "replacement": "검찰"}, **se), "evidence": [], "counter_considerations": []}


@pytest.mark.parametrize("se", [{"related_edits": 1}, {"related_edits": "x"}, {"related_edits": [1]}, {"required_verification": 5},
                                {"required_verification": None}, {"related_edits": None}])
def test_cx1_7_malformed_finding_fields_return_errors_not_exceptions(corpus, mk_article, se):
    a = mk_article([("title", "title", "제목"), ("p1", "body", "경찰이 수사 중이다.")])
    res = review.build_report_ex(a, [_finding(**se)], {}, corpus, now=NOW)
    assert res.report is None and res.errors and any("finding-001" in e for e in res.errors)


# ---- CX1-8: counter 신호
def test_cx1_8_reversal_and_negated_immunity_are_not_counter_signals():
    f = cs.counter_signals
    assert f("judgment", [], "원고 패소 부분을 파기하고 환송한다.") == []
    assert f("judgment", [], "원심판결 중 원고 패소 부분을 파기한다") == []
    assert f("judgment", ["공익성 부정"], None) == []
    assert f("judgment", ["상당성이 인정되지 않음"], None) == []
    assert f("judgment", ["진실성 불인정"], None) == []
    # 통제: 신호는 그대로 남는다
    assert f("judgment", ["공익성"], None) == ["tag:공익성"]
    assert f("judgment", [], "원고의 청구를 기각한다")
    assert f("judgment", [], "원심판결을 파기한다. 원고의 청구를 기각한다")


# ---- CX1-9: 후보 상한보다 counter 우선
@pytest.mark.parametrize("fts", [True, False])
def test_cx1_9_counter_survives_candidate_cap(tmp_path, monkeypatch, fts):
    rows = [(f"p{i:03d}", "judgment", "judgment", "원고 승소", f"정정보도 청구 사건에서 법원이 판단하였다. 순번 {i}")      # 본문이 같으면 색인이 하나만 남긴다
            for i in range(1, 202)]
    rows[-1] = ("p201", "judgment", "judgment", "원고의 청구를 기각한다", "정정보도 청구 사건에서 법원이 판단하였다. 순번 201")
    idx, ids = build(tmp_path, rows, monkeypatch, fts=fts)
    try:
        got = [h["passage_id"] for h in idx.search("정정보도 청구 사건", stance="counter", limit=5)]
        assert got[0] == ids["p201"], got
    finally:
        idx.close()
