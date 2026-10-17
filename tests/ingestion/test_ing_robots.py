import hashlib
from datetime import timedelta

import pytest

from conftest import ROBOTS_OK, FakeTransport
from press_review.ingestion.http import FetchFailed, RobotsDisallowed
from press_review.ingestion.robots import ALLOW_ALL, parse_robots

PAC = "https://www.pac.or.kr"
RB = PAC + "/robots.txt"


def test_parser_longest_match_wildcard_and_anchor():
    rules, any_rule, ai = parse_robots(
        "User-agent: *\nDisallow: /private/\nAllow: /private/open/\nDisallow: /*.pdf$\nDisallow: /search?q=\n")
    assert any_rule and not ai
    assert not rules.allowed("/private/x") and rules.allowed("/private/open/x")
    assert not rules.allowed("/a/b.pdf") and rules.allowed("/a/b.pdf?x=1") and rules.allowed("/a/b.pdfx")
    assert not rules.allowed("/search?q=abc") and rules.allowed("/search")


def test_parser_specific_group_wins_over_star_and_empty_disallow_means_allow():
    txt = ("User-agent: *\nDisallow: /\n\nUser-agent: press-check\nDisallow:\n\n"
           "User-agent: GPTBot\nDisallow: /\n")
    rules, _, _ = parse_robots(txt)
    assert rules.allowed("/anything")
    rules2, _, _ = parse_robots("User-agent: *\nDisallow: /\n")
    assert not rules2.allowed("/")


def test_parser_detects_ai_rag_notice_only_with_prohibition():
    naver = ("# BOT ACCESS FOR THE PURPOSES OF AI TRAINING AND RETRIEVAL-AUGMENTED GENERATION (RAG) IS STRICTLY PROHIBITED.\n"
             "User-agent: GPTBot\nDisallow: /\n")
    assert parse_robots(naver)[2] is True
    assert parse_robots("# We welcome AI training use\nUser-agent: *\nAllow: /\n")[2] is False
    assert parse_robots("# 생성형 AI 학습 목적 수집 금지\nUser-agent: *\nAllow: /\n")[2] is True


def test_missing_robots_404_allows_and_is_recorded(make_client, manifest):
    t = FakeTransport({RB: (404, b"<html>not found</html>", {}), PAC + "/x": (200, b"ok", {})})
    c = make_client(t)
    assert c.get(PAC + "/x").body == b"ok"
    row = manifest.latest_robots("www.pac.or.kr")
    assert row["http_status"] == 404 and row["purpose_status"] == "no_rules" and row["body_sha256"] is None
    assert manifest.failures() == [] and t.counts[RB] == 1               # 404는 백오프·실패 아님


def test_allow_all_robots_records_sha_and_status(make_client, manifest):
    body = "User-agent: *\nAllow: /\n"
    t = FakeTransport({"https://www.iendc.go.kr/robots.txt": (200, body, {}), "https://www.iendc.go.kr/x": (200, b"ok", {})})
    make_client(t).get("https://www.iendc.go.kr/x")
    row = manifest.latest_robots("www.iendc.go.kr")
    assert row["http_status"] == 200 and row["purpose_status"] == "allowed"
    assert row["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_disallowed_path_is_never_requested(make_client):
    t = FakeTransport({RB: (200, "User-agent: *\nDisallow: /kor/admin/\n", {}), PAC + "/kor/admin/x": (200, b"secret", {}),
                       PAC + "/kor/ok": (200, b"ok", {})})
    c = make_client(t)
    with pytest.raises(RobotsDisallowed):
        c.get(PAC + "/kor/admin/x")
    assert c.get(PAC + "/kor/ok").body == b"ok"
    assert PAC + "/kor/admin/x" not in t.urls()


def test_server_error_on_robots_is_fail_closed_and_refetched_next_time(make_client, manifest):
    t = FakeTransport({RB: [(503, b"", {})] * 5 + [(200, "User-agent: *\nAllow: /\n", {})], PAC + "/x": (200, b"ok", {})})
    c = make_client(t)
    with pytest.raises(RobotsDisallowed):
        c.get(PAC + "/x")
    assert manifest.latest_robots("www.pac.or.kr")["purpose_status"] == "unknown"
    assert PAC + "/x" not in t.urls()
    c2 = make_client(t)                                                  # 새 프로세스: unknown은 신선하지 않다
    assert c2.get(PAC + "/x").body == b"ok"
    assert manifest.latest_robots("www.pac.or.kr")["purpose_status"] == "allowed"


def test_html_robots_is_recorded_unparseable_and_blocks_collection(make_client, manifest):
    host = "https://open.law.go.kr"
    t = FakeTransport({host + "/robots.txt": (200, "<!DOCTYPE html><html><body>Page Not Found</body></html>", {}),
                       host + "/LSO/openApi/guideResult.do": (200, b"guide", {})})
    with pytest.raises(RobotsDisallowed):
        make_client(t).get(host + "/LSO/openApi/guideResult.do")
    assert manifest.latest_robots("open.law.go.kr")["purpose_status"] == "unparseable"
    assert host + "/LSO/openApi/guideResult.do" not in t.urls()


def test_unparseable_robots_is_recorded_as_disallowed_in_the_ledger(manifest):
    from press_review.ingestion import rights as R
    manifest.ensure_source("s", "law_go_kr", "t", "https://www.law.go.kr/")
    manifest.record_robots("www.law.go.kr", "2026-10-08T00:00:00+00:00", 200, "a" * 64, "unparseable")
    rec = R.build_source_record(manifest, _doc(manifest))
    assert rec["robots"]["purpose_status"] == "disallowed" and "unparseable" in rec["robots"]["notes"]
    assert rec["collection_enabled"] is False


def _doc(m):
    m.upsert_document("law_go_kr:1", "s", title="x")
    return "law_go_kr:1"


def test_ai_rag_prohibition_blocks_whole_host(make_client, manifest):
    body = "# AI TRAINING AND RAG IS PROHIBITED\nUser-agent: *\nAllow: /\n"
    t = FakeTransport({RB: (200, body, {}), PAC + "/x": (200, b"ok", {})})
    c = make_client(t)
    with pytest.raises(RobotsDisallowed):
        c.get(PAC + "/x")
    assert manifest.latest_robots("www.pac.or.kr")["purpose_status"] == "ai_rag_prohibited"


def test_root_disallow_is_recorded(make_client, manifest):
    t = FakeTransport({RB: (200, "User-agent: *\nDisallow: /\n", {})})
    with pytest.raises(RobotsDisallowed):
        make_client(t).get(PAC + "/x")
    assert manifest.latest_robots("www.pac.or.kr")["purpose_status"] == "root_disallowed"


def test_robots_fetched_once_per_day_per_host(make_client, manifest, clock):
    t = FakeTransport({RB: ROBOTS_OK, PAC + "/a": (200, b"a", {}), PAC + "/b": (200, b"b", {}), PAC + "/c": (200, b"c", {})})
    make_client(t).get(PAC + "/a")
    make_client(t).get(PAC + "/b")                                       # 새 클라이언트, 23시간 이내 → 재조회 없음
    assert t.counts[RB] == 1
    clock.advance(25 * 3600)
    make_client(t).get(PAC + "/c")
    assert t.counts[RB] == 2
    assert len(manifest.robots_rows()) == 2                              # 이력 보존(해시 변화 추적)


def test_cache_hit_does_not_touch_robots_or_network(make_client):
    t = FakeTransport({RB: ROBOTS_OK, PAC + "/a": (200, b"a", {})})
    c = make_client(t)
    c.get(PAC + "/a")
    t2 = FakeTransport({})
    c2 = make_client(t2)
    assert c2.get(PAC + "/a").from_cache and t2.calls == []


def test_robots_request_itself_is_not_robots_gated_and_uses_interval(make_client, clock):
    t = FakeTransport({RB: (200, "User-agent: *\nDisallow: /\nDisallow: /robots.txt\n", {})})
    with pytest.raises(RobotsDisallowed):
        make_client(t).get(PAC + "/x")
    assert t.counts[RB] == 1


def test_missing_cached_body_forces_refetch(make_client, corpus):
    t = FakeTransport({RB: ROBOTS_OK, PAC + "/a": (200, b"a", {}), PAC + "/b": (200, b"b", {})})
    make_client(t).get(PAC + "/a")
    for p in (corpus / "http_cache" / "www.pac.or.kr").glob("*"):
        if p.read_bytes().startswith(b"User-agent"):
            p.unlink()
    make_client(t).get(PAC + "/b")
    assert t.counts[RB] == 2
