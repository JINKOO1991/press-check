"""다운로드 단계(P2_DESIGN §1): 원자적 저장·해시·중복 연결·크기/매직 검사·재개·순위. 네트워크 없음."""
import io
import json

import pytest

from conftest import FakeClock, FakeTransport, ROBOTS_OK
from ext_helpers import SOURCE, make_pdf, seed
from press_review.ingestion import __main__ as cli
from press_review.ingestion import fetch as fetch_mod
from press_review.ingestion import tiers
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import pac

ROB = {"https://www.pac.or.kr/robots.txt": ROBOTS_OK, "https://www.iendc.go.kr/robots.txt": ROBOTS_OK}
U1, U2, U3 = ("https://www.pac.or.kr/files/%s.pdf" % n for n in ("a", "b", "c"))
PDF_A = make_pdf(["alpha page text for the first synthetic file", "second"])
PDF_B = make_pdf(["beta page text for the second synthetic file"])


def run_fetch(corpus, transport, clock, *argv):
    out = io.StringIO()
    code = cli.main(["fetch", *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=io.StringIO())
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def seed_atts(corpus, specs, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    out = seed(m, atts=specs)
    m.close()
    return out


def att(corpus, att_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        return m.select("SELECT * FROM attachments WHERE att_id=?", (att_id,))[0], m.get_document(att_id.split("#")[0])
    finally:
        m.close()


def test_happy_path_atomic_save_relative_path_no_cache_copy(corpus, clock):
    doc, (a1,) = seed_atts(corpus, [(1, U1, "full", len(PDF_A), "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="casebook.pdf"'})})
    code, res = run_fetch(corpus, t, clock)
    assert code == 0 and res["status"] == "completed" and res["result"]["fetched"] == 1
    row, d = att(corpus, a1)
    assert row["status"] == "fetched" and row["detected_type"] == "pdf" and row["orig_filename"] == "casebook.pdf"
    assert row["raw_path"].startswith("raw/pac_cases/") and row["raw_path"].endswith(".pdf") and len(row["raw_path"]) < 60
    assert (corpus / row["raw_path"]).read_bytes() == PDF_A
    import hashlib
    assert row["raw_sha256"] == hashlib.sha256(PDF_A).hexdigest() and row["dedup_of"] is None
    assert d["status"] == "fetched"
    assert not list((corpus / "raw").rglob(".*tmp*"))                         # 임시 파일이 남지 않는다
    assert not any(p.read_bytes() == PDF_A for p in (corpus / "http_cache").rglob("*.body"))      # PDF 사본이 캐시에 남지 않는다(디스크 이중 사용 방지)


def test_restart_does_not_refetch_and_retry_only_failed(corpus, clock):
    _, ids = seed_atts(corpus, [(1, U1, "full", len(PDF_A), "application/pdf", "PDF"),
                                (2, U2, "full", None, "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {"Content-Type": "application/pdf"}), U2: (404, b"nf", {})})
    _, r1 = run_fetch(corpus, t, clock)
    assert r1["result"]["fetched"] == 1 and r1["result"]["errors"] == {"http_404": 1}
    n = len(t.urls("www.pac.or.kr"))
    t.routes[U2] = (200, PDF_B, {"Content-Type": "application/pdf"})
    _, r2 = run_fetch(corpus, t, clock)
    assert r2["result"]["candidates"] == 1 and r2["result"]["fetched"] == 1       # 실패분만 재개
    assert U1 not in t.urls()[n:] and t.urls().count(U1) == 1                     # 완료분은 다시 받지 않는다
    _, r3 = run_fetch(corpus, t, clock)
    assert r3["result"]["candidates"] == 0


def test_html_error_page_declared_pdf_is_failed_and_not_stored_or_cached(corpus, clock):
    _, (a1,) = seed_atts(corpus, [(1, U1, "full", None, "application/pdf", "PDF")], clock)
    err = b"<html><body>Service temporarily unavailable</body></html>"
    t = FakeTransport({**ROB, U1: (200, err, {"Content-Type": "text/html"})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["failed"] == 1 and res["result"]["errors"] == {"magic_mismatch": 1}
    row, d = att(corpus, a1)
    assert row["status"] == "failed" and row["raw_path"] is None and d["status"] == "failed"
    assert not (corpus / "raw").exists() or not list((corpus / "raw").rglob("*.*"))
    assert not any(p.read_bytes().startswith(b"<html") for p in (corpus / "http_cache").rglob("*.body"))
    # HEAD 가 pdf 라고 했는데 오류 HTML 이 .pdf 로 오는 경우도 kind_of(blob, ext) 폴백에 속지 않는다
    assert fetch_mod.detect_kind(err) == "html" and fetch_mod.kind_of(err, ".pdf") == "pdf"


def test_size_mismatch_with_head_is_failed(corpus, clock):
    _, (a1,) = seed_atts(corpus, [(1, U1, "full", len(PDF_A) + 500, "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {"Content-Type": "application/pdf"})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["errors"] == {"size_mismatch": 1}
    row, _ = att(corpus, a1)
    assert row["status"] == "failed" and row["raw_path"] is None
    assert not any(p.read_bytes() == PDF_A for p in (corpus / "http_cache").rglob("*.body"))     # 깨진 본문 캐시 제거


def test_unknown_and_declared_hwp_vs_pdf_bytes(corpus, clock):
    _, ids = seed_atts(corpus, [(1, U1, "other", None, None, "x.hwp"), (2, U2, "other", None, None, "blob")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {}), U2: (200, b"\x00\x01\x02 random", {})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["errors"] == {"magic_mismatch": 1, "unknown_type": 1}


def test_same_hash_is_stored_once_and_linked(corpus, clock):
    _, ids = seed_atts(corpus, [(1, U1, "full", None, "application/pdf", "PDF"), (2, U2, "chapter", None, "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {}), U2: (200, PDF_A, {})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["fetched"] == 1 and res["result"]["dedup"] == 1
    r1, _ = att(corpus, ids[0])
    r2, _ = att(corpus, ids[1])
    assert r1["dedup_of"] is None and r2["dedup_of"] == ids[0] and r1["raw_path"] == r2["raw_path"]
    assert len(list((corpus / "raw").rglob("*.pdf"))) == 1


def test_time_limit_pauses_and_resume_completes(corpus, clock):
    urls = [f"https://www.pac.or.kr/files/f{i}.pdf" for i in range(6)]
    bodies = {u: make_pdf([f"unique body number {i} " + "x" * 30]) for i, u in enumerate(urls)}
    seed_atts(corpus, [(i + 1, u, "full", None, "application/pdf", "PDF") for i, u in enumerate(urls)], clock)
    t = FakeTransport({**ROB, **{u: (200, b, {"Content-Type": "application/pdf"}) for u, b in bodies.items()}})
    statuses, done = [], 0
    for _ in range(10):
        _, res = run_fetch(corpus, t, clock, "--max-seconds", "5")
        statuses.append(res["status"])
        if res["status"] == "completed":
            break
    assert statuses[0] == "paused_time_limit" and statuses[-1] == "completed"
    for u in urls:
        assert t.urls().count(u) == 1                                               # 한 번씩만 받았다
    m = Manifest(corpus / "manifest.sqlite")
    assert [a["status"] for a in m.list_attachments()] == ["fetched"] * 6
    m.close()


def test_post_attachment_uses_form_post(corpus, clock):
    dl = "https://www.iendc.go.kr/fileDownload.do"
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    m.ensure_source("iendc_review_results", "iendc", "iendc", "https://www.iendc.go.kr/")
    m.upsert_document("iendc:77", "iendc_review_results", title="d")
    hwp = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 fake hwp body"
    m.upsert_attachment("iendc:77#1", "iendc:77", dl + "?atchFileId=A1&fileSn=0", label="결과.hwp", kind="other")
    m.close()
    t = FakeTransport({**ROB, dl: (200, hwp, {"Content-Type": "application/octet-stream"})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["fetched"] == 1 and t.bodies[-1] == b"atchFileId=A1&fileSn=0"
    row, _ = att(corpus, "iendc:77#1")
    assert row["detected_type"] == "hwp" and row["raw_path"].endswith(".hwp")


def test_rights_not_allowed_is_not_fetched(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    doc, (a1,) = seed(m, atts=[(1, U1, "full", None, "application/pdf", "PDF")])
    from press_review.ingestion.rights import RIGHT_KEYS
    r = {k: "allowed" for k in RIGHT_KEYS}
    r["store_raw"] = "permission_required"
    m.set_rights("document", doc, r, [{"scope": "test", "review_status": "pending"}])
    m.close()
    t = FakeTransport(ROB)
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["candidates"] == 0 and U1 not in t.urls()


def test_limit_does_not_become_limit_pages_and_tier_filter(corpus, clock):
    seed_atts(corpus, [(1, U1, "full", None, "application/pdf", "PDF"), (2, U2, "full", None, "application/pdf", "PDF")], clock)
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    seed(m, "200", source="pac_research_quarterly", family="pac_other",
         atts=[(1, "https://www.pac.or.kr/files/q.pdf", "full", None, "application/pdf", "PDF")])
    seed(m, "300", source="made_up_source", family="pac_other",
         atts=[(1, "https://www.pac.or.kr/files/z.pdf", "full", None, "application/pdf", "PDF")])
    m.close()
    t = FakeTransport({**ROB, U1: (200, PDF_A, {}), U2: (200, PDF_B, {}), "https://www.pac.or.kr/files/q.pdf": (200, make_pdf(["q" * 40]), {})})
    parser = cli.build_parser()
    from press_review.ingestion.sources import fetch as fetch_src
    fetch_src.add_arguments(parser)
    ns = parser.parse_args(["fetch", "--limit", "2"])
    assert ns.limit == 2 and ns.limit_pages is None
    _, res = run_fetch(corpus, t, clock, "--limit", "1")
    assert res["result"]["attempted"] == 1 and res["result"]["unmapped_sources"] == ["made_up_source"]
    _, res = run_fetch(corpus, t, clock, "--tier", "A")
    assert res["result"]["attempted"] == 1 and "https://www.pac.or.kr/files/q.pdf" not in t.urls()   # B 는 아직
    _, res = run_fetch(corpus, t, clock)
    assert "https://www.pac.or.kr/files/q.pdf" in t.urls() and "https://www.pac.or.kr/files/z.pdf" not in t.urls()


def test_every_adapter_source_id_has_a_tier():
    ids = {m["source_id"] for m in pac.MENUS}
    from press_review.ingestion.sources import iendc
    ids.add(iendc.SOURCE_ID)
    assert not [i for i in ids if tiers.tier_of(i) is None], "unmapped sources"
    assert tiers.tier_of("pac_standards_correction_page") == "A" and tiers.tier_of("pac_other_misc_reports") == "B"
    assert tiers.parse_tiers("a,B") == ("A", "B")
    with pytest.raises(ValueError):
        tiers.parse_tiers("Z")


def test_dry_run_writes_no_files(corpus, clock):
    seed_atts(corpus, [(1, U1, "full", None, "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {})})
    _, res = run_fetch(corpus, t, clock, "--dry-run")
    assert res["status"] == "dry_run_stopped" and t.calls == []                     # 캐시에 없어 멈춤, 네트워크 0
    assert not (corpus / "raw").exists()


def test_disallowed_host_attachment_is_withheld_not_a_permanent_document_failure(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    doc, ids = seed(m, atts=[(1, U1, "full", None, "application/pdf", "PDF"), (2, "https://blog.naver.com/x/1", "other", None, None, "blog")])
    m.close()
    t = FakeTransport({**ROB, U1: (200, PDF_A, {})})
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["errors"] == {"blocked_url": 1}
    assert not [u for u in t.urls() if "naver" in u]                              # 요청하지 않았다
    r2, d = att(corpus, ids[1])
    assert r2["status"] == "withheld" and d["status"] == "fetched"
    _, res = run_fetch(corpus, t, clock)
    assert res["result"]["candidates"] == 0                                        # 다시 시도하지 않는다
    m = Manifest(corpus / "manifest.sqlite")
    assert [f["error_class"] for f in m.failures() if f["target"] == ids[1]] == ["blocked_url"]    # 실패 행은 남는다
    m.close()


def test_xls_is_stored_under_its_declared_kind_not_as_hwp(corpus, clock):
    _, (a1,) = seed_atts(corpus, [(1, "https://www.pac.or.kr/files/t.asp?menu=boardfile", "full", None, "application/octet-stream", "통계.xls")], clock)
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 workbook"
    t = FakeTransport({**ROB, "https://www.pac.or.kr/files/t.asp?menu=boardfile": (200, ole, {"Content-Type": "application/octet-stream"})})
    _, res = run_fetch(corpus, t, clock)
    row, _ = att(corpus, a1)
    assert res["result"]["fetched"] == 1 and row["detected_type"] == "xls" and row["raw_path"].endswith(".xls")


def test_att_filter_fetches_only_the_named_attachment(corpus, clock):
    _, ids = seed_atts(corpus, [(1, U1, "full", None, "application/pdf", "PDF"), (2, U2, "full", None, "application/pdf", "PDF")], clock)
    t = FakeTransport({**ROB, U1: (200, PDF_A, {}), U2: (200, PDF_B, {})})
    _, res = run_fetch(corpus, t, clock, "--att", ids[1])
    assert res["result"]["fetched"] == 1 and res["result"]["candidates"] == 1
    assert U2 in t.urls() and U1 not in t.urls()
    assert att(corpus, ids[0])[0]["status"] == "discovered" and att(corpus, ids[1])[0]["status"] == "fetched"
    _, again = run_fetch(corpus, t, clock, "--att", ids[1])                      # 완료분은 다시 받지 않는다
    assert again["result"]["candidates"] == 0


def test_iendc_post_url_keeps_plus_in_downname(corpus, clock):
    from press_review.ingestion.sources import iendc
    url = iendc.post_download_url("조치내역(제32차)_2022.hwp", "unfair_C0+C1B6C4A1+C0A7_2022.hwp")
    assert "downname=unfair_C0%2BC1B6C4A1%2BC0A7_2022.hwp" in url and url.endswith("&uniqueKey=&downauth=")
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    m.ensure_source("iendc_review_results", "iendc", "iendc", "https://www.iendc.go.kr/")
    m.upsert_document("iendc:78", "iendc_review_results", title="d")
    m.upsert_attachment("iendc:78#1", "iendc:78", url, label="a.hwp", kind="other")
    m.close()
    hwp = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 fake hwp body"
    t = FakeTransport({**ROB, "https://www.iendc.go.kr/fileDownload.do": (200, hwp, {"Content-Type": "application/octet-stream"})})
    _, res = run_fetch(corpus, t, clock)
    body = t.bodies[-1].decode()
    assert res["result"]["fetched"] == 1
    assert "downname=unfair_C0%2BC1B6C4A1%2BC0A7_2022.hwp" in body and "filename=" in body and "uniqueKey=" in body and "downauth=" in body
