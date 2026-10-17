"""네트워크 없는 가짜 transport·시계로 수집 계층을 시험하기 위한 공용 도구."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdr_ing_paths  # noqa: E402,F401

from press_review import logsafe  # noqa: E402
from press_review.ingestion.context import Budget  # noqa: E402
from press_review.ingestion.http import HttpClient, RawResponse, TransportError  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

FAKE_OC = "FAKEOC_9f3a7c21d"


@pytest.fixture(autouse=True)
def _clean_logsafe():
    logsafe.reset()
    yield
    logsafe.reset()


class FakeClock:
    """단조 시계 + 벽시계 겸용. sleep 은 시간을 앞으로 보낸다."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def advance(self, s):
        self.t += s

    def now(self):
        from datetime import datetime, timedelta, timezone
        return datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=self.t - 1000.0)


class FakeTransport:
    """routes: {url: response | callable(url, n) | list}. response = (status, body, headers) 또는 TransportError 인스턴스.

    매칭은 요청 URL(실제 전송 URL, OC 포함 가능) 전체 일치 → 접두 일치 순. 호출 기록은 calls."""

    def __init__(self, routes=None, default=None, clock=None, latency=0.0):
        self.routes = dict(routes or {})
        self.default = default if default is not None else (404, b"not found", {})
        self.calls = []        # (method, url)
        self.seen_headers = []
        self.bodies = []
        self.counts = {}
        self.clock = clock
        self.latency = latency

    def request(self, method, url, headers, timeout, max_bytes, body=None):
        self.calls.append((method, url))
        self.seen_headers.append(headers)
        self.bodies.append(body)
        n = self.counts[url] = self.counts.get(url, 0) + 1
        if self.clock is not None and self.latency:
            self.clock.advance(self.latency)
        spec = self.routes.get(url)
        if spec is None:
            for k, v in self.routes.items():
                if url.startswith(k):
                    spec = v
                    break
        if spec is None:
            spec = self.default
        if isinstance(spec, list):
            spec = spec[min(n, len(spec)) - 1]
        if callable(spec):
            spec = spec(url, n)
        if isinstance(spec, Exception):
            raise spec
        status, body, hdrs = spec
        if isinstance(body, str):
            body = body.encode("utf-8")
        return RawResponse(status=status, headers={k.lower(): v for k, v in hdrs.items()}, body=body, url=url)

    def urls(self, host=None):
        return [u for _, u in self.calls if host is None or f"//{host}/" in u]


ROBOTS_OK = (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def corpus(tmp_path):
    return tmp_path / "corpus"


@pytest.fixture()
def manifest(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    yield m
    m.close()


@pytest.fixture()
def make_client(corpus, clock, manifest):
    made = []

    def _make(transport, **kw):
        kw.setdefault("manifest", manifest)
        kw.setdefault("oc_provider", lambda: FAKE_OC)
        c = HttpClient(corpus / "http_cache", transport=transport, clock=clock, sleep=clock.sleep, now=clock.now, **kw)
        made.append(c)
        return c

    yield _make
    for c in made:
        c.close()


@pytest.fixture()
def log_stream():
    """logsafe 핸들러가 붙은 수집 로거. propagate=False 라 caplog 대신 스트림을 본다."""
    import logging
    buf = io.StringIO()
    lg = logging.getLogger("press_review.ingestion")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    h = logsafe.make_handler(buf)
    lg.addHandler(h)
    yield buf
    lg.removeHandler(h)
