"""코드 검토 1회 지적 수정(F1~F4) 시험. 모든 fixture 는 합성이다(가짜 번호·'갑'·'A법 제1조'). 실제 결정문·성명은 넣지 않는다."""
import json
import re
import types

import pytest

from press_review import corpus_sqlite as CS
from press_review.ingestion import structure_records as SR
from press_review.ingestion.sources import lx_prec as X
from test_src_lx_prec import Server, detail_html, doc, failures, list_html, one, run_cli, title, _indexed, _passage


# ------------------------------------------------------------------ F1 등록 검증
def test_f1_detail_without_the_requested_number_is_not_registered_even_if_a_list_number_overlaps(corpus):
    srv = Server(lists={"2018다2": list_html([("11", "대법원", title("2018다1, 2018다2"))])}, details={"11": detail_html("2018다1")})
    code, out = run_cli(["--number", "2018다2"], corpus, srv.transport())
    assert failures(corpus)["lx_no:2018다2"]["error_class"] == "serial_mismatch" and doc(corpus, "lx_prec:11")[0] is None
    assert out["result"]["docs_new"] == 0 and out["result"]["serial_mismatch"] == 1


def test_f1_requested_number_alone_missing_from_a_one_number_list_item_is_also_rejected():
    ok = detail_html("2018다1").encode()
    assert X.accept_detail(ok, {}, "2018다1", {"numbers": ["2018다1"]}) is None
    assert X.accept_detail(ok, {}, "2018다2", {"numbers": ["2018다1"]}) == "serial_mismatch"


@pytest.mark.parametrize("detail_kw", [{"court": "수원지방법원"}, {"date": "2018. 11. 1."}], ids=["court", "date"])
def test_f1_court_or_date_that_differs_between_list_and_detail_is_a_failure_and_not_cached(corpus, detail_kw):
    srv = Server(lists={"2018다1": list_html([("11", "서울고등법원", title("2018다1"))])}, details={"11": detail_html("2018다1", **{"court": "서울고등법원", **detail_kw})})
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert failures(corpus)["lx_no:2018다1"]["error_class"] == "serial_mismatch" and doc(corpus, "lx_prec:11")[0] is None
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert srv.detail_calls == ["11", "11"]                                   # 맞지 않는 응답은 캐시하지 않는다


@pytest.mark.parametrize("list_court, body_court", [("서울고등법원", "서울고법"), ("서울중앙지방법원", "서울중앙지법"), ("수원지방법원 성남지원", "수원지법 성남지원"), ("서울고등법원", "서울고등법원")])
def test_f1_court_abbreviations_are_the_same_court(corpus, list_court, body_court):
    srv = Server(lists={"2018다1": list_html([("11", list_court, title("2018다1"))])}, details={"11": detail_html("2018다1", court=body_court)})
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:11")
    assert out["result"]["docs_new"] == 1 and d["status"] == "extracted" and extra["body"]["verify_mismatches"] == [] and failures(corpus) == {}


def test_f1_merged_numbers_in_both_list_and_detail_register_for_each_requested_number(corpus):
    srv = Server(lists={"2018다2": list_html([("11", "대법원", title("2018다1, 2"))])}, details={"11": detail_html("2018다1, 2018다2")})
    code, out = run_cli(["--number", "2018다2"], corpus, srv.transport())
    assert out["result"]["docs_new"] == 1 and doc(corpus, "lx_prec:11")[1]["resolved_numbers"] == ["2018다2"]


def test_f1_two_digit_year_request_matches_a_detail_printed_with_four_digits(corpus):
    srv = Server(lists={"97도158": list_html([("11", "대법원", title("97도158", date="1997. 3. 4."))])}, details={"11": detail_html("1997도158", date="1997. 3. 4.")})
    code, out = run_cli(["--number", "1997도158"], corpus, srv.transport())
    assert out["result"]["docs_new"] == 1 and failures(corpus) == {}


def test_f1_court_name_normalisation():
    assert X.norm_court("서울고법") == X.norm_court("서울 고등법원") == "서울고등법원"
    assert X.norm_court("의정부지법") == "의정부지방법원" and X.norm_court("대법원") == "대법원" and X.norm_court("서울가법") == "서울가정법원" and X.norm_court("서울행법") == "서울행정법원"


# ------------------------------------------------------------------ F3 목록 쪽 종료
def _with_malformed(html):
    return html.replace("</ul>", '<li class="search_result_item" id="x-broken"><div>깨진 항목</div></li></ul>')


def test_f3_an_unparsed_row_does_not_end_the_list_early(corpus):
    page1 = _with_malformed(list_html([(str(100 + i), "대법원", title(f"2019다{i + 50}")) for i in range(99)], total=101))
    page2 = list_html([("999", "대법원", title("2018다999"))], total=101)
    srv = Server(lists={"2018다999": [page1, page2]}, details={"999": detail_html("2018다999")})
    code, out = run_cli(["--number", "2018다999"], corpus, srv.transport())
    assert srv.list_calls == [("2018다999", 1), ("2018다999", 2)] and out["result"]["docs_new"] == 1 and failures(corpus) == {}


def test_f3_incomplete_list_without_a_match_is_a_retryable_failure_not_not_found(corpus):
    page = _with_malformed(list_html([("11", "대법원", title("2019다7"))], total=2))
    srv = Server(lists={"2018다999": page})
    code, out = run_cli(["--number", "2018다999"], corpus, srv.transport())
    f = failures(corpus)["lx_no:2018다999"]
    assert f["error_class"] == "list_format" and out["result"]["not_found"] == 0 and out["result"]["list_format"] == 1
    code, out = run_cli(["--number", "2018다999"], corpus, srv.transport())          # SKIP_CLASSES 가 아니므로 다음 실행에서 다시 요청한다
    assert out["result"]["skipped_known"] == 0 and out["result"]["list_format"] == 1                  # 다시 처리 대상이다(응답은 http 캐시에서 온다)


def test_f3_complete_list_without_a_match_is_still_not_found(corpus):
    srv = Server(lists={"2018다999": list_html([("11", "대법원", title("2019다7"))])})
    run_cli(["--number", "2018다999"], corpus, srv.transport())
    assert failures(corpus)["lx_no:2018다999"]["error_class"] == "not_found"


# ------------------------------------------------------------------ F4 사건번호 비교
def test_f4_two_digit_and_four_digit_years_are_the_same_number_and_merged_notations_split():
    assert CS._prec_numbers("97도158") == CS._prec_numbers("1997도158") == {"1997도158"}
    assert CS._prec_numbers("2018다1 및 2018다2") == {"2018다1", "2018다2"}
    assert CS._prec_numbers("2018다1, 2") == CS._prec_numbers("2018다1·2") == {"2018다1", "2018다2"}
    assert CS._prec_numbers("97도158 및 99도1") == {"1997도158", "1999도1"} and CS._prec_numbers(None) == set()
    from press_review import case_numbers as CN
    assert CN.canon_prec_number("4293형상823") == "4293형상823"                    # 단기 연호는 그대로


def test_f4_same_judgment_in_two_notations_is_indexed_once(monkeypatch):
    rows = [_passage("lx_prec", "x1", "97도158", date="1997-03-04"), _passage("lx_prec", "x2", "2018다1 및 2018다2"),
            _passage("lawgo_prec_supreme", "l1", "1997도158", date="1997-03-04"), _passage("lawgo_prec_supreme", "l2", "2018다2")]
    out, excluded = _indexed(monkeypatch, rows)
    assert sorted(out) == ["l1", "l2"] and excluded == {"case_superseded_by_lawgo": 2}


def test_f4_index_module_does_not_import_ingestion_sources():
    from press_review import case_numbers as CN
    src = open(CS.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(?:from|import)\s+\.?ingestion\.sources", src, re.M)
    assert not re.search(r"^\s*(?:from|import)\s+[.\w]*ingestion\b", open(CN.__file__, encoding="utf-8").read(), re.M)


# ------------------------------------------------------------------ F2 판결요지 소수의견
HEAD = "[다수의견] 합성 다수의견 요지 문장이다. 이어지는 합성 다수 문장이다. [대법관 갑의 반대의견] 합성 반대의견 요지 문장이다. 이어지는 합성 반대 문장이다."
MAJ_Q, DIS_Q = "합성 다수의견 요지 문장이다", "합성 반대의견 요지 문장이다"


def _ctx(profile, text, section="headnote_holding", kind=None):
    meta = {"quality_json": json.dumps({"section_kind": section, "opinion_kind": kind}), "needs_review": 0}
    return SR.WindowCtx(profile=SR.PROFILES[profile], doc_id="d:1", att_id=None, target_key="k", unit="w0000", pages={"p1": SR.PageText(text)},
                        page_meta={"p1": {"page_id": "p1", **meta}}, structurer={"version": "v", "model": "m", "input_sha256": "0" * 64}, source_id="lawgo_prec_supreme")


def _items(ctx, quote):
    st = SR._Stats()
    raw = [{"text": "서술", "speaker": "court_majority", "quote": {"page_id": "p1", "quote": quote}}]
    return SR._items(raw, "legal_reasoning", ctx, st, "judgment", None), st


def test_f2_quote_after_the_first_minority_marker_is_barred_and_one_before_it_is_kept():
    ctx = _ctx("court_judgment", HEAD)
    kept, st = _items(ctx, MAJ_Q)
    assert len(kept) == 1
    kept, st = _items(ctx, DIS_Q)
    assert kept == [] and {"field": "legal_reasoning[0]", "reason": "opinion_guard:headnote_minority_span"} in st.dropped


@pytest.mark.parametrize("marker", ["[대법관 갑, 을의 반대의견]", "[대법관 병의 별개의견]", "[대법관 정의 보충의견]", "[다수의견에 대한 대법관 무의 보충의견]"])
def test_f2_every_minority_marker_form_counts_even_when_it_is_not_at_a_line_head(marker):
    text = f"[1] 합성 요지다. 합성 다수의견 요지 문장이다. {marker} 합성 소수 요지 문장이다."
    ctx = _ctx("court_judgment", text)
    assert len(_items(ctx, MAJ_Q)[0]) == 1 and _items(ctx, "합성 소수 요지 문장이다")[0] == []


def test_f2_majority_marker_alone_is_not_a_minority_marker():
    ctx = _ctx("court_judgment", "[다수의견] 합성 다수의견 요지 문장이다. [다수의견] 합성 둘째 다수 문장이다.")
    assert len(_items(ctx, "합성 둘째 다수 문장이다")[0]) == 1


def test_f2_unmarked_structural_heading_starts_a_minority_span():
    for text, minority, majority in (
            ("합성 다수 요지 문장이다.\n대법관 갑의 반대의견 합성 반대 요지 문장이다.", "합성 반대 요지 문장이다", "합성 다수 요지 문장이다"),
            ("합성 다수 요지 문장이다.\n반대의견: 합성 반대 요지 문장이다.", "합성 반대 요지 문장이다", "합성 다수 요지 문장이다"),
            ("[1] 합성 다수 요지 문장이다.\n[2] 대법관 을의 별개의견 합성 별개 요지다.", "합성 별개 요지다", "합성 다수 요지 문장이다")):
        kept, st = _items(_ctx("court_judgment", text), minority)
        assert kept == [] and {"field": "legal_reasoning[0]", "reason": "opinion_guard:headnote_minority_span"} in st.dropped, text
        assert len(_items(_ctx("court_judgment", text), majority)[0]) == 1, text


def test_f2_descriptive_mention_inside_an_explicit_majority_is_kept():
    text = "[다수의견] 이 판결은 종전 판결의 반대의견이 제시한 기준을 채택하지 않는다. 합성 정상 다수 판시."
    assert len(_items(_ctx("court_judgment", text), "합성 정상 다수 판시")[0]) == 1
    assert len(_items(_ctx("court_judgment", "합성 다수 요지 문장이다. 반대의견은 이유 없다."), "합성 다수 요지 문장이다")[0]) == 1


def test_f2_plain_headnote_and_other_sections_are_not_barred():
    assert len(_items(_ctx("court_judgment", "합성 다수의견 요지 문장이다. 합성 뒤 문장이다."), MAJ_Q)[0]) == 1
    assert len(_items(_ctx("court_judgment", HEAD, section="reasons"), DIS_Q)[0]) == 1          # 판결요지 쪽만 대상이다


def test_f2_constitutional_profile_is_unchanged():
    assert len(_items(_ctx("constitutional_decision", HEAD), DIS_Q)[0]) == 1


def test_f2_article_speaker_reroute_applies_the_same_guard():
    for quote, kept in (("위 기사는 합성 요지 문장이다", 1), ("위 기사는 합성 반대 문장이다", 0)):
        text = "합성 요지 문장이다 위 기사는 합성 요지 문장이다 [대법관 갑의 반대의견] 위 기사는 합성 반대 문장이다"
        ctx = _ctx("court_judgment", text)
        start = text.index(quote)
        it = {"text": "서술", "quote": {"page_id": "p1", "start": start, "end": start + len(quote), "quote": quote}}
        items = {"claims_or_requested_remedy": [], "factual_background": [], "article_claims": [it], "disputed_spans": []}
        SR._fix_article_speaker_items(items, SR._Stats(), "judgment", ctx)
        assert len(items["factual_background"]) == kept


# ------------------------------------------------------------------ 2회차: F1 상세 제목 필수
def _swap_h1(html, new_h1):
    return re.sub(r"<h1[^>]*>.*?</h1>", new_h1, html, flags=re.S)


@pytest.mark.parametrize("h1", ["", "<h1>합성 깨진 제목</h1>", "<h1>대법원 2018. 13. 45. 선고 2018다1 판결</h1>"], ids=["missing", "unparseable", "invalid-date"])
def test_f1r2_missing_or_broken_detail_title_is_a_failure_when_the_list_has_court_and_date(corpus, h1):
    html = _swap_h1(detail_html("2018다1"), h1)
    ok = detail_html("2018다1").encode()
    item = {"numbers": ["2018다1"], "court": "대법원", "date": "2018-10-30"}
    assert X.accept_detail(ok, {}, "2018다1", item) is None
    assert X.accept_detail(html.encode(), {}, "2018다1", item) in ("serial_mismatch", "detail_format")
    srv = Server(lists={"2018다1": list_html([("11", "대법원", title("2018다1"))])}, details={"11": html})
    code, out = run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert doc(corpus, "lx_prec:11")[0] is None and failures(corpus)["lx_no:2018다1"]["error_class"] in ("serial_mismatch", "detail_format")


def test_f1r2_stored_raw_is_checked_by_the_same_function_as_a_fresh_response():
    """저장 raw 로 이어 받는 경로(event)는 원문에 detail_mismatches 를 다시 적용한다. 목록값으로 보충해 등록하지 않는다."""
    item = {"numbers": ["2018다1"], "court": "대법원", "date": "2018-10-30"}
    for h1 in ("", "<h1>합성 깨진 제목</h1>", "<h1>대법원 2018. 13. 45. 선고 2018다1 판결</h1>"):
        assert X.detail_mismatches(_swap_h1(detail_html("2018다1"), h1), "2018다1", item)
    assert X.detail_mismatches(detail_html("2018다1"), "2018다1", item) == []
    tail = _swap_h1(detail_html("2018다1"), "<h1>대법원 2018. 10. 30. 선고 2018다1 판결 : 항소</h1>")           # 실제 제목에는 '판결 : 항소' 꼬리나 잘린 '결'이 있다
    assert X.detail_mismatches(tail, "2018다1", item) == []
    assert X.detail_mismatches(_swap_h1(detail_html("2018다1"), "<h1>대법원 2018. 10. 30. 선고 2018다1 결</h1>"), "2018다1", item) == []
    assert X.detail_mismatches(_swap_h1(detail_html("2018다1"), ""), "2018다1", {"numbers": ["2018다1"]}) == []      # 목록에 법원·날짜가 없으면 요구하지 않는다


# ------------------------------------------------------------------ 2회차: F3 id 없는 행도 센다
def test_f3r2_row_without_an_id_is_counted_and_the_next_page_is_read(corpus):
    page1 = list_html([(str(100 + i), "대법원", title(f"2019다{i + 50}")) for i in range(99)], total=101).replace(
        "</ul>", '<li class="search_result_item"><div>id 없는 행</div></li></ul>')
    assert X.parse_list(page1)["rows"] == 100
    page2 = list_html([("999", "대법원", title("2018다999"))], total=101)
    srv = Server(lists={"2018다999": [page1, page2]}, details={"999": detail_html("2018다999")})
    code, out = run_cli(["--number", "2018다999"], corpus, srv.transport())
    assert srv.list_calls == [("2018다999", 1), ("2018다999", 2)] and out["result"]["docs_new"] == 1


def test_f3r2_short_page_with_a_larger_total_does_not_stop_and_ends_incomplete(corpus):
    page1 = list_html([("11", "대법원", title("2019다7"))], total=5)
    srv = Server(lists={"2018다999": [page1]})
    code, out = run_cli(["--number", "2018다999"], corpus, srv.transport())
    assert srv.list_calls == [("2018다999", 1), ("2018다999", 2)] and failures(corpus)["lx_no:2018다999"]["error_class"] == "list_format"


# ------------------------------------------------------------------ 3회차: F1 완료 문서 경로
def _first_run(corpus, list_title="2018다1", detail_no="2018다1", court="대법원"):
    srv = Server(lists={"2018다1": list_html([("11", court, title(list_title))])}, details={"11": detail_html(detail_no, court=court)})
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    assert doc(corpus, "lx_prec:11")[0]["status"] == "extracted"
    return srv


def test_f1r3_done_document_does_not_absorb_a_number_its_detail_does_not_have(corpus):
    _first_run(corpus)
    srv2 = Server(lists={"2018다2": list_html([("11", "대법원", title("2018다2"))])}, details={})
    code, out = run_cli(["--number", "2018다2"], corpus, srv2.transport())
    d, extra, pages = doc(corpus, "lx_prec:11")
    assert extra["resolved_numbers"] == ["2018다1"] and out["result"]["docs_seen"] == 0
    assert failures(corpus)["lx_no:2018다2"]["error_class"] == "serial_mismatch" and srv2.detail_calls == []      # 저장 raw 로 판정하므로 요청은 없다


def test_f1r3_done_document_merges_a_second_number_that_the_stored_detail_has(corpus):
    srv = Server(lists={"2018다1": list_html([("11", "대법원", title("2018다1, 2"))]), "2018다2": list_html([("11", "대법원", title("2018다1, 2"))])},
                 details={"11": detail_html("2018다1, 2018다2")})
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    code, out = run_cli(["--number", "2018다2"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lx_prec:11")
    assert extra["resolved_numbers"] == ["2018다1", "2018다2"] and out["result"]["docs_seen"] == 1 and failures(corpus) == {}


@pytest.mark.parametrize("court, list_date", [("수원지방법원", "2018. 10. 30."), ("대법원", "2018. 11. 2.")], ids=["court", "date"])
def test_f1r3_done_document_with_a_different_court_or_date_in_the_new_list_item_is_rejected(corpus, court, list_date):
    srv = Server(lists={"2018다1": list_html([("11", "대법원", title("2018다1, 2"))])}, details={"11": detail_html("2018다1, 2018다2")})
    run_cli(["--number", "2018다1"], corpus, srv.transport())
    srv2 = Server(lists={"2018다2": list_html([("11", court, title("2018다1, 2", date=list_date))])})
    code, out = run_cli(["--number", "2018다2"], corpus, srv2.transport())
    assert failures(corpus)["lx_no:2018다2"]["error_class"] == "serial_mismatch" and out["result"]["docs_seen"] == 0
    assert doc(corpus, "lx_prec:11")[1]["resolved_numbers"] == ["2018다1"]


# ------------------------------------------------------------------ 3회차: F2 구간 판정
SEG = ("[1] [다수의견] 첫째 다수 요지 문장이다.\n[대법관 갑의 반대의견] 첫째 반대 요지 문장이다.\n"
       "[2] 독립된 둘째 다수 요지 문장이다.\n[3] [다수의견] 셋째 다수 요지 문장이다. [대법관 을의 별개의견] 셋째 별개 요지 문장이다. [다수의견] 복귀한 넷째 다수 요지 문장이다.")


@pytest.mark.parametrize("quote, kept", [
    ("첫째 다수 요지 문장이다", 1), ("첫째 반대 요지 문장이다", 0), ("독립된 둘째 다수 요지 문장이다", 1), ("셋째 다수 요지 문장이다", 1),
    ("셋째 별개 요지 문장이다", 0), ("복귀한 넷째 다수 요지 문장이다", 1)])
def test_f2r3_minority_span_ends_at_the_next_item_or_majority_marker(quote, kept):
    assert len(_items(_ctx("court_judgment", SEG), quote)[0]) == kept


@pytest.mark.parametrize("quote", ["다수 요지 문장이다.\n[대법관 갑의 반대의견] 첫째 반대", "첫째 반대 요지 문장이다.\n[2] 독립된 둘째", "문장이다. [대법관 을의 별개의견] 셋째 별개", "별개 요지 문장이다. [다수의견] 복귀한"])
def test_f2r3_quote_crossing_a_span_boundary_is_barred(quote):
    kept, st = _items(_ctx("court_judgment", SEG), quote)
    assert kept == [] and any(d["reason"] == "opinion_guard:headnote_minority_span" for d in st.dropped)


def test_f2r3_marker_at_the_very_start_and_unreturned_span_run_to_the_page_end():
    text = "[대법관 갑의 반대의견] 합성 반대 요지 문장이다. 이어지는 합성 반대 문장이다."
    assert _items(_ctx("court_judgment", text), "이어지는 합성 반대 문장이다")[0] == []
    text = "합성 다수 요지 문장이다.\n[2] 합성 다수 둘째 문장이다."
    assert len(_items(_ctx("court_judgment", text), "합성 다수 둘째 문장이다")[0]) == 1          # 소수의견 표지가 없으면 [n] 은 아무 영향이 없다


# ------------------------------------------------------------------ 3회차: F3 목록 캐시
def _bad_list():
    return Server(lists={"2018다5": _with_malformed(list_html([("12", "대법원", title("2019다7"))], total=2))})


def _good_server():
    return Server(lists={"2018다5": list_html([("21", "대법원", title("2018다5"))])}, details={"21": detail_html("2018다5")})


def test_f3r3_default_rerun_recovers_after_the_server_returns_a_complete_list(corpus):
    bad = _bad_list()
    code, out = run_cli(["--number", "2018다5"], corpus, bad.transport())
    assert failures(corpus)["lx_no:2018다5"]["error_class"] == "list_format"
    good = _good_server()
    code, out = run_cli(["--number", "2018다5"], corpus, good.transport())          # 다른 transport 지만 http 캐시는 corpus 에 있다
    assert good.list_calls == [("2018다5", 1)] and out["result"]["docs_new"] == 1 and failures(corpus) == {}


def test_f3r3_explicit_refresh_recovers_after_the_retries_are_exhausted(corpus):
    bad = _bad_list()
    for _ in range(3):
        run_cli(["--number", "2018다5"], corpus, bad.transport())
    good = _good_server()
    code, out = run_cli(["--number", "2018다5"], corpus, good.transport())
    assert out["result"]["skipped_exhausted"] == 1 and good.list_calls == []
    code, out = run_cli(["--number", "2018다5", "--refresh"], corpus, good.transport())
    assert out["result"]["skipped_exhausted"] == 0 and out["result"]["docs_new"] == 1 and failures(corpus) == {}
