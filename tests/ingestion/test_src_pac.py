"""PAC 어댑터 오프라인 시험. fixture 는 공개 페이지에서 잘라 쓴 30줄 이내 조각(개인정보 없는 부분만)."""
import io
import json

import pytest

from conftest import FakeClock, FakeTransport
from press_review.ingestion import __main__ as cli
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import pac

LIST_1 = """<section class="x"><div class="publi-list"><ul>
<li><a href="../pages/?p=207&magazine_new=M03&cate=MA04&nPage=1&idx=1212&m=view&f=&s="><figure><img src="/upload_data/magazine/a.jpg" alt="x" /></figure></a>
<p>2025년도 언론조정중재 사례집</p></li>
<li><a href="../pages/?p=207&magazine_new=M03&cate=MA04&nPage=1&idx=1169&m=view&f=&s="><figure><img src="/upload_data/magazine/b.jpg" alt="x" /></figure></a>
<p>2024년도 언론조정중재 사례집</p></li>
</ul></div>
<section class="paginate">
<span class='current'>1</span>
<a href='/kor/pages/index.asp?nPage=2&p=207&magazine_new=M03&cate=MA04&f=&s='>2</a>
<a href='/kor/pages/index.asp?nPage=2&p=207&magazine_new=M03&cate=MA04&f=&s=' class='page-btn btn1'>last</a>
</section></section>"""
LIST_2 = LIST_1.replace("1212", "793").replace("1169", "700").replace("2025년도", "2016년도").replace("2024년도", "2015년도").replace(
    "<span class='current'>1</span>", "<span class='current'>2</span>")
DETAIL = """<div class="webZineView"><div class="prevNext-mp prevNext-w">
<a href="../pages/?p=207&magazine_new=M03&cate=MA04&nPage=1&idx=1169&m=view&f=&s=">2024년도 언론조정중재 사례집</a></div>
<div class="box1"><p>2025년도 언론조정중재 사례집</p><div class="downBtn">
<a href="/_common/new_download_file.asp?menu=magazine&idx=1212&field=pdf_nm" class="btn-white">PDF</a></div></div>
<div class="box2"><ul>
<li><a href="/_common/new_download_file.asp?menu=magazine_sub&sub_idx=8576">제1부 언론조정현황</a></li>
<li><a href="/_common/new_download_file.asp?menu=magazine_sub&sub_idx=8585">- 제1장 정정보도문 게재 사례</a></li>
<li><a href="" target="_blank">링크 없는 항목</a></li>
</ul></div>
<div class="prevNext-mp hidden-mp"><a href="../pages/?p=207&magazine_new=M03&cate=MA04&nPage=1&idx=1169&m=view&f=&s=">이전호</a></div>
</div><div class="list_btn"></div>"""
BOARD = """<div class="statistics-list-wrap"><table><thead><tr><th>번호</th></tr></thead><tbody>
<tr><td>2</td><td class="tl"><a href="javascript:js_board_view(3491);">2026년 제9차 시정권고소위원회 결과</a></td>
<td><img alt="첨부파일" /></td><td>2026.10.06.</td><td>24</td></tr>
<tr><td>1</td><td class="tl"><a href="javascript:js_board_view(3486);">2026년 제8차 시정권고소위원회 결과</a></td>
<td></td><td>2026.09.04.</td><td>190</td></tr>
</tbody></table></div>"""
BOARD_DETAIL = """<div class="statistics-view-wrap"><div class="box1-1"><p><strong>[시정권고 현황]</strong>2026년 제9차 시정권고소위원회 결과</p></div>
<div class="box2-2"><ul><li><a href="/_common/new_download_file.asp?menu=boardfile&file_no=76983">회의 결과.pdf (size:163.1 Kb)</a></li></ul></div>
<div class="prevNext-mp"><a href="/_common/new_download_file.asp?menu=boardfile&file_no=1">다음글 첨부</a></div></div>"""
BOARD_DETAIL_B = """<div class="view-box"><div class="box1"><p class="title">언론중재위원회 기본규칙</p>
<div class="flie-box"><ul><li><a href="/_common/new_download_file.asp?menu=boardfile&file_no=76930">기본규칙 일부개정안.pdf</a></li></ul></div></div></div>
<div class="prevNext-mp"><a href="javascript:js_board_view(1792);">선거기사심의위원회규칙</a></div>"""
STATIC = """<div class="print-contents"><div class="fR"><a class="btn-gray" href="javascript:js_file_download2('시정권고편람(2025.12).pdf');">시정권고 편람</a></div></div><footer></footer>"""

BASE = "https://www.pac.or.kr"
U207 = BASE + "/kor/pages/index.asp?nPage={n}&p=207&magazine_new=M03&cate=MA04&f=&s="
D = BASE + "/kor/pages/?p=207&magazine_new=M03&cate=MA04&idx={i}&m=view"
FULL = BASE + "/_common/new_download_file.asp?menu=magazine&idx=1212&field=pdf_nm"
CH1 = BASE + "/_common/new_download_file.asp?menu=magazine_sub&sub_idx=8576"
CH2 = BASE + "/_common/new_download_file.asp?menu=magazine_sub&sub_idx=8585"
PDF_HEAD = (200, b"", {"Content-Length": "2136847", "Content-Type": "application/octet-stream; Charset=UTF-8",
                       "Content-Disposition": "attachment; filename=%EC%82%AC%EB%A1%80%EC%A7%91.pdf"})


def test_parsers():
    pl = pac.parse_magazine_list(LIST_1)
    assert [i["idx"] for i in pl["items"]] == [1212, 1169] and pl["li_count"] == 2
    assert pac.parse_paginate(LIST_1) == ({1, 2}, 2) and pac.parse_paginate("<p>none</p>") == (set(), 1)
    d = pac.parse_magazine_detail(DETAIL)
    assert [a["kind"] for a in d["attachments"]] == ["full", "chapter", "chapter"] and d["empty_href"] == 1
    assert d["title"] == "2025년도 언론조정중재 사례집"      # 이전호 링크는 첨부로 세지 않는다
    b = pac.parse_board_list(BOARD)
    assert [(r["num"], r["bn"], r["date"], r["has_file"]) for r in b["rows"]] == [(2, 3491, "2026-10-06", True), (1, 3486, "2026-09-04", False)]
    bd = pac.parse_board_detail(BOARD_DETAIL)
    assert len(bd["attachments"]) == 1 and bd["attachments"][0]["label"] == "회의 결과.pdf" and bd["title"].startswith("2026년")
    bb = pac.parse_board_detail(BOARD_DETAIL_B)
    assert bb["title"] == "언론중재위원회 기본규칙" and bb["attachments"][0]["url"].endswith("file_no=76930")
    st = pac.parse_static(STATIC)["attachments"][0]
    assert st["url"].endswith("path_download_file2.asp?f=%uC2DC%uC815%uAD8C%uACE0%uD3B8%uB78C%282025.12%29.pdf")


def test_att_numbers_and_year():
    assert pac.att_number(FULL, "full") == 0 and pac.att_number(CH2, "chapter") == 8585
    assert pac.att_number(BASE + "/_common/new_download_file.asp?menu=boardfile&file_no=76983", "full") == 76983
    assert pac.title_year("2026년도 현안보고서 3 [ISSN: 3140-0841]") == 2026 and pac.title_year("제9회 전국동시지방선거") is None


def _routes(head=PDF_HEAD):
    r = {BASE + "/robots.txt": (404, b"", {}), U207.format(n=1): (200, LIST_1, {}), U207.format(n=2): (200, LIST_2, {})}
    for i in (1212, 1169, 793, 700):
        r[D.format(i=i)] = (200, DETAIL, {})
    for u in (FULL, CH1, CH2):
        r[u] = head
    return r


def _run(corpus, transport, clock, *extra):
    out = io.StringIO()
    code = cli.main(["pac", "--menu", "207", *extra], transport=transport, clock=clock, sleep=clock.sleep, stdout=out,
                    corpus=corpus, log_stream=io.StringIO())
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_enumerate_then_idempotent_rerun(corpus, monkeypatch):
    monkeypatch.setattr(pac, "PAGE_SIZE", 2)          # fixture 쪽당 2건
    clock = FakeClock()
    t = FakeTransport(_routes(), clock=clock)
    code, o = _run(corpus, t, clock)
    s = o["result"]["menus"]["207"]
    assert code == 0 and s["docs"] == 4 and s["new_docs"] == 4 and s["list_pages_ok"] == 2
    assert s["screen_count_match"].startswith("ok") and (s["year_min"], s["year_max"]) == (2015, 2025)
    assert s["head_ok"] == 12 and s["head_failed"] == 0 and s["attachments"] == 12
    n_net = t.counts.copy()
    code, o2 = _run(corpus, t, clock)
    s2 = o2["result"]["menus"]["207"]
    assert s2["new_docs"] == 0 and s2["seen_docs"] == 4 and s2["new_attachments"] == 0 and s2["head_ok"] == s["head_ok"]
    assert t.counts == n_net                      # 재실행은 네트워크를 쓰지 않는다
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert m.counts()["documents"] == 4
    assert sorted(a["att_id"] for a in m.attachments_for("pac_cases:1212")) == ["pac_cases:1212#0", "pac_cases:1212#8576", "pac_cases:1212#8585"]
    assert m.attachments_for("pac_cases:1212")[0]["content_length"] == 2136847
    assert json.loads(m.get_document("pac_cases:1212")["extra_json"])["att_files"]


def test_gate_flags_limit_work(corpus):
    clock = FakeClock()
    t = FakeTransport(_routes(), clock=clock)
    _, o = _run(corpus, t, clock, "--limit-pages", "1", "--max-details", "1", "--max-heads", "2")
    s = o["result"]["menus"]["207"]
    assert s["list_pages"] == 1 and s["details_fetched"] == 1 and s["head_ok"] == 2 and s["screen_count_match"].startswith("not_checked")


def test_html_head_is_failure_not_success_and_three_in_a_row_stops(corpus):
    clock = FakeClock()
    t = FakeTransport(_routes(head=(200, b"", {"Content-Length": "900", "Content-Type": "text/html"})), clock=clock)
    code, o = _run(corpus, t, clock)
    assert o["result"]["stopped"].startswith("head/head_unusable x3")
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert all(a["head_checked_at"] is None for a in m.attachments_for("pac_cases:1212"))
    assert {f["error_class"] for f in m.failures()} == {"head_unusable"}


def test_lists_that_disagree_with_screen_are_not_ok(corpus):
    clock = FakeClock()
    bad = LIST_1.replace('<p>2024년도 언론조정중재 사례집</p>', "").replace("idx=1169", "idy=1169")   # li 2개, 파싱 1개
    t = FakeTransport({**_routes(), U207.format(n=1): (200, bad, {})}, clock=clock)
    _, o = _run(corpus, t, clock, "--limit-pages", "1", "--no-heads")
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert m.list_pages("pac_adjustment_casebooks")[0]["ok"] == 0
    assert any(f["error_class"] == "count_mismatch" for f in m.failures())


def test_error_html_without_marker_is_not_cached(corpus):
    clock = FakeClock()
    t = FakeTransport({**_routes(), U207.format(n=1): (200, "<html>오류</html>", {})}, clock=clock)
    _, o = _run(corpus, t, clock, "--limit-pages", "1")
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert m.counts()["documents"] == 0 and m.failures()[0]["error_class"] == "bad_page"


@pytest.mark.parametrize("name", ["시정권고편람(2025.12).pdf"])
def test_js_escape_matches_javascript(name):
    assert pac.js_escape(name) == "%uC2DC%uC815%uAD8C%uACE0%uD3B8%uB78C%282025.12%29.pdf"
    assert pac.js_escape("a b_c-d.e") == "a%20b_c-d.e"


def test_persistent_5xx_list_page_is_recorded_and_run_continues(corpus, monkeypatch):
    """V-ROB2: 목록 쪽 하나가 재시도 뒤에도 5xx 여도 실행은 이어지고, 메뉴 요약과 실패 장부가 남는다."""
    monkeypatch.setattr(pac, "PAGE_SIZE", 2)
    clock = FakeClock()
    t = FakeTransport({**_routes(), U207.format(n=2): (500, b"", {})}, clock=clock)
    for _ in range(2):
        code, o = _run(corpus, t, clock)
        assert code == 0 and "stopped" not in o["result"]
    s = o["result"]["menus"]["207"]
    assert s["docs"] == 2 and s["list_pages_ok"] == 1 and s["list_pages"] == 2 and s["screen_count_match"].startswith("not_checked")
    assert s["attachments"] == 6 and s["head_ok"] == 6                          # 1쪽 문서의 상세·HEAD 는 처리됐다
    m = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert [(f["stage"], f["error_class"]) for f in m.failures() if f["stage"] == "list"] == [("list", "fetch_failed")]
    assert m.list_pages("pac_adjustment_casebooks")[1]["ok"] == 0
    m.close()


def test_persistent_5xx_static_page_is_recorded(corpus):
    clock = FakeClock()
    u = BASE + "/kor/pages/?p=12"
    t = FakeTransport({BASE + "/robots.txt": (404, b"", {}), u: (500, b"", {})}, clock=clock)
    out = io.StringIO()
    code = cli.main(["pac", "--menu", "12"], transport=t, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
    o = json.loads(out.getvalue().strip().splitlines()[-1])
    assert code == 0 and o["result"]["menus"]["12"]["failed"] is True


def test_hash_placeholder_anchor_is_not_an_attachment():
    """V-NR-02: href="#none" 은 화면에서 링크가 아닌 목차 텍스트다(빈 href 와 같이 센다)."""
    h = ('<div class="webZineView"><div class="box1"><p>연표</p><ul><li><a href="#none" style="cursor:default">1. 연혁</a></li>'
         '<li><a href="#none">2. 조직</a></li><li><a href="/_common/new_download_file.asp?menu=magazine&idx=8&field=pdf_nm">PDF</a></li>'
         '</ul></div><div class="prevNext-mp hidden-mp"></div></div>')
    d = pac.parse_magazine_detail(h)
    assert d["empty_href"] == 2 and [a["kind"] for a in d["attachments"]] == ["full"]
    assert all(a["url"] != BASE + "/kor/pages/" for a in d["attachments"])


def test_mediation_persuasion_scope_note_matches_site():
    """V-NR-06: 첫 호는 2013년 3월호이고 2013~2014년은 월간이다(사이트 상세 idx=640). 범위 메모에 '계간(2015~2017)' 만 적지 않는다."""
    m = next(x for x in pac.MENUS if x["key"] == "214")
    assert "2013" in m["why"] and "(2015~2017)" not in m["why"]
