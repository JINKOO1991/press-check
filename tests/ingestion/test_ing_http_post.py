"""POST(허용 호스트의 /fileDownload.do 만)와 추가 요청 헤더(Range 등) 계약."""
import pytest

from conftest import ROBOTS_OK, FakeTransport
from press_review.ingestion.http import BlockedURL, FetchFailed

DL = "https://www.iendc.go.kr/fileDownload.do"
ROB = {"https://www.iendc.go.kr/robots.txt": ROBOTS_OK, "https://www.pac.or.kr/robots.txt": ROBOTS_OK}


def test_post_sends_body_and_caches_per_body(make_client):
    t = FakeTransport({**ROB, DL: (200, b"%PDF-1.4 x", {"Content-Type": "application/pdf"})})
    c = make_client(t)
    r1 = c.post(DL, {"atchFileId": "A1", "fileSn": "0"})
    assert r1.ok and r1.body.startswith(b"%PDF") and not r1.from_cache
    assert t.bodies[-1] == b"atchFileId=A1&fileSn=0"
    assert [h for h in t.seen_headers if h.get("Content-Type")] [-1]["Content-Type"] == "application/x-www-form-urlencoded"
    n = len(t.calls)
    assert c.post(DL, {"atchFileId": "A1", "fileSn": "0"}).from_cache and len(t.calls) == n      # 같은 본문: 캐시
    r3 = c.post(DL, {"atchFileId": "A2", "fileSn": "0"})                                          # 다른 본문: 별도 요청
    assert not r3.from_cache and len(t.calls) == n + 1 and t.bodies[-1] == b"atchFileId=A2&fileSn=0"


@pytest.mark.parametrize("url", [
    "https://www.iendc.go.kr/board.do?menuPos=45",
    "https://www.iendc.go.kr/fileDownload.do/x",
    "https://www.pac.or.kr/fileDownload.do.evil",
    "http://www.iendc.go.kr/fileDownload.do",
    "https://news.naver.com/fileDownload.do",
    "https://www.law.go.kr/DRF/lawSearch.do",
])
def test_post_outside_filedownload_is_blocked_without_network(make_client, url):
    t = FakeTransport(ROB)
    with pytest.raises(BlockedURL):
        make_client(t).post(url, {"a": "1"})
    assert t.calls == []


def test_post_never_carries_oc_and_body_type_is_checked(make_client):
    t = FakeTransport(ROB)
    c = make_client(t)
    with pytest.raises(BlockedURL):
        c.post("https://www.law.go.kr/DRF/fileDownload.do?OC=x", {"a": "1"})
    with pytest.raises(BlockedURL):
        c.post(DL, "a=1")
    with pytest.raises(BlockedURL):
        c.post(DL, b"x" * 70000)
    assert t.calls == []


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_post_redirect_is_not_followed(make_client, manifest, code):
    t = FakeTransport({**ROB, DL: (code, b"", {"Location": "https://www.iendc.go.kr/fileDownload.do?x=1"})})
    with pytest.raises(FetchFailed):
        make_client(t).post(DL, {"a": "1"})
    assert [m for m, u in t.calls if u == DL] == ["POST"] and len(t.calls) == 2      # robots + POST 한 번
    assert [f["error_class"] for f in manifest.failures()] == ["post_redirect"]


def test_range_header_is_sent_and_never_cached(make_client):
    u = "https://www.pac.or.kr/files/a.pdf"
    t = FakeTransport({**ROB, u: (206, b"partial", {"Content-Range": "bytes 0-6/100"})})
    c = make_client(t)
    r = c.get(u, headers={"range": "bytes=0-6"})
    assert r.status == 206 and t.seen_headers[-1]["Range"] == "bytes=0-6"
    n = len(t.calls)
    c.get(u, headers={"Range": "bytes=0-6"})
    assert len(t.calls) == n + 1                                    # 206은 캐시되지 않는다
    t.routes[u] = (200, b"full body", {})
    full = c.get(u)                                                  # 전체 요청은 부분 응답을 받지 않는다
    assert full.body == b"full body" and "Range" not in t.seen_headers[-1]


@pytest.mark.parametrize("hdr", [
    {"User-Agent": "x"}, {"Host": "evil.example"}, {"Cookie": "a=b"}, {"Authorization": "Bearer x"},
    {"Range": "bytes=0-1\r\nX-Evil: 1"}, {"Referer": "https://www.pac.or.kr/\nX: y"}, {"Content-Type": "text/plain"},
])
def test_extra_headers_are_allowlisted_and_sanitized(make_client, hdr):
    t = FakeTransport(ROB)
    with pytest.raises(BlockedURL):
        make_client(t).get("https://www.pac.or.kr/x", headers=hdr)
    assert t.calls == []
