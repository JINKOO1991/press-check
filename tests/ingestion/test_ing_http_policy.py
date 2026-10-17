import pytest

from conftest import ROBOTS_OK, FakeTransport
from press_review.ingestion.http import BlockedURL, TransportError, validate_url

P = "https://www.pac.or.kr/kor/pages/?p=207"


def routes_with_robots(**extra):
    r = {"https://www.pac.or.kr/robots.txt": ROBOTS_OK, "https://www.iendc.go.kr/robots.txt": ROBOTS_OK,
         "https://www.law.go.kr/robots.txt": ROBOTS_OK}
    r.update(extra)
    return r


@pytest.mark.parametrize("url", [
    "https://news.naver.com/ombudsman/errorArticleList",
    "https://n.news.naver.com/article/000/0000000000",
    "https://www.kci.go.kr/kciportal/landing/article.kci?arti_id=ART001556126",
    "https://news.daum.net/correct",
    "https://example.com/",
    "https://www.pac.or.kr.evil.example/x",
    "https://evilwww.pac.or.kr/x",
])
def test_host_outside_allowlist_is_rejected_without_network(make_client, url):
    t = FakeTransport()
    c = make_client(t)
    with pytest.raises(BlockedURL):
        c.get(url)
    assert t.calls == []


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/", "https://127.1/", "https://2130706433/", "https://0x7f.0.0.1/",
    "https://10.0.0.5/x", "https://192.168.1.1/", "https://172.16.0.9/", "https://169.254.169.254/latest/meta-data",
    "https://[::1]/", "https://localhost/", "https://0.0.0.0/",
])
def test_private_and_loopback_addresses_are_rejected(make_client, url):
    t = FakeTransport()
    c = make_client(t)
    with pytest.raises(BlockedURL):
        c.get(url)
    assert t.calls == []


@pytest.mark.parametrize("url", [
    "https://user:pw@www.pac.or.kr/x", "https://user@www.pac.or.kr/x", "https://@www.pac.or.kr/x",
    "ftp://www.pac.or.kr/x", "file:///etc/passwd", "javascript:alert(1)", "https://www.pac.or.kr:8443/x",
    "http://www.pac.or.kr/x", "http://www.law.go.kr/LSW/precInfoP.do", "//www.pac.or.kr/x", "",
])
def test_credentials_scheme_port_and_plain_http_rejected(make_client, url):
    t = FakeTransport()
    c = make_client(t)
    with pytest.raises(BlockedURL):
        c.get(url)
    assert t.calls == []


def test_plain_http_allowed_only_for_law_go_kr_drf_path():
    validate_url("http://www.law.go.kr/DRF/lawSearch.do?target=prec")
    with pytest.raises(BlockedURL):
        validate_url("http://www.law.go.kr/LSW/x")
    with pytest.raises(BlockedURL):
        validate_url("http://open.law.go.kr/DRF/x")


def test_error_message_does_not_echo_credentials(make_client):
    c = make_client(FakeTransport())
    with pytest.raises(BlockedURL) as e:
        c.get("https://admin:s3cretpw@www.pac.or.kr/x")
    assert "s3cretpw" not in str(e.value) and "admin" not in str(e.value)


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/admin", "https://10.1.1.1/", "https://news.naver.com/x", "https://user:pw@www.pac.or.kr/x",
    "file:///etc/passwd", "http://www.pac.or.kr/plain-http", "https://[::1]/",
])
def test_redirect_hops_get_the_same_checks(make_client, location):
    t = FakeTransport(routes_with_robots(**{P: (302, b"", {"Location": location})}))
    c = make_client(t)
    with pytest.raises(BlockedURL):
        c.get(P)
    assert t.urls() == ["https://www.pac.or.kr/robots.txt", P]       # 리다이렉트 목적지에는 요청하지 않는다


def test_relative_redirect_is_followed_and_checked_by_robots(make_client):
    t = FakeTransport(routes_with_robots(**{
        P: (301, b"", {"Location": "/kor/pages/?p=208"}),
        "https://www.pac.or.kr/kor/pages/?p=208": (200, b"ok", {}),
    }))
    c = make_client(t)
    r = c.get(P)
    assert r.status == 200 and r.body == b"ok"
    assert r.final_url == "https://www.pac.or.kr/kor/pages/?p=208"


def test_redirect_loop_is_capped(make_client):
    t = FakeTransport(routes_with_robots(**{P: (302, b"", {"Location": P})}))
    c = make_client(t)
    from press_review.ingestion.http import FetchFailed
    with pytest.raises(FetchFailed):
        c.get(P)
    assert len(t.urls("www.pac.or.kr")) <= 8


def test_redirect_hops_respect_host_interval(make_client, clock):
    t = FakeTransport(routes_with_robots(**{P: (302, b"", {"Location": "/b"}),
                                            "https://www.pac.or.kr/b": (200, b"ok", {})}))
    c = make_client(t)
    c.get(P)
    # robots, P, /b = 같은 호스트 3요청 → 사이마다 2초 간격
    assert clock.sleeps == [2.0, 2.0]


def test_resolver_returning_private_address_is_rejected(make_client):
    t = FakeTransport(routes_with_robots())
    c = make_client(t, resolver=lambda host: ["10.0.0.7"])
    with pytest.raises(BlockedURL):
        c.get(P)
    assert t.calls == []


def test_resolver_public_address_passes(make_client):
    t = FakeTransport(routes_with_robots(**{P: (200, b"ok", {})}))
    c = make_client(t, resolver=lambda host: ["8.8.8.8"])
    assert c.get(P).status == 200


def test_oversized_response_is_reported_not_cached(make_client, corpus):
    from press_review.ingestion.http import ResponseTooLarge

    class Big(FakeTransport):
        def request(self, method, url, headers, timeout, max_bytes):
            if url.endswith("robots.txt"):
                return super().request(method, url, headers, timeout, max_bytes)
            raise ResponseTooLarge("response exceeds 10 bytes")

    c = make_client(Big(routes_with_robots()))
    with pytest.raises(ResponseTooLarge):
        c.get(P, max_bytes=10)
    bodies = [p.read_bytes() for p in (corpus / "http_cache" / "www.pac.or.kr").glob("*.body")]
    assert bodies == [ROBOTS_OK[1].encode()]            # robots만 캐시되고 초과 응답은 캐시에 없다


def test_user_agent_and_no_gzip(make_client):
    t = FakeTransport(routes_with_robots(**{P: (200, b"ok", {})}))
    make_client(t).get(P)
    for h in t.seen_headers:
        assert h["User-Agent"] == "press-dispute-review/0.1 (+research; non-commercial index)"
        assert "Accept-Encoding" not in h


def test_host_lock_blocks_second_process_on_same_host(corpus, clock):
    from press_review.ingestion.http import HostBusy, HttpClient
    t = FakeTransport(routes_with_robots(**{P: (200, b"ok", {})}))
    mk = lambda: HttpClient(corpus / "http_cache", transport=t, clock=clock, sleep=clock.sleep, now=clock.now,
                            robots=None, lock_dir=corpus / "locks")
    a, b = mk(), mk()
    try:
        a.get(P)
        with pytest.raises(HostBusy):
            b.get(P, refresh=True)
    finally:
        a.close()
        b.close()


def test_transport_error_kinds_are_exceptions():
    assert TransportError("timeout").retriable
    assert not TransportError("tls", retriable=False).retriable


def test_host_interval_holds_across_consecutive_runs(corpus, clock):
    """V-POL1: --max-seconds 로 이어 붙인 CLI 실행 사이에도 호스트별 2초 간격이 지켜져야 한다(새 프로세스 = 새 클라이언트)."""
    from press_review.ingestion.http import HttpClient
    t = FakeTransport(routes_with_robots(**{P: (200, b"ok", {})}))
    mk = lambda: HttpClient(corpus / "http_cache", transport=t, clock=clock, sleep=clock.sleep, now=clock.now,
                            robots=None, lock_dir=corpus / "locks")
    a = mk()
    a.get(P, refresh=True)
    a.close()                                             # 첫 실행 종료
    clock.advance(0.5)                                    # 다음 CLI 가 0.5초 뒤 시작
    b = mk()
    before = clock()
    b.get(P, refresh=True)
    b.close()
    assert clock() - before >= 1.5 - 1e-9 and clock.sleeps[-1] == pytest.approx(1.5)
    clock.advance(10)                                      # 간격이 이미 지났으면 기다리지 않는다
    c = mk()
    n = len(clock.sleeps)
    c.get(P, refresh=True)
    c.close()
    assert len(clock.sleeps) == n
