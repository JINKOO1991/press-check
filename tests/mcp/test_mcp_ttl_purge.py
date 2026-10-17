"""V-SEC-02/03: 도구 호출마다 만료분을 지우고, 만료된 원고의 로그 색인도 지운다."""
import datetime as dt

from press_review import logsafe
from press_review.mcp_server import SUPPORTED_PROTOCOL_VERSIONS, Server
from test_mcp_server import Clock, NOW, rpc, tool


def test_any_tool_call_after_expiry_purges_manuscript_and_log_index(article_obj):
    clock = Clock()
    srv = Server(clock=clock, now_fn=lambda: NOW)
    rpc(srv, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    h = tool(srv, "prepare_article", {"article": article_obj})["structuredContent"]["article_handle"]
    assert f"article:{h}" in srv.store._items and logsafe._articles
    clock.t += 1801
    assert tool(srv, "corpus_status", {})["isError"] is False
    assert not srv.store._items
    assert not logsafe._articles


def test_reaper_purges_without_a_tool_call(article_obj):
    clock = Clock()
    srv = Server(clock=clock, now_fn=lambda: NOW)
    rpc(srv, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    tool(srv, "prepare_article", {"article": article_obj})
    clock.t += 1801
    srv.reap_once()
    assert not srv.store._items and not logsafe._articles
