import json

import pytest

from conftest import ROBOTS_OK, FakeTransport
from press_review.ingestion.context import Budget, TimeBudgetExceeded
from press_review.ingestion.http import DryRunMiss, FetchFailed, TransportError

U = "https://www.pac.or.kr/kor/pages/?p=207&magazine_new=M03&cate=MA04"
RB = "https://www.pac.or.kr/robots.txt"


def tr(**routes):
    d = {RB: (404, b"", {})}
    d.update(routes)
    return FakeTransport(d)


# ---- 호스트별 간격 ----
def test_same_host_requests_are_spaced_by_two_seconds(make_client, clock):
    t = tr(**{U: (200, b"a", {}), U + "&x=1": (200, b"b", {})})
    c = make_client(t)
    c.get(U)
    c.get(U + "&x=1")
    assert clock.sleeps == [2.0, 2.0]                                   # robots → U → U&x=1


def test_interval_credits_time_already_elapsed(make_client, clock):
    t = tr(**{U: (200, b"a", {}), U + "&x=1": (200, b"b", {})})
    c = make_client(t)
    c.get(U)
    clock.advance(1.5)
    c.get(U + "&x=1")
    assert clock.sleeps[-1] == pytest.approx(0.5)


def test_different_hosts_do_not_wait_on_each_other(make_client, clock):
    t = FakeTransport({RB: (404, b"", {}), "https://www.iendc.go.kr/robots.txt": ROBOTS_OK,
                       U: (200, b"a", {}), "https://www.iendc.go.kr/home/kor/board.do?menuPos=45": (200, b"b", {})})
    c = make_client(t)
    c.get(U)
    c.get("https://www.iendc.go.kr/home/kor/board.do?menuPos=45")
    assert clock.sleeps == [2.0, 2.0]                                    # 호스트마다 robots→페이지 사이 한 번씩, 호스트 사이는 대기 없음


# ---- 백오프 ----
def test_503_backoff_is_exponential_with_four_retries_then_failure_recorded(make_client, clock, manifest):
    t = tr(**{U: (503, b"x", {})})
    c = make_client(t)
    with pytest.raises(FetchFailed):
        c.get(U)
    assert t.counts[U] == 5                                              # 시도 1 + 재시도 4
    assert clock.sleeps == [2.0, 2.0, 4.0, 8.0, 16.0]                    # robots→U 간격 2.0 뒤 백오프 2,4,8,16
    f = manifest.failures()
    assert len(f) == 1 and f[0]["stage"] == "http" and f[0]["error_class"] == "retries_exhausted"
    assert f[0]["retry_count"] == 4


def test_retry_then_success_returns_and_clears_nothing_as_failure(make_client, manifest):
    t = tr(**{U: [(500, b"", {}), (429, b"", {}), (200, b"ok", {})]})
    c = make_client(t)
    assert c.get(U).body == b"ok"
    assert t.counts[U] == 3 and manifest.failures() == []


def test_429_honors_retry_after(make_client, clock):
    t = tr(**{U: [(429, b"", {"Retry-After": "30"}), (200, b"ok", {})]})
    make_client(t).get(U)
    assert 30.0 in clock.sleeps


def test_timeouts_and_connection_errors_are_retried(make_client):
    t = tr(**{U: [TransportError("timeout"), TransportError("connection"), (200, b"ok", {})]})
    assert make_client(t).get(U).body == b"ok"
    assert t.counts[U] == 3


@pytest.mark.parametrize("status", [404, 403, 400, 410])
def test_other_4xx_not_retried_not_failures_not_cached(make_client, manifest, status):
    t = tr(**{U: (status, b"nope", {})})
    c = make_client(t)
    r = c.get(U)
    assert r.status == status and not r.ok and t.counts[U] == 1
    assert manifest.failures() == []
    c.get(U)
    assert t.counts[U] == 2                                              # 캐시되지 않음


def test_tls_error_not_retried(make_client):
    t = tr(**{U: TransportError("tls", "bad cert", retriable=False)})
    with pytest.raises(FetchFailed):
        make_client(t).get(U)
    assert t.counts[U] == 1


def test_backoff_wait_beyond_budget_stops_instead_of_sleeping(make_client, clock):
    t = tr(**{U: (503, b"", {})})
    c = make_client(t, budget=Budget(3.0, clock))
    with pytest.raises(TimeBudgetExceeded):
        c.get(U)
    assert sum(clock.sleeps) <= 3.0


# ---- 캐시 ----
def test_cache_hit_makes_zero_network_requests_including_robots(make_client):
    t = tr(**{U: (200, b"body", {"Content-Type": "text/html; charset=euc-kr"})})
    c = make_client(t)
    r1 = c.get(U)
    calls = len(t.calls)
    r2 = make_client(t).get(U)                                           # 새 클라이언트(프로세스 재시작 모사)
    assert r2.from_cache and r2.body == r1.body and len(t.calls) == calls


def test_refresh_ignores_cache(make_client):
    t = tr(**{U: (200, b"body", {})})
    c = make_client(t)
    c.get(U)
    c.get(U, refresh=True)
    assert t.counts[U] == 2
    c2 = make_client(t, refresh=True)
    c2.get(U)
    assert t.counts[U] == 3


def test_cache_layout_and_meta(make_client, corpus):
    import hashlib
    t = tr(**{U: (200, b"body", {"Content-Type": "text/html", "Set-Cookie": "sid=1", "X-Other": "z"})})
    make_client(t).get(U)
    d = corpus / "http_cache" / "www.pac.or.kr"
    h = hashlib.sha256(U.encode()).hexdigest()[:16]
    assert (d / f"{h}.body").read_bytes() == b"body"
    meta = json.loads((d / f"{h}.meta.json").read_text())
    assert meta["url"] == U and meta["status"] == 200 and set(meta["headers"]) == {"content-type"}
    assert meta["body_sha256"] == hashlib.sha256(b"body").hexdigest()
    assert not list(d.glob("*.tmp*"))


def test_accept_rejection_is_not_cached_and_reported(make_client):
    t = tr(**{U: [(200, "오류 안내", {}), (200, "정상 목록", {})]})
    c = make_client(t)
    reject = lambda body, headers: "error page" if "오류" in body.decode() else None
    r1 = c.get(U, accept=reject)
    assert not r1.ok and r1.content_error == "error page" and r1.status == 200
    r2 = c.get(U, accept=reject)
    assert r2.ok and t.counts[U] == 2
    r3 = c.get(U, accept=reject)
    assert r3.from_cache and t.counts[U] == 2


def test_cached_entry_failing_new_accept_is_refetched(make_client):
    t = tr(**{U: [(200, "v1", {}), (200, "v2", {})]})
    c = make_client(t)
    c.get(U)
    r = c.get(U, accept=lambda b, h: "stale" if b == b"v1" else None)
    assert r.body == b"v2" and t.counts[U] == 2


def test_evict_removes_entry(make_client):
    t = tr(**{U: (200, b"x", {})})
    c = make_client(t)
    c.get(U)
    assert c.evict(U) is True and c.evict(U) is False
    c.get(U)
    assert t.counts[U] == 2


def test_corrupted_cache_body_is_a_miss(make_client, corpus):
    t = tr(**{U: (200, b"good", {})})
    c = make_client(t)
    c.get(U)
    for p in (corpus / "http_cache" / "www.pac.or.kr").glob("*.body"):
        if p.read_bytes() == b"good":
            p.write_bytes(b"tampered")
    assert c.get(U).body == b"good" and t.counts[U] == 2


def test_head_is_cached_separately_from_get(make_client):
    t = tr(**{U: [(200, b"bodybody", {"Content-Length": "8", "Content-Type": "application/pdf"})]})
    c = make_client(t)
    h = c.head(U)
    assert h.headers["content-length"] == "8" and h.body == b""
    assert c.head(U).from_cache
    assert [m for m, u in t.calls if u == U] == ["HEAD"]


def test_dry_run_serves_cache_only_and_stops_on_miss(make_client):
    t = tr(**{U: (200, b"x", {})})
    make_client(t).get(U)
    n = len(t.calls)
    dry = make_client(t, network=False)
    assert dry.get(U).from_cache
    with pytest.raises(DryRunMiss) as e:
        dry.get(U + "&other=1")
    assert "other=1" in str(e.value) and len(t.calls) == n


def test_response_text_decoding():
    from press_review.ingestion.http import Response
    kr = "가나다"
    r = Response("u", "u", 200, {"content-type": "text/html; charset=euc-kr"}, kr.encode("cp949"))
    assert r.text() == kr
    r2 = Response("u", "u", 200, {}, kr.encode("cp949"))
    assert r2.text() == kr                                               # utf-8 실패 → cp949
    assert Response("u", "u", 200, {}, kr.encode()).text() == kr
