"""헌재결정례 본문 파서(detc_parse): 네트워크 없는 순수 함수 시험. 모든 fixture 는 합성이다(가짜 번호·'갑 재판관'·'A법 제1조').
실제 결정문의 문장·당사자 성명은 넣지 않는다(공개 저장소, 재배포는 permission_required)."""
import pytest

from press_review.ingestion.sources import detc_parse as D
from press_review.ingestion.sources.lawapi import BodyParseError


def detc_xml(serial="900001", **over):
    f = {"헌재결정례일련번호": serial, "종국일자": "20061231", "사건번호": "2001헌마10", "사건명": "A법 제1조 위헌확인",
         "사건종류코드": "430201", "사건종류명": "헌마", "재판부구분코드": "430301", "판시사항": "가 쟁점", "결정요지": "나 요지",
         "전문": "【주 문】\n기각한다.", "참조조문": "A법 제1조", "참조판례": "", "심판대상조문": ""}
    f.update(over)
    return "<DetcService>" + "".join(f"<{k}>{v}</{k}>" for k, v in f.items()) + "</DetcService>"


def case(reasons_tail="", order="1. A법 제1조는 헌법에 위반된다."):
    return f"【당 사 자】\n청구인 갑\n2001헌마10\n【주　　문】\n{order}\n【이  유】\n1. 사건\n다수 판단이다.\n{reasons_tail}"


# ---------------------------------------------------------------- parse_detc_xml
def test_parse_field_mapping_and_empty_fields():
    p = D.parse_detc_xml(detc_xml(), expected_serial="900001")
    assert (p["serial"], p["body_case_no"], p["case_nos"], p["title"]) == ("900001", "2001헌마10", ["2001헌마10"], "A법 제1조 위헌확인")
    assert p["decision_date"] == "2006-12-31"
    assert (p["headnote_issue"], p["headnote_holding"], p["ref_statutes"], p["full_text"]) == ("가 쟁점", "나 요지", "A법 제1조", "【주 문】\n기각한다.")
    assert p["ref_cases"] is None and p["review_target"] is None            # 빈 필드는 None
    assert p["extra"] == {"case_kind_name": "헌마", "case_kind_code": "430201", "bench_code": "430301"}
    assert "link" not in str(p.keys()).lower() and p["warnings"] == []


@pytest.mark.parametrize("raw,iso", [("20061231", "2006-12-31"), ("2006.12.31", "2006-12-31"), ("2006.1.5.", "2006-01-05")])
def test_parse_date_forms(raw, iso):
    assert D.parse_detc_xml(detc_xml(종국일자=raw))["decision_date"] == iso


def test_parse_zero_and_empty_are_none_with_warning():
    p = D.parse_detc_xml(detc_xml(종국일자="0", 판시사항="0", 전문=""))
    assert p["decision_date"] is None and p["headnote_issue"] is None and p["full_text"] is None
    assert {"decision_date_missing", "zero_value:판시사항", "full_text_missing"} <= set(p["warnings"])
    assert D.parse_detc_xml(detc_xml(종국일자="없음"))["warnings"] == ["decision_date_unparsed"]


@pytest.mark.parametrize("body,kind", [
    ("<Error>일치하는 헌재결정례가 없습니다.</Error>", "no_match"),
    ("<Error>권한이 없습니다. OC 필요</Error>", "denied"),
    ("<html><body>x</body></html>", "denied"),
    ("not xml", "malformed"),
    ("<DetcService><a></DetcService>", "malformed"),
    ('<!DOCTYPE x [<!ENTITY a "b">]><DetcService><사건번호>&a;</사건번호></DetcService>', "malformed"),
])
def test_parse_error_kinds(body, kind):
    with pytest.raises(BodyParseError) as e:
        D.parse_detc_xml(body)
    assert e.value.kind == kind


def test_parse_serial_mismatch_and_accept():
    with pytest.raises(BodyParseError) as e:
        D.parse_detc_xml(detc_xml(serial="900002"), expected_serial="900001")
    assert e.value.kind == "serial_mismatch"
    assert D.accept_detc_body(detc_xml().encode(), {}, "900001") is None
    assert D.accept_detc_body(detc_xml().encode(), {}, "1").startswith("serial_mismatch")


# ---------------------------------------------------------------- split_detc
@pytest.mark.parametrize("party,order,reasons", [("【당 사 자】", "【주 문】", "【이 유】"), ("【당　사　자】", "【주　　문】", "【이　　유】"),
                                                   ("【당사자】", "【주  문】", "【이  유】")])
def test_split_marker_whitespace_variants(party, order, reasons):
    text = f"{party}\n청구인 갑\n{order}\n기각한다.\n{reasons}\n판단이다.\n"
    secs, info = D.split_detc(text)
    assert [s.kind for s in secs] == ["party", "order", "reasons"]
    assert secs[0].no_page is True and not secs[1].no_page and not secs[2].no_page
    assert "".join(s.text for s in secs) == text and info["status"] == "none"


@pytest.mark.parametrize("heading,kind,vote", [
    ("재판관 갑의 법정의견에 대한 보충의견", "majority_supplement", None),
    ("재판관 갑의 다수의견에 대한 보충의견", "majority_supplement", None),
    ("재판관 갑의 한정위헌의견", "vote_opinion", "한정위헌"),
    ("재판관 을의 반대의견에 대한 재판관 병의 보충의견", "dissent_supplement", None),
    ("재판관 갑의 기각의견에 대한 보충의견", "supplement", None),
    ("재판관 갑, 을의 반대의견(위헌)", "dissent", "위헌"),
    ("재판관 갑의 별개의견", "concurrence", None),
])
def test_opinion_heading_kinds(heading, kind, vote):
    text = case(f"6. {heading}\n의견 본문이다.\n")
    secs, info = D.split_detc(text)
    ops = [s for s in secs if s.kind == "opinion"]
    assert info["status"] == "split" and len(ops) == 1
    assert (ops[0].opinion_kind, ops[0].vote) == (kind, vote)
    assert "".join(s.text for s in secs) == text
    assert [s.opinion_kind for s in secs if s.kind == "reasons"] == ["majority"]


def test_vote_opinion_without_label_word():
    secs, _ = D.split_detc(case("(가) 재판관 갑, 을의 의견(헌법불합치)\n본문이다.\n"))
    op = [s for s in secs if s.kind == "opinion"][0]
    assert (op.opinion_kind, op.vote) == ("vote_opinion", "헌법불합치")


def test_heading_variants_midline_nested_and_suffix():
    tail = ("6. 적법요건에 대한 반대의견가. 재판관 갑의 반대의견(1) 서 언 본문이다.\n"       # 문장 중간 표제
            "7. 본안\n사. (1) 재판관 을의 반대의견(합헌)\n요지다.\n"                          # 중첩 번호 머리 + 끝 표기
            "(4) 제3항의 위헌 여부(가) 재판관 병, 정의 의견(위헌)\n본문이다.\n")
    secs, info = D.split_detc(case(tail))
    ops = [(s.opinion_kind, s.vote) for s in secs if s.kind == "opinion"]
    assert info["status"] == "split" and ops == [("dissent", None), ("dissent", "합헌"), ("vote_opinion", "위헌")]


def test_statements_are_not_headings():
    tail = ("이 결정에는 재판관 갑의 다음 6.과 같은 반대의견이 있는 외에는 일치하였다.\n"
            "이 부분은 뒤의 ‘7. 가.’와 같은 재판관 갑의 반대의견(위헌)이 있는 외에는 일치한 것이다.\n"
            "이 부분은 재판관 전원의 의견일치에 따른 것이다.\n")
    secs, info = D.split_detc(case(tail))
    assert info["marker_count"] == 0 and not [s for s in secs if s.kind == "opinion"]
    assert info["status"] == "unsplit"                                      # 언급은 있으나 표제 0개
    assert D.split_detc(case("이 부분은 재판관 전원의 의견일치에 따른 것이다.\n"))[1]["status"] == "none"


def test_needs_review_on_count_mismatch():
    # 번호 없는 문장 시작 표제(느슨 검출만 잡음) + 번호 있는 표제 1개
    secs, info = D.split_detc(case("다수 판단이다. 재판관 갑의 반대의견\n본문이다.\n6. 재판관 을의 별개의견\n본문이다.\n"))
    assert info["status"] == "needs_review" and info["marker_count"] == 1 and info["loose_count"] == 2
    # 결론 문장의 번호 수(6, 7)보다 적게 잡음
    secs, info = D.split_detc(case("이 결정에는 아래 6.과 같은 반대의견과 아래 7.과 같은 별개의견이 있다.\n6. 재판관 갑의 반대의견\n본문이다.\n"))
    assert info["status"] == "needs_review" and info["conclusion_numbers"] == [6, 7]


def test_unsplit_when_mention_without_heading():
    _, info = D.split_detc(case("다수 판단이다. 재판관 갑의 반대의견\n본문이다.\n"))
    assert info["status"] == "unsplit"


def test_split_roundtrip_and_no_page_party():
    text = case("6. 재판관 갑의 반대의견\n본문이다.\n【별지】\n목록\n")
    secs, _ = D.split_detc(text)
    assert "".join(s.text for s in secs) == text
    assert [s.kind for s in secs] == ["party", "order", "reasons", "opinion", "heading"]
    assert secs[-1].opinion_kind == "dissent"                                # 그 밖의 표지는 앞 구간 종류를 잇는다
    assert [s.no_page for s in secs] == [True, False, False, False, False]
    assert D.split_detc("표지 없는 본문")[0][0].kind == "preamble"


# ---------------------------------------------------------------- split_head_opinions
def test_head_opinions_split_and_unsplit():
    secs, info = D.split_head_opinions("다수 요지이다.\n(1) 재판관 갑의 반대의견\n반대 요지이다.\n(2) 재판관 을의 보충의견\n보충 요지이다.")
    assert info == {"headnote_split": "split", "marker_count": 2}
    assert [(s.kind, s.opinion_kind) for s in secs] == [("holding", "majority"), ("opinion", "dissent"), ("opinion", "supplement")]
    assert D.split_head_opinions("요지이다. 재판관 갑의 반대의견이 있다.")[1]["headnote_split"] == "unsplit"
    assert D.split_head_opinions("의견 언급 없는 요지")[1]["headnote_split"] == "none"
    assert D.split_head_opinions(None) == ([], {"headnote_split": "none", "marker_count": 0})


# ---------------------------------------------------------------- classify_orders
@pytest.mark.parametrize("order,expected", [
    ("1. A법 제1조는 헌법에 위반된다고 할 수 없다.", ["위헌불선언"]),
    ("A법 제1조는 헌법에 위반된다고 선고할 수 없다.", ["위헌불선언"]),
    ("1. A법 제1조는 헌법에 합치되지 아니한다.", ["헌법불합치"]),
    ("1. A법 제1조를 갑으로 해석하는 한 헌법에 위반된다.", ["한정위헌"]),
    ("1. A법 제1조를 갑으로 해석하는 한 헌법에 위반되지 아니한다.", ["한정합헌"]),
    ("1. A법 제1조는 헌법에 위반되지 아니한다.", ["합헌"]),
    ("1. A법 제1조는 헌법에 위반된다.", ["위헌"]),
    ("1. 피청구인의 처분을 취소한다.", ["인용"]),
    ("1. 갑의 청구는 위헌임을 확인한다. 2. 나머지 청구를 각하한다.", ["인용", "각하"]),
    ("1. A법 제1조는 헌법에 합치되지 아니한다. 2. 나머지 청구를 기각한다. 3. 이 사건 심판절차는 종료되었다.", ["헌법불합치", "기각", "심판절차종료"]),
    ("1. 달리 정한다.", ["unknown"]),
    ("【주 문】\n청구를 기각한다.", ["기각"]),
    ("", []),
])
def test_classify_orders(order, expected):
    assert D.classify_orders(order) == expected


# ---------------------------------------------------------------- 병합 번호·머리 쪽
def test_extract_merged_numbers_party_section_only():
    text = ("【당 사 자】\n청구인 갑\n2001헌마10, 2001 헌마 11 2002헌가3\n2001헌마10\n【주 문】\n기각한다.\n【이 유】\n선례 2009헌바9 를 본다.\n")
    assert D.extract_merged_numbers(text) == ["2001헌마10", "2001헌마11", "2002헌가3"]          # 이유의 2009헌바9 제외, 중복 제거
    assert D.extract_merged_numbers("판시사항 머리 2005헌마165 (병합)\n【주 문】\n기각") == []        # 사건 라벨 줄·번호만 나열한 줄이 아니면 병합 근거가 아니다
    assert D.extract_merged_numbers("【당 사 자】\n2001헌마10") == []                          # 주문 표지가 없으면 구간 불명
    assert D.extract_merged_numbers(case()) == ["2001헌마10"]


def test_build_head_page():
    assert D.build_head_page("2001헌마10", "2006-01-05") == "사건번호: 2001헌마10\n종국일자: 2006. 1. 5.\n기관: 헌법재판소"
    assert D.build_head_page("2001헌마10", None) == "사건번호: 2001헌마10\n기관: 헌법재판소"
    assert D.build_head_page(None, None) == ""


# ---------------------------------------------------------------- 구형 표지 [당사자]/[주 문]/[이 유] (1990년대 결정, 합성)
def legacy(party="[당사자]", order="[주 문]", reasons="[이 유]", tail="", order_text="1. 청구인의 심판청구를 기각한다."):
    return f"{party}\n청구인 갑\n2001헌마10\n{order}\n{order_text}\n{reasons}\n가. 사건의 개요\n다수 판단이다.\n{tail}"


@pytest.mark.parametrize("party,order,reasons", [("[당사자]", "[주 문]", "[이 유]"), ("[당 사 자]", "[주문]", "[이유]"),
                                                   ("［당사자］", "[주\u3000\u3000문]", " [이  유]")])
def test_legacy_bracket_markers_recognized(party, order, reasons):
    text = legacy(party, order, reasons)
    secs, info = D.split_detc(text)
    assert [s.kind for s in secs] == ["party", "order", "reasons"] and secs[0].no_page is True
    assert "".join(s.text for s in secs) == text and info["status"] == "none"
    assert D.extract_merged_numbers(text) == ["2001헌마10"]
    assert D.classify_orders(secs[1].text) == ["기각"]


def test_legacy_markers_only_at_line_start_and_only_three_names():
    # 줄 머리가 아닌 [주 문], 다른 이름의 대괄호, 조문 인용 [1] 은 표지가 아니다
    text = "머리 글 [주 문] 문자열이다.\n[별지]\n[1] 조문 인용이다.\n[이 유]\n다수 판단이다.\n"
    secs, _ = D.split_detc(text)
    assert [s.kind for s in secs] == ["preamble", "reasons"] and secs[1].text.startswith("[이 유]")
    assert D.extract_merged_numbers("[당사자]\n2001헌마10\n본문 [주 문] 이다.\n") == []              # 줄 머리 [주 문] 이 없으면 구간 불명
    assert D.extract_merged_numbers(legacy()) == ["2001헌마10"]


def test_legacy_signature_and_title_lines_are_not_headings():
    tail = "재판관 갑(재판장) 을 병 정 무\n"
    secs, info = D.split_detc(legacy(tail=tail + "3. 판단\n4. 결론\n"))
    assert info["status"] == "none" and info["marker_count"] == 0 and [s.kind for s in secs] == ["party", "order", "reasons"]
    secs, info = D.split_detc(legacy(tail="1. 재판관 갑의 반대의견\n반대 이유이다.\n" + tail))
    assert info["status"] == "split" and [s.opinion_kind for s in secs if s.kind == "opinion"] == ["dissent"]


@pytest.mark.parametrize("order_text,expected", [
    ("1. 청구인의 심판청구를 기각한다.", ["기각"]),
    ("A법 제1조는 헌법에 위반된다.", ["위헌"]),
    ("1. 피청구인의 처분은 위헌임을 확인한다. 2. 나머지 청구를 각하한다.", ["인용", "각하"]),
    ("이 사건 심판청구를 각하한다.", ["각하"]),
])
def test_legacy_order_sentences(order_text, expected):
    secs, _ = D.split_detc(legacy(order_text=order_text))
    assert D.classify_orders(next(s.text for s in secs if s.kind == "order")) == expected


def test_modern_markers_unchanged_by_legacy_support():
    text = case("6. 재판관 갑의 반대의견\n본문이다.\n")
    assert [s.kind for s in D.split_detc(text)[0]] == ["party", "order", "reasons", "opinion"]


# ---------------------------------------------------------------- 독립 검토 1회차 후속(병합 번호·줄바꿈 표제·복합 입장·주문 날짜)
def test_merged_numbers_exclude_cited_decisions():
    modern = ("【당 사 자】\n사 건 2020헌아1 재심청구\n재심대상결정 헌법재판소 2018. 5. 1. 선고 2018헌마1 결정\n종전 결정 2017헌바2\n(2020헌아3)\n"
              "【주 문】\n각하한다.\n")
    assert D.extract_merged_numbers(modern) == ["2020헌아1", "2020헌아3"]
    one_line = "【당 사 자】\n사건 2020헌아1 / 재심대상결정 헌법재판소 2018. 5. 1. 선고 2018헌마1 결정\n【주 문】\n각하한다.\n"
    assert D.extract_merged_numbers(one_line) == ["2020헌아1"]
    old = "[당사자]\n사건 2020헌아1\n재심대상결정 2018. 5. 1. 선고 2018헌마1 결정\n관련사건 2019헌바7\n[주 문]\n각하한다.\n"
    assert D.extract_merged_numbers(old) == ["2020헌아1"]
    labeled = "【당 사 자】\n청구인 갑 (2020헌마3)\n청구인 을 (이전 청구 2019헌마5)\n2020헌마1, 2020헌마2 (병합)\n【주 문】\n기각한다.\n"
    assert D.extract_merged_numbers(labeled) == ["2020헌마3", "2020헌마1", "2020헌마2"]      # 사건 라벨 줄·번호만 나열한 줄·번호만 든 괄호 주석만 인정한다


def test_heading_wrapped_across_lines():
    secs, info = D.split_detc(case("6. 재판관 갑, 을의\n반대의견\n반대 이유 문장이다.\n"))
    ops = [s for s in secs if s.kind == "opinion"]
    assert info["status"] == "split" and len(ops) == 1 and ops[0].opinion_kind == "dissent"
    assert [s.opinion_kind for s in secs if s.kind == "reasons"] == ["majority"]
    # 줄바꿈이 둘 이상이라 표제로 잡지 못해도 검토 대상으로 수렴한다
    _, info2 = D.split_detc(case("6. 재판관 갑, 을의\n\n반대의견\n반대 이유 문장이다.\n"))
    assert info2["status"] in ("unsplit", "needs_review")
    assert D.split_detc(case("이 부분은 재판관 전원의\n의견일치에 따른 것이다.\n"))[1]["status"] == "none"


@pytest.mark.parametrize("heading,vote", [
    ("재판관 갑의 합헌 및 위헌의견", None),
    ("재판관 갑의 각하 및 합헌의견", None),
    ("재판관 갑의 위헌 및 헌법불합치의견", "헌법불합치"),
    ("재판관 갑의 한정위헌 및 위헌의견", "위헌"),
    ("재판관 갑의 위헌의견", "위헌"),
])
def test_compound_vote_opinion(heading, vote):
    assert D.classify_detc_opinion(heading) == ("vote_opinion", vote)


@pytest.mark.parametrize("order,expected", [
    ("1. A법(2000. 1. 2. 법률 제1호)은 헌법에 위반된다. 2. 나머지 청구를 각하한다.", ["위헌", "각하"]),
    ("1. A법(2000. 1. 2. 법률 제1호, 2003. 3. 4. 개정)은 헌법에 합치되지 아니한다.\n2. 나머지 청구를 기각한다.", ["헌법불합치", "기각"]),
    ("1. A법 제1조(제2조 2. 항 참조)는 헌법에 위반되지 아니한다. 2. 청구를 각하한다.", ["합헌", "각하"]),
    ("1. 청구인의 심판청구를 기각한다.\n2. 심판청구 중 일부를 각하한다.", ["기각", "각하"]),
])
def test_classify_orders_masks_dates_and_parentheses(order, expected):
    assert D.classify_orders(order) == expected


# ---------------------------------------------------------------- 독립 검토 2회차 후속(회귀 4건)
def test_merged_numbers_keep_party_names_that_start_with_retrial():
    text = "【당 사 자】\n재심청구인 갑 (2020헌아1)\n사건 2020헌아2\n재심대상 사건 2018헌마1\n【주 문】\n각하한다.\n"
    assert D.extract_merged_numbers(text) == ["2020헌아1", "2020헌아2"]               # 재심대상 인용만 잘라 낸다


def test_wrapped_narrative_is_not_a_heading():
    tail = "재판관 갑은\n반대의견과 같은 의견.\n법정의견 본문이다.\n"
    secs, info = D.split_detc(case(tail))
    assert not [s for s in secs if s.kind == "opinion"] and info["marker_count"] == 0
    assert info["status"] in ("unsplit", "needs_review", "none")                       # 표제가 아니므로 법정의견이 dissent 로 재표기되지 않는다
    assert [s.opinion_kind for s in secs if s.kind == "reasons"] == ["main"]
    quoted = "이 부분은 재판관 갑의\n‘반대의견’ 의견이다.\n본문이다.\n"
    assert not [s for s in D.split_detc(case(quoted))[0] if s.kind == "opinion"]
    ok = D.split_detc(case("6. 재판관 갑, 을의\n반대의견\n반대 이유 문장이다.\n"))                 # 정상 줄바꿈 표제는 유지
    assert [s.opinion_kind for s in ok[0] if s.kind == "opinion"] == ["dissent"] and ok[1]["status"] == "split"


@pytest.mark.parametrize("heading,vote", [
    ("재판관 갑의 위헌이 아닌 합헌의견", "합헌"),
    ("재판관 갑의 합헌이 아니라 위헌의견", "위헌"),
    ("재판관 갑의 합헌 및 위헌의견", None),
    ("재판관 갑, 을의 각하·합헌의견", None),
    ("재판관 갑과 을의 위헌의견", "위헌"),
    ("재판관 갑의 위헌 및 헌법불합치의견", "헌법불합치"),
])
def test_vote_negation_and_parallel_stances(heading, vote):
    assert D.classify_detc_opinion(heading) == ("vote_opinion", vote)


def test_classify_orders_indented_item_heads():
    assert D.classify_orders("1. A법 제1조는 헌법에 위반된다.\n  2. 나머지 청구를 각하한다.") == ["위헌", "각하"]
    assert D.classify_orders("1. A법 제1조는 헌법에 위반된다.\n\u3000\u30002. 나머지 청구를 각하한다.") == ["위헌", "각하"]
    assert D.classify_orders("1. A법(2000. 1. 2. 법률 제1호)은 헌법에 위반된다.\n  2. 나머지 청구를 각하한다.") == ["위헌", "각하"]


# ---------------------------------------------------------------- 독립 검토 3회차 후속(인용 절단어·따옴표 표제·입장어 병렬)
@pytest.mark.parametrize("cite", ["종전 결정", "종전의 결정", "이전의 결정", "기존 결정례", "선행 사건", "관련 사건", "재심대상 결정", "재심대상결정",
                                  "앞선 판례", "종전인 심판"])
def test_cited_decision_variants_are_not_merged(cite):
    text = f"【당 사 자】\n사건 2020헌아1 / {cite} 2018헌마1\n【주 문】\n각하한다.\n"
    assert D.extract_merged_numbers(text) == ["2020헌아1"]


def test_party_lines_that_merely_start_with_citation_words_are_kept():
    text = ("【당 사 자】\n재심청구인 갑 (2020헌아1)\n사건 2020헌아2\n이전 청구인 을 (2020헌아4)\n관련 청구인 병 (2020헌아5)\n"
            "2020헌아6, 2020헌아7 (병합)\n【주 문】\n각하한다.\n")
    assert D.extract_merged_numbers(text) == ["2020헌아1", "2020헌아2", "2020헌아4", "2020헌아5", "2020헌아6", "2020헌아7"]


@pytest.mark.parametrize("quoted", [
    "재판관 갑은 ‘반대의견’을 인용했다.", "재판관 갑은 “반대의견”을 인용했다.", "재판관 갑은 「반대의견」을 인용했다.",
    "재판관 갑은 『반대의견』을 인용했다.", "재판관 갑은 〈반대의견〉을 인용했다.", "재판관 갑은 \"반대의견\"을 인용했다.",
    "재판관 갑은\n‘반대의견’을 인용했다.", "‘재판관 갑의 반대의견’이라 한다.", "그는 “다음 6. 재판관 갑의 반대의견”이라고 썼다.",
])
def test_quoted_opinion_words_are_not_headings(quoted):
    secs, info = D.split_detc(case(quoted + "\n법정의견 본문이다.\n"))
    assert not [s for s in secs if s.kind == "opinion"] and info["status"] in ("none", "unsplit")      # 언급 플래그(unsplit)는 안전한 쪽
    assert [s.opinion_kind for s in secs if s.kind == "reasons"] == ["main"]


@pytest.mark.parametrize("tail,kinds", [
    ("6. 재판관 갑의 반대의견\n본문이다.\n", [("dissent", None)]),                                                      # 줄 머리
    ("6. 적법요건에 대한 반대의견가. 재판관 갑의 반대의견(1) 서 언 본문이다.\n", [("dissent", None)]),                    # 문장 중간
    ("사. (1) 재판관 을의 반대의견(합헌)\n요지다.\n", [("dissent", "합헌")]),                                           # 중첩 번호 머리 + 꼬리
    ("6. 법 제2조 중 ‘정의’ 부분에 대한 재판관 갑의 반대의견(합헌)\n본문이다.\n", [("dissent", "합헌")]),                # 앞부분의 닫힌 따옴표 쌍
    ("‘인용문\n6. 재판관 갑의 반대의견\n본문이다.\n", [("dissent", None)]),                                            # 앞 줄의 따옴표는 이어지지 않는다
])
def test_real_headings_survive_quote_filter(tail, kinds):
    secs, info = D.split_detc(case(tail))
    assert [(s.opinion_kind, s.vote) for s in secs if s.kind == "opinion"] == kinds and info["status"] == "split"


@pytest.mark.parametrize("heading,vote", [
    ("재판관 갑의 합헌 및 위헌의견", None),
    ("재판관 갑의 합헌과 위헌의견", None),
    ("재판관 갑의 합헌 또는 위헌의견", None),
    ("재판관 갑의 합헌, 위헌 및 각하의견", None),
    ("재판관 갑의 위헌, 헌법불합치의견", "헌법불합치"),
    ("재판관 갑의 위헌 및 한정위헌 및 헌법불합치의견", "헌법불합치"),
    ("재판관 갑의 위헌이라는 결과에 반대하는 합헌의견", "합헌"),
    ("재판관 갑의 위헌이 아닌 합헌의견", "합헌"),
    ("재판관 갑의 합헌이 아니라 위헌의견", "위헌"),
    ("재판관 갑의 합헌의견", "합헌"),
])
def test_stance_is_the_word_before_opinion_and_only_its_parallel_chain(heading, vote):
    assert D.classify_detc_opinion(heading) == ("vote_opinion", vote)


# ---------------------------------------------------------------- 한 항목 안의 복수 처분(S2 파일럿 결함)
@pytest.mark.parametrize("order,expected", [
    ("이 사건 심판청구 중 갑 부분은 각하하고, 나머지 심판청구는 기각한다.", ["각하", "기각"]),                       # 재현
    ("이 사건 심판청구 중 갑 부분은 각하하며 나머지 심판청구는 기각한다.", ["각하", "기각"]),
    ("갑 부분은 각하, 나머지 청구는 기각한다.", ["각하", "기각"]),
    ("일부는 각하 및 기각한다.", ["각하", "기각"]),
    ("갑 부분은 각하한다. 나머지 청구는 기각한다.", ["각하", "기각"]),                                            # 접속어 없는 두 문장
    ("나머지 청구는 기각하고, 갑 부분은 각하한다.", ["기각", "각하"]),                                            # '기각하고' 는 각하가 아니다
    ("A법 제1조는 헌법에 위반되고, 나머지 청구는 각하한다.", ["위헌", "각하"]),                                    # 한 문장에 위헌과 각하
    ("1. A법 제1조는 헌법에 위반된다. 2. 나머지 청구를 각하한다.", ["위헌", "각하"]),                              # 기존 다항 주문 불변
    ("1. A법 제1조를 갑으로 해석하는 한, 헌법에 위반된다.", ["한정위헌"]),                                         # 쉼표가 낀 한정위헌 단일 문장
    ("1. A법 제1조는 헌법에 합치되지 아니한다. 위 조항은 2026. 12. 31.까지 계속 적용한다.", ["헌법불합치"]),            # 계속 적용 문장은 처분이 아니다
    ("1. A법(2000. 1. 2. 법률 제1호, 각하하고, 기각한다)은 헌법에 위반된다.", ["위헌"]),                          # 괄호 속 접속어는 가린다
    ("청구인 갑 및 을의 심판청구를 각하한다.", ["각하"]),                                                         # 주체 나열의 '및'
])
def test_classify_orders_multiple_dispositions_in_one_item(order, expected):
    assert D.classify_orders(order) == expected
