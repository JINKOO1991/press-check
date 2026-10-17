"""MCP 서버: subprocess 왕복, 오류 반환, 출력·로그 위생, TTL(시계 주입)."""
import copy
import datetime as dt
import io
import json

import pytest

from press_review import corpus as corpus_mod
from press_review import mcp_server, review
from press_review.mcp_server import SUPPORTED_PROTOCOL_VERSIONS, Server, serve
from press_review.providers import MockProvider
from press_review.store import EphemeralStore

TOOLS = {"prepare_article", "search_cases", "submit_report", "explain_finding", "corpus_status"}
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def sc(result):
    return result["structuredContent"]


def mock_findings(article):
    findings, explanations = MockProvider().analyze(article, corpus_mod.load_default())
    assert findings
    return copy.deepcopy(findings), copy.deepcopy(explanations)


def shingles(article, n=8):
    out = set()
    for b in article["blocks"]:
        t = b["text"]
        out |= {t[i:i + n] for i in range(len(t) - n + 1)}
    return out


def has_article_text(s: str, article) -> bool:
    return any(w in s for w in shingles(article))


# ------------------------------------------------------------------ 프로토콜

def test_initialize_echoes_supported_client_version(server):
    for v in SUPPORTED_PROTOCOL_VERSIONS:
        resp = server.request("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert resp["result"]["protocolVersion"] == v
    r = resp["result"]
    assert r["serverInfo"]["name"] == "press-review"
    assert "tools" in r["capabilities"]


def test_initialize_unknown_version_gets_latest_supported(server):
    resp = server.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]
    assert SUPPORTED_PROTOCOL_VERSIONS[0] == max(SUPPORTED_PROTOCOL_VERSIONS)


def test_initialize_requires_string_version(server):
    resp = server.request("initialize", {"capabilities": {}})
    assert resp["error"]["code"] == mcp_server.INVALID_PARAMS


def test_tools_before_initialize_rejected_but_ping_ok(server):
    assert server.request("tools/list")["error"]["code"] == mcp_server.NOT_INITIALIZED
    assert server.request("ping")["result"] == {}


def test_tools_list(ready):
    tools = ready.request("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == TOOLS
    for t in tools:
        s = t["inputSchema"]
        assert s["type"] == "object" and "properties" in s
        assert not ({"oneOf", "anyOf", "allOf"} & set(s))         # Anthropic API는 최상위 조합 키워드를 거부한다
        assert t["description"]
    by = {t["name"]: t for t in tools}
    assert by["submit_report"]["inputSchema"]["required"] == ["article_handle", "findings"]
    ev = by["submit_report"]["inputSchema"]["properties"]["findings"]["items"]["properties"]["evidence"]["items"]
    assert ev["required"] == ["source_id", "passage_id"]
    assert "detailed_explanation_ref" not in by["submit_report"]["inputSchema"]["properties"]["findings"]["items"]["properties"]


def test_unknown_method_and_unknown_tool_are_jsonrpc_errors(ready):
    assert ready.request("resources/list")["error"]["code"] == mcp_server.METHOD_NOT_FOUND
    resp = ready.request("tools/call", {"name": "delete_everything", "arguments": {}})
    assert resp["error"]["code"] == mcp_server.INVALID_PARAMS
    resp = ready.request("tools/call", {"name": "corpus_status", "arguments": [1]})
    assert resp["error"]["code"] == mcp_server.INVALID_PARAMS


def test_parse_error_and_recovery(ready):
    ready.send_raw(b"{not json\n")
    resp = ready.read_response()
    assert resp["error"]["code"] == mcp_server.PARSE_ERROR and resp["id"] is None
    ready.send_raw(b"\n   \n")                                     # 빈 줄은 무시
    ready.send_raw(b'{"jsonrpc":"1.0","id":9,"method":"ping"}\n')
    assert ready.read_response()["error"]["code"] == mcp_server.INVALID_REQUEST
    assert ready.request("ping")["result"] == {}                  # 계속 동작


def test_notifications_get_no_response(ready):
    ready.notify("notifications/cancelled", {"requestId": 99})
    ready.notify("some/unknown_notification")
    assert ready.request("ping")["result"] == {}                  # 앞선 알림에 응답이 끼어들지 않았다


def test_clean_exit_on_stdin_close(ready):
    assert ready.close() == 0


# ------------------------------------------------------------------ 왕복

def test_full_roundtrip(ready, article_obj):
    prep = ready.call("prepare_article", {"article": article_obj, "publication_date": "2026-10-05"})
    assert prep["isError"] is False
    p = sc(prep)
    assert p["ok"] and p["article_handle"].startswith("art-") and p["hash_mode"] == "cjson-1"
    assert [b["block_id"] for b in p["blocks"]] == ["title", "subtitle", "p1", "p2"]
    assert p["blocks"][0]["length_cp"] == len(article_obj["blocks"][0]["text"])
    assert json.loads(prep["content"][0]["text"])["article_handle"] == p["article_handle"]

    search = ready.call("search_cases", {"query": "기사 전체 인상 제목 본문 단정 혐의 수사", "authority_types": ["judgment"], "risk_channel": "civil", "limit": 3})
    s = sc(search)
    assert s["ok"] and s["count"] >= 1 and s["is_fixture"] is True
    assert all(x["is_fixture"] for x in s["passages"])
    none = sc(ready.call("search_cases", {"query": "zzzqqq"}))
    assert none["ok"] and none["count"] == 0 and "만들지" in none["message"]

    findings, explanations = mock_findings(article_obj)
    sub = ready.call("submit_report", {"article_handle": p["article_handle"], "findings": findings, "explanations": explanations})
    assert sub["isError"] is False, sub
    r = sc(sub)
    assert r["report_id"].startswith("rpt-") and "★★★" in r["table"]
    assert r["table"] in sub["content"][0]["text"]
    assert "확률이 아니라 검토 우선순위" in r["table"]

    exp = ready.call("explain_finding", {"report_id": r["report_id"], "finding_id": findings[0]["finding_id"]})
    assert exp["isError"] is False and exp["content"][0]["text"] == sc(exp)["markdown"]
    assert "적용 쟁점" in exp["content"][0]["text"]

    st = sc(ready.call("corpus_status", {}))
    assert st["ok"] and st["is_fixture"] is True and st["passage_count"] > 0


def test_plaintext_prepare_roundtrip(ready):
    txt = "회사 돈 빼돌린 대표, 경찰 수사\n\n경찰, 횡령 혐의로 사실관계 확인\n\n경찰은 A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다.\n"
    p = sc(ready.call("prepare_article", {"text": txt, "publication_date": "2026-10-05"}))
    assert p["ok"] and p["hash_mode"] == "cjson-1"
    assert [b["section"] for b in p["blocks"]] == ["title", "subtitle", "body"]


def test_prepare_requires_exactly_one_of_text_or_article(ready, article_obj):
    for args in ({}, {"text": "a", "article": article_obj}):
        res = ready.call("prepare_article", args)
        assert res["isError"] is True and sc(res)["ok"] is False


# ------------------------------------------------------------------ 오류 반환

@pytest.fixture()
def handle(ready, article_obj):
    return sc(ready.call("prepare_article", {"article": article_obj}))["article_handle"]


def submit(ready, handle, findings, **kw):
    return ready.call("submit_report", {"article_handle": handle, "findings": findings, **kw})


def test_wrong_quote_is_rejected(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    f[0]["span"]["quote"] = "틀린 인용 문구"
    res = submit(ready, handle, f)
    assert res["isError"] is True
    assert any("quote does not equal" in e for e in sc(res)["errors"])


def test_unknown_passage_id_is_rejected(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    f[0]["evidence"] = [{"source_id": f[0]["evidence"][0]["source_id"], "passage_id": "no-such-passage"}]
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("not in the corpus" in e for e in sc(res)["errors"])


def test_model_supplied_verified_cannot_vouch_for_invented_evidence(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    f[0]["evidence"] = [{"source_id": "law-prec-000", "passage_id": "x", "url": "https://www.law.go.kr/made-up", "verified": True}]
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("not in the corpus" in e for e in sc(res)["errors"])


def test_model_supplied_verified_flag_is_overridden_by_server(ready, handle, article_obj):
    # 엔진 계약(tests/engine/test_eng_pipeline.py): 코퍼스에 있는 근거면 모델의 verified 값은 버리고 서버가 true 를 채운다.
    f, _ = mock_findings(article_obj)
    f[0]["evidence"][0]["verified"] = False
    res = submit(ready, handle, f)
    assert res["isError"] is False
    f[0]["evidence"][0]["probability"] = 0.9                      # 알 수 없는 키는 오류
    assert submit(ready, handle, f)["isError"] is True


def test_url_differing_from_corpus_is_rejected(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    f[0]["evidence"][0]["url"] = "https://www.law.go.kr/made-up"
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("url differs" in e for e in sc(res)["errors"])


def test_change_inside_direct_quote_is_rejected(ready, handle, article_obj):
    p2 = next(b["text"] for b in article_obj["blocks"] if b["block_id"] == "p2")
    inner = "횡령한 사실이 없다"
    start = p2.index(inner)
    f, _ = mock_findings(article_obj)
    f[0]["span"] = {"block_id": "p2", "start": start, "end": start + len(inner), "quote": inner}
    f[0]["suggested_edit"] = {"mode": "replace", "replacement": "횡령한 사실이 있다", "related_edits": [], "required_verification": []}
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("DIRECT_QUOTE_CHANGED" in e for e in sc(res)["errors"])
    # 인용 전체를 span으로 잡고 안의 말을 바꾸면 새 인용으로 취급해 거부한다
    whole = "“횡령한 사실이 없다”"
    st = p2.index(whole)
    f[0]["span"] = {"block_id": "p2", "start": st, "end": st + len(whole), "quote": whole}
    f[0]["suggested_edit"]["replacement"] = "“횡령한 사실이 일부 있다”"
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("NEW_QUOTE" in e for e in sc(res)["errors"])


def test_new_number_in_replacement_is_rejected(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    f[0]["suggested_edit"] = {"mode": "replace", "replacement": "회사 돈 3억 원 빼돌린 혐의…경찰, 대표 수사", "related_edits": [], "required_verification": []}
    res = submit(ready, handle, f)
    assert res["isError"] is True and any("NEW_NUMBER" in e for e in sc(res)["errors"])


def test_unknown_handle_and_unknown_report(ready, handle, article_obj):
    f, _ = mock_findings(article_obj)
    res = submit(ready, "art-doesnotexist", f)
    assert res["isError"] is True and "prepare_article" in sc(res)["errors"][0]
    res = ready.call("explain_finding", {"report_id": "rpt-nope", "finding_id": "finding-001"})
    assert res["isError"] is True
    f, e = mock_findings(article_obj)
    rep = sc(submit(ready, handle, f, explanations=e))["report_id"]
    res = ready.call("explain_finding", {"report_id": rep, "finding_id": "finding-099"})
    assert res["isError"] is True and "finding-099" in res["content"][0]["text"]


def test_invalid_arguments_are_tool_errors_not_crashes(ready):
    cases = [
        ("search_cases", {}),
        ("search_cases", {"query": "x", "limit": 99}),
        ("search_cases", {"query": "x", "risk_channel": "nope"}),
        ("search_cases", {"query": "x", "as_of": "2026-13-45"}),
        ("search_cases", {"query": "x", "extra": 1}),
        ("submit_report", {"findings": []}),
        ("submit_report", {"article_handle": "a", "findings": "no"}),
        ("explain_finding", {"report_id": 5, "finding_id": "x"}),
        ("prepare_article", {"text": 5}),
    ]
    for name, args in cases:
        res = ready.call(name, args)
        assert res["isError"] is True and sc(res)["ok"] is False, (name, args)
    assert ready.request("ping")["result"] == {}


def test_enum_error_does_not_echo_input_value(ready):
    secret = "취재원이 알려준 비공개 사실 문장입니다"
    res = ready.call("search_cases", {"query": "x", "risk_channel": secret})
    assert res["isError"] is True and secret not in json.dumps(res, ensure_ascii=False)


# ------------------------------------------------------------------ 출력·로그 위생

def test_stdout_is_protocol_only_and_no_article_text_leaks(server, article_obj):
    server.initialize()
    server.send_raw(b"garbage line\n")
    server.read_response()
    handle = sc(server.call("prepare_article", {"article": article_obj}))["article_handle"]
    server.call("search_cases", {"query": "혐의 수사 제목 본문"})
    f, e = mock_findings(article_obj)
    ok = server.call("submit_report", {"article_handle": handle, "findings": f, "explanations": e})
    rid = sc(ok)["report_id"]
    server.call("explain_finding", {"report_id": rid, "finding_id": f[0]["finding_id"]})
    bad = copy.deepcopy(f)
    bad[0]["span"]["quote"] = "틀린 인용 문구입니다"
    server.call("submit_report", {"article_handle": handle, "findings": bad})
    bad = copy.deepcopy(f)
    bad[0]["evidence"] = [{"source_id": "x", "passage_id": "y"}]
    server.call("submit_report", {"article_handle": handle, "findings": bad})
    server.call("submit_report", {"article_handle": "art-gone", "findings": f})
    server.call("corpus_status", {})
    server.request("tools/call", {"name": "nope", "arguments": {}})
    server.close()

    lines = server.stdout_lines
    assert lines
    for raw in lines:                                              # 비프로토콜 출력 0줄
        assert raw.endswith(b"\n") and raw.count(b"\n") == 1
        msg = json.loads(raw.decode("utf-8"))
        assert msg["jsonrpc"] == "2.0" and ("result" in msg or "error" in msg)

    err = server.stderr_text
    assert err.strip()                                             # 시작·호출 로그는 있다
    assert not has_article_text(err, article_obj)
    assert "tool_call" in err and "tool=submit_report" in err

    # 원문이 나와야 하는 응답(등록 echo, 성공 표, 상세 설명)을 뺀 stdout 전체에는 기사 8자 조각이 없다.
    checked = 0
    for raw in lines:
        msg = json.loads(raw.decode("utf-8"))
        res = msg.get("result", {})
        if res.get("isError") is False and res.get("structuredContent", {}).get("ok") and (
            "blocks" in res["structuredContent"] or "table" in res["structuredContent"] or "markdown" in res["structuredContent"]
        ):
            continue
        checked += 1
        assert not has_article_text(raw.decode("utf-8"), article_obj), raw[:200]
    assert checked >= 8


def test_internal_exception_returns_generic_error_without_text(article_obj, monkeypatch, caplog):
    leak = "비공개 원고 문장이 예외 메시지에 섞인 경우"
    srv = Server(clock=lambda: 0.0, now_fn=lambda: NOW)
    srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]}})
    handle = srv.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "prepare_article", "arguments": {"article": article_obj}}})
    h = handle["result"]["structuredContent"]["article_handle"]

    def boom(*a, **k):
        raise RuntimeError(leak)

    monkeypatch.setattr(review, "submit_report", boom)
    import logging
    buf = io.StringIO()
    handler = mcp_server.logsafe.make_handler(buf)
    lg = logging.getLogger("press_review.mcp")
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        resp = srv.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "submit_report", "arguments": {"article_handle": h, "findings": []}}})
    finally:
        lg.removeHandler(handler)
    out = json.dumps(resp, ensure_ascii=False)
    assert resp["result"]["isError"] is True and "RuntimeError" in out and leak not in out
    assert leak not in buf.getvalue() and "RuntimeError" in buf.getvalue()


def test_stray_print_does_not_reach_protocol_channel(tmp_path):
    # main()은 sys.stdout을 stderr로 돌린다: print() 한 줄이 stdout 프로토콜 스트림에 섞이지 않는다.
    import subprocess
    import sys
    from pdr_mcp_paths import ENGINE
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from press_review import mcp_server as m\n"
        "orig = m.serve\n"
        "def serve(i, o, s):\n"
        "    print('STRAY-OUTPUT')\n"
        "    orig(i, o, s)\n"
        "m.serve = serve\n"
        "sys.exit(m.main())\n" % str(ENGINE)
    )
    p = subprocess.run([sys.executable, "-c", code], input=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, timeout=30)
    assert p.returncode == 0
    assert b"STRAY-OUTPUT" not in p.stdout and b"STRAY-OUTPUT" in p.stderr
    assert json.loads(p.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_oversized_line_is_rejected_and_stream_recovers(monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_LINE_BYTES", 64)
    srv = Server(clock=lambda: 0.0)
    inp = io.BytesIO(b"x" * 200 + b"\n" + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    out = io.BytesIO()
    serve(inp, out, srv)
    first, second = [json.loads(l) for l in out.getvalue().splitlines()]
    assert first["error"]["code"] == mcp_server.INVALID_REQUEST
    assert second == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ------------------------------------------------------------------ TTL(시계 주입)

class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def rpc(srv, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return srv.handle_message(msg)


def tool(srv, name, args):
    r = rpc(srv, "tools/call", {"name": name, "arguments": args})
    return r["result"]


def test_default_ttl_is_1800_seconds():
    srv = Server(clock=Clock())
    assert srv.store.ttl_seconds == 1800 and isinstance(srv.store, EphemeralStore)


def test_explain_finding_refused_after_ttl(article_obj):
    clock = Clock()
    srv = Server(clock=clock, now_fn=lambda: NOW)
    rpc(srv, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    h = tool(srv, "prepare_article", {"article": article_obj})["structuredContent"]["article_handle"]
    f, e = mock_findings(article_obj)
    sub = tool(srv, "submit_report", {"article_handle": h, "findings": f, "explanations": e})
    assert sub["isError"] is False
    assert sub["structuredContent"]["expires_at"] == "2026-10-08T12:30:00+09:00"
    rid = sub["structuredContent"]["report_id"]

    clock.t += 1799
    assert tool(srv, "explain_finding", {"report_id": rid, "finding_id": "finding-001"})["isError"] is False
    clock.t += 1                                                    # submit 시점에서 정확히 1800초: 만료(explain 성공은 TTL을 연장하지 않는다)
    res = tool(srv, "explain_finding", {"report_id": rid, "finding_id": "finding-001"})
    assert res["isError"] is True
    assert "다시 제출" in res["content"][0]["text"]
    assert len(srv.store) == 0                                      # 만료분은 메모리에서도 사라졌다


def test_article_handle_expires_and_submit_is_refused(article_obj):
    clock = Clock()
    srv = Server(clock=clock, now_fn=lambda: NOW)
    rpc(srv, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    h = tool(srv, "prepare_article", {"article": article_obj})["structuredContent"]["article_handle"]
    f, _ = mock_findings(article_obj)
    clock.t += 1800
    res = tool(srv, "submit_report", {"article_handle": h, "findings": f})
    assert res["isError"] is True and "prepare_article" in res["structuredContent"]["errors"][0]


def test_server_writes_nothing_to_disk(article_obj, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    srv = Server(clock=Clock(), now_fn=lambda: NOW)
    rpc(srv, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    h = tool(srv, "prepare_article", {"article": article_obj})["structuredContent"]["article_handle"]
    f, e = mock_findings(article_obj)
    tool(srv, "submit_report", {"article_handle": h, "findings": f, "explanations": e})
    assert sorted(tmp_path.rglob("*")) == before
