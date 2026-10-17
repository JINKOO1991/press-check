"""G-D27(docs/P2_DESIGN.md §11): 대조할 때 ``<br>``·``<br/>``·``<br />`` 는 공백과 같이 지운다(G-D13 확장). 저장 텍스트와 offset 은 원문 그대로다.

합성 텍스트이며 실제 사건이 아니다. 네트워크·장부 없음.
"""
import pytest

import pdr_ing_paths  # noqa: F401
from press_review import contracts
from press_review.ingestion import structure_records as sr
from test_p2_collect_gd import ctx_for


def test_gd27_br_variants_are_ignored_on_the_page_side_and_offsets_stay_in_the_original():
    page = "제2021<br>-813호 사건은<br/>다음과 같이<br />결정되었다."
    ctx = ctx_for({"pg": page})
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "제2021-813호 사건은 다음과 같이 결정되었다."}, ctx)
    assert why == "ok" and loc is not None
    assert (loc["start"], loc["end"]) == (0, len(page))
    assert page[loc["start"]:loc["end"]] == loc["quote"] == page                      # 저장 인용은 원문 그대로(<br> 포함)
    assert contracts._collapse(page[loc["start"]:loc["end"]]) == contracts._collapse(loc["quote"])


def test_gd27_br_in_the_model_quote_is_ignored_too():
    page = "제2021-813호 사건은\n다음과 같이 결정되었다."
    ctx = ctx_for({"pg": page})
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "제2021<br>-813호 사건은<br />다음과 같이<br/>결정되었다."}, ctx)
    assert why == "ok" and loc["quote"] == page


def test_gd27_span_never_starts_or_ends_inside_a_br_tag():
    page = "머리<br>가나다라마바<br>꼬리"
    ctx = ctx_for({"pg": page})
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "가나다라마바"}, ctx)
    assert why == "ok" and page[loc["start"]:loc["end"]] == "가나다라마바"
    loc2, _ = sr.locate_quote({"page_id": "pg", "quote": "머리가나다라마바꼬리"}, ctx)
    assert page[loc2["start"]:loc2["end"]] == page                                    # 바깥 쪽 글자는 br 이 아니라 '머'와 '리'


def test_gd27_only_br_is_normalised_not_other_tags_or_symbols():
    ctx = ctx_for({"pg": "가나다<b>라마</b>바사 <u>아자</u>차카타 파하 가나다라마바"})
    assert sr.locate_quote({"page_id": "pg", "quote": "가나다라마바사"}, ctx)[1] == "quote_not_found"       # <b> 는 지우지 않는다
    assert sr.locate_quote({"page_id": "pg", "quote": "가나다<b>라마</b>바사"}, ctx)[1] == "ok"
    assert sr.locate_quote({"page_id": "pg", "quote": "가나다라마바"}, ctx)[1] == "ok"
    ctx2 = ctx_for({"pg": "가나다<brx>라마바 가나다<br class='x'>라마바"})
    assert sr.locate_quote({"page_id": "pg", "quote": "가나다라마바"}, ctx2)[1] == "quote_not_found"          # 속성이 있거나 br 이 아닌 태그는 그대로


def test_gd27_find_all_counts_matches_across_br_for_v_fid_01():
    pt = sr.PageText("결과 주의사항 확인<br>결과 주의사항 확인 결과<br/>주의사항 확인")
    spans = pt.find_all("결과 주의사항 확인")
    assert len(spans) == 3 and spans[0][0] == 0


def test_gd27_table_cell_with_br_matches_whole_cell_in_kordoc_text():
    md = "| 번호 | 의결번호 | 결과 |\n| --- | --- | --- |\n| 1 | 제2021<br>-813호 | 주의 |\n| 2 | 제2021<br>-814호 | 경고 |"
    pt = sr.PageText(md)
    s, e = pt.find_cell("제2021-813호")
    assert md[s:e] == "제2021<br>-813호"
    s2, e2 = pt.find_cell("경고")
    assert md[s2:e2] == "경고"


def test_gd27_br_at_the_edge_of_a_cell_is_not_part_of_the_stored_span():
    md = "| 번호 | 결과 |\n| --- | --- |\n| 1 | <br>주의<br/> |"
    pt = sr.PageText(md)
    s, e = pt.find_cell("주의")
    assert md[s:e] == "주의"


@pytest.mark.parametrize("source_id", ["iendc_review_results", "pac_board_correction_status"])
def test_gd27_short_cell_rule_still_applies_when_the_model_quote_carries_br(source_id):
    md = "| 번호 | 결과 |\n| --- | --- |\n| 1 | 주의 |"
    ctx = ctx_for({"pg": md}, source_id=source_id)
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "주의<br>"}, ctx)
    assert why == "ok_cell" and md[loc["start"]:loc["end"]] == "주의"
    ctx2 = ctx_for({"pg": md}, source_id="pac_adjustment_casebooks")
    assert sr.locate_quote({"page_id": "pg", "quote": "주의<br>"}, ctx2)[1] == "quote_too_short"


def test_gd27_case_number_split_by_br_is_found_in_source():
    src = "| 1 | 제2021<br>-813호 | 주의 |"
    assert sr.case_number_in_source("제2021-813호", src, sr.compact(src))
    assert not sr.case_number_in_source("제2021-815호", src, sr.compact(src))


def test_gd27_article_number_split_by_br_is_found_in_page():
    pt = sr.PageText("공직선거법 제18<br>조 위반")
    assert sr.citation_in_page("공직선거법 제18조", pt)
    assert not sr.citation_in_page("공직선거법 제19조", pt)


def test_gd27_date_split_by_br_is_found_in_source():
    assert sr.date_in_source("2021-05-04", "결정일 2021.<br>5.<br/>4.")
    assert not sr.date_in_source("2021-05-05", "결정일 2021.<br>5.<br/>4.")
