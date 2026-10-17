"""IENDC 어댑터 오프라인 시험. 공개 페이지에서 잘라낸 구조를 축소한 fixture(개인정보 없음)."""
import io
import json

from conftest import FakeClock, FakeTransport, ROBOTS_OK
from press_review.ingestion import __main__ as cli
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import iendc

TOTAL, PER = 7, 3          # 3쪽: 3+3+1
H = "https://www.iendc.go.kr"
PDF = {i: f"/images/pdf/p{i}/pdf/loader.do" for i in range(1, 8)}


def list_html(n, total=TOTAL, last=3):
    rows = ""
    for k in range(PER):
        no = total - PER * (n - 1) - k
        if no < 1:
            break
        rows += (f"<tr><td>{no}</td><td style=\"text-align:left;\"><a onClick=\"fn_edit('detail', '{100 + no}', 'N');\">"
                 f"조치내역(2026.06.{no:02d}.)</a></td><td>관리자</td><td>2026-06-{no:02d}</td><td>9</td></tr>")
    return (f'<div class="synthesis">총 게시물 <span class="num">{total}</span>개</div><table class="board_list"><tbody>{rows}</tbody></table>'
            f'<div id="paging"><a class="page_arrow r2" onclick="fn_cmmn_list_page({last}); return false;">x</a></div>')


def detail_html(no):
    return (f'<input id="idx" name="idx" type="hidden" value="{100 + no}"/><div class="board_view_top"><strong class="tit">'
            f'조치내역(2026.06.{no:02d}.)<div class="info"><span class="ft_color">작성일 :</span> 2026-06-{no:02d}</div></strong></div>'
            f'<script>PDFObject.embed("{PDF[no]}", "#p", o);</script><a href="{PDF[no]}">PDF 보기</a>'
            f"<a onClick=\"cmmn_file_down('조치내역{no}.pdf','x{no}.pdf')\">조치내역{no}.pdf</a>")


def routes(total=TOTAL):
    r = {H + "/robots.txt": ROBOTS_OK}
    for n in (1, 2, 3):
        r[f"{H}/home/kor/board.do?menuPos=45&pageIndex={n}"] = (200, list_html(n, total), {})
    for no in range(1, 8):
        r[f"{H}/home/kor/board.do?menuPos=45&act=detail&idx={100 + no}"] = (200, detail_html(no), {})
        r[H + PDF[no]] = (200, b"", {"Content-Type": "application/pdf", "Content-Length": str(1000 + no)})
    return r


def run(argv, corpus, transport, clock):
    out = io.StringIO()
    code = cli.main(["iendc", *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_parsers_on_fixture_and_bare_error_page():
    p = iendc.parse_list(list_html(3))
    assert (p["total"], len(p["rows"]), p["last_page"], p["rows"][0]["idx"], p["rows"][0]["no"]) == (7, 1, 3, "101", 1)
    d = iendc.parse_detail(detail_html(5))
    assert d["pdf_paths"] == [PDF[5]] and d["file_names"] == ["조치내역5.pdf"] and d["reg_date"] == "2026-06-05" and d["idx"] == "105"
    assert iendc.title_dates("2012년도 제1차 위원회의 결정사항", "2017-04-21") == (None, 2012, "title_year")
    assert iendc.title_dates("x(2026.06.10.)", None) == ("2026-06-10", 2026, "title_date")
    assert iendc.accept_list(b"<html>\xeb\xb9\x84\xec\xa0\x95\xec\x83\x81\xec\xa0\x81 \xec\xa0\x91\xea\xb7\xbc</html>", {})
    assert iendc.accept_detail_for("999")(detail_html(5).encode(), {})        # idx 불일치는 오류


def test_full_run_counts_reconcile_and_idempotent(corpus):
    clock, t = FakeClock(), FakeTransport(routes())
    code, o = run([], corpus, t, clock)
    r = o["result"]
    assert code == 0 and o["status"] == "completed"
    assert (r["total_shown"], r["docs_in_db"], r["list_item_sum"], r["docs_new"]) == (7, 7, 7, 7) and r["reconcile_total_eq_docs_eq_list_sum"]
    assert (r["attachments"], r["head_checked"], r["attachment_bytes_total"]) == (7, 7, sum(1000 + i for i in range(1, 8)))
    assert r["year_distribution"] == {"2026": 7}
    n_net = o["http"]["network_requests"]
    code, o2 = run([], corpus, t, clock)                                          # 재실행: 신규 0, 네트워크는 1쪽 갱신뿐
    r2 = o2["result"]
    assert r2["docs_new"] == 0 and r2["docs_in_db"] == 7 and r2["details_skipped_done"] == 7
    assert o2["http"]["network_requests"] <= 2 and n_net > 14
    m = Manifest(corpus / "manifest.sqlite")
    assert m.get_document("iendc:103")["decision_date"] == "2026-06-03" and m.attachments_for("iendc:103")[0]["content_length"] == 1003
    m.close()


def test_gate_sample_pages_and_detail_idx(corpus):
    clock, t = FakeClock(), FakeTransport(routes())
    code, o = run(["--pages", "1,last", "--detail-idx", "107,101"], corpus, t, clock)
    r = o["result"]
    assert code == 0 and r["pages_processed"] == 2 and r["docs_in_db"] == 4 and r["details_fetched"] == 2 and r["head_checked"] == 2
    assert not r["full_listing"] and not r["reconcile_total_eq_docs_eq_list_sum"]


def test_bare_error_html_is_not_success(corpus):
    bad = (200, "<html><body>비정상적 접근입니다.</body></html>", {})
    t = FakeTransport({H + "/robots.txt": ROBOTS_OK, H + "/home/kor/board.do?menuPos=45&pageIndex=1": bad})
    code, o = run([], corpus, t, FakeClock())
    assert code == 1 and o["status"] == "failed" and o["manifest_counts"]["documents"] == 0


def test_stale_cached_page_is_refetched_when_numbers_shift(corpus):
    clock, t = FakeClock(), FakeTransport(routes())
    run([], corpus, t, clock)
    t2 = FakeTransport(routes(total=8) | {f"{H}/home/kor/board.do?menuPos=45&pageIndex={n}": (200, list_html(n, 8), {}) for n in (1, 2, 3)})
    code, o = run(["--no-details"], corpus, t2, clock)
    assert o["result"]["total_shown"] == 8 and o["result"]["last_page"] == 3 and o["result"]["list_item_sum"] == 8
    assert any("pageIndex=2" in u for u in t2.urls())                              # 낡은 캐시를 버리고 다시 받음


def test_time_limit_pause_then_resume_completes(corpus):
    clock, t = FakeClock(), FakeTransport(routes(), clock=None)
    code, o = run(["--max-seconds", "6"], corpus, t, clock)
    assert code == 0 and o["status"] == "paused_time_limit"
    for _ in range(10):
        code, o = run(["--max-seconds", "30"], corpus, t, clock)
        if o["status"] == "completed":
            break
    assert o["status"] == "completed" and o["result"]["docs_in_db"] == 7 and o["result"]["head_checked"] == 7


def test_hwp_only_attachment_is_registered_as_post_attachment_without_head(corpus):
    r = routes()
    r[f"{H}/home/kor/board.do?menuPos=45&act=detail&idx=107"] = (200, detail_html(7).split("<script>PDFObject")[0] + "<a onClick=\"cmmn_file_down('a.hwp','b+c.hwp')\">a.hwp</a>", {})
    tr = FakeTransport(r)
    code, o = run([], corpus, tr, FakeClock())
    t_urls = tr.urls()
    m = Manifest(corpus / "manifest.sqlite")
    f = [x for x in m.failures(open_only=True) if x["target"] == "iendc:107"]
    atts = m.attachments_for("iendc:107")
    m.close()
    assert code == 0 and o["result"]["no_attachment_url"] == 1 and o["result"]["attachments"] == 7
    assert not f and len(atts) == 1 and atts[0]["url"].startswith(H + "/fileDownload.do?filename=a.hwp&downname=b%2Bc.hwp")
    assert atts[0]["head_checked_at"] is None and not any("fileDownload" in u for u in t_urls)


def test_permanent_5xx_on_one_head_or_detail_does_not_stop_the_run(corpus):
    """V-ROB1: 재시도 뒤에도 실패하는 URL 하나가 실행 전체를 멈추면 안 된다. 실패 장부에 남기고 나머지를 처리한다."""
    r = routes()
    r[H + PDF[6]] = (500, b"", {})                                                          # 첨부 HEAD 가 항상 500
    r[f"{H}/home/kor/board.do?menuPos=45&act=detail&idx=103"] = (503, b"", {})              # 상세 GET 이 항상 503
    clock = FakeClock()
    t = FakeTransport(r)
    for _ in range(2):                                                                      # 이어받기에서도 같은 문서에서 죽지 않는다
        code, o = run([], corpus, t, clock)
        assert code == 0 and o["status"] == "completed"
    res = o["result"]
    assert res["docs_in_db"] == 7 and res["head_checked"] == 5 and res["head_failed_this_run"] == 1
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    open_f = {(f["target"], f["stage"]): f["error_class"] for f in m.failures()}
    m.close()
    assert open_f[("iendc:103", "detail")] == "fetch_failed" and open_f[("iendc:106#1", "head")] == "fetch_failed"
    assert sum(1 for k in open_f if k[1] in ("detail", "head")) == 2


def test_non_pdf_head_response_is_not_frozen_in_cache(corpus):
    """V-ROB3: 2xx 오류 페이지(text/html) HEAD 는 실패로 기록하고 캐시에서 빼서, 다음 실행이 다시 확인한다."""
    r = routes()
    u = H + PDF[1]
    r[u] = [(200, b"", {"Content-Type": "text/html; charset=UTF-8", "Content-Length": "900"}),
            (200, b"", {"Content-Type": "application/pdf", "Content-Length": "1001"})]
    clock, t = FakeClock(), FakeTransport(r)
    _, o1 = run([], corpus, t, clock)
    assert o1["result"]["head_checked"] == 6 and o1["result"]["head_failed_this_run"] == 1
    _, o2 = run([], corpus, t, clock)
    assert o2["result"]["head_checked"] == 7 and t.counts[u] == 2
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert not [f for f in m.failures() if f["stage"] == "head"]
    m.close()
