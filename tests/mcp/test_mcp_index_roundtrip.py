"""실제 색인(합성 장부에서 build_index 로 만든 SQLite) 위에서 search_cases -> submit_report -> explain_finding 왕복."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import corpus_sqlite  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def sc(r):
    return r["structuredContent"]


@pytest.fixture()
def live(tmp_path, mcp_client_cls):
    data = tmp_path / "data"
    sl.build_ledger(data / "corpus")
    built = corpus_sqlite.build_index(data / "corpus" / "manifest.sqlite", data / "index", now=NOW)
    c = mcp_client_cls(tmp_path)
    try:
        c.initialize()
        yield c, built
    finally:
        c.close()


def test_tool_schema_has_stance_and_institution(live):
    c, _ = live
    tools = {t["name"]: t for t in c.request("tools/list")["result"]["tools"]}
    props = tools["search_cases"]["inputSchema"]["properties"]
    assert props["stance"]["enum"] == ["support", "counter", "both"] and "supreme_court" in props["institution"]["enum"]
    assert set(tools) == {"prepare_article", "search_cases", "submit_report", "explain_finding", "corpus_status"}   # 외부 검색 도구 없음


def test_search_to_submit_roundtrip_on_real_index(live, article_obj):
    c, built = live
    st = sc(c.call("corpus_status"))
    assert st["is_fixture"] is False and st["snapshot_id"] == built["snapshot_id"] and st["passage_count"] == 7
    assert st["search_mode"] == "trigram+bigram" and st["latest_decision_date"] == "2024-11-07"

    handle = sc(c.call("prepare_article", {"article": article_obj}))["article_handle"]

    exact = sc(c.call("search_cases", {"query": "2021다20003", "limit": 3}))
    assert exact["is_fixture"] is False and exact["passages"][0]["passage_id"] == sl.J2 and exact["passages"][0]["match"] == "case_number"
    counter = sc(c.call("search_cases", {"query": "공적 인물 비판 상당한 이유", "stance": "counter", "as_of": "2026-10-05"}))
    assert counter["count"] >= 1 and counter["passages"][0]["passage_id"] == sl.J2
    top = counter["passages"][0]
    assert top["opinion_kind"] == "mixed" and top["includes_dissent"] is True and top["is_fixture"] is False
    sup = sc(c.call("search_cases", {"query": "정정보도 요건", "authority_types": ["judgment"]}))
    j1 = next(p for p in sup["passages"] if p["passage_id"] == sl.J1)
    assert j1["display_truncated"] is True and len(j1["display_text"]) == 300 and len(j1["text"]) > 300 and "citation_rule" in sup
    assert sc(c.call("search_cases", {"query": "바나나 축구장"}))["count"] == 0

    def finding(src, pid, **ev):
        return {"span": {"block_id": "title", "start": 0, "end": 18, "quote": article_obj["blocks"][0]["text"][:18]},
                "priority_stars": 2, "risk_channels": ["civil", "correction"], "issue_category": "headline_body_certainty_mismatch",
                "review_confidence": "medium", "reason_short": "제목이 본문보다 확정적입니다.",
                "suggested_edit": {"mode": "verify_first", "replacement": None, "related_edits": [], "required_verification": ["수사 단계 확인"]},
                "evidence": [{"source_id": src, "passage_id": pid, **ev}], "counter_considerations": ["공익 보도"]}

    bad = c.call("submit_report", {"article_handle": handle, "findings": [finding("lawgo_prec:900002", "lawgo_prec:900002:9999다1#c1")]})
    assert bad["isError"] is True and any("not in the corpus" in e for e in sc(bad)["errors"])
    mismatch = c.call("submit_report", {"article_handle": handle, "findings": [finding("lawgo_prec:900001", sl.J2)]})
    assert mismatch["isError"] is True
    forged = c.call("submit_report", {"article_handle": handle, "findings": [finding("lawgo_prec:900002", sl.J2, url="https://www.law.go.kr/made-up")]})
    assert forged["isError"] is True and any("url differs" in e for e in sc(forged)["errors"])

    ok = c.call("submit_report", {"article_handle": handle, "findings": [finding(top["source_id"], top["passage_id"], verified=False)]})
    assert ok["isError"] is False, ok
    r = sc(ok)
    assert "합성" not in r["table"] and "근거 미확보" not in r["table"]
    exp = c.call("explain_finding", {"report_id": r["report_id"], "finding_id": "finding-001"})
    md = exp["content"][0]["text"]
    assert sl.J2 in md and "https://www.law.go.kr/LSW/precInfoP.do?precSeq=900002" in md

    notes_probe = sc(c.call("search_cases", {"query": "x y", "stance": "maybe"}))
    assert notes_probe["ok"] is False
