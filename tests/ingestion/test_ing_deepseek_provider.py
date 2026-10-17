"""DeepSeek 제공자: 허용 목록·/models 확인·재시도·백오프·예산·비용·호스트 고정. 가짜 transport, 네트워크 없음."""
import json

import pytest

import pdr_ing_paths  # noqa: F401
from ds_fakes import FRI_PEAK, SAT_OFF, TEST_KEY, DSFake, chat_payload, raw
from press_review import logsafe
from press_review.ingestion.http import ResponseTooLarge, TransportError
from press_review.providers import deepseek as ds

MSGS = [{"role": "system", "content": "Answer in json."}, {"role": "user", "content": "문서 본문 " * 20}]


def make(handler, *, now=FRI_PEAK, sleeps=None, **kw):
    fake = DSFake(handler, **{k: kw.pop(k) for k in ("models", "models_status") if k in kw})
    sl = sleeps if sleeps is not None else []
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=sl.append, now=lambda: now, **kw)
    return prov, fake, sl


def ok(obj=None, **kw):
    return lambda b, n: (200, chat_payload(obj if obj is not None else {"cases": []}, **kw), {})


def test_prices_snapshot_is_dated_and_sourced():
    pb = ds.PriceBook.load()
    assert pb.data["source_url"] == "https://api-docs.deepseek.com/quick_start/pricing" and pb.data["checked_at"] == "2026-10-09"
    assert {e["canonical_id"] for e in pb.data["models"].values()} == set(ds.ALLOWED_MODELS)      # G-D11: 허용 목록은 가격표의 정식 이름
    for m in ds.ALLOWED_MODELS:
        e = pb.entry(m)
        for tier in ("peak", "off_peak"):
            assert set(e[tier]) == {"input_cache_hit", "input_cache_miss", "output"}
        assert e["off_peak"]["output"] * 2 == pytest.approx(e["peak"]["output"])
    assert pb.canonical_id("deepseek-v4-flash") == "deepseek-flash"


def test_peak_and_off_peak_tiers_follow_the_rule_data():
    pb = ds.PriceBook.load()
    from datetime import datetime, timezone
    t = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    assert pb.tier(t(2026, 10, 9, 7, 0)) == "peak"          # 금 07:00
    assert pb.tier(t(2026, 10, 9, 2, 0)) == "peak"          # 금 02:00
    assert pb.tier(t(2026, 10, 9, 5, 0)) == "off_peak"      # 금 05:00 (두 구간 사이)
    assert pb.tier(t(2026, 10, 9, 12, 0)) == "off_peak"
    assert pb.tier(t(2026, 10, 9, 10, 2)) == "peak"         # 경계 +5분은 피크로 본다(과대 계산)
    assert pb.tier(t(2026, 10, 9, 10, 30)) == "off_peak"
    assert pb.tier(t(2026, 10, 10, 7, 0)) == "off_peak"     # 토요일
    assert pb.tier(t(2026, 10, 11, 8, 0)) == "off_peak"     # 일요일


def test_cost_uses_hit_miss_output_rates_by_tier():
    prov, _fake, _ = make(ok(hit=1000, miss=2000, completion=500), now=FRI_PEAK)
    r = prov.complete_json("deepseek-v4-flash", MSGS)
    expect = (1000 * 0.006 + 2000 * 0.3 + 500 * 1.2) / 1_000_000
    assert r.cost_usd == pytest.approx(expect) and r.attempts[0].tier == "peak"
    prov2, _f2, _ = make(ok(hit=1000, miss=2000, completion=500), now=SAT_OFF)
    r2 = prov2.complete_json("deepseek-v4-flash", MSGS)
    assert r2.cost_usd == pytest.approx((1000 * 0.003 + 2000 * 0.15 + 500 * 0.6) / 1_000_000) and r2.attempts[0].tier == "off_peak"
    prov3, _f3, _ = make(ok(hit=10, miss=20, completion=5), now=FRI_PEAK)
    assert prov3.complete_json("deepseek-v4-pro", MSGS).cost_usd == pytest.approx((10 * 0.044 + 20 * 1.32 + 5 * 3.96) / 1_000_000)


def test_returned_model_name_differs_from_requested_and_price_lookup_still_works():
    prov, _f, _ = make(ok(model="deepseek-v4.1-flash"))
    r = prov.complete_json("deepseek-v4-flash", MSGS)               # 구 이름으로 요청하면 정식 이름으로 보낸다(G-D11)
    assert r.model == "deepseek-v4.1-flash" and r.attempts[0].requested_model == "deepseek-flash"
    assert r.attempts[0].price_snapshot["requested_model"] == "deepseek-flash"


def test_request_body_and_headers():
    prov, fake, _ = make(ok())
    prov.complete_json("deepseek-v4-flash", MSGS, max_tokens=1234)
    b = fake.bodies[0]
    assert b["model"] == "deepseek-flash" and b["response_format"] == {"type": "json_object"} and b["max_tokens"] == 1234
    assert b["thinking"] == {"type": "disabled"} and b["stream"] is False and b["temperature"] == 0.0 and "reasoning_effort" not in b
    h = fake.headers[0]
    assert h["Authorization"] == f"Bearer {TEST_KEY}" and h["Content-Type"] == "application/json"
    prov2, fake2, _ = make(ok(), thinking="enabled")
    prov2.complete_json("deepseek-v4-pro", MSGS)
    assert fake2.bodies[0]["thinking"] == {"type": "enabled"} and fake2.bodies[0]["reasoning_effort"] == "high" and "temperature" not in fake2.bodies[0]


def test_json_word_required_in_prompt():
    prov, fake, _ = make(ok())
    with pytest.raises(ValueError):
        prov.complete_json("deepseek-v4-flash", [{"role": "user", "content": "문서"}])
    assert fake.calls == []


def test_model_outside_allowlist_is_refused_without_any_request():
    prov, fake, _ = make(ok())
    for bad in ("deepseek-chat", "gpt-5", "deepseek-v4"):          # G-D11: deepseek-flash 는 이제 허용 목록의 정식 이름이다
        with pytest.raises(ds.ModelNotAllowed):
            prov.verify_model(bad)
        with pytest.raises(ds.ModelNotAllowed):
            prov.complete_json(bad, MSGS)
    assert fake.calls == []


def test_models_endpoint_checked_once_and_canonical_name_accepted():
    prov, fake, _ = make(ok(), models=("deepseek-flash", "deepseek-v4-pro"))
    prov.verify_model("deepseek-v4-flash")      # 구 이름은 나열돼 있지 않아도 현행 이름(deepseek-flash)이 있으면 통과
    prov.verify_model("deepseek-v4-pro")
    prov.verify_model("deepseek-v4-flash")
    assert fake.calls == [("GET", "/models")]


def test_model_missing_from_models_listing():
    prov, _fake, _ = make(ok(), models=("deepseek-v4-pro",))
    with pytest.raises(ds.ModelUnavailable):
        prov.verify_model("deepseek-v4-flash")


def test_429_retries_with_exponential_backoff_then_succeeds():
    seq = [(429, {"error": {}}, {}), (503, {"error": {}}, {}), (200, chat_payload({"cases": []}), {})]
    prov, fake, sleeps = make(lambda b, n: seq[n - 1])
    r = prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.n_chat == 3 and sleeps == [2.0, 4.0]
    assert [a.status for a in r.attempts] == ["http_429", "http_503", "ok"]
    assert r.attempts[0].cost_usd == 0 and r.cost_usd > 0


def test_retry_after_header_is_honoured():
    seq = [(429, {"error": {}}, {"Retry-After": "7"}), (200, chat_payload({"cases": []}), {})]
    prov, _f, sleeps = make(lambda b, n: seq[n - 1])
    prov.complete_json("deepseek-v4-flash", MSGS)
    assert sleeps == [7.0]


def test_timeout_is_retried_and_gives_up_after_two_retries():
    prov, fake, sleeps = make(lambda b, n: TransportError("timeout", "request timed out"))
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.n_chat == 3 and len(sleeps) == 2 and ei.value.error_class == "transport_timeout"
    assert [a.status for a in ei.value.attempts] == ["transport_timeout"] * 3


def test_empty_response_retried_twice_then_call_failed():
    prov, fake, sleeps = make(lambda b, n: (200, chat_payload("   ", completion=0), {}))
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.n_chat == 3 and ei.value.error_class == "empty_response" and len(sleeps) == 2


def test_truncated_json_retries_with_doubled_max_tokens_then_fails():
    prov, fake, _ = make(lambda b, n: (200, chat_payload('{"cases": [{"case_number": "20', finish="length", completion=100), {}))
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-v4-flash", MSGS, max_tokens=1000)
    assert [b["max_tokens"] for b in fake.bodies] == [1000, 2000, 4000]
    assert ei.value.error_class == "truncated_output" and ei.value.last_text.startswith('{"cases"')
    assert all(a.cost_usd > 0 for a in ei.value.attempts)      # 잘린 응답도 청구된다


def test_invalid_json_and_non_object_are_content_failures():
    prov, fake, _ = make(lambda b, n: (200, chat_payload("not json at all"), {}))
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert ei.value.error_class == "invalid_json" and fake.n_chat == 3
    prov2, _f2, _ = make(lambda b, n: (200, chat_payload("[1, 2]"), {}))
    with pytest.raises(ds.CallFailed):
        prov2.complete_json("deepseek-v4-flash", MSGS)


def test_code_fence_around_json_is_stripped():
    prov, _f, _ = make(lambda b, n: (200, chat_payload('```json\n{"cases": []}\n```'), {}))
    assert prov.complete_json("deepseek-v4-flash", MSGS).obj == {"cases": []}


def test_resource_and_filter_finish_reasons_are_retried():
    seq = [(200, chat_payload("", finish="insufficient_system_resource"), {}), (200, chat_payload("x", finish="content_filter"), {}),
           (200, chat_payload({"cases": []}), {})]
    prov, fake, _ = make(lambda b, n: seq[n - 1])
    r = prov.complete_json("deepseek-v4-flash", MSGS)
    assert [a.status for a in r.attempts] == ["resource", "filtered", "ok"]


def test_400_422_rejected_without_retry():
    for code in (400, 422):
        prov, fake, _ = make(lambda b, n, c=code: (c, {"error": {"message": "bad"}}, {}))
        with pytest.raises(ds.RequestRejected) as ei:
            prov.complete_json("deepseek-v4-flash", MSGS)
        assert fake.n_chat == 1 and ei.value.status == code


def test_401_402_are_fatal_not_retried():
    for code in (401, 402):
        prov, fake, _ = make(lambda b, n, c=code: (c, {"error": {"message": "x"}}, {}))
        with pytest.raises(ds.FatalProviderError) as ei:
            prov.complete_json("deepseek-v4-flash", MSGS)
        assert fake.n_chat == 1 and ei.value.status == code


def test_oversized_response_is_rejected():
    prov, fake, _ = make(lambda b, n: ResponseTooLarge("too big"))
    with pytest.raises(ds.RequestRejected):
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.n_chat == 1


def test_reasoning_tokens_recorded_when_present():
    prov, _f, _ = make(ok(completion=300, reasoning=200))
    assert prov.complete_json("deepseek-v4-flash", MSGS).attempts[0].reasoning_tokens == 200


def test_budget_precheck_blocks_before_any_request():
    prov, fake, _ = make(ok(), budget=ds.BudgetGuard(0.0000001))
    est = prov.estimate_cost("deepseek-v4-flash", MSGS, 16384)
    assert est > 0.0000001
    with pytest.raises(ds.BudgetExceeded):
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.calls == []


def test_budget_accumulates_actual_spend_and_blocks_next_call():
    est = ds.DeepSeekProvider(api_key=TEST_KEY, transport=DSFake(ok())).estimate_cost("deepseek-v4-flash", MSGS, 16384)
    prov, fake, _ = make(ok(miss=100, completion=50), budget=ds.BudgetGuard(est * 1.00001))
    prov.complete_json("deepseek-v4-flash", MSGS)                   # 첫 호출은 상한 추정 < 한도라 시작
    spent = prov.budget.spent
    assert 0 < spent < est and prov.budget.reserved == 0
    with pytest.raises(ds.BudgetExceeded):                          # 남은 한도 < 두 번째 호출의 상한 추정
        prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.n_chat == 1


def test_estimate_is_an_upper_bound_using_peak_prices():
    prov, _f, _ = make(ok())
    est = prov.estimate_cost("deepseek-v4-pro", MSGS, 1000)
    chars = sum(len(m["content"]) for m in MSGS)
    assert est == pytest.approx(((chars + 32) * 1.32 + 1000 * 3.96) / 1_000_000)


def test_base_url_is_pinned_to_the_official_host(monkeypatch):
    assert ds.resolve_base_url(None) == "https://api.deepseek.com"
    assert ds.resolve_base_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"
    for bad in ("http://api.deepseek.com", "https://evil.example.com", "https://api.deepseek.com.evil.com",
                "https://user:pw@api.deepseek.com", "https://api.deepseek.com:8443", "https://api.deepseek.com/x/y",
                "https://api.deepseek.com/?a=1"):
        with pytest.raises(ValueError):
            ds.resolve_base_url(bad)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.example.com")
    with pytest.raises(ValueError):
        ds.DeepSeekProvider(api_key=TEST_KEY, transport=DSFake(ok()))


def test_api_key_never_appears_in_errors_or_usage_rows(manifest):
    prov, _f, _ = make(lambda b, n: (401, {"error": {"message": f"bad key {TEST_KEY}"}}, {}))
    with pytest.raises(ds.FatalProviderError) as ei:
        prov.complete_json("deepseek-v4-flash", MSGS, job_id="j", target_id="t")
    assert TEST_KEY not in str(ei.value)
    ds.record_attempts(manifest, ei.value.attempts, job_id="j", target_id="t")
    dump = json.dumps(manifest.select("SELECT * FROM api_usage"), ensure_ascii=False)
    assert TEST_KEY not in dump


def test_secret_loaded_lazily_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk-ENVKEY-777")
    fake = DSFake(ok())
    prov = ds.DeepSeekProvider(transport=fake, now=lambda: FRI_PEAK)
    prov.complete_json("deepseek-v4-flash", MSGS)
    assert fake.headers[0]["Authorization"] == "Bearer dsk-ENVKEY-777"
    assert logsafe.contains_sensitive("dsk-ENVKEY-777")       # 로그 필터에 등록된다


def test_record_attempts_writes_one_api_usage_row_per_attempt(manifest):
    seq = [(429, {"error": {}}, {}), (200, chat_payload({"cases": []}, hit=300, miss=700, completion=40, reasoning=5), {})]
    prov, _f, _ = make(lambda b, n: seq[n - 1])
    r = prov.complete_json("deepseek-v4-flash", MSGS, job_id="job-1", target_id="tgt-1")
    total = ds.record_attempts(manifest, r.attempts, job_id="job-1", target_id="tgt-1")
    rows = manifest.select("SELECT * FROM api_usage ORDER BY id")
    assert len(rows) == 2 and rows[0]["status"] == "http_429" and rows[0]["prompt_tokens"] == 0 and rows[0]["cost_usd"] == 0
    row = rows[1]
    assert (row["provider"], row["model"], row["job_id"], row["target_id"], row["attempt"]) == ("deepseek", "deepseek-flash", "job-1", "tgt-1", 2)
    assert (row["prompt_tokens"], row["cache_hit_tokens"], row["cache_miss_tokens"], row["completion_tokens"]) == (1000, 300, 700, 40)
    assert row["cost_usd"] == pytest.approx(total) == pytest.approx((300 * 0.006 + 700 * 0.3 + 40 * 1.2) / 1_000_000)
    snap = json.loads(row["price_snapshot_json"])
    assert snap["snapshot_id"] == "2026-10-09" and snap["tier"] == "peak" and snap["requested_model"] == "deepseek-flash"
    assert row["requested_model"] == "deepseek-flash"                     # G-D11: 요청 이름 열
    assert snap["returned_model"] == "deepseek-flash" and snap["reasoning_tokens"] == 5
    assert manifest.api_usage_summary(provider="deepseek")["total"]["calls"] == 2
