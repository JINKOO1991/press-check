"""가짜 OC가 장부·캐시·로그·예외 어디에도 남지 않는지 바이트 단위로 확인한다."""
import os
import re
from pathlib import Path

import pytest

import pdr_ing_paths

from conftest import FAKE_OC, ROBOTS_OK, FakeTransport
from press_review import logsafe
from press_review.ingestion.http import (BlockedURL, FetchFailed, TransportError, mask_url, scrub_body, scrub_text,
                                         strip_oc)
from press_review.ingestion.manifest import sanitize_message

LIST = "https://www.law.go.kr/DRF/lawSearch.do?target=prec&type=JSON&query=%EB%AA%85%EC%98%88"
SENT = LIST + "&OC=" + FAKE_OC
BODY = ('{"PrecSearch":{"totalCnt":1,"prec":[{"판례상세링크":"/DRF/lawService.do?OC=' + FAKE_OC +
        '&target=prec&ID=1&type=HTML","사건명":"the OC=law applies"}]}}')


def all_bytes(root: Path) -> bytes:
    out = b""
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out += p.read_bytes()
    return out


@pytest.mark.parametrize("register", [True, False])     # False: logsafe가 가려 주지 않아도 소스에서 마스킹됨을 증명
def test_oc_is_injected_into_request_but_never_persisted(make_client, corpus, manifest, log_stream, register):
    if register:
        logsafe.register_secret(FAKE_OC)
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, BODY, {"Content-Type": "application/json"})})
    c = make_client(t)
    r = c.get(LIST, with_oc=True)
    assert r.status == 200
    assert (("GET", SENT)) in t.calls                                   # 실제 전송에는 OC가 들어갔다
    assert FAKE_OC not in r.url and "OC=[MASKED]" in r.url
    assert FAKE_OC.encode() not in r.body                                # 되울림 마스킹
    assert b"OC=[MASKED]&target=prec" in r.body
    manifest.record_failure(r.url, "parse", "x", "bad url " + SENT)
    manifest.close()
    assert FAKE_OC.encode() not in all_bytes(corpus)                     # sqlite(-wal 포함)·캐시 본문·meta 전부
    assert FAKE_OC not in log_stream.getvalue()


def test_cache_hit_returns_masked_body_with_zero_network(make_client):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, BODY, {})})
    c = make_client(t)
    c.get(LIST, with_oc=True)
    n = len(t.calls)
    r = c.get(LIST, with_oc=True)
    assert r.from_cache and len(t.calls) == n and FAKE_OC.encode() not in r.body


def test_body_masking_touches_only_oc_values():
    assert scrub_body(b'x OC=abc&amp;target a "OC=zz" TOC=keep law=OC') == b'x OC=[MASKED]&amp;target a "OC=[MASKED]" TOC=keep law=OC'
    assert scrub_text("see OC=secret123 here") == "see OC=[MASKED] here"
    assert scrub_text("law applies, the oc is fine") == "law applies, the oc is fine"


def test_mask_and_strip_helpers():
    assert mask_url("https://x/y?a=1&OC=secret&b=2") == "https://x/y?a=1&OC=[MASKED]&b=2"
    assert mask_url("https://x/y?oc=secret") == "https://x/y?oc=[MASKED]"
    assert mask_url("https://x/y?TOC=keep") == "https://x/y?TOC=keep"
    assert strip_oc("https://x/y?a=1&OC=secret&b=2#f") == "https://x/y?a=1&b=2"


def test_oc_written_in_url_without_with_oc_is_refused(make_client):
    t = FakeTransport()
    c = make_client(t)
    with pytest.raises(BlockedURL) as e:
        c.get(SENT)
    assert FAKE_OC not in str(e.value)
    assert t.calls == []


def test_with_oc_refused_outside_law_go_kr_drf(make_client):
    t = FakeTransport()
    c = make_client(t)
    for u in ("https://www.pac.or.kr/kor/pages/?p=207", "https://open.law.go.kr/LSO/openApi/guideResult.do",
              "https://www.law.go.kr/LSW/precInfoP.do?precSeq=1"):
        with pytest.raises(BlockedURL):
            c.get(u, with_oc=True)
    assert t.calls == []


def test_redirect_leaving_drf_does_not_carry_oc(make_client):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, "https://www.pac.or.kr/robots.txt": ROBOTS_OK,
                       SENT: (302, b"", {"Location": "https://www.pac.or.kr/x?OC=" + FAKE_OC})})
    c = make_client(t)
    with pytest.raises(BlockedURL) as e:
        c.get(LIST, with_oc=True)
    assert FAKE_OC not in str(e.value)
    assert all(FAKE_OC not in u for u in t.urls("www.pac.or.kr"))


def test_redirect_inside_drf_reinjects_oc_and_drops_server_echo(make_client):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK,
                       SENT: (302, b"", {"Location": "/DRF/lawSearch.do?x=1&OC=SERVERECHO"}),
                       "https://www.law.go.kr/DRF/lawSearch.do?x=1&OC=" + FAKE_OC: (200, b"{}", {})})
    c = make_client(t)
    r = c.get(LIST, with_oc=True)
    assert r.status == 200 and "SERVERECHO" not in r.final_url and "OC=[MASKED]" in r.final_url
    assert all("SERVERECHO" not in u for u in t.urls())


@pytest.mark.parametrize("register", [True, False])
def test_failures_and_exceptions_after_retries_never_contain_oc(make_client, manifest, corpus, log_stream, clock, register):
    if register:
        logsafe.register_secret(FAKE_OC)
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (503, "down " + SENT, {})})
    c = make_client(t)
    with pytest.raises(FetchFailed) as e:
        c.get(LIST, with_oc=True)
    assert FAKE_OC not in str(e.value) and "OC=[MASKED]" in str(e.value)
    fails = manifest.failures()
    assert len(fails) == 1 and FAKE_OC not in repr(fails)
    manifest.close()
    assert FAKE_OC.encode() not in all_bytes(corpus)
    assert FAKE_OC not in log_stream.getvalue()


def test_transport_exception_with_oc_in_message_is_scrubbed(make_client, manifest):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK,
                       SENT: TransportError("tls", "bad " + SENT, retriable=False)})
    c = make_client(t)
    with pytest.raises(FetchFailed) as e:
        c.get(LIST, with_oc=True)
    assert FAKE_OC not in str(e.value) and FAKE_OC not in repr(manifest.failures())


def test_sanitize_message_redacts_registered_secret_and_oc_param():
    logsafe.register_secret(FAKE_OC)
    assert FAKE_OC not in sanitize_message(f"boom {FAKE_OC}")
    assert sanitize_message("u?OC=abc&x=1") == "u?OC=[MASKED]&x=1"


def test_default_provider_reads_secret_file_not_logged(tmp_path, monkeypatch, make_client, corpus):
    home = tmp_path / "home"
    d = home / ".config" / "press-dispute-review"
    d.mkdir(parents=True)
    f = d / "law_oc"
    f.write_text(FAKE_OC + "\n")
    os.chmod(f, 0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LAW_OPEN_DATA_OC", raising=False)
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, b"{}", {})})
    c = make_client(t, oc_provider=None)
    assert c.get(LIST, with_oc=True).status == 200
    assert FAKE_OC not in repr(c.stats)


@pytest.mark.parametrize("echo", [
    "인증 오류: 등록되지 않은 OC " + FAKE_OC + " 입니다",
    '{"링크":"/DRF/x?id=' + FAKE_OC + '"}',
])
def test_oc_echo_in_other_forms_fails_closed_and_is_not_cached(make_client, manifest, corpus, log_stream, echo):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, echo, {})})
    c = make_client(t)
    r = c.get(LIST, with_oc=True)
    assert not r.ok and r.content_error == "oc_echo_unmasked" and r.body == b""
    assert [f["error_class"] for f in manifest.failures()] == ["oc_echo_unmasked"]
    r2 = c.get(LIST, with_oc=True)                                        # 캐시에 없으므로 다시 받는다
    assert not r2.from_cache and t.counts[SENT] == 2
    manifest.close()
    assert FAKE_OC.encode() not in all_bytes(corpus)
    assert FAKE_OC not in log_stream.getvalue()


@pytest.mark.parametrize("echo, masked", [
    ("<PrecSearch><OC>" + FAKE_OC + "</OC></PrecSearch>", "<PrecSearch><OC>[MASKED]</OC></PrecSearch>"),
    ('{"OC":"' + FAKE_OC + '"}', '{"OC":"[MASKED]"}'),
    ('{"링크":"/DRF/x?OC%3D' + FAKE_OC + '&id=1"}', '{"링크":"/DRF/x?OC%3D[MASKED]&id=1"}'),
])
def test_oc_echo_in_known_contexts_is_masked_not_dropped(make_client, corpus, echo, masked):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, echo, {})})
    r = make_client(t).get(LIST, with_oc=True)
    assert r.ok and r.body.decode() == masked
    assert FAKE_OC.encode() not in all_bytes(corpus)


PLAIN_OC = "treaty"                                    # 평범한 단어 OC(가짜)
PLAIN_SENT = LIST + "&OC=" + PLAIN_OC
PLAIN_BODY = ('{"사건명":"the treaty applies","링크":"/DRF/lawService.do?OC=' + PLAIN_OC +
              '&target=prec&ID=1","x":{"OC":"' + PLAIN_OC + '"},"y":"<OC>' + PLAIN_OC + '</OC>"}')


def test_plain_word_oc_masks_contexts_only_and_keeps_body(make_client, corpus, manifest):
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, PLAIN_SENT: (200, PLAIN_BODY, {})})
    c = make_client(t, oc_provider=lambda: PLAIN_OC)
    r = c.get(LIST, with_oc=True)
    body = r.body.decode()
    assert r.ok and "the treaty applies" in body                         # 법령 문구는 그대로
    assert "OC=[MASKED]&target" in body and '"OC":"[MASKED]"' in body and "<OC>[MASKED]</OC>" in body
    assert not re.search(r"OC(=|\W+)" + PLAIN_OC, body)                  # 어떤 되울림 문맥에도 값이 남지 않는다
    assert manifest.failures() == []


def test_plain_word_oc_does_not_wipe_messages(log_stream):
    logsafe.register_secret(PLAIN_OC, context_only=True)
    msg = f"GET https://www.law.go.kr/treaty/x?OC={PLAIN_OC}&q=treaty failed"
    out = sanitize_message(msg)
    assert out == "GET https://www.law.go.kr/treaty/x?OC=[MASKED]&q=treaty failed"
    import logging
    lg = logging.getLogger("press_review.ingestion")
    lg.info("%s", msg)
    lg.info("path /treaty/ only")
    text = log_stream.getvalue()
    assert "OC=[MASKED]&q=treaty failed" in text and "path /treaty/ only" in text
    assert f"OC={PLAIN_OC}" not in text and logsafe.REDACTED not in text


def test_load_secret_registers_plain_oc_as_context_only(monkeypatch):
    from press_review import secrets
    monkeypatch.setenv("LAW_OPEN_DATA_OC", PLAIN_OC)
    secrets.load_secret("LAW_OPEN_DATA_OC")
    assert not logsafe.contains_sensitive("the treaty applies")
    monkeypatch.setenv("LAW_OPEN_DATA_OC", FAKE_OC)
    secrets.load_secret("LAW_OPEN_DATA_OC")
    assert logsafe.contains_sensitive("x " + FAKE_OC)                      # 비평범 값은 기존대로 전체 치환 대상


def test_oc_embedded_inside_longer_token_is_not_a_false_positive(make_client):
    body = '{"사건번호":"x' + FAKE_OC + 'y"}'
    t = FakeTransport({"https://www.law.go.kr/robots.txt": ROBOTS_OK, SENT: (200, body, {})})
    assert make_client(t).get(LIST, with_oc=True).ok


def test_rights_basis_urls_satisfy_the_source_record_schema():
    import json
    from press_review import contracts
    from press_review.ingestion import rights
    sub = json.loads((pdr_ing_paths.ENGINE / "press_review" / "schemas" / "source-record.schema.json").read_text(encoding="utf-8"))["properties"]["rights_basis"]
    for fam in ("pac_cases", "pac_election", "pac_other", "iendc", "law_go_kr", "naver", "daum"):
        basis = [dict(b, checked_at="2026-10-08T12:00:00+00:00") for b in rights.default_rights(fam)["rights_basis"]]
        assert contracts.schema_errors(basis, sub) == [], fam
