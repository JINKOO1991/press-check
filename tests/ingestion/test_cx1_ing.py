"""Codex 1회차(gpt-6-astra) 지적 CX1-1·4·5·6 회귀 테스트. 가짜 transport, 네트워크 없음."""
import json
import shutil

import pytest

import pdr_ing_paths  # noqa: F401
from ds_fakes import FRI_PEAK, TEST_KEY, DSFake, chat_payload, good_case, seed
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from press_review.ingestion.http import TransportError
from press_review.providers import deepseek as ds

MSGS = [{"role": "system", "content": "Answer in json."}, {"role": "user", "content": "문서 본문 " * 20}]


def prov_for(fake, budget=None, **kw):
    return ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(budget),
                               concurrency=1, **kw)


def pids(att):
    return [f"{att}@p1", f"{att}@p2"]


# ---- CX1-1
@pytest.mark.parametrize("bad", [{}, {"cases": "x"}, {"cases": {"a": 1}}, {"cases": [1, "x"]}, {"cases": [None]}])
def test_cx1_1_invalid_cases_array_is_not_a_completion(manifest, corpus, bad):
    doc, att, _ = seed(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload(bad), {}))
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["windows_dead"] == 1 and res["cases"] == 0
    assert [d["reason"].split(":")[0] for d in manifest.dead_letters()] == ["invalid_schema"]
    assert manifest.select("SELECT status FROM attachments")[0]["status"] != "structured"
    assert all(j["status"] == "dead" for j in manifest.jobs())


def test_cx1_1_invalid_rerun_keeps_previous_cases_and_state(manifest, corpus, tmp_path, monkeypatch):
    doc, att, _ = seed(manifest, corpus)
    d = good_case(pids(att))
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [d]}), {}))
    S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    old = manifest.get_case(f"{att}:2001-조정-1")
    old_att = manifest.select("SELECT status, structurer_version, structure_input_sha256 FROM attachments")[0]
    assert old_att["status"] == "structured"
    copy = tmp_path / "prompts2"
    shutil.copytree(S.PROMPTS_DIR, copy)
    (copy / "pac_adjustment.md").write_text((S.PROMPTS_DIR / "pac_adjustment.md").read_text(encoding="utf-8") + "\n- extra rule\n", encoding="utf-8")
    monkeypatch.setattr(S, "PROMPTS_DIR", copy)
    fake.handler = lambda b, n: (200, chat_payload({}), {})
    res = S.Structurer(manifest, prov_for(fake), corpus, force=True).run(S.find_targets(manifest))
    assert res["held_back"] == 1 and res["windows_dead"] == 1
    kept = manifest.get_case(f"{att}:2001-조정-1")
    assert kept["record"] == old["record"]
    assert len(manifest.select("SELECT * FROM chunks")) == 1
    assert manifest.select("SELECT status, structurer_version, structure_input_sha256 FROM attachments")[0] == old_att
    assert [d["reason"].split(":")[0] for d in manifest.dead_letters()] == ["invalid_schema"]


def test_cx1_1_empty_cases_array_is_still_a_valid_completion(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": []}), {}))
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["windows_dead"] == 0 and manifest.dead_letters() == []


def test_cx1_1_cached_invalid_response_from_older_run_is_dead_lettered_not_assembled(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    d = good_case(pids(att))
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [d]}), {}))
    S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    rp = [p for p in corpus.rglob("*.json") if p.parent.name == "structured" or "structured" in p.parts and p.name.startswith("w")]
    assert rp
    for p in rp:
        o = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(o.get("response"), dict):
            o["response"] = {}
            p.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    res = S.Structurer(manifest, prov_for(fake), corpus, force=True).run(S.find_targets(manifest))
    assert fake.n_chat == 1 and res["windows_dead"] == 1
    assert manifest.get_case(f"{att}:2001-조정-1") is not None          # 기존 사건 보존


# ---- CX1-4
def _envelope_no_choices(usage=True):
    p = {"id": "x", "model": "deepseek-flash", "choices": []}
    if usage:
        p["usage"] = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100, "completion_tokens": 50}
    return p


def test_cx1_4_usage_is_recovered_when_choices_are_missing():
    fake = DSFake(lambda b, n: (200, _envelope_no_choices(), {}))
    prov = prov_for(fake)
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-flash", MSGS)
    expect = (100 * 0.3 + 50 * 1.2) / 1_000_000
    assert all(a.cost_usd == pytest.approx(expect) and a.completion_tokens == 50 for a in ei.value.attempts)
    assert prov.budget.spent == pytest.approx(expect * 3)


def test_cx1_4_envelope_without_usage_is_settled_at_the_upper_bound():
    fake = DSFake(lambda b, n: (200, _envelope_no_choices(usage=False), {}))
    prov = prov_for(fake)
    est = prov.estimate_cost("deepseek-flash", MSGS, ds.DEFAULT_MAX_TOKENS)
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-flash", MSGS)
    assert all(a.cost_usd == pytest.approx(est) for a in ei.value.attempts) and est > 0


def test_cx1_4_budget_blocks_retries_after_unreadable_but_billed_responses():
    probe = prov_for(DSFake(lambda b, n: None))
    est = probe.estimate_cost("deepseek-flash", MSGS, ds.DEFAULT_MAX_TOKENS)
    fake = DSFake(lambda b, n: (200, _envelope_no_choices(usage=False), {}))
    prov = prov_for(fake, budget=est * 1.5)             # 상한 추정으로 정산하면 두 번째 시도는 예산에 맞지 않는다
    with pytest.raises(ds.BudgetExceeded):
        prov.complete_json("deepseek-flash", MSGS)
    assert fake.n_chat == 1


def test_cx1_4_timeout_is_settled_at_the_reserved_amount():
    fake = DSFake(lambda b, n: TransportError("timeout", "request timed out"))
    prov = prov_for(fake)
    est = prov.estimate_cost("deepseek-flash", MSGS, ds.DEFAULT_MAX_TOKENS)
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-flash", MSGS)
    assert [a.cost_usd for a in ei.value.attempts] == [pytest.approx(est)] * 3
    assert prov.budget.spent == pytest.approx(est * 3)
    # 요청이 나가지 않은 연결 오류(이름 해석 실패·연결 거부)는 0 그대로
    for msg in ("dns lookup failed for api.deepseek.com", "connection error (ConnectionRefusedError)"):
        fake2 = DSFake(lambda b, n, msg=msg: TransportError("connection", msg))
        prov2 = prov_for(fake2)
        with pytest.raises(ds.CallFailed) as ei2:
            prov2.complete_json("deepseek-flash", MSGS)
        assert all(a.cost_usd == 0 for a in ei2.value.attempts)


# ---- CX2-4: 전송 뒤 끊긴 연결(reset·불완전 본문)은 청구됐을 수 있으므로 보수 정산하고, 예산 상한을 넘겨 재시도하지 않는다
@pytest.mark.parametrize("msg", ["connection error (ConnectionResetError)",
                                 "connection error (RemoteDisconnected)",
                                 "incomplete body (120 of 4096 bytes)"])
def test_cx2_4_post_send_connection_errors_are_settled_and_budget_bounded(msg):
    fake = DSFake(lambda b, n: TransportError("connection", msg))
    prov = prov_for(fake)
    est = prov.estimate_cost("deepseek-flash", MSGS, ds.DEFAULT_MAX_TOKENS)
    prov.budget.limit = est * 1.5
    with pytest.raises((ds.CallFailed, ds.BudgetExceeded)) as ei:
        prov.complete_json("deepseek-flash", MSGS)
    attempts = ei.value.attempts
    assert len(attempts) == 1                       # 두 번째 시도는 예산 사전 검사에서 막힌다
    assert attempts[0].cost_usd == pytest.approx(est)
    assert prov.budget.spent <= est * 1.5 + 1e-12


# ---- CX1-5
def _real_surrogate_fake(att):
    d = good_case(pids(att))
    d["issue_tags"] = ["tag \ud83d"]
    content = json.dumps({"cases": [d]}, ensure_ascii=False)                 # 진짜 짝 없는 서로게이트가 본문에 들어 있다
    body = json.dumps(chat_payload(content), ensure_ascii=True).encode("ascii")     # 봉투에서는 \ud83d 이스케이프 -> 디코딩하면 실제 서로게이트
    return DSFake(lambda b, n: (200, body, {}))


def test_cx1_5_real_lone_surrogate_in_failed_body_is_escaped_and_job_is_failed_not_running(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    res = S.Structurer(manifest, prov_for(_real_surrogate_fake(att)), corpus).run(S.find_targets(manifest))
    assert res["windows_dead"] == 1 and res["cases"] == 0
    assert all(j["status"] != "running" for j in manifest.jobs())
    dl = manifest.dead_letters()
    assert [d["reason"].split(":")[0] for d in dl] == ["invalid_json"]
    saved = open(dl[0]["response_path"], encoding="utf-8").read()
    assert "\\ud83d" in saved


def test_cx1_5_body_save_failure_still_finalizes_job_state(manifest, corpus, monkeypatch):
    doc, att, _ = seed(manifest, corpus)
    from pathlib import Path
    monkeypatch.setattr(Path, "write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = S.Structurer(manifest, prov_for(_real_surrogate_fake(att)), corpus).run(S.find_targets(manifest))
    assert res["windows_dead"] == 1
    assert all(j["status"] != "running" for j in manifest.jobs())
    assert len(manifest.dead_letters()) == 1


# ---- CX1-6
def _art(status):
    return {"status": status, "quote": None}


def test_cx1_6_partial_quotes_do_not_become_full_pair_verified():
    f = sr.derive_pair_confidence
    assert f("full_pair_verified", _art("quoted_in_source"), _art("quoted_in_source")) == "partial_quote"
    assert f("full_pair_verified", _art("quoted_in_source"), _art("quoted_in_source"), full_text_confirmed=True) == "full_pair_verified"
    assert f("unverified_pair", _art("quoted_in_source"), _art("quoted_in_source")) == "unverified_pair"


# ---- CX3: 응답 헤더를 받은 뒤 본문을 읽다 난 TLS 오류는 전송 뒤 실패로 보수 정산한다. 핸드셰이크 TLS 실패는 0원
def test_cx3_tls_error_during_body_read_is_marked_and_billed():
    import ssl
    from press_review.ingestion import http as H

    class _Resp:
        status, headers = 200, {"Content-Length": "100"}
        def read(self, *a, **k):
            raise ssl.SSLError("bad record mac")
        def close(self):
            pass

    t = H.UrllibTransport()
    t._opener = type("O", (), {"open": lambda self, req, timeout=None: _Resp()})()
    with pytest.raises(TransportError) as ei:
        t.request("POST", "https://api.deepseek.com/chat/completions", {}, 5, 1000, body=b"{}")
    assert ei.value.kind == "tls" and "during body read" in str(ei.value)
    assert ds._possibly_billed(ei.value) is True
    assert ds._possibly_billed(TransportError("tls", "tls error (SSLCertVerificationError)", retriable=False)) is False
