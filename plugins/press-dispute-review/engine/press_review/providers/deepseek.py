"""DeepSeek API 제공자(수집·구조화 배치 전용). 계약: docs/P2_DESIGN.md §4.

분석 제공자(``AnalysisProvider``)가 아니다. 미공개 기사·사용자 원고는 이 경로로 보내지 않는다. 호출자(``ingestion/structure.py``)가
전송 직전에 권리(external_api = allowed)와 source_family 허용 목록을 검사하며, 이 모듈은 전송 목적지·모델을 코드 상수로 고정한다.

- 목적지: https://api.deepseek.com 만(``DEEPSEEK_BASE_URL`` 이 있어도 호스트가 정확히 api.deepseek.com 이고 https 일 때만 받는다).
  키가 환경변수 때문에 다른 곳으로 가지 않게 하려는 장치다. 요청 헤더는 기록하지 않는다.
- 모델: 허용 목록(``ALLOWED_MODELS``)은 코드 상수이고 공급자의 정식 이름(``deepseek-flash``, ``deepseek-v4-pro``)이다(P2_DESIGN G-D11).
  구 이름 ``deepseek-v4-flash`` 는 ``LEGACY_MODEL_ALIASES`` 로 정식 이름에 옮겨 받고 요청에는 정식 이름을 보낸다. 실행 시 ``GET /models`` 에
  정식 이름이 나열돼 있어야 한다(2026-10-09 실측: deepseek-flash, deepseek-v4-pro). 장부(``api_usage``)에는 요청 이름(``requested_model``)과
  응답이 돌려준 모델 이름(``model``)을 모두 남긴다.
- 가격: ``prices/deepseek_prices.json`` snapshot(확인일·출처 URL 포함). 피크/오프피크 시간대별 단가를 데이터로 갖는다.
  사전 예산 검사는 항상 피크 단가의 상한이다. 실제 비용은 호출 시각(UTC)이 주말이거나 피크 구간에서 충분히 먼 때만 오프피크로 계산한다
  (중국 공휴일은 계산하지 않으므로 그날은 과대 계산한다).
- 호출: OpenAI 호환 chat completions, JSON 출력(response_format json_object), 기본 thinking 비활성(추론 토큰이 출력 단가로 청구돼
  비용·max_tokens 가 예측 불가해지는 것을 막는다). thinking 설정은 호출자가 ``structurer_version`` 에 넣는다.
- 재시도(기본 2회, 지수 백오프, Retry-After 존중): 429·500·502·503·504·timeout·연결 오류, 빈 응답, 잘린 JSON(finish_reason=length 이면
  다음 시도의 max_tokens 를 2배로 올린다), insufficient_system_resource, content_filter, 파싱 불가 본문. 다 쓰면 ``CallFailed``(호출자가 dead-letter).
  400·422 등 다른 4xx 는 재시도 없이 ``RequestRejected``. 401·402 는 ``FatalProviderError``(실행 전체 중단: 원인을 가리지 않으려고 dead-letter 로 돌리지 않는다).
- 동시성은 기본 2(내부 세마포어로 강제). 스레드는 HTTP 호출만 하고 장부(``Manifest``)에는 쓰지 않는다: 시도 기록(``Attempt``)을 돌려주면
  호출자가 메인 스레드에서 ``record_attempts`` 로 ``api_usage`` 에 쓴다.
- 비용 상한: ``BudgetGuard`` 가 시도마다 (누적 + 예약 + 이번 시도 상한 추정) > 상한이면 ``BudgetExceeded`` 로 시작 전에 막는다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .. import logsafe, secrets
from ..ingestion.http import RawResponse, ResponseTooLarge, TransportError, UrllibTransport

PROVIDER_NAME = "deepseek"
ALLOWED_MODELS = ("deepseek-flash", "deepseek-v4-pro")
# 공급자가 계속 받는다고 안내한 구 이름(가격 snapshot 의 notes). 받아서 정식 이름으로 바꿀 뿐, 허용 목록에는 없다.
LEGACY_MODEL_ALIASES = {"deepseek-v4-flash": "deepseek-flash"}
ALLOWED_HOST = "api.deepseek.com"
DEFAULT_BASE_URL = "https://api.deepseek.com"
_ALLOWED_BASE_PATHS = ("", "/v1")
PRICES_PATH = Path(__file__).resolve().parent.parent / "prices" / "deepseek_prices.json"
USER_AGENT = "press-dispute-review/0.1 (+research; non-commercial index)"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOKENS = 16384
OUTPUT_TOKEN_CAP = 65536          # finish_reason=length 재시도에서 max_tokens 를 올리는 상한(모델 한도 384K 보다 훨씬 작게)
RETRIABLE_HTTP = frozenset({429, 500, 502, 503, 504})
FATAL_HTTP = frozenset({401, 402})
# 실제 토큰 수를 알 수 없을 때 쓰는 상한 환산: 글자 1개 = 토큰 1개(한글·ASCII 모두 실제보다 크거나 같다고 본다).
CHARS_TO_TOKENS_UPPER = 1.0
MESSAGE_OVERHEAD_TOKENS = 16


def canonical_model(name: str) -> str:
    """요청 이름 -> 허용 목록의 정식 이름. 구 이름은 옮기고, 목록에 없으면 ModelNotAllowed."""
    n = LEGACY_MODEL_ALIASES.get(name, name)
    if n not in ALLOWED_MODELS:
        raise ModelNotAllowed(f"model '{name}' is not in the allowlist {list(ALLOWED_MODELS)}")
    return n


# 요청 바이트가 서버에 닿지 않았음이 확실한 연결 오류(이름 해석 실패, 연결 거부)만 0원 정산한다.
# 그 밖의 timeout·reset·불완전 본문은 서버가 처리·청구했을 수 있어 예약액으로 보수 정산한다(CX2-4).
_NOT_SENT_MARKERS = ("dns lookup failed", "ConnectionRefusedError")


def _possibly_billed(e: TransportError) -> bool:
    if e.kind == "timeout":
        return True
    if e.kind == "connection":
        msg = str(e)
        return not any(m in msg for m in _NOT_SENT_MARKERS)
    if e.kind == "tls":                            # 핸드셰이크 실패는 미전송, 응답 본문을 읽다 난 TLS 오류는 전송 뒤(CX3)
        return "during body read" in str(e)
    return False


# ---------------------------------------------------------------- 오류
class DeepSeekError(Exception):
    error_class = "deepseek_error"

    def __init__(self, message: str = "", *, error_class: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message or (error_class or self.error_class))
        if error_class:
            self.error_class = error_class
        self.status = status
        self.attempts: list = []          # 실패 전에 이미 한 시도(청구됐을 수 있다). 호출자가 api_usage 에 기록한다.
        self.last_text: Optional[str] = None


class ModelNotAllowed(DeepSeekError):
    error_class = "model_not_allowed"


class ModelUnavailable(DeepSeekError):
    error_class = "model_unavailable"


class BudgetExceeded(DeepSeekError):
    error_class = "budget_exceeded"


class FatalProviderError(DeepSeekError):
    """401·402 등: 이후 모든 호출이 같은 이유로 실패한다. 실행 전체를 멈춘다."""
    error_class = "fatal_provider_error"


class RequestRejected(DeepSeekError):
    """400·422 등 재시도해도 같은 결과일 요청 오류. 호출자는 작업을 dead-letter 로 보낸다."""
    error_class = "request_rejected"


class CallFailed(DeepSeekError):
    """재시도를 다 쓰고도 쓸 수 있는 응답을 못 받았다. ``attempts`` 와 마지막 응답 본문(dead-letter 보관용)을 가진다."""
    error_class = "call_failed"

    def __init__(self, message: str, *, error_class: str, attempts: list, last_text: Optional[str] = None):
        super().__init__(message, error_class=error_class)
        self.attempts = attempts
        self.last_text = last_text


# ---------------------------------------------------------------- 가격
class PriceBook:
    """가격 snapshot 과 피크/오프피크 규칙. 데이터가 정본이고 코드는 규칙을 읽기만 한다."""

    def __init__(self, data: dict):
        self.data = data
        pk = data["peak_hours_utc"]
        self._weekdays = set(pk["weekdays"])
        self._ranges = [(self._minutes(a), self._minutes(b)) for a, b in pk["ranges"]]
        self._guard = int(pk.get("guard_minutes", 0))
        self._excluded = set(pk.get("excluded_dates", []))

    @staticmethod
    def _minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> "PriceBook":
        return cls(json.loads(Path(path or PRICES_PATH).read_text(encoding="utf-8")))

    def entry(self, model: str) -> dict:
        """가격 항목. 정식 이름(``canonical_id``)·구 이름·가격표 키 어느 쪽으로 불러도 같은 항목이다."""
        name = LEGACY_MODEL_ALIASES.get(model, model)
        models = self.data["models"]
        if name in models:
            return models[name]
        for e in models.values():
            if e.get("canonical_id") == name:
                return e
        raise ModelNotAllowed(f"no price entry for model '{model}'")

    def canonical_id(self, model: str) -> str:
        return self.entry(model).get("canonical_id", model)

    def tier(self, at: datetime) -> str:
        """'peak' | 'off_peak'. 주말·제외일은 오프피크, 피크 구간 경계 ±guard 분은 피크로 본다(과대 계산 쪽)."""
        t = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        if t.date().isoformat() in self._excluded or t.weekday() not in self._weekdays:
            return "off_peak"
        m = t.hour * 60 + t.minute
        for a, b in self._ranges:
            if a - self._guard <= m < b + self._guard:
                return "peak"
        return "off_peak"

    def rates(self, model: str, at: datetime) -> tuple[str, dict]:
        tier = self.tier(at)
        return tier, self.entry(model)[tier]

    def snapshot(self, model: str, tier: str) -> dict:
        e = self.entry(model)
        return {"snapshot_id": self.data["snapshot_id"], "source_url": self.data["source_url"], "checked_at": self.data["checked_at"],
                "unit": self.data["unit"], "requested_model": model, "canonical_id": e.get("canonical_id"), "tier": tier,
                "rates": e[tier]}

    def cost(self, model: str, at: datetime, *, cache_hit: int, cache_miss: int, completion: int) -> tuple[float, str]:
        tier, r = self.rates(model, at)
        usd = (cache_hit * r["input_cache_hit"] + cache_miss * r["input_cache_miss"] + completion * r["output"]) / 1_000_000
        return round(usd, 8), tier

    def upper_bound(self, model: str, prompt_tokens_upper: int, max_tokens: int) -> float:
        """사전 예산 검사용 상한: 피크 단가, 입력은 전부 캐시 미적중, 출력은 max_tokens 전부."""
        r = self.entry(model)["peak"]
        return round((prompt_tokens_upper * r["input_cache_miss"] + max_tokens * r["output"]) / 1_000_000, 8)


# ---------------------------------------------------------------- 예산
class BudgetGuard:
    """실행 단위 비용 상한(--budget-usd). limit 이 None 이면 무제한. 스레드 안전."""

    def __init__(self, limit_usd: Optional[float] = None):
        if limit_usd is not None and limit_usd < 0:
            raise ValueError("budget must be >= 0")
        self.limit = limit_usd
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = threading.Lock()

    def reserve(self, est: float) -> None:
        with self._lock:
            if self.limit is not None and self.spent + self.reserved + est > self.limit + 1e-12:
                raise BudgetExceeded(f"estimated cost {est:.6f} USD exceeds remaining budget "
                                     f"{max(0.0, self.limit - self.spent - self.reserved):.6f} USD (limit {self.limit:.6f})")
            self.reserved += est

    def settle(self, est: float, actual: float) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - est)
            self.spent += actual

    def remaining(self) -> Optional[float]:
        with self._lock:
            return None if self.limit is None else max(0.0, self.limit - self.spent - self.reserved)

    def would_fit(self, est: float) -> bool:
        with self._lock:
            return self.limit is None or self.spent + self.reserved + est <= self.limit + 1e-12


# ---------------------------------------------------------------- 결과 형식
@dataclass
class Attempt:
    n: int
    status: str                       # ok | empty | truncated | parse_error | http_<code> | transport_<kind> | filtered | resource
    model: str                        # 응답이 돌려준 model 문자열(없으면 요청 모델)
    requested_model: str
    at: str = ""
    max_tokens: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: Optional[int] = None
    cost_usd: float = 0.0
    tier: str = ""
    finish_reason: Optional[str] = None
    latency_ms: int = 0
    error_class: Optional[str] = None
    price_snapshot: Optional[dict] = None


@dataclass
class CallResult:
    obj: dict
    text: str
    attempts: list
    cost_usd: float
    model: str


def record_attempts(manifest: Any, attempts: list, *, job_id: Optional[str], target_id: Optional[str]) -> float:
    """시도마다 ``api_usage`` 한 행(실패 시도도 기록: 토큰 0이면 비용 0). 메인 스레드에서만 부른다. 합계 비용을 돌려준다."""
    total = 0.0
    for a in attempts:
        snap = dict(a.price_snapshot or {})
        snap["returned_model"] = a.model
        if a.reasoning_tokens is not None:
            snap["reasoning_tokens"] = a.reasoning_tokens
        snap["max_tokens"] = a.max_tokens
        manifest.record_api_usage(
            PROVIDER_NAME, a.model or a.requested_model, requested_model=a.requested_model, price_snapshot=snap, prompt_tokens=a.prompt_tokens,
            cache_hit_tokens=a.cache_hit_tokens, cache_miss_tokens=a.cache_miss_tokens, completion_tokens=a.completion_tokens,
            cost_usd=a.cost_usd, job_id=job_id, target_id=target_id, attempt=a.n, status=a.status, error_class=a.error_class,
            latency_ms=a.latency_ms, finish_reason=a.finish_reason)
        total += a.cost_usd
    return total


# ---------------------------------------------------------------- 보조
def resolve_base_url(value: Optional[str] = None) -> str:
    """``DEEPSEEK_BASE_URL``(또는 인자)을 검증한다. https, 호스트 정확히 api.deepseek.com, 자격증명·쿼리·비표준 포트 없음."""
    v = (value if value is not None else os.environ.get("DEEPSEEK_BASE_URL", "")).strip() or DEFAULT_BASE_URL
    try:
        p = urllib.parse.urlsplit(v)
        port = p.port
    except ValueError:
        raise ValueError("DEEPSEEK_BASE_URL is not a valid URL") from None
    path = p.path.rstrip("/")
    if (p.scheme != "https" or p.hostname != ALLOWED_HOST or port not in (None, 443) or p.username or p.password
            or p.query or p.fragment or path not in _ALLOWED_BASE_PATHS):
        raise ValueError(f"DEEPSEEK_BASE_URL must be https://{ALLOWED_HOST}[/v1]")
    return f"https://{ALLOWED_HOST}{path}"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_object(text: str) -> Any:
    """응답 content 를 JSON 객체로 읽는다. 코드펜스 하나는 벗긴다. 실패하면 ValueError."""
    t = text.strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1)
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON value is not an object")
    return obj


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _retry_after(headers: dict) -> Optional[float]:
    v = (headers or {}).get("retry-after")
    try:
        return max(0.0, min(60.0, float(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 제공자
class DeepSeekProvider:
    name = PROVIDER_NAME

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None, transport: Any = None,
                 sleep: Callable[[float], None] = time.sleep, now: Optional[Callable[[], datetime]] = None,
                 prices: Optional[PriceBook] = None, max_retries: int = 2, concurrency: int = 2, timeout: float = 300.0,
                 backoff_base: float = 2.0, backoff_cap: float = 30.0, thinking: str = "disabled",
                 reasoning_effort: str = "high", budget: Optional[BudgetGuard] = None, temperature: float = 0.0,
                 output_token_cap: int = OUTPUT_TOKEN_CAP):
        if thinking not in ("disabled", "enabled"):
            raise ValueError("thinking must be 'disabled' or 'enabled'")
        if max_retries < 0 or concurrency < 1:
            raise ValueError("max_retries >= 0 and concurrency >= 1 required")
        self.base_url = resolve_base_url(base_url)
        self.transport = transport or UrllibTransport()
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.prices = prices or PriceBook.load()
        self.max_retries = max_retries
        self.concurrency = concurrency
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.budget = budget or BudgetGuard(None)
        self.output_token_cap = output_token_cap
        self._api_key = api_key
        self._sem = threading.BoundedSemaphore(concurrency)
        self._models_cache: Optional[set] = None
        self._lock = threading.Lock()
        if api_key:
            logsafe.register_secret(api_key)

    # ---- 키·요청 ----
    def _key(self) -> str:
        if not self._api_key:
            self._api_key = secrets.load_secret("DEEPSEEK_API_KEY")
        return self._api_key

    def _request(self, method: str, path: str, body: Optional[dict]) -> RawResponse:
        headers = {"Authorization": f"Bearer {self._key()}", "Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with self._sem:
            return self.transport.request(method, self.base_url + path, headers, self.timeout, MAX_RESPONSE_BYTES, data)

    # ---- 모델 ----
    def list_models(self, *, refresh: bool = False) -> set:
        """``GET /models`` 의 id 집합(한 인스턴스에서 한 번만 호출한다)."""
        with self._lock:
            if self._models_cache is not None and not refresh:
                return set(self._models_cache)
        r = None
        failure: Optional[DeepSeekError] = None
        for attempt in range(self.max_retries + 1):          # 채팅 호출과 같은 재시도 집합(429·5xx·일시적 전송 오류)을 지수 백오프로
            if attempt:
                self.sleep(min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1))))
            try:
                r = self._request("GET", "/models", None)
            except TransportError as e:
                failure = DeepSeekError(f"models listing failed ({e.kind})", error_class="models_listing_failed")
                if e.retriable:
                    continue
                raise failure from None
            if r.status in RETRIABLE_HTTP:
                failure = DeepSeekError(f"models listing failed: HTTP {r.status}", error_class="models_listing_failed", status=r.status)
                continue
            failure = None
            break
        if failure is not None:
            raise failure
        if r.status in FATAL_HTTP:
            raise FatalProviderError(f"models listing refused: HTTP {r.status}", status=r.status)
        if r.status != 200:
            raise DeepSeekError(f"models listing failed: HTTP {r.status}", error_class="models_listing_failed", status=r.status)
        try:
            ids = {str(m["id"]) for m in json.loads(r.body.decode("utf-8"))["data"]}
        except (ValueError, KeyError, TypeError):
            raise DeepSeekError("models listing is not valid JSON", error_class="models_listing_failed") from None
        with self._lock:
            self._models_cache = ids
        return set(ids)

    def verify_model(self, model: str) -> str:
        """허용 목록(코드 상수) -> /models 확인. 정식 이름이 목록에 있어야 통과하고, 통과한 정식 이름을 돌려준다."""
        name = canonical_model(model)
        if name not in self.list_models():
            raise ModelUnavailable(f"model '{name}' is not listed by the API")
        return name

    # ---- 비용 ----
    def estimate_prompt_tokens(self, messages: list) -> int:
        chars = sum(len(str(m.get("content", ""))) for m in messages)
        return int(chars * CHARS_TO_TOKENS_UPPER) + MESSAGE_OVERHEAD_TOKENS * len(messages)

    def estimate_cost(self, model: str, messages: list, max_tokens: int = DEFAULT_MAX_TOKENS) -> float:
        """한 번의 시도의 비용 상한(피크 단가, 입력 전부 캐시 미적중, 출력 max_tokens 전부). 실제보다 크다. 토큰 수는 글자 수로 환산한 상한이다."""
        model = canonical_model(model)
        return self.prices.upper_bound(model, self.estimate_prompt_tokens(messages), max_tokens)

    # ---- 호출 ----
    def _body(self, model: str, messages: list, max_tokens: int) -> dict:
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False,
                      "response_format": {"type": "json_object"}, "thinking": {"type": self.thinking}}
        if self.thinking == "enabled":
            body["reasoning_effort"] = self.reasoning_effort
        else:
            body["temperature"] = self.temperature
        return body

    def complete_json(self, model: str, messages: list, *, max_tokens: int = DEFAULT_MAX_TOKENS,
                      job_id: Optional[str] = None, target_id: Optional[str] = None) -> CallResult:
        """JSON 객체 응답을 받는다. 시도 기록은 결과(``CallResult.attempts``)나 예외(``CallFailed.attempts``)에 담긴다.

        예외: ModelNotAllowed, BudgetExceeded(시작 전 차단, 이미 한 시도가 있으면 호출자가 그 기록을 못 받으므로 ``attempts`` 속성에 담는다),
        FatalProviderError, RequestRejected, CallFailed.
        """
        model = canonical_model(model)
        if not any("json" in str(m.get("content", "")).lower() for m in messages):
            raise ValueError("JSON output mode requires the word 'json' in the prompt")
        attempts: list[Attempt] = []
        cur_max = min(max_tokens, self.output_token_cap) if max_tokens > self.output_token_cap else max_tokens
        failures = 0
        last_text: Optional[str] = None
        while True:
            est = self.estimate_cost(model, messages, cur_max)
            try:
                self.budget.reserve(est)
            except BudgetExceeded as e:
                e.attempts = attempts
                raise
            n = len(attempts) + 1
            t0 = time.monotonic()
            started = self.now()
            att = Attempt(n=n, status="", model=model, requested_model=model, at=started.astimezone(timezone.utc).isoformat(timespec="seconds"),
                          max_tokens=cur_max)
            retry_delay: Optional[float] = None
            obj: Optional[dict] = None
            settled = False
            try:
                try:
                    raw = self._request("POST", "/chat/completions", self._body(model, messages, cur_max))
                except TransportError as e:
                    att.status, att.error_class = f"transport_{e.kind}", f"transport_{e.kind}"
                    if _possibly_billed(e):          # CX1-4/CX2-4: 요청이 서버에 닿았을 수 있다(timeout·reset·불완전 본문). 예약액(상한)으로 보수 정산한다
                        att.cost_usd, att.tier = est, "peak"
                        att.price_snapshot = self.prices.snapshot(model, "peak")
                    if not e.retriable:
                        att.latency_ms = int((time.monotonic() - t0) * 1000)
                        attempts.append(att)
                        err = RequestRejected(f"transport failure: {e.kind}", error_class=f"transport_{e.kind}")
                        err.attempts = attempts
                        raise err from None
                    outcome = "retry"
                except ResponseTooLarge:
                    att.status, att.error_class = "response_too_large", "response_too_large"
                    att.latency_ms = int((time.monotonic() - t0) * 1000)
                    attempts.append(att)
                    err = RequestRejected("response too large", error_class="response_too_large")
                    err.attempts = attempts
                    raise err from None
                else:
                    outcome, last_text, obj = self._interpret(raw, att, model, started, last_text,
                                                              prompt_tokens_upper=self.estimate_prompt_tokens(messages))
                    if outcome == "retry":
                        retry_delay = _retry_after(raw.headers)
                att.latency_ms = int((time.monotonic() - t0) * 1000)
                attempts.append(att)
                self.budget.settle(est, att.cost_usd)
                settled = True
            finally:
                if not settled:
                    self.budget.settle(est, 0.0)
            if outcome == "ok":
                return CallResult(obj=obj or {}, text=last_text or "", attempts=attempts,
                                  cost_usd=sum(a.cost_usd for a in attempts), model=att.model)
            if outcome == "fatal":
                err = FatalProviderError(f"HTTP {att.status.removeprefix('http_')}: provider refused the request",
                                         error_class=att.error_class or "fatal_provider_error", status=_status_of(att.status))
                err.attempts = attempts
                raise err
            if outcome == "reject":
                err = RequestRejected(f"HTTP {att.status.removeprefix('http_')}: request rejected", error_class=att.error_class,
                                      status=_status_of(att.status))
                err.attempts = attempts
                err.last_text = last_text
                raise err
            failures += 1
            if failures > self.max_retries:
                raise CallFailed(f"gave up after {len(attempts)} attempts: {att.error_class}", error_class=att.error_class or "call_failed",
                                 attempts=attempts, last_text=last_text)
            if att.status == "truncated":
                cur_max = min(cur_max * 2, self.output_token_cap)
            delay = retry_delay if retry_delay is not None else min(self.backoff_cap, self.backoff_base * (2 ** (failures - 1)))
            self.sleep(delay)

    def _charge(self, att: Attempt, model: str, started: datetime, usage: dict, prompt_tokens_upper: int) -> None:
        """응답의 usage 로 시도 비용을 채운다. usage 가 없으면 상한 추정(입력은 요청 메시지의 글자 수 환산 상한, 출력은 max_tokens 전부)으로 보수적으로 계산한다."""
        hit = _int(usage.get("prompt_cache_hit_tokens", (usage.get("prompt_tokens_details") or {}).get("cached_tokens")))
        total_prompt = _int(usage.get("prompt_tokens"))
        miss = _int(usage.get("prompt_cache_miss_tokens")) if "prompt_cache_miss_tokens" in usage else max(0, total_prompt - hit)
        att.prompt_tokens, att.cache_hit_tokens, att.cache_miss_tokens = hit + miss, hit, miss
        att.completion_tokens = _int(usage.get("completion_tokens"))
        det = usage.get("completion_tokens_details")
        att.reasoning_tokens = _int(det.get("reasoning_tokens")) if isinstance(det, dict) and "reasoning_tokens" in det else None
        if usage:
            att.cost_usd, att.tier = self.prices.cost(model, started, cache_hit=hit, cache_miss=miss, completion=att.completion_tokens)
        else:
            att.cost_usd = self.prices.upper_bound(model, prompt_tokens_upper, att.max_tokens)
            att.tier = "peak"
        att.price_snapshot = self.prices.snapshot(model, att.tier)

    def _interpret(self, raw: RawResponse, att: Attempt, model: str, started: datetime,
                   prev_text: Optional[str], prompt_tokens_upper: int = 0) -> tuple[str, Optional[str], Optional[dict]]:
        """HTTP 응답 하나를 분류한다. 반환 (ok|retry|reject|fatal, 마지막 본문 텍스트, 파싱된 객체). att 를 채운다."""
        st = raw.status
        if st in FATAL_HTTP:
            att.status, att.error_class = f"http_{st}", f"http_{st}"
            return "fatal", prev_text, None
        if st in RETRIABLE_HTTP:
            att.status, att.error_class = f"http_{st}", f"http_{st}"
            return "retry", prev_text, None
        if st != 200:
            att.status, att.error_class = f"http_{st}", f"http_{st}"
            return "reject", prev_text, None
        try:
            payload = json.loads(raw.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        usage_raw = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        if isinstance(payload, dict) and payload.get("model"):
            att.model = str(payload.get("model"))
        choice: Any = None
        content: Any = None
        try:
            choice = payload["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            choice = None
        if choice is None:                      # CX1-4: HTTP 200 이면 청구됐을 수 있다. 본문 파싱과 독립으로 사용량을 회수하고, 없으면 상한으로 정산한다
            self._charge(att, model, started, usage, prompt_tokens_upper)
            att.status, att.error_class = "parse_error", "response_envelope_invalid"
            return "retry", prev_text, None
        att.finish_reason = choice.get("finish_reason")
        self._charge(att, model, started, usage, prompt_tokens_upper)
        text = content if isinstance(content, str) else ""
        fr = att.finish_reason
        if fr == "insufficient_system_resource":
            att.status, att.error_class = "resource", "insufficient_system_resource"
            return "retry", text or prev_text, None
        if fr == "content_filter":
            att.status, att.error_class = "filtered", "content_filter"
            return "retry", text or prev_text, None
        if fr == "length":                                    # 본문이 비어도(thinking 이 max_tokens 를 다 쓴 경우) 잘림으로 본다: 다음 시도에서 max_tokens 를 올린다
            att.status, att.error_class = "truncated", "truncated_output"
            return "retry", text or prev_text, None
        if not text.strip():
            att.status, att.error_class = "empty", "empty_response"
            return "retry", prev_text, None
        try:
            obj = parse_json_object(text)
            json.dumps(obj, ensure_ascii=False).encode("utf-8")   # 짝 없는 서로게이트 이스케이프(\ud83d)는 저장·장부에 쓸 수 없다: 유효하지 않은 JSON 으로 본다
        except (ValueError, UnicodeEncodeError):
            att.status, att.error_class = "parse_error", "invalid_json"
            return "retry", text, None
        att.status = "ok"
        return "ok", text, obj


def _status_of(status: str) -> Optional[int]:
    m = re.fullmatch(r"http_(\d+)", status)
    return int(m.group(1)) if m else None
