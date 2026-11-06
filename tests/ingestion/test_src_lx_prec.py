"""법원도서관 법고을 판례 어댑터(lx_prec) 와 대상 생성: 네트워크 없는 가짜 transport.

모든 fixture 는 합성이다(가짜 번호·'갑'·'A법 제1조'). 실제 결정문의 문장·성명은 넣지 않는다. HTML 구조(목록 li·summary_title·showViewerPopup,
상세의 h2 id 표지·parabm·judges, article 바깥의 UI 꼬리말)만 저장 응답에서 본떴다."""
import io
import json
import sqlite3
import types
import urllib.parse as up

import pytest

from conftest import FakeClock, FakeTransport
from pdr_ing_paths import ENGINE, REPO
from press_review import corpus_sqlite as CS
from press_review.ingestion import http as H
from press_review.ingestion import __main__ as cli, lx_targets as T, rights as R, structure as S, structure_records as SR
from press_review.ingestion.admission import CASE_ADMISSION_SOURCES, CASE_ID_PREFIX
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import lx_prec as X

HOST = "lx.scourt.go.kr"
ROBOTS = {f"https://{HOST}/robots.txt": (404, "", {})}
HTML_H = {"Content-Type": "text/html;charset=UTF-8"}
SIG = "대법관 김가나(재판장) 이다라(주심) 박마바"


def li(key, court, title, ptype="P01"):
    pop = f"showViewerPopup('{ptype}', '/search/detail/precedent/0/{key}','{key}');"
    return (f'<li class="search_result_item" id="{key}"><div class="tit_wrap"><div class="tit"><span class="ctg ctg_green_ty01">{court}</span>'
            f'<a class="ellipsis wrap" href="javascript:void(0);" onclick="{pop}" title="새 창"> </a></div></div>'
            f'<div class="txt_wrap" data-limit="270"> 합성 요약 문장이다. </div><div class="summary_wrap" id="summary_{key}" style="display:none;">'
            f'<div class="summary_title">{title}</div></div><!-- <div class="btn_wrap"><a onclick="{pop}">원문보기</a></div> --></li>')


def list_html(items, total=None):
    n = len(items) if total is None else total
    nav = '<a href="javascript:requsetScourtLibUrlGET(\'login\');" class="header_util_inn">로그인</a>'
    return (f'<html><body><nav>{nav}</nav><div class="cnt">총 <span class="c_green_01 fw_b">\n {n}\n </span>건</div>'
            f'<article id="searchArea"><ul class="search_result_list">{"".join(li(*i) for i in items)}</ul></article></body></html>')


EMPTY_LIST = '<html><body><nav>로그인</nav><div class="cnt">총 <span class="c_green_01 fw_b"> 0 </span>건</div></body></html>'


def title(number, date="2018. 10. 30.", form="판결", case="[손해배상(기)]", court_kind=""):
    return f"{date} 선고 {number} {court_kind}{form} {case}"


def para(t):
    return f'<div class="para"><div class="parabm">{t}</div></div>'


def h2(name, label):
    return f'<div class="sectiontitle2"><h2 name="{name}" id="{name}">{label}</h2></div>'


def detail_html(number, court="대법원", date="2018. 10. 30.", form="판결", pansi=("[1] 합성 쟁점 하나",), note=("[1] 합성 요지 하나",),
                order=("상고를 기각한다.",), reasons=("합성 이유 첫 문장이다.", "합성 이유 둘째 문장이다."), judges=SIG, ref_law="A법 제1조", ref_case="대법원 2000. 1. 1. 선고 99다1 판결",
                with_parties=True, ui_tail="법령 전문보기 북마크 폴더 추가 UI_TAIL_MARK", after_reason=(), date_word="선고"):
    parts = [h2("pansi", "【판시사항】"), *map(para, pansi), h2("JudgementNote", "【판결요지】"), *map(para, note),
             h2("RefLaw", "【참조조문】"), f'<div class="para">{ref_law}</div>', h2("RefCase", "【참조판례】"), f'<div class="para">{ref_case}</div>',
             h2("junmun", "【전 문】")]
    if with_parties:
        parts += ['<div class="partyDiv"><div class="partysectiontitle"><h3 name="Party1" id="Party1">【원고, 상고인】</h3></div>'
                  '<div class="partyMemberDiv"><div class="partypara">PARTY_SECRET 갑 (소송대리인 변호사 을)</div></div></div>']
    parts += ['<div class="partyDiv"><div class="partysectiontitle"><h3 name="SubjectDecision" id="SubjectDecision">【원심판결】</h3></div>'
              '<div class="partyMemberDiv"><div class="partypara">서울고법 2017. 1. 2. 선고 2016나1 판결</div></div></div>',
              h2("OutCome", "【주 문】"), *map(para, order), h2("Reason", "【이 유】"), *map(para, reasons),
              *[h2(i, lab) + "".join(map(para, ps)) for i, lab, ps in after_reason],
              '<div class="sectiontitle2"><h2 name="judges" id="judges"></h2></div>', f'<div class="judges" id="judges">{judges}</div>']
    return (f'<html><body><div id="popupContent"><div class="sentencingPopup"><header><h1>{court} {date}{"" if date_word == "자" else " "}{date_word} {number} {form}</h1>'
            f'<input type="hidden" id="case_num" value="{number}"><p>【손해배상(기)】【집1,1】</p></header>'
            f'<article class="container sentencing">{"".join(parts)}<hr></article></div></div><div id="lawService">{ui_tail}</div></body></html>')


class Server:
    """목록·상세 가짜 서버. lists: {번호: [html…] (페이지 순) | html | (status, body)}, details: {키: html | (status, body)}."""

    def __init__(self, lists=None, details=None):
        self.lists, self.details = dict(lists or {}), dict(details or {})
        self.list_calls, self.detail_calls, self.urls, self.headers = [], [], [], []

    def __call__(self, url, n):
        self.urls.append(url)
        parts = up.urlsplit(url)
        q = up.parse_qs(parts.query)
        if parts.path == "/search/precedent/get/list":
            no, page = q["search_txt"][0], int(q["page_no"][0])
            assert q["tab_idx"] == ["0"] and q["display"] == ["100"] and q["search_mode"] == ["simple"], url
            self.list_calls.append((no, page))
            v = self.lists.get(no, EMPTY_LIST)
            if isinstance(v, tuple):
                return v[0], v[1], HTML_H
            if isinstance(v, list):
                v = v[page - 1] if page <= len(v) else EMPTY_LIST
            return 200, v, HTML_H
        if parts.path.startswith("/search/detail/precedent/0/"):
            key = parts.path.rsplit("/", 1)[1]
            self.detail_calls.append(key)
            v = self.details.get(key)
            if v is None:
                return 404, "not found", HTML_H
            return (v[0], v[1], HTML_H) if isinstance(v, tuple) else (200, v, HTML_H)
        return 404, b"", {}

    def transport(self):
        return FakeTransport(ROBOTS, default=self)


def run_cli(argv, corpus, transport, clock=None, log_stream=None):
    clock = clock or FakeClock()
    out = io.StringIO()
    code = cli.main(["lx_prec", *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=log_stream or io.StringIO(), oc_provider=lambda: "UNUSED_OC", resolver=lambda h: ["93.184.216.34"])
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def doc(corpus, doc_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        d = m.get_document(doc_id)
        return d, (json.loads(d["extra_json"]) if d and d["extra_json"] else {}), m.pages_of(doc_id=doc_id)
    finally:
        m.close()


def qual(p):
    q = p["quality_json"]
    return json.loads(q) if isinstance(q, str) else q


def failures(corpus, stage=None):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        return {f["target"]: f for f in m.failures() if stage is None or f["stage"] == stage}
    finally:
        m.close()


def ptext(corpus, pages):
    return {qual(p)["locator"]: (corpus / p["text_path"]).read_text(encoding="utf-8") for p in pages}


def one(number="2018다1", key="1001", court="대법원", **over):
    """번호 하나, 정확 일치 항목 하나(상세는 over 로 조정)."""
    return Server(lists={number: list_html([(key, court, title(number))])}, details={key: detail_html(number, court=court, **over)})


def stop_log(srv):
    return [u for u in srv.urls if "/search/" in u]


# ------------------------------------------------------------------------------------------------ 목록 파싱
def test_list_parse_reads_court_title_type_and_total():
    html = list_html([("11", "대법원", title("2018다1")), ("12", "서울고등법원", title("2017나5", date="2018. 1. 5.", form="판결")), ("13", "대법원", "x", "P04")], total=3)
    r = X.parse_list(html)
    assert r["total"] == 3 and [i["key"] for i in r["items"]] == ["11", "12", "13"]
    a, b, c = r["items"]
    assert (a["court"], a["ptype"], a["numbers"], a["date"]) == ("대법원", "P01", ["2018다1"], "2018-10-30")
    assert b["court"] == "서울고등법원" and b["date"] == "2018-01-05" and c["ptype"] == "P04"
    assert "login" not in json.dumps(r)


def test_exact_match_uses_the_number_token_set_and_rejects_citing_judgments():
    items = [{"key": "1", "numbers": ["2018다1"]}, {"key": "2", "numbers": ["2018다10"]}, {"key": "3", "numbers": ["2018다1", "2018다2"]},
             {"key": "4", "numbers": ["2019다1"]}]
    assert [i["key"] for i in X.exact_items("2018다1", items)] == ["1", "3"]
    r = X.parse_list(list_html([("5", "대법원", title("2014다100, 101"))]))
    assert r["items"][0]["numbers"] == ["2014다100", "2014다101"]            # 병합 표기: 뒤 번호는 앞 번호의 연도·부호를 잇는다
    assert [i["key"] for i in X.exact_items("2014다101", r["items"])] == ["5"]


def test_list_run_registers_only_exact_matches_and_counts_citing_ones(corpus):
    srv = Server(lists={"2018다1": list_html([("11", "대법원", title("2018다1")), ("12", "대법원", title("2019다7")), ("13", "대법원", title("2018다10"))])},
                 details={"11": detail_html("2018다1")})
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    assert srv.detail_calls == ["11"] and out["result"]["docs_new"] == 1 and out["result"]["list_items_not_exact"] == 2
    d, extra, pages = doc(corpus, "lx_prec:11")
    assert d["status"] == "extracted" and d["decision_date"] == "2018-10-30" and extra["resolved_numbers"] == ["2018다1"] and extra["court"] == "대법원"
    assert d["canonical_url"] == f"https://{HOST}/search/detail/precedent/0/11" and extra["body"]["case_nos"] == ["2018다1"]


def test_zero_hits_is_not_found_and_pdf_types_are_skipped_and_counted(corpus):
    srv = Server(lists={"2018다1": EMPTY_LIST, "2018다2": list_html([("21", "대법원", title("2018다2"), "P04")]),
                        "2018다3": list_html([("31", "대법원", title("2018다3"), "P01"), ("32", "대법원", title("2018다3"), "I01")])},
                 details={"31": detail_html("2018다3")})
    code, out = run_cli([a for n in (1, 2, 3) for a in ("--number", f"2018다{n}")], corpus, srv.transport())
    assert code == 0 and srv.detail_calls == ["31"], out
    assert out["result"]["not_found"] == 1 and out["result"]["pdf_only"] == 1 and out["result"]["skipped_pdf"] == 2 and out["result"]["docs_new"] == 1
    assert {t: f["error_class"] for t, f in failures(corpus).items()} == {"lx_no:2018다1": "not_found", "lx_no:2018다2": "pdf_only"}


def test_same_number_in_two_courts_registers_two_documents_and_counts_multi(corpus):
    items = [("41", "서울고등법원", title("2016나1", date="2017. 5. 1.")), ("42", "수원지방법원", title("2016나1", date="2017. 6. 1."))]
    srv = Server(lists={"2016나1": list_html(items)}, details={"41": detail_html("2016나1", court="서울고등법원", date="2017. 5. 1."),
                                                          "42": detail_html("2016나1", court="수원지방법원", date="2017. 6. 1.")})
    code, out = run_cli(["--number", "2016나1"], corpus, srv.transport())
    assert out["result"]["multi_numbers"] == 1 and out["result"]["docs_new"] == 2 and srv.detail_calls == ["41", "42"]
    assert doc(corpus, "lx_prec:41")[1]["multi"] is True and doc(corpus, "lx_prec:42")[0]["decision_date"] == "2017-06-01"
    assert failures(corpus, "resolve") == {}


def test_multi_in_progress_is_left_when_a_later_detail_fails_and_resumed_next_run(corpus):
    items = [("41", "서울고등법원", title("2016나1")), ("42", "수원지방법원", title("2016나1"))]
    srv = Server(lists={"2016나1": list_html(items)}, details={"41": detail_html("2016나1", court="서울고등법원"), "42": (500, "err")})
    run_cli(["--number", "2016나1"], corpus, srv.transport())
    assert [f["error_class"] for f in failures(corpus, "resolve").values()] == ["http_500"] and doc(corpus, "lx_prec:41")[0]["status"] == "extracted"
    srv2 = Server(lists=srv.lists, details={"41": detail_html("2016나1", court="서울고등법원"), "42": detail_html("2016나1", court="수원지방법원")})
    code, out = run_cli(["--number", "2016나1"], corpus, srv2.transport())
    assert srv2.detail_calls == ["42"] and out["result"]["docs_new"] == 1 and failures(corpus, "resolve") == {}


def test_list_pages_are_followed_until_the_total_and_overflow_registers_nothing(corpus):
    page1 = list_html([(str(100 + i), "대법원", title(f"2019다{i + 50}")) for i in range(100)], total=101)
    page2 = list_html([("999", "대법원", title("2018다1"))], total=101)
    srv = Server(lists={"2018다1": [page1, page2]}, details={"999": detail_html("2018다1")})
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert srv.list_calls == [("2018다1", 1), ("2018다1", 2)] and out["result"]["docs_new"] == 1
    big = list_html([("1", "대법원", title("2019다5"))], total=X.MAX_LIST_PAGES * X.LIST_ROWS + 1)
    srv2 = Server(lists={"2018다2": big})
    run_cli(["--number", "2018다2"], corpus, srv2.transport())
    assert failures(corpus)["lx_no:2018다2"]["error_class"] == "page_overflow" and srv2.list_calls == [("2018다2", 1)]


def test_malformed_and_constitutional_numbers_are_recorded_without_request(corpus):
    srv = Server()
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1", "--number", "가나다", "--number", "2018 다 1", "--number", "2018다1"], corpus, tr)
    assert out["result"]["malformed_number"] == 2 and out["result"]["duplicates"] == 1 and srv.list_calls == [("2018다1", 1)]
    assert X.normalize_number("97도 158") == "97도158" and X.normalize_number("2008도11847.") is None and X.normalize_number("4293형상823") is None


# ------------------------------------------------------------------------------------------------ 상세 쪽
def test_detail_pages_follow_the_headings_and_leave_out_parties_and_ui_tail(corpus):
    srv = one("2018다1", "1001")
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    q = [qual(p) for p in pages]
    kinds = {x["section_kind"] for x in q}
    assert {"case_header", "headnote_issue", "headnote_holding", "ref_statutes", "ref_cases", "lower_court", "order", "reasons", "signature"} <= kinds
    assert [x["locator"] for x in q][0] == "h000" and all(not x["locator"].startswith("p") for x in q)
    texts = ptext(corpus, pages)
    blob = "".join(texts.values())
    assert "PARTY_SECRET" not in blob and "UI_TAIL_MARK" not in blob and "법령 전문보기" not in blob and "【전 문】" not in blob
    assert "사건번호: 2018다1" in texts["h000"] and "선고일자: 2018. 10. 30." in texts["h000"] and "법원명: 대법원" in texts["h000"]
    by = {x["section_kind"]: x for x in q}
    assert by["order"]["opinion_kind"] is None and by["reasons"]["opinion_kind"] == "main"
    assert extra["body"]["opinion_status"] == "none" and extra["body"]["text_available"] is True and extra["body"]["warnings"] == []
    assert SIG in texts[by["signature"]["locator"]] and all(not p["needs_review"] for p in pages)


def test_en_banc_majority_and_dissent_are_split_with_kinds(corpus):
    reasons = ("합성 다수 이유 첫 문장이다.", "5. 상고이유에 대한 대법관 갑, 대법관 을의 반대의견", "합성 반대 이유 문장이다.", "6. 대법관 병의 별개의견", "합성 별개 이유 문장이다.")
    srv = one("2018다1", "1001", reasons=reasons)
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    q = [qual(p) for p in pages]
    body = [x for x in q if x["section_kind"] in ("reasons", "opinion")]
    assert [(x["section_kind"], x["opinion_kind"]) for x in body] == [("reasons", "majority"), ("opinion", "dissent"), ("opinion", "concurrence")]
    assert extra["body"]["opinion_status"] == "split" and extra["body"]["opinion_split"]["marker_count"] == 2
    texts = ptext(corpus, pages)
    maj = next(texts[qual(p)["locator"]] for p in pages if qual(p)["opinion_kind"] == "majority" and qual(p)["section_kind"] == "reasons")
    assert "합성 반대 이유" not in maj and "합성 다수 이유" in maj
    for p in pages:                          # 법정의견이 아닌 쪽은 가드가 court_majority 를 막는다
        if qual(p)["opinion_kind"] in ("dissent", "concurrence"):
            assert SR.detc_majority_barred(p, {}) == f"opinion_kind_{qual(p)['opinion_kind']}"


def test_headnote_with_dissent_markers_is_split_and_resumes_at_the_next_item(corpus):
    note = ("[1] [다수의견] 합성 다수 요지이다.", "합성 다수 요지 이어짐이다.", "[대법관 갑, 대법관 을의 반대의견] 합성 반대 요지이다.", "[2] 합성 둘째 항목 요지이다.")
    srv = one("2018다1", "1001", note=note)
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    hs = [(qual(p), (corpus / p["text_path"]).read_text(encoding="utf-8")) for p in pages if qual(p)["section_kind"] in ("headnote_holding", "headnote_opinion")]
    assert [(x["section_kind"], x["opinion_kind"]) for x, _t in hs] == [("headnote_holding", "majority"), ("headnote_opinion", "dissent"), ("headnote_holding", "majority")]
    assert "합성 반대 요지" in hs[1][1] and "합성 반대 요지" not in hs[0][1] + hs[2][1] and hs[2][1].startswith("[2]")
    assert len({x["locator"] for x, _t in hs}) == 3 and extra["body"]["headnote_split"]["status"] == "split"


def test_unsplittable_opinion_mentions_flag_pages_needs_review(corpus):
    reasons = ("합성 이유이다. 대법관 갑의 반대의견이 있다고 서술하지만 표제가 없다.", "합성 이유 이어짐이다.")
    note = ("[1] 표제 없는 합성 요지이다. 대법관 을의 별개의견은 아래와 같다.",)
    srv = one("2018다1", "1001", reasons=reasons, note=note)
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    flagged = {qual(p)["section_kind"]: p for p in pages if p["needs_review"]}
    assert set(flagged) == {"reasons", "headnote_holding"} and extra["body"]["opinion_status"] == "unsplit" and extra["body"]["headnote_split"]["status"] == "unsplit"
    assert SR.detc_majority_barred(flagged["reasons"], {}) == "needs_review" and SR.detc_majority_barred(flagged["headnote_holding"], {}) == "needs_review"
    assert qual(flagged["reasons"])["opinion_kind"] == "main"


def test_missing_article_or_wrong_number_is_a_detail_failure_and_not_cached(corpus):
    srv = Server(lists={"2018다1": list_html([("11", "대법원", title("2018다1"))])}, details={"11": detail_html("2019다9")})
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert code == 0 and failures(corpus)["lx_no:2018다1"]["error_class"] == "serial_mismatch" and doc(corpus, "lx_prec:11")[0] is None
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert srv.detail_calls == ["11", "11"]                                          # 맞지 않는 응답은 캐시하지 않는다


def test_no_judgment_text_is_not_marked_text_available(corpus):
    srv = one("2018다1", "1001", order=(), reasons=())
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    assert extra["body"]["text_available"] is False and "empty_judgment_text" in extra["body"]["warnings"]


# ------------------------------------------------------------------------------------------------ 권리·정지·간격
def test_rights_gate_stops_before_any_request_until_derivatives_are_allowed(corpus, real_default_rights):
    srv = one()
    tr = srv.transport()
    code, out = run_cli(["--number", "2018다1"], corpus, tr)
    assert code == 1 and out["error_class"] == "rights_not_recorded" and tr.calls == []
    m = Manifest(corpus / "manifest.sqlite")
    assert m.get_source(X.SOURCE_ID)["source_family"] == "lx_scourt"
    R.record_installer_decision(m, "lx_scourt", {"read": "allowed", "store_raw": "allowed"}, "시험", "t", source_ids=[X.SOURCE_ID])      # 파생물 미허용
    m.close()
    tr = srv.transport()
    code, out = run_cli(["--number", "2018다1"], corpus, tr)
    assert code == 1 and out["error_class"] == "rights_not_recorded" and tr.calls == []
    m = Manifest(corpus / "manifest.sqlite")
    R.record_installer_decision(m, "lx_scourt", {"create_derivatives": "allowed"}, "시험", "t", source_ids=[X.SOURCE_ID])
    m.close()
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed" and out["result"]["docs_new"] == 1


@pytest.mark.parametrize("resp", [(403, "forbidden"), (429, "slow down"),
                                  (200, "<html><body>이 서비스는 로그인이 필요합니다.</body></html>"),
                                  (200, "<html><body>비정상적인 접근이 감지되었습니다.</body></html>"),
                                  (200, "<html><body><img alt='captcha'></body></html>")])
def test_block_signals_stop_everything_at_once_without_retry(corpus, resp):
    srv = Server(lists={f"2018다{n}": resp for n in range(1, 4)})
    tr = srv.transport()
    code, out = run_cli([a for n in range(1, 4) for a in ("--number", f"2018다{n}")], corpus, tr)
    assert code == 1 and out["status"] == "failed" and out["error_class"] == "access_stopped", out
    assert len(stop_log(srv)) == 1                                                  # 재시도 없음, 다음 번호로 넘어가지 않음


def test_block_signal_on_a_detail_page_also_stops(corpus):
    srv = Server(lists={f"2018다{n}": list_html([(str(n), "대법원", title(f"2018다{n}"))]) for n in (1, 2)},
                 details={"1": (429, "slow"), "2": detail_html("2018다2")})
    code, out = run_cli(["--number", "2018다1", "--number", "2018다2"], corpus, srv.transport())
    assert out["error_class"] == "access_stopped" and srv.detail_calls == ["1"]


def test_navigation_login_link_and_judgment_text_never_trigger_a_stop(corpus):
    reasons = ("합성 이유이다. 피고인은 비정상적인 접근 방법으로 로그인이 필요한 사이트에 침입하였다.",)
    srv = one("2018다1", "1001", reasons=reasons)
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed" and out["result"]["docs_new"] == 1


def test_consecutive_failures_stop_after_three_and_success_resets(corpus):
    odd = "<html><body>서비스 점검 중입니다.</body></html>"
    srv = Server(lists={f"2018다{n}": odd for n in range(1, 6)})
    code, out = run_cli([a for n in range(1, 6) for a in ("--number", f"2018다{n}")], corpus, srv.transport())
    assert code == 1 and out["status"] == "failed" and "consecutive" in out["error"] and srv.list_calls == [(f"2018다{n}", 1) for n in (1, 2, 3)]
    assert failures(corpus, "resolve") and {f["error_class"] for f in failures(corpus).values()} == {"list_format"}


def test_server_errors_are_not_retried_and_count_toward_the_stop(corpus):
    srv = Server(lists={f"2018다{n}": (500, "err") for n in range(1, 6)})
    tr = srv.transport()
    code, out = run_cli([a for n in range(1, 6) for a in ("--number", f"2018다{n}")], corpus, tr)
    assert code == 1 and len(stop_log(srv)) == 3 and "consecutive" in out["error"]
    assert {f["error_class"] for f in failures(corpus).values()} == {"http_500"}


def test_requests_are_spaced_at_least_five_seconds_and_use_a_generic_user_agent(corpus):
    clock = FakeClock()
    srv = Server(lists={f"2018다{n}": list_html([(str(n), "대법원", title(f"2018다{n}"))]) for n in (1, 2)},
                 details={str(n): detail_html(f"2018다{n}") for n in (1, 2)})
    tr = srv.transport()
    run_cli([a for n in (1, 2) for a in ("--number", f"2018다{n}")], corpus, tr, clock=clock)
    assert len(stop_log(srv)) == 4 and clock.sleeps and min(clock.sleeps) >= 5.0
    ua = {h["User-Agent"] for h in tr.seen_headers}
    assert len(ua) == 1 and "@" not in next(iter(ua)) and HOST in H.ALLOWED_HOSTS and H.HOST_MIN_INTERVAL[HOST] == 5.0 and HOST in H.NO_RETRY_HOSTS


def test_dry_run_makes_no_request_and_reports_the_miss(corpus):
    srv = one()
    tr = srv.transport()
    code, out = run_cli(["--number", "2018다1", "--dry-run"], corpus, tr)
    assert tr.calls == [] and out["status"] == "dry_run_stopped" and "search_txt=" in json.dumps(out)


def test_limit_caps_the_numbers_processed(corpus):
    srv = Server(lists={f"2018다{n}": EMPTY_LIST for n in range(1, 6)})
    code, out = run_cli([a for n in range(1, 6) for a in ("--number", f"2018다{n}")] + ["--limit", "2"], corpus, srv.transport())
    assert srv.list_calls == [("2018다1", 1), ("2018다2", 1)] and out["result"]["not_found"] == 2 and out["result"]["skipped_limit"] == 3


# ------------------------------------------------------------------------------------------------ 이어받기·재파싱·건너뛰기
def test_resume_skips_done_numbers_and_rebuilds_from_raw_without_detail_request(corpus):
    run_cli(["--number", "2018다1"], corpus, one().transport())
    tr2 = Server().transport()
    code, out = run_cli(["--number", "2018다1"], corpus, tr2)
    assert out["result"]["skipped_done"] == 1 and tr2.calls == []
    m = Manifest(corpus / "manifest.sqlite")                                        # 추출이 중간에 끊긴 상태를 만든다
    m.delete_derived(doc_id="lx_prec:1001")
    m.set_document_status("lx_prec:1001", "fetched")
    m.update_document_fields("lx_prec:1001", extra_merge={"resolved_numbers": []})
    m.close()
    srv3 = Server(lists=one().lists)
    code, out = run_cli(["--number", "2018다1"], corpus, srv3.transport())
    assert srv3.detail_calls == [] and out["result"]["resumed_from_raw"] == 1
    d, extra, pages = doc(corpus, "lx_prec:1001")
    assert d["status"] == "extracted" and len(pages) >= 8 and "2018다1" in extra["resolved_numbers"]
    assert extra["body"]["raw_path"].startswith("raw/lx_scourt/") and (corpus / extra["body"]["raw_path"]).is_file()


def test_reparse_rebuilds_pages_from_stored_raw_with_no_request(corpus):
    run_cli(["--number", "2018다1"], corpus, one().transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    (corpus / pages[0]["text_path"]).write_text("손상", encoding="utf-8")
    tr = Server().transport()
    code, out = run_cli(["--reparse"], corpus, tr)
    assert tr.calls == [] and out["result"]["reparsed"] == 1 and out["result"]["reparse_skipped"] == 0
    d2, e2, p2 = doc(corpus, "lx_prec:1001")
    assert d2["status"] == "extracted" and len(p2) == len(pages) and (corpus / pages[0]["text_path"]).read_text(encoding="utf-8") != "손상"
    raw = corpus / e2["body"]["raw_path"]
    raw.write_bytes(raw.read_bytes() + b"x")                                          # sha 가 달라지면 건드리지 않는다
    code, out = run_cli(["--reparse"], corpus, tr)
    assert out["result"]["reparse_skipped"] == 1 and out["result"]["reparsed"] == 0


def seed_lawgo_prec(corpus, number, *, text=True, serial="900", status="extracted"):
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source("lawgo_prec_supreme", "law_go_kr", "판례", "https://www.law.go.kr/")
    m.upsert_document(f"lawgo_prec:{serial}", "lawgo_prec_supreme", title="합성", decision_date="2018-10-30",
                      extra={"serial": serial, "case_no": number, "body": {"case_nos": [number], "text_available": text}})
    m.set_document_status(f"lawgo_prec:{serial}", status)
    m.close()


def test_number_whose_text_is_already_in_the_ledger_is_skipped_before_any_request(corpus):
    seed_lawgo_prec(corpus, "2018다1")
    srv = one()
    tr = srv.transport()
    code, out = run_cli(["--number", "2018다1"], corpus, tr)
    assert out["result"]["skipped_covered"] == 1 and tr.calls == [] and doc(corpus, "lx_prec:1001")[0] is None
    seed_lawgo_prec(corpus, "2018다2", text=False, serial="901")                  # 본문 없는 법제처 문서는 막지 않는다
    srv2 = one("2018다2", "2002")
    code, out = run_cli(["--number", "2018다2"], corpus, srv2.transport())
    assert out["result"]["docs_new"] == 1


# ------------------------------------------------------------------------------------------------ 연결 상수
def test_registration_constants_and_attribution():
    assert X.SOURCE_ID == "lx_prec" and X.FAMILY == "lx_scourt" and X.DOC_PREFIX == "lx_prec"
    assert X.FAMILY in R.COLLECTED_FAMILIES and R.FAMILY_HOST[X.FAMILY] == HOST and X.FAMILY in S.EXTERNAL_API_FAMILIES
    assert SR.SOURCE_PROFILE[X.SOURCE_ID] == "court_judgment" and X.SOURCE_ID in CASE_ADMISSION_SOURCES and CASE_ID_PREFIX[X.SOURCE_ID] == "lx_prec:"
    prof = SR.PROFILES["court_judgment"]
    assert prof.opinion_guard and not prof.detc_guards
    assert "court_judgment" in S.LEDGER_PROFILES
    for schema in (ENGINE / "press_review" / "schemas" / "source-record.schema.json", REPO / "docs" / "handoff" / "contracts" / "source-record.schema.json"):
        assert "lx_scourt" in json.loads(schema.read_text(encoding="utf-8"))["properties"]["source_family"]["enum"]
    assert CS.SOURCE_ATTRIBUTION["lx_prec"] == "법원 판결, 법원도서관 법고을 제공"
    assert CS.attribution_for("lx_prec:3272600") == "법원 판결, 법원도서관 법고을 제공"
    assert CS.attribution_for("lawgo_prec:1") == "법원 판결, 법제처 국가법령정보센터 제공"


def test_structure_candidate_requires_body_text():
    mk = lambda t: {"source_id": "lx_prec", "extra_json": json.dumps({"body": {"text_available": t}})}
    assert S._structure_candidate_doc(mk(True)) and not S._structure_candidate_doc(mk(False))


def test_disk_guard_registration_and_adapter_discovery():
    assert cli.DISK_GUARDED["lx_prec"][0] == "case_json" and "lx_prec" in cli.available_sources()


def test_public_url_is_the_open_detail_page():
    assert X.public_url("3272600") == f"https://{HOST}/search/detail/precedent/0/3272600" and X.detail_url("3272600") == X.public_url("3272600")
    with pytest.raises(ValueError):
        X.detail_url("12/../x")


def test_ledger_values_use_the_printed_number_and_date_for_the_lx_profile(corpus):
    run_cli(["--number", "2018다1"], corpus, one().transport())
    m = Manifest(corpus / "manifest.sqlite")
    led = S.ledger_values(m, "lx_prec:1001")
    m.close()
    assert led == {"case_number": "2018다1", "case_number_tokens": ["2018다1"], "decision_date": "2018-10-30"}


# ------------------------------------------------------------------------------------------------ 색인 단계: 같은 사건이 법제처 경로에도 있을 때
def _passage(family, pid, number, date="2018-10-30", created="2026-01-01"):
    return {"source_family": family, "source_id": f"{family}:{pid}", "passage_id": pid, "case_number": number, "decision_date": date, "_created": created}


def _indexed(monkeypatch, rows, key=lambda p: p["passage_id"]):
    monkeypatch.setattr(CS, "_iter_eligible", lambda *a, **k: iter([dict(r) for r in rows]))
    monkeypatch.setattr(CS, "_dedup_key", key)
    excluded = {}
    return [p["passage_id"] for p in CS._iter_passages(None, {}, {}, excluded, None)], excluded


def test_lx_passage_is_dropped_only_when_lawgo_indexes_the_same_number_on_the_same_date(monkeypatch):
    rows = [_passage("lx_prec", "x1", "2018다1"), _passage("lx_prec", "x2", "2018다1", date="2019-01-01"), _passage("lx_prec", "x3", "2018다3"),
            _passage("lawgo_prec_supreme", "l1", "2018다1, 2")]
    out, excluded = _indexed(monkeypatch, rows)
    assert sorted(out) == ["l1", "x2", "x3"] and excluded == {"case_superseded_by_lawgo": 1}


def test_lx_passage_stays_without_an_eligible_lawgo_passage_and_cannot_win_the_dedup(monkeypatch):
    out, excluded = _indexed(monkeypatch, [_passage("lx_prec", "x1", "2018다1")])
    assert out == ["x1"] and excluded == {}
    rows = [_passage("lx_prec", "x1", "2018다1", created="2020-01-01"), _passage("lawgo_prec_supreme", "l1", "2018다1", created="2026-01-01")]
    out, excluded = _indexed(monkeypatch, rows, key=lambda p: "same")
    assert out == ["l1"] and excluded == {"case_superseded_by_lawgo": 1}


def test_ccourt_rule_is_unchanged_by_the_generalization(monkeypatch):
    rows = [_passage("ccourt_detc", "c1", "2003헌가6"), _passage("lawgo_detc", "l1", "2003헌가5, 2003헌가6"), _passage("lx_prec", "x1", "2003헌가6")]
    out, excluded = _indexed(monkeypatch, rows)
    assert sorted(out) == ["l1", "x1"] and excluded == {"case_superseded_by_lawgo": 1}


# ------------------------------------------------------------------------------------------------ 대상 생성
def test_number_extraction_keeps_court_case_codes_and_drops_constitutional_and_noise():
    t = ("대법원 2018. 10. 30. 선고 2014다61654 전원합의체 판결, 97도158 판결, 대법원 2000다37524, 37531 판결, 헌재 2020헌마1 결정, "
         "2013년 12월 1일, 제3조 제2항 20가 1, 서울고법 2013나38444 판결, 2015. 9. 10. 선고 2013다26432, 2000다1 참조, 2018도1234, 5678원")
    got = T.extract_numbers(t)
    assert got == ["2014다61654", "1997도158", "2000다37524", "2000다37531", "2013나38444", "2013다26432", "2000다1", "2018도1234"]
    assert T.to_lx_form("1997도158") == "97도158" and T.to_lx_form("2014다61654") == "2014다61654" and T.to_lx_form("2000다1") == "2000다1"


def _index(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE passages(rid INTEGER PRIMARY KEY, passage_id TEXT, source_id TEXT, source_family TEXT, authority_type TEXT, case_number TEXT, text TEXT)")
    c.execute("CREATE TABLE case_numbers(norm TEXT NOT NULL, rid INTEGER NOT NULL)")
    for i, (sid, fam, auth, cn, text) in enumerate(rows):
        c.execute("INSERT INTO passages VALUES(?,?,?,?,?,?,?)", (i, f"p{i}", sid, fam, auth, cn, text))
        if cn:
            c.execute("INSERT INTO case_numbers VALUES(?,?)", (cn, i))
    c.commit()
    c.close()


def test_build_targets_counts_citing_documents_and_splits_covered_and_structure_only(tmp_path, corpus):
    idx = tmp_path / "idx.sqlite"
    rows = []
    for d in ("d1", "d2", "d3"):                                                  # 세 문서가 2018다1 을 인용한다(문서마다 passage 가 둘이어도 1로 센다)
        rows += [(d, "pac_research_quarterly", "research", None, "참조 2018다1 판결."), (d, "pac_research_quarterly", "research", None, "또 2018다1")]
    rows += [("d1", "pac_research_quarterly", "research", None, "2019다2"), ("d2", "pac_research_quarterly", "research", None, "2019다2"),
             ("d1", "pac_research_quarterly", "research", None, "2020다3 와 2020헌마9"),
             ("d1", "pac_research_quarterly", "research", None, "2018다4 판결"), ("d2", "pac_research_quarterly", "research", None, "2018다4"), ("d3", "pac_research_quarterly", "research", None, "2018다4"),
             ("d1", "pac_research_quarterly", "research", None, "2018다5"), ("d2", "pac_research_quarterly", "research", None, "2018다5"), ("d3", "pac_research_quarterly", "research", None, "2018다5"),
             ("lawgo_prec:7", "lawgo_prec_supreme", "judgment", "2018다4", "합성 판결문이 2018다1 을 언급한다"),            # 색인된 번호 2018다4 는 대상이 아니다
             ("d1", "pac_other_misc_reports", "research", None, "자기 인용 2018다6"), ("lawgo_prec:8", "lawgo_prec_supreme", "judgment", "2018다6", "본문")]
    _index(idx, rows)
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source("lawgo_prec_supreme", "law_go_kr", "판례", "https://www.law.go.kr/")
    m.upsert_document("lawgo_prec:50", "lawgo_prec_supreme", title="합성", extra={"case_no": "2018다5", "body": {"case_nos": ["2018다5"], "text_available": True}})
    m.upsert_document("lawgo_prec:51", "lawgo_prec_supreme", title="합성", extra={"case_no": "2019다2", "body": {"case_nos": ["2019다2"], "text_available": False}})
    res = T.build_targets(idx, m)
    m.close()
    req = {x["number"]: x for x in res["request"]}
    assert req["2018다1"]["docs"] == 4 and "2018다4" not in req and "2018다5" not in req and "2018다6" not in req       # lawgo 판결 문서 1건이 더 인용한다
    assert req["2019다2"]["docs"] == 2 and req["2020다3"]["docs"] == 1 and all("헌" not in n for n in req)
    assert [x["number"] for x in res["structure_only"]] == ["2018다5"] and res["structure_only"][0]["docs"] == 3
    assert res["counts"]["request"] == {"min3": 1, "min2": 2, "all": 3} and res["counts"]["structure_only"] == {"min3": 1, "min2": 1, "all": 1}
    assert res["counts"]["indexed_numbers"] == 2 and res["lx_numbers"]["min3"] == ["2018다1"]
    json.dumps(res)


def test_build_targets_opens_the_index_read_only(tmp_path, corpus):
    idx = tmp_path / "idx.sqlite"
    _index(idx, [("d1", "pac_research_quarterly", "research", None, "2018다1")])
    before = idx.read_bytes()
    m = Manifest(corpus / "manifest.sqlite")
    T.build_targets(idx, m)
    m.close()
    assert idx.read_bytes() == before and not (tmp_path / "idx.sqlite-journal").exists()


# ------------------------------------------------------------------------------------------------ 구조화 의견 가드
def _page(kind, section, review=False):
    return {"quality_json": json.dumps({"opinion_kind": kind, "section_kind": section}), "needs_review": review}


@pytest.mark.parametrize("profile, kind, section, review, kept", [
    ("court_judgment", "dissent", "opinion", False, 0), ("court_judgment", "concurrence", "headnote_opinion", False, 0),
    ("court_judgment", "main", "reasons", True, 0), ("court_judgment", "majority", "reasons", False, 1), ("court_judgment", "main", "reasons", False, 1)])
def test_lx_profile_keeps_dissent_and_flagged_pages_out_of_court_majority(profile, kind, section, review, kept):
    ctx = types.SimpleNamespace(profile=SR.PROFILES[profile], ledger={}, page_meta={"p1": _page(kind, section, review)})
    it = {"text": "서술", "quote": {"page_id": "p1", "start": 0, "end": 8, "quote": "위 기사는 합성 문장이다"}}
    items = {"claims_or_requested_remedy": [], "factual_background": [], "article_claims": [it], "disputed_spans": []}
    SR._fix_article_speaker_items(items, SR._Stats(), "judgment", ctx)
    assert len(items["factual_background"]) == kept


def test_decision_type_titles_ending_in_ja_are_matched_in_list_and_detail(corpus):
    items = [("61", "서울중앙지방법원", "2014. 3. 5.자 2014카합123 결정 [가처분]")]
    srv = Server(lists={"2014카합123": list_html(items)}, details={"61": detail_html("2014카합123", court="서울중앙지방법원", date="2014. 3. 5.", form="결정", date_word="자")})
    r = X.parse_list(list_html(items))["items"][0]
    assert (r["numbers"], r["date"], r["form"], r["case_name"]) == (["2014카합123"], "2014-03-05", "결정", "가처분")
    code, out = run_cli(["--number", "2014카합123"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:61")
    assert out["result"]["docs_new"] == 1 and d["decision_date"] == "2014-03-05" and extra["body"]["warnings"] == []
    assert "선고일자: 2014. 3. 5." in ptext(corpus, pages)["h000"]


def test_unknown_sections_after_the_reasons_stay_in_the_judgment_text_as_headings(corpus):
    srv = one("2018나1", "1001", court="서울고등법원", after_reason=[("Appendix", "【별지】", ["합성 별지 문장이다."])])
    run_cli(["--number", "2018나1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:1001")
    q = {qual(p)["section_kind"]: p for p in pages}
    assert "합성 별지 문장" in ptext(corpus, pages)[qual(q["heading"])["locator"]] and extra["body"]["warnings"] == ["unknown_section:Appendix"]
    assert [qual(p)["section_kind"] for p in pages if qual(p)["section_kind"] in ("reasons", "heading", "signature")] == ["reasons", "heading", "signature"]
    assert all("합성 별지" not in v for k, v in ptext(corpus, pages).items() if k != qual(q["heading"])["locator"])


@pytest.mark.parametrize("kind, section, barred", [("dissent", "opinion", True), ("concurrence", "opinion", True), ("majority_supplement", "opinion", True), ("main", "reasons", False), ("majority", "reasons", False)])
def test_court_judgment_profile_guards_majority_quotes(kind, section, barred):
    assert SR.majority_guard(SR.PROFILES["court_judgment"]) is True
    row = {"quality_json": json.dumps({"opinion_kind": kind, "section_kind": section})}
    assert bool(SR.detc_majority_barred(row, {})) is barred
