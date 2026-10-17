"""공용 HTTP 계층(표준 라이브러리만). 계약: docs/P1_DESIGN.md '공용 HTTP 계층'.

- 허용 호스트 목록은 코드 상수다. 네이버·KCI·daum은 넣지 않는다. 설정·인자·환경변수로 바꿀 수 없다.
- https만 허용한다. http는 www.law.go.kr 의 ``/DRF/`` 경로에서만 예외다.
- 사설 IP·loopback·링크로컬·자격증명 포함 URL은 거부한다. 리다이렉트는 직접 따라가며 hop마다 같은 검사를 한다.
- 호스트별 최소 간격 2초, 동시성 1(프로세스 안은 직렬, 프로세스 사이는 호스트별 flock). 429·5xx·timeout·연결 오류만
  지수 백오프(재시도 최대 4회 = 최대 5번 시도). 404 같은 4xx는 재시도하지 않는다.
- 캐시 적중이면 네트워크도 robots 조회도 하지 않는다(--refresh 로 무시).
- 법제처 OC는 ``with_oc=True`` 일 때 클라이언트가 secrets 모듈로 읽어 주입한다. 캐시 키·메타·로그·예외·장부에는 ``OC=[MASKED]`` 만 남고,
  DRF 응답 본문에 OC가 되울려 나오면(상세링크 필드 등) 캐시에 쓰기 전에 ``OC=`` 값과 되울림 문맥(``<OC>값</OC>``, ``"OC":"값"``)의 값만 마스킹한다.
  문맥 밖에 값이 남은 경우: 값이 평범한 단어가 아니면 fail-closed(본문 폐기·미캐시), 평범한 단어이면 정상 본문으로 보고 그대로 둔다(P2 §8).

이식 메모: 법제처 호출·마스킹은 작성자 소유 코드(비공개)의 기존 구현이 가진 결함 목록을 반영한 새 구현이다.
"""
from __future__ import annotations

import hashlib
import http.client as _httpc
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import private_mkdir
from .. import logsafe
from .context import Budget, TimeBudgetExceeded, iso, utcnow

try:  # POSIX 전용. 없으면 프로세스 간 호스트 잠금은 건너뛴다.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

ALLOWED_HOSTS = frozenset({"www.pac.or.kr", "pac.or.kr", "www.iendc.go.kr", "www.law.go.kr", "open.law.go.kr"})
DRF_HOST = "www.law.go.kr"
DRF_PATH_PREFIX = "/DRF/"
USER_AGENT = "press-check/0.1 (+research; non-commercial index)"
MIN_INTERVAL_SECONDS = 2.0
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
RETRY_AFTER_CAP_SECONDS = 120.0
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CACHED_HEADERS = ("content-type", "content-length", "last-modified", "etag", "content-disposition", "content-language")
OC_PARAM = "OC"
# POST는 허용 호스트의 이 경로에서만 보낼 수 있다(P2 §8: IENDC 첨부 다운로드)
POST_PATHS = frozenset({"/fileDownload.do"})
MAX_POST_BYTES = 64 * 1024
ALLOWED_EXTRA_HEADERS = frozenset({"range", "referer", "accept", "accept-language", "content-type"})

_OC_QUERY_RE = re.compile(r"(?i)((?:^|[?&;])OC=)[^&#\s]*")
_OC_TEXT_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(OC=)[^&\s\"'<>\\]*")
_OC_BYTES_RE = re.compile(rb"(?i)(?<![A-Za-z0-9_])(OC=)[^&\s\"'<>\\]*")


# ---------------------------------------------------------------- 예외
class IngestionError(Exception):
    error_class = "error"


class BlockedURL(IngestionError):
    error_class = "blocked_url"


class RobotsDisallowed(IngestionError):
    error_class = "robots_disallowed"


class FetchFailed(IngestionError):
    error_class = "fetch_failed"


class ResponseTooLarge(IngestionError):
    error_class = "response_too_large"


class DryRunMiss(IngestionError):
    """--dry-run 에서 캐시에 없는 URL(마스킹)이 나왔다. 네트워크를 쓰지 않고 멈춘다."""
    error_class = "dry_run_miss"


class HostBusy(IngestionError):
    error_class = "host_busy"


class TransportError(IngestionError):
    """transport 계층 오류. kind: timeout | connection | tls. retriable이면 백오프 재시도 대상."""
    error_class = "transport_error"

    def __init__(self, kind: str, message: str = "", retriable: bool = True):
        super().__init__(message or kind)
        self.kind = kind
        self.retriable = retriable


# ---------------------------------------------------------------- 마스킹
def mask_url(url: str) -> str:
    """URL의 OC 쿼리 파라미터 값만 [MASKED] 로 바꾼다(본문 문자열 전체 치환이 아님: 기존 구현의 결함 회피)."""
    return _OC_QUERY_RE.sub(r"\1[MASKED]", url)


def scrub_text(text: str) -> str:
    return _OC_TEXT_RE.sub(r"\1[MASKED]", text)


def scrub_body(body: bytes, oc: Optional[str] = None) -> bytes:
    """DRF 본문에 되울려 나온 ``OC=값`` 의 값만 마스킹한다.

    oc 를 주면 되울림 위치인 ``<OC>값</OC>``·``"OC":"값"``·``OC%3D값`` 문맥의 그 값도 마스킹한다.
    본문의 다른 곳(법령 문구 등)에 같은 글자가 있어도 건드리지 않는다.
    """
    body = _OC_BYTES_RE.sub(rb"\1[MASKED]", body)
    if oc:
        text = body.decode("utf-8", "surrogateescape")
        body = logsafe.mask_contexts(text, oc).encode("utf-8", "surrogateescape")
    return body


def strip_oc(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    # 원 쿼리의 인코딩을 보존하려고 OC 쌍만 제거한다.
    raw = [seg for seg in parts.query.split("&") if seg and seg.split("=", 1)[0].upper() != OC_PARAM]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(raw), ""))


def has_oc(url: str) -> bool:
    return any(seg.split("=", 1)[0].upper() == OC_PARAM for seg in urllib.parse.urlsplit(url).query.split("&") if seg)


# ---------------------------------------------------------------- URL 검증
def _as_ip(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:  # 127.1, 0x7f.0.0.1, 2130706433 같은 축약·진법 표기
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _is_private(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified)


def validate_url(url: str) -> urllib.parse.SplitResult:
    """정책 위반이면 BlockedURL. 메시지에 URL 전체를 넣지 않는다."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise BlockedURL("malformed url") from None
    if parts.scheme not in ("https", "http"):
        raise BlockedURL(f"scheme not allowed: {parts.scheme or '(none)'}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedURL("credentials in url are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedURL("url has no host")
    ip = _as_ip(host)
    if ip is not None:
        if _is_private(ip):
            raise BlockedURL("private, loopback or reserved address is not allowed")
        raise BlockedURL("ip address hosts are not allowed")
    if host not in ALLOWED_HOSTS:
        raise BlockedURL(f"host not allowed: {host}")
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        raise BlockedURL("non-default port is not allowed")
    if parts.scheme == "http" and not (host == DRF_HOST and parts.path.startswith(DRF_PATH_PREFIX)):
        raise BlockedURL("plain http is allowed only for law.go.kr DRF paths")
    return parts


def default_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def _check_resolved(host: str, resolver: Callable[[str], list[str]]) -> None:
    try:
        addrs = resolver(host)
    except OSError:
        raise TransportError("connection", f"dns lookup failed for {host}") from None
    for a in addrs:
        ip = _as_ip(a)
        if ip is None or _is_private(ip):
            raise BlockedURL(f"host {host} resolves to a private or loopback address")


# ---------------------------------------------------------------- transport
@dataclass
class RawResponse:
    status: int
    headers: dict
    body: bytes
    url: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class UrllibTransport:
    """urllib 기반 기본 transport. 리다이렉트는 따라가지 않는다(클라이언트가 hop별 검사 후 직접 따라간다)."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, url: str, headers: dict, timeout: float, max_bytes: int,
                body: Optional[bytes] = None) -> RawResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            resp = self._opener.open(req, timeout=timeout)
            status, hdrs = resp.status, resp.headers
            try:                                  # 응답 헤더를 받은 뒤의 TLS 오류는 전송 뒤 실패다(청구 가능성, CX3). 핸드셰이크 실패와 메시지로 구분한다
                body = self._read(resp, max_bytes) if method != "HEAD" else b""
            except ssl.SSLError as e2:
                raise TransportError("tls", f"tls error during body read ({type(e2).__name__})", retriable=False) from None
            resp.close()
            self._check_complete(method, status, hdrs, body)
        except urllib.error.HTTPError as e:
            status, hdrs = e.code, e.headers
            try:                                  # 이 핸들러 안의 예외는 아래 형제 except 가 잡지 못하므로 오류 본문 읽기는 같은 매핑을 따로 건다
                body = self._read(e, max_bytes) if method != "HEAD" else b""
            except ResponseTooLarge:
                raise
            except (socket.timeout, TimeoutError):
                raise TransportError("timeout", "request timed out") from None
            except ssl.SSLError as e2:
                raise TransportError("tls", f"tls error during body read ({type(e2).__name__})", retriable=False) from None
            except (OSError, _httpc.HTTPException) as e2:
                raise TransportError("connection", f"connection error ({type(e2).__name__})") from None
        except TransportError:
            raise
        except ResponseTooLarge:
            raise
        except ssl.SSLError as e:
            raise TransportError("tls", f"tls error ({type(e).__name__})", retriable=False) from None
        except urllib.error.URLError as e:
            r = e.reason
            if isinstance(r, ssl.SSLError):
                raise TransportError("tls", f"tls error ({type(r).__name__})", retriable=False) from None
            if isinstance(r, (socket.timeout, TimeoutError)):
                raise TransportError("timeout", "request timed out") from None
            raise TransportError("connection", f"connection error ({type(r).__name__})") from None
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout", "request timed out") from None
        except (OSError, _httpc.HTTPException) as e:
            raise TransportError("connection", f"connection error ({type(e).__name__})") from None
        return RawResponse(status=status, headers={k.lower(): v for k, v in hdrs.items()}, body=body, url=url)

    @staticmethod
    def _check_complete(method: str, status: int, hdrs: Any, body: bytes) -> None:
        """RFC 9112 §6.3: Content-Length 만큼 오기 전에 연결이 닫혔으면 메시지는 불완전하다(``read(amt)`` 는 짧은 본문을 오류 없이 돌려준다).
        불완전한 본문은 재시도 대상 연결 오류로 올려, 200 으로 저장·캐시되지 않게 한다."""
        if method == "HEAD" or status in (204, 304) or hdrs.get("Transfer-Encoding") is not None:
            return
        try:
            expected = int(hdrs.get("Content-Length"))
        except (TypeError, ValueError):
            return
        if expected >= 0 and len(body) < expected:
            raise TransportError("connection", f"incomplete body ({len(body)} of {expected} bytes)")

    @staticmethod
    def _read(fp: Any, max_bytes: int) -> bytes:
        data = fp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ResponseTooLarge(f"response exceeds {max_bytes} bytes")
        return data


# ---------------------------------------------------------------- 응답·캐시
@dataclass
class Response:
    url: str                       # 마스킹된 요청 URL
    final_url: str                 # 마스킹된 최종 URL
    status: int
    headers: dict
    body: bytes
    from_cache: bool = False
    fetched_at: str = ""
    content_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.content_error is None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self, encoding: Optional[str] = None) -> str:
        """디코딩. 지정 없으면 Content-Type charset, meta charset, utf-8, cp949 순으로 시도한다."""
        cands: list[str] = []
        if encoding:
            cands.append(encoding)
        m = re.search(r"charset=([\w.-]+)", self.headers.get("content-type", ""), re.I)
        if m:
            cands.append(m.group(1))
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", self.body[:4096], re.I)
        if m2:
            cands.append(m2.group(1).decode("ascii", "ignore"))
        cands += ["utf-8", "cp949"]
        for enc in cands:
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")


def _digest(method: str, display_url: str, body_sha: Optional[str] = None) -> str:
    key = display_url if method == "GET" else f"{method} {display_url}"
    if body_sha:                                   # POST: 같은 URL이라도 본문이 다르면 다른 응답이다
        key += f" body={body_sha}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class HttpCache:
    """corpus/http_cache/<host>/<sha256(url)[:16]>.{body,meta.json}. 쓰기는 임시파일+os.replace, meta가 있어야 유효하다."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _paths(self, host: str, method: str, display_url: str, body_sha: Optional[str] = None) -> tuple[Path, Path]:
        d = self.root / host
        h = _digest(method, display_url, body_sha)
        return d / f"{h}.body", d / f"{h}.meta.json"

    def get(self, host: str, method: str, display_url: str, body_sha: Optional[str] = None) -> Optional[Response]:
        body_p, meta_p = self._paths(host, method, display_url, body_sha)
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            body = body_p.read_bytes() if method != "HEAD" else b""
        except (OSError, ValueError):
            return None
        if method != "HEAD" and hashlib.sha256(body).hexdigest() != meta.get("body_sha256"):
            return None
        return Response(url=meta.get("url", display_url), final_url=meta.get("final_url", display_url),
                        status=int(meta.get("status", 200)), headers=meta.get("headers", {}), body=body,
                        from_cache=True, fetched_at=meta.get("fetched_at", ""))

    def put(self, host: str, method: str, display_url: str, resp: Response, body_sha: Optional[str] = None) -> None:
        body_p, meta_p = self._paths(host, method, display_url, body_sha)
        private_mkdir(body_p.parent)
        meta = {
            "url": display_url, "final_url": resp.final_url, "method": method, "status": resp.status,
            "headers": {k: v for k, v in resp.headers.items() if k in CACHED_HEADERS},
            "fetched_at": resp.fetched_at, "body_sha256": hashlib.sha256(resp.body).hexdigest(),
            "body_bytes": len(resp.body),
        }
        if method != "HEAD":
            _atomic_write(body_p, resp.body)
        _atomic_write(meta_p, json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    def evict(self, host: str, method: str, display_url: str, body_sha: Optional[str] = None) -> bool:
        removed = False
        for p in self._paths(host, method, display_url, body_sha):
            try:
                p.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 클라이언트
def _encode_post_body(data: Any) -> bytes:
    if data is None:
        return b""
    if isinstance(data, dict):
        out = urllib.parse.urlencode(data).encode("ascii")
    elif isinstance(data, (bytes, bytearray)):
        out = bytes(data)
    else:
        raise BlockedURL("POST data must be a dict or bytes")
    if len(out) > MAX_POST_BYTES:
        raise BlockedURL("POST body too large")
    return out


def _check_post_target(url: str) -> None:
    parts = validate_url(url)
    if parts.scheme != "https" or (parts.path or "") not in POST_PATHS:
        raise BlockedURL("POST is allowed only for https /fileDownload.do on allowed hosts")


def _check_extra_headers(headers: Optional[dict], method: str) -> dict:
    """추가 헤더 검증: 이름은 허용 목록(User-Agent·Host·Cookie·Authorization 등은 거부), 값에 CR/LF·제어문자 금지. 키는 Range 식 표기로 정규화한다."""
    out: dict = {}
    for k, v in (headers or {}).items():
        name = str(k).strip()
        val = str(v)
        if name.lower() not in ALLOWED_EXTRA_HEADERS:
            raise BlockedURL(f"header not allowed: {name[:40]}")
        if name.lower() == "content-type" and method != "POST":
            raise BlockedURL("Content-Type is allowed only with POST")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name + val):
            raise BlockedURL("control characters in header")
        out[name.lower()] = val
    return {_canon(k): v for k, v in out.items()}


def _canon(name: str) -> str:
    return "-".join(p.capitalize() for p in name.split("-"))


class HttpClient:
    def __init__(self, cache_root: Path | str, *, manifest: Any = None, transport: Any = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = utcnow, budget: Optional[Budget] = None,
                 oc_provider: Optional[Callable[[], str]] = None, resolver: Optional[Callable[[str], list]] = None,
                 robots: Any = "default", refresh: bool = False, network: bool = True,
                 min_interval: float = MIN_INTERVAL_SECONDS, max_retries: int = MAX_RETRIES,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, lock_dir: Optional[Path | str] = None,
                 logger: Optional[logging.Logger] = None):
        self.cache = HttpCache(Path(cache_root))
        self.manifest = manifest
        self._transport = transport or UrllibTransport()
        self._clock, self._sleep, self._now = clock, sleep, now
        self.budget = budget
        self._oc_provider = oc_provider
        self._resolver = resolver if resolver is not None else (default_resolver if transport is None else None)
        self.refresh = refresh
        self.network = network
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.log = logger or logging.getLogger("press_review.ingestion.http")
        self._last_end: dict[str, float] = {}
        self._cross_run_checked: set[str] = set()
        self._lock_dir = Path(lock_dir) if lock_dir else None
        self._locks: dict[str, Any] = {}
        self.stats = {"network_requests": 0, "cache_hits": 0, "retries": 0}
        if robots == "default":
            from .robots import RobotsPolicy
            robots = RobotsPolicy(self)
        self.robots = robots

    # ---- 공개 API ----
    def get(self, url: str, *, with_oc: bool = False, accept: Optional[Callable[[bytes, dict], Optional[str]]] = None,
            refresh: Optional[bool] = None, max_bytes: Optional[int] = None,
            headers: Optional[dict] = None) -> Response:
        """headers: 추가 요청 헤더(허용 목록: Range, Referer, Accept, Accept-Language). Range 요청은 캐시를 읽지도 쓰지도 않는다."""
        return self._fetch("GET", url, with_oc, accept, refresh, max_bytes, headers=headers)

    def head(self, url: str, *, with_oc: bool = False, refresh: Optional[bool] = None,
             headers: Optional[dict] = None) -> Response:
        return self._fetch("HEAD", url, with_oc, None, refresh, None, headers=headers)

    def post(self, url: str, data: Any, *, accept: Optional[Callable[[bytes, dict], Optional[str]]] = None,
             refresh: Optional[bool] = None, max_bytes: Optional[int] = None,
             headers: Optional[dict] = None) -> Response:
        """허용 호스트의 ``/fileDownload.do`` 에만 보낼 수 있다(그 밖은 BlockedURL). data: dict(urlencode) 또는 bytes.

        OC 주입 불가, 리다이렉트는 따라가지 않고 실패로 기록한다. 캐시 키에는 본문 해시가 들어간다.
        """
        body = _encode_post_body(data)
        return self._fetch("POST", url, False, accept, refresh, max_bytes, headers=headers, body=body)

    def evict(self, url: str, *, with_oc: bool = False, method: str = "GET", data: Any = None) -> bool:
        host, display = self._prepare(url, with_oc)
        sha = hashlib.sha256(_encode_post_body(data)).hexdigest() if method == "POST" else None
        return self.cache.evict(host, method, display, sha)

    def close(self) -> None:
        for fh in self._locks.values():
            try:
                fh.close()
            except OSError:
                pass
        self._locks.clear()

    # ---- 내부 ----
    def _prepare(self, url: str, with_oc: bool) -> tuple[str, str]:
        if has_oc(url) and not with_oc:
            raise BlockedURL("OC must be injected via with_oc=True, not written in the url")
        base = strip_oc(url) if has_oc(url) else url.split("#", 1)[0]
        parts = validate_url(base)
        host = (parts.hostname or "").lower()
        display = base
        if with_oc:
            if not (host == DRF_HOST and parts.path.startswith(DRF_PATH_PREFIX)):
                raise BlockedURL("OC may only be sent to law.go.kr DRF paths")
            display = self._append_oc(base, "[MASKED]", raw=True)
        return host, display

    @staticmethod
    def _append_oc(url: str, value: str, raw: bool = False) -> str:
        sep = "&" if "?" in url else "?"
        v = value if raw else urllib.parse.quote(value, safe="")
        return f"{url}{sep}{OC_PARAM}={v}"

    def _fetch(self, method: str, url: str, with_oc: bool, accept: Optional[Callable], refresh: Optional[bool],
               max_bytes: Optional[int], headers: Optional[dict] = None, body: Optional[bytes] = None) -> Response:
        extra = _check_extra_headers(headers, method)
        host, display = self._prepare(url, with_oc)
        if method == "POST":
            if with_oc:
                raise BlockedURL("OC may not be sent with POST")
            _check_post_target(display)
        base = strip_oc(url) if has_oc(url) else url.split("#", 1)[0]
        body_sha = hashlib.sha256(body).hexdigest() if body is not None else None
        use_cache = "Range" not in extra                      # 부분 응답(206)을 전체 응답 키로 캐시하지 않는다
        use_refresh = self.refresh if refresh is None else refresh
        if use_cache and not use_refresh:
            hit = self.cache.get(host, method, display, body_sha)
            if hit is not None:
                reason = accept(hit.body, hit.headers) if (accept and method in ("GET", "POST")) else None
                if reason is None:
                    self.stats["cache_hits"] += 1
                    return hit
        if not self.network:
            raise DryRunMiss(f"{method} {display}")
        if self.budget:
            self.budget.check()
        resp = self._request(method, base, display, with_oc, max_bytes or DEFAULT_MAX_BYTES, extra_headers=extra, body=body)
        if with_oc:
            oc = self._oc()
            resp.body = scrub_body(resp.body, oc)
            if not logsafe.is_plain_word(oc) and self._oc_echoed(resp.body):
                # 문맥(OC=, <OC>, "OC":) 밖에 값이 남은 되울림: 값이 평범한 단어가 아닐 때만 단서로 본다.
                # 평범한 단어(예: 법령 문구에도 나올 수 있는 값)는 문맥 밖 출현이 정상 본문일 수 있어 본문을 버리지 않는다.
                resp.body = b""
                resp.content_error = "oc_echo_unmasked"
                self._fail(display, "http", "oc_echo_unmasked",
                           "response body contains the OC outside the 'OC=' form; body dropped and not cached")
        if resp.content_error is None and accept and method in ("GET", "POST") and 200 <= resp.status < 300:
            resp.content_error = accept(resp.body, resp.headers)
        if use_cache and 200 <= resp.status < 300 and resp.status != 206 and resp.content_error is None:
            self.cache.put(host, method, display, resp, body_sha)
        return resp

    def _request(self, method: str, base_url: str, display: str, with_oc: bool, max_bytes: int,
                 check_robots: bool = True, extra_headers: Optional[dict] = None, body: Optional[bytes] = None) -> Response:
        current = base_url
        for hop in range(MAX_REDIRECTS + 1):
            parts = validate_url(current)
            host = (parts.hostname or "").lower()
            request_url = current
            if with_oc:
                if not (host == DRF_HOST and parts.path.startswith(DRF_PATH_PREFIX)):
                    raise BlockedURL("OC may only be sent to law.go.kr DRF paths (redirect left DRF)")
                request_url = self._append_oc(current, self._oc())
            if self._resolver is not None:
                _check_resolved(host, self._resolver)
            if check_robots and self.robots is not None:
                pq = parts.path + (f"?{parts.query}" if parts.query else "")
                self.robots.check(host, pq or "/")
            raw = self._send_with_retries(method, host, request_url, display, max_bytes, extra_headers, body)
            if raw.status in REDIRECT_STATUSES and raw.headers.get("location"):
                if method == "POST":                      # 307/308은 POST를 유지하고 303은 GET으로 바뀐다: 어느 쪽도 따라가지 않는다
                    self._fail(display, "redirect", "post_redirect", "redirect on POST is not followed")
                    raise FetchFailed(f"redirect on POST not followed: {display}")
                if hop == MAX_REDIRECTS:
                    self._fail(display, "redirect", "too_many_redirects", f"more than {MAX_REDIRECTS} redirects")
                    raise FetchFailed(f"too many redirects for {display}")
                nxt = urllib.parse.urljoin(current, raw.headers["location"])
                current = strip_oc(nxt) if has_oc(nxt) else nxt.split("#", 1)[0]
                continue
            final = mask_url(current) if not with_oc else self._append_oc(current, "[MASKED]", raw=True)
            return Response(url=display, final_url=final, status=raw.status, headers=raw.headers,
                            body=b"" if method == "HEAD" else raw.body,
                            from_cache=False, fetched_at=iso(self._now()))
        raise AssertionError("unreachable")  # pragma: no cover

    def _oc_echoed(self, body: bytes) -> bool:
        oc = self._oc()
        forms = {oc, urllib.parse.quote(oc), urllib.parse.quote(oc, safe="")}
        for f in forms:
            if re.search(rb"(?<![A-Za-z0-9])" + re.escape(f.encode("utf-8")) + rb"(?![A-Za-z0-9])", body):
                return True
        return False

    def _oc(self) -> str:
        if self._oc_provider is None:
            from .. import secrets
            value = secrets.load_secret("LAW_OPEN_DATA_OC")
        else:
            value = self._oc_provider()
        if not value:
            raise IngestionError("OC is empty")
        return value

    def _send_with_retries(self, method: str, host: str, request_url: str, display: str, max_bytes: int,
                           extra_headers: Optional[dict] = None, body: Optional[bytes] = None) -> RawResponse:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers.update(extra_headers or {})
        kw = {"body": body} if body is not None else {}
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            self._acquire_host(host)
            self._throttle(host)
            if self.budget:
                self.budget.check()
            self.stats["network_requests"] += 1
            self.log.info("%s %s (attempt %d)", method, display, attempt + 1)
            retry_after = 0.0
            try:
                raw = self._transport.request(method, request_url, headers, self.timeout, max_bytes, **kw)
                self._mark_end(host)
                if raw.status not in RETRY_STATUSES:
                    return raw
                last = f"http {raw.status}"
                retry_after = _retry_after(raw.headers.get("retry-after"))
            except TransportError as e:
                self._mark_end(host)
                if not e.retriable:
                    self._fail(display, "http", e.kind, str(e), attempt)
                    raise FetchFailed(f"{e.kind} for {display}: {scrub_text(str(e))}") from None
                last = e.kind
            except ResponseTooLarge as e:
                self._mark_end(host)
                self._fail(display, "http", e.error_class, str(e), attempt)
                raise
            if attempt == self.max_retries:
                break
            self.stats["retries"] += 1
            delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
            self._sleep_checked(max(delay, min(retry_after, RETRY_AFTER_CAP_SECONDS)))
        self._fail(display, "http", "retries_exhausted", f"{last} after {self.max_retries + 1} attempts", self.max_retries)
        raise FetchFailed(f"{last} after {self.max_retries + 1} attempts: {display}")

    def _throttle(self, host: str) -> None:
        last = self._last_end.get(host)
        if last is not None:
            wait = self.min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep_checked(wait)
        elif host not in self._cross_run_checked:
            # 이 프로세스의 첫 요청: 앞선 실행(--max-seconds 로 이어 붙인 CLI)이 같은 호스트에 마지막으로 응답받은 시각을 본다.
            self._cross_run_checked.add(host)
            prev = self._read_last_end(host)
            if prev is not None:
                wait = min(self.min_interval, self.min_interval - (self._now().timestamp() - prev))
                if wait > 0:
                    self._sleep_checked(wait)

    def _stamp_path(self, host: str) -> Optional[Path]:
        return self._lock_dir / f"{host}.last" if self._lock_dir else None

    def _mark_end(self, host: str) -> None:
        """호스트별 마지막 응답 시각을 메모리(단조 시계)와 lock_dir 의 파일(벽시계)에 남긴다. 파일은 실행 사이 간격용이다."""
        self._last_end[host] = self._clock()
        path = self._stamp_path(host)
        if path is not None:
            try:
                private_mkdir(path.parent)
                path.write_text(repr(self._now().timestamp()), encoding="ascii")
            except OSError:
                pass                                   # 간격 기록 실패는 수집을 막지 않는다(메모리 간격은 유지)

    def _read_last_end(self, host: str) -> Optional[float]:
        path = self._stamp_path(host)
        if path is None:
            return None
        try:
            return float(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def _sleep_checked(self, seconds: float) -> None:
        if self.budget:
            self.budget.check()
            rem = self.budget.remaining()
            if rem is not None and rem < seconds:
                raise TimeBudgetExceeded("time budget would be exceeded while waiting")
        self._sleep(seconds)

    def _acquire_host(self, host: str) -> None:
        if fcntl is None or self._lock_dir is None or host in self._locks:
            return
        private_mkdir(self._lock_dir)
        fh = open(self._lock_dir / f"{host}.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise HostBusy(f"another ingestion process is using {host}") from None
        self._locks[host] = fh

    def _fail(self, target: str, stage: str, error_class: str, message: str, retry_count: Optional[int] = None) -> None:
        if self.manifest is not None and self.network:
            self.manifest.record_failure(target, stage, error_class, message, retry_count)


def _retry_after(v: Optional[str]) -> float:
    if not v:
        return 0.0
    try:
        return max(0.0, float(v.strip()))
    except ValueError:
        return 0.0
