"""적대 검증에서 확인된 수집·구조화 결함의 회귀 테스트(SR-01~04, DS-01~03, HTTP-01·02, FETCH-01, MAN-01). 네트워크는 로컬 127.0.0.1 소켓만 쓴다."""
import io
import json
import re
import socket
import sqlite3
import threading
import urllib.parse

import pytest

import pdr_ing_paths  # noqa: F401
from conftest import ROBOTS_OK, FakeClock, FakeTransport
from ds_fakes import FRI_PEAK, TEST_KEY, DSFake, chat_payload, good_case, seed
from ext_helpers import make_pdf
from ext_helpers import seed as ext_seed
from press_review.ingestion import __main__ as cli
from press_review.ingestion import manifest as man_mod
from press_review.ingestion import rights as R
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from press_review.ingestion.http import FetchFailed, HttpClient, IngestionError, TransportError, UrllibTransport
from press_review.ingestion.manifest import Manifest
from press_review.providers import deepseek as ds


def prov_for(fake, **kw):
    return ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None),
                               concurrency=1, **kw)


def pids_in(body):
    return re.findall(r"<<PAGE (.+?)>>", "\n".join(m["content"] for m in body["messages"]))[:2]


# ---------------------------------------------------------------- SR-01
def test_sr01_list_or_dict_speaker_is_dropped_not_fatal(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    d = good_case([f"{att}@p1", f"{att}@p2"])
    d["factual_background"][0]["speaker"] = ["claimant"]
    d["legal_reasoning"][0]["speaker"] = {"who": "institution"}
    d["outcome"]["speaker"] = ["institution"]
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [d]}), {}))
    for _ in range(2):                                                    # 응답은 done 으로 캐시되므로 다시 돌려도 같은 조립을 한다
        res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
        assert res["status"] == "completed" and res["failed"] == 0
    assert fake.n_chat == 1
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    reasons = " ".join(rec["quote_check"]["dropped_fields"])
    assert "factual_background[0]:speaker_not_allowed" in reasons and "outcome:speaker_not_allowed" in reasons


def test_sr01_unexpected_error_in_one_target_does_not_stop_the_next(manifest, corpus, monkeypatch):
    seed(manifest, corpus, doc_n=1)
    seed(manifest, corpus, doc_n=2)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [good_case(pids_in(b))]}), {}))
    real = S.Structurer._assemble
    calls = []

    def boom(self, t, *a, **kw):
        calls.append(t.key)
        if len(calls) == 1:
            raise TypeError("unhashable type: 'list'")
        return real(self, t, *a, **kw)
    monkeypatch.setattr(S.Structurer, "_assemble", boom)
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["status"] == "completed" and res["failed"] == 1 and res["cases"] == 1
    assert [f["error_class"] for f in manifest.failures()] == ["unexpected_error"]


# ---------------------------------------------------------------- SR-02 / SR-03
PAGE_A = "가나다 사건의 경위는 다음과 같다. 신청인은 기사 정정을 요구하였다."
PAGE_B = "사건번호 제2021-813호 조정 결과 양측은 정정보도문 게재에 합의하였다."
LAW = "정기간행물의 등록 등에 관한 법률"
LAW_A = f"{LAW} 시행 2005. 7. 28. 신청인은 기사 정정을 요구하였다."
LAW_B = f"{LAW} 2009. 7. 31. 개정 이후 조정 결과 양측은 정정보도문 게재에 합의하였다."


def _ctx(pages, unit, pids):
    meta = {"p1": {"pdf_page_no": 1}, "p2": {"pdf_page_no": 2}}
    return sr.WindowCtx(profile=sr.PROFILES["pac_adjustment"], doc_id="d", att_id="a", target_key="a", unit=unit,
                        pages={p: pages[p] for p in pids}, page_meta=meta, structurer={"version": "v", "model": "m", "input_sha256": "0" * 64},
                        source_id="pac_adjustment_casebooks")


def test_sr02_continuation_merge_keeps_case_number_and_number_based_id():
    pages = {"p1": sr.PageText(PAGE_A), "p2": sr.PageText(PAGE_B)}
    da = {"authority_type": "pac_settlement", "institution": "pac", "continues_after_window": True,
          "factual_background": [{"text": "경위", "speaker": "record", "quote": {"page_id": "p1", "quote": "신청인은 기사 정정을 요구하였다"}}]}
    db = {"authority_type": "pac_settlement", "institution": "pac", "starts_before_window": True, "case_number": "제2021-813호",
          "outcome_type": "settlement", "outcome_scope": "whole", "settlement_or_adjudication": "settlement",
          "outcome": {"text": "합의", "speaker": "institution", "quote": {"page_id": "p2", "quote": "정정보도문 게재에 합의하였다"}}}
    a = sr.assemble_case(da, _ctx(pages, "w0000", ["p1"]), 0)
    b = sr.assemble_case(db, _ctx(pages, "w0001", ["p2"]), 0)
    assert a.key is None and b.key and b.record["case_number"] == "제2021-813호"
    m = sr.merge_cases(a, b, {"p1": PAGE_A, "p2": PAGE_B}, 0.25)
    assert m.key == b.key
    assert m.record["case_number"] == "제2021-813호"
    assert m.record["case_id"] == b.record["case_id"] and "813" in m.record["case_id"]


def test_sr03_quoteless_law_versions_with_different_dates_are_both_kept():
    pages = {"p1": sr.PageText(LAW_A), "p2": sr.PageText(LAW_B)}

    def draft(page, quote, ef, rel, **extra):
        return dict({"authority_type": "pac_settlement", "institution": "pac", "case_number": "제2021-813호",
                     "factual_background": [{"text": "x", "speaker": "record", "quote": {"page_id": page, "quote": quote}}],
                     "law_versions": [{"law": LAW, "effective_from": ef, "relation": rel}]}, **extra)
    a = sr.assemble_case(draft("p1", "신청인은 기사 정정을 요구하였다", "2005-07-28", "applied_at_time", continues_after_window=True),
                         _ctx(pages, "w0000", ["p1"]), 0)
    b = sr.assemble_case(draft("p2", "양측은 정정보도문 게재에 합의하였다", "2009-07-31", "current", starts_before_window=True),
                         _ctx(pages, "w0001", ["p2"]), 0)
    assert len(a.record["law_versions"]) == 1 and len(b.record["law_versions"]) == 1
    m = sr.merge_cases(a, b, {"p1": LAW_A, "p2": LAW_B}, 0.25)
    got = sorted((x["effective_from"], x["relation"]) for x in m.record["law_versions"])
    assert got == [("2005-07-28", "applied_at_time"), ("2009-07-31", "current")]


def test_sr03_identical_quoteless_law_version_is_still_deduplicated():
    a = {"law": "X", "effective_from": "2005-07-28", "effective_to": None, "relation": "current", "quote": None}
    b = dict(a)
    assert sr._span_key(a) == sr._span_key(b)
    assert sr._span_key(a) != sr._span_key(dict(b, effective_from="2009-07-31"))
    h1 = {"relation": "appeal", "case_number": "2020다1", "note": "n", "quote": None}
    assert sr._span_key(h1) != sr._span_key(dict(h1, case_number="2020다2"))


# ---------------------------------------------------------------- SR-04
def _lone_surrogate_fake(att):
    d = good_case([f"{att}@p1", f"{att}@p2"])
    d["issue_tags"] = ["tag"]
    content = json.dumps({"cases": [d]}, ensure_ascii=True).replace('"tag"', '"tag \\ud83d"')
    body = json.dumps(chat_payload(content), ensure_ascii=True).encode("ascii")
    return DSFake(lambda b, n: (200, body, {}))


def test_sr04_lone_surrogate_in_model_text_is_invalid_json_not_a_crash(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    fake = _lone_surrogate_fake(att)
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))        # UnicodeEncodeError 로 끝나면 안 된다
    assert res["windows_dead"] == 1 and res["cases"] == 0
    assert all(j["status"] != "running" for j in manifest.jobs())
    assert [d["reason"].split(":")[0] for d in manifest.dead_letters()] == ["invalid_json"]


def test_sr04_unexpected_error_after_the_paid_call_marks_the_job_not_running(manifest, corpus, monkeypatch):
    doc, att, _ = seed(manifest, corpus)
    d = good_case([f"{att}@p1", f"{att}@p2"])
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [d]}), {}))
    monkeypatch.setattr(S, "_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["windows_dead"] == 1
    assert all(j["status"] != "running" for j in manifest.jobs())
    assert fake.n_chat == 1


# ---------------------------------------------------------------- DS-01 / DS-02 / DS-03
def test_ds01_source_forbidden_after_document_override_blocks_egress(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [good_case([f"{att}@p1", f"{att}@p2"])]}), {}))
    src = manifest.get_rights("source", "pac_adjustment_casebooks")
    rights = {k: src[k] for k in R.RIGHT_KEYS}
    R.set_document_override(manifest, doc, dict(rights), src["rights_basis"])
    assert S.egress_reason(manifest, doc) is None
    manifest.set_rights("source", "pac_adjustment_casebooks", dict(rights, external_api="forbidden"), src["rights_basis"], "verified")
    assert S.egress_reason(manifest, doc) == "external_api_forbidden"
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert fake.n_chat == 0 and res["refused"] == 1


def test_ds01_both_rows_allowed_still_sends(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    assert S.egress_reason(manifest, doc) is None


def test_ds01_document_row_forbidden_blocks_even_if_source_allowed(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    src = manifest.get_rights("source", "pac_adjustment_casebooks")
    R.set_document_override(manifest, doc, {k: src[k] for k in R.RIGHT_KEYS}, src["rights_basis"])
    own = manifest.get_rights("document", doc)
    manifest.set_rights("document", doc, dict({k: own[k] for k in R.RIGHT_KEYS}, external_api="forbidden"), own["rights_basis"], "verified")
    assert S.egress_reason(manifest, doc) == "external_api_forbidden"


class _FlakyModels(DSFake):
    def __init__(self, handler, fail_times):
        super().__init__(handler)
        self.fail_times = fail_times

    def request(self, method, url, headers, timeout, max_bytes, body=None):
        if url.endswith("/models") and self.fail_times > 0:
            self.fail_times -= 1
            self.calls.append((method, "/models"))
            return ds_raw(503)
        return super().request(method, url, headers, timeout, max_bytes, body)


def ds_raw(status):
    from ds_fakes import raw
    return raw(status, {"error": {"message": "busy"}})


def test_ds02_transient_models_error_is_retried(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    fake = _FlakyModels(lambda b, n: (200, chat_payload({"cases": [good_case([f"{att}@p1", f"{att}@p2"])]}), {}), fail_times=1)
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["status"] == "completed" and res["cases"] == 1
    assert sum(1 for c in fake.calls if c[1] == "/models") == 2


def test_ds02_persistent_models_error_aborts_the_run_without_raising(manifest, corpus):
    doc, att, _ = seed(manifest, corpus)
    fake = DSFake(lambda b, n: pytest.fail("no chat call expected"), models_status=503)
    res = S.Structurer(manifest, prov_for(fake), corpus).run(S.find_targets(manifest))
    assert res["status"] == "aborted_models_listing_failed"
    assert sum(1 for c in fake.calls if c[1] == "/models") == 3          # 첫 시도 + 재시도 2회
    assert res["windows_unfinished"] >= 1


def test_ds03_thinking_mode_length_with_empty_content_doubles_max_tokens():
    seen = []

    def h(b, n):
        seen.append(b["max_tokens"])
        return (200, chat_payload("", completion=b["max_tokens"], finish="length", reasoning=b["max_tokens"]), {})
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=DSFake(h), sleep=lambda s: None, now=lambda: FRI_PEAK, thinking="enabled")
    with pytest.raises(ds.CallFailed) as ei:
        prov.complete_json("deepseek-flash", [{"role": "user", "content": "return json"}], max_tokens=1000)
    assert seen == [1000, 2000, 4000]
    assert [a.status for a in ei.value.attempts] == ["truncated"] * 3
    assert ei.value.error_class == "truncated_output"


# ---------------------------------------------------------------- HTTP-01 / HTTP-02 (로컬 소켓)
class LocalServer:
    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.handler, self.hits, self._stop = handler, 0, threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.hits += 1
            try:
                c.settimeout(2)
                c.recv(65536)
                self.handler(c, self._stop)
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self._stop.set()
        self.sock.close()


class LocalTransport(UrllibTransport):
    def __init__(self, port):
        super().__init__()
        self.port = port

    def request(self, method, url, headers, timeout, max_bytes, body=None):
        p = urllib.parse.urlsplit(url)
        return super().request(method, f"http://127.0.0.1:{self.port}{p.path}", headers, timeout, max_bytes, body=body)


def _slow_503(c, stop):
    c.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 1000\r\n\r\n" + b"x" * 10)
    stop.wait(1.5)


def _short_200(c, stop):
    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n%PDF-1.4 " + b"x" * 100)


@pytest.fixture()
def local_client(tmp_path):
    made = []

    def make(handler, **kw):
        srv = LocalServer(handler)
        cl = HttpClient(tmp_path / "cache", transport=LocalTransport(srv.port), resolver=lambda h: ["8.8.8.8"], robots=None,
                        sleep=lambda s: None, timeout=0.3, refresh=True, **kw)
        made.append((srv, cl))
        return srv, cl
    yield make
    for srv, cl in made:
        srv.close()
        cl.close()


def test_http01_timeout_while_reading_an_error_body_is_a_retried_ingestion_error(local_client):
    srv, cl = local_client(_slow_503)
    with pytest.raises(IngestionError):
        cl.get("https://www.pac.or.kr/b.pdf")
    assert srv.hits >= 2                                                  # 재시도했다


def test_http01_transport_maps_error_body_timeout(local_client):
    srv, _cl = local_client(_slow_503)
    with pytest.raises(TransportError) as ei:
        LocalTransport(srv.port).request("GET", "https://www.pac.or.kr/b.pdf", {}, 0.3, 1 << 20)
    assert ei.value.kind == "timeout" and ei.value.retriable


def test_http02_short_body_under_content_length_is_retried_and_never_cached(local_client):
    srv, cl = local_client(_short_200)
    with pytest.raises(IngestionError):
        cl.get("https://www.pac.or.kr/a.pdf")
    assert srv.hits >= 2
    assert cl.cache.get("www.pac.or.kr", "GET", "https://www.pac.or.kr/a.pdf") is None


def test_http02_complete_body_is_accepted(local_client):
    body = b"%PDF-1.4 " + b"y" * 50

    def ok(c, stop):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    _srv, cl = local_client(ok)
    r = cl.get("https://www.pac.or.kr/a.pdf")
    assert r.ok and r.body == body


# ---------------------------------------------------------------- FETCH-01
ROB = {"https://www.pac.or.kr/robots.txt": ROBOTS_OK}
U1 = "https://www.pac.or.kr/files/a.pdf"
PDF_A = make_pdf(["alpha page text for the first synthetic file"])


def _run_fetch(corpus, t, clock):
    out = io.StringIO()
    cli.main(["fetch"], transport=t, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
    return json.loads(out.getvalue().strip().splitlines()[-1])


def test_fetch01_failed_redownload_of_a_missing_file_is_retried_later(tmp_path):
    clock = FakeClock()
    corpus = tmp_path / "corpus"
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    ext_seed(m, atts=[(1, U1, "full", len(PDF_A), "application/pdf", "PDF")])
    m.close()
    t = FakeTransport({**ROB, U1: (200, PDF_A, {"Content-Type": "application/pdf"})})
    assert _run_fetch(corpus, t, clock)["result"]["fetched"] == 1
    m = Manifest(corpus / "manifest.sqlite")
    rel = m.select("SELECT raw_path FROM attachments")[0]["raw_path"]
    m.close()
    (corpus / rel).unlink()
    t.routes[U1] = (404, b"nf", {})
    _run_fetch(corpus, t, clock)
    t.routes[U1] = (200, PDF_A, {"Content-Type": "application/pdf"})
    r3 = _run_fetch(corpus, t, clock)
    assert r3["result"]["fetched"] == 1 and (corpus / rel).exists()


# ---------------------------------------------------------------- MAN-01
def test_man01_add_column_race_is_tolerated(tmp_path):
    p = tmp_path / "m.sqlite"
    Manifest(p).close()
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE api_usage DROP COLUMN requested_model")
    con.commit()
    con.close()
    a = sqlite3.connect(str(p), isolation_level=None)
    b = sqlite3.connect(str(p), isolation_level=None)
    stale = set(man_mod._columns(b, "api_usage"))
    man_mod._ensure_added_columns(a)
    orig = man_mod._columns
    man_mod._columns = lambda c, t: list(stale) if c is b and t == "api_usage" else orig(c, t)
    try:
        man_mod._ensure_added_columns(b)                                   # 'duplicate column name' 으로 죽으면 안 된다
    finally:
        man_mod._columns = orig
    assert "requested_model" in orig(b, "api_usage")


def test_sr02_two_window_run_stores_one_case_with_the_number_from_the_later_window(manifest, corpus):
    big = "문장입니다. " * 100
    pages = {"p1": "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.\n" + big,
             "p2": "위원회는 양 당사자가 조정합의에 이르렀음을 확인한다.\n사건번호 2002-조정-9\n" + big}
    doc, att, texts = seed(manifest, corpus, pages)
    p1, p2 = f"{att}@p1", f"{att}@p2"
    c1 = {"case_number": None, "institution": "pac", "authority_type": "pac_settlement", "first_page_id": p1, "continues_after_window": True,
          "factual_background": [{"text": "청구", "speaker": "claimant", "quote": {"page_id": p1, "quote": "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다."}}]}
    c2 = {"case_number": "2002-조정-9", "institution": "pac", "authority_type": "pac_settlement", "first_page_id": p2, "starts_before_window": True,
          "outcome_type": "settlement", "outcome_scope": "whole", "settlement_or_adjudication": "settlement",
          "outcome": {"text": "조정합의", "speaker": "institution", "quote": {"page_id": p2, "quote": "위원회는 양 당사자가 조정합의에 이르렀음을 확인한다."}}}

    def handler(b, n):
        return 200, chat_payload({"cases": [c2 if "조정합의에 이르렀음" in b["messages"][1]["content"] else c1]}), {}
    fake = DSFake(handler)
    res = S.Structurer(manifest, prov_for(fake), corpus, window_chars=1200).run(S.find_targets(manifest))
    assert res["windows"] == 2 and res["windows_dead"] == 0
    rows = manifest.select("SELECT case_id FROM cases")
    assert len(rows) == 1 and rows[0]["case_id"].endswith("2002-조정-9")
    rec = manifest.get_case(rows[0]["case_id"])["record"]
    assert rec["case_number"] == "2002-조정-9" and rec["outcome"] is not None
