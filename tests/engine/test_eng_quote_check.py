"""U8: explanations 의 인용부호 구간이 기사 또는 근거 passage 원문의 부분문자열(공백 축약)이 아니면 서버가 경고한다(제출은 막지 않는다).

설계: private_docs/search_design_20261106/design.md 정정 절 U8. 범위는 보고서 수준(그 보고서 근거 passage 전체 + 기사 전문).
"""
import copy
import datetime as dt

import pytest

from press_review import review
from press_review.providers import MockProvider

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
TAG = "[인용대조]"
ART = "경찰은 A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다"            # p1 앞부분
ART2 = "혐의와 관련된 금액이나 구체적인 범행 경위는 아직 확인되지 않았다"
PASS = "시험용 자리표시 문구다"                                              # law-prec-194652/summary-2 본문 일부
PASS2 = "이 판결의 실제 판시 내용을 요약한 것이 아니다"


def run(article, corpus, explanation, field="support"):
    f = copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])
    res = review.build_report_ex(article, [f], {"finding-001": {field: explanation}}, corpus, now=NOW)
    assert res.report is not None, res.errors                    # 경고는 제출을 막지 않는다
    return [w for w in res.warnings if TAG in w]


FABRICATED = [
    pytest.param('대법원은 "원고의 청구는 이유 없다고 판시하였다"고 했다', id="straight-double"),
    pytest.param("대법원은 “원고의 청구는 이유 없다고 판시하였다”고 했다", id="curly-double"),
    pytest.param("대법원은 ＂원고의 청구는 이유 없다고 판시하였다＂고 했다", id="fullwidth-double"),
    pytest.param("대법원은 ＇원고의 청구는 이유 없다고 판시하였다＇고 했다", id="fullwidth-single"),
    pytest.param('대법원은 “원고의 청구는 이유 없다고 판시하였다"고 했다', id="mismatched-curly-open"),
    pytest.param('대법원은 "원고의 청구는 이유 없다고 판시하였다”고 했다', id="mismatched-curly-close"),
    pytest.param("대법원은 ‘원고의 청구는 이유 없다고 판시하였다\'고 했다", id="mismatched-single"),
    pytest.param("대법원은 「원고의 청구는 이유 없다고 판시하였다」고 했다", id="corner-bracket"),
    pytest.param("대법원은 『원고의 청구는 이유 없다고 판시하였다』고 했다", id="double-corner-bracket"),
    pytest.param("대법원은 ‘원고의 청구는 이유 없다고 판시하였다’고 했다", id="curly-single"),
    pytest.param("대법원은 '원고의 청구는 이유 없다고 판시하였다'고 했다", id="straight-single"),
    pytest.param("기사는 “" + ART + "고 보도했다” 라고 했다", id="article-quote-altered"),
    pytest.param("판결은 “" + PASS + " 그러나 이 사건은 다르다고 한다”", id="passage-quote-altered"),
]


@pytest.mark.parametrize("text", FABRICATED)
def test_quote_not_in_article_or_passage_warns(article, corpus, text):
    w = run(article, corpus, text)
    assert len(w) == 1 and "explanations[finding-001].support" in w[0]


CLEAN = [
    pytest.param("연결된 passage는 시험용 자리표시이며 실제 판결의 내용이 아니다.", id="no-quote-marks"),
    pytest.param("기사는 “" + ART + "”고 썼다", id="article-quote-curly"),
    pytest.param('기사는 "' + ART2 + '"고 썼다', id="article-quote-straight"),
    pytest.param("기사는 「" + ART + "」고 썼다", id="article-quote-corner"),
    pytest.param('기사는 "경찰은  A사 대표\n김씨의 횡령   혐의를 수사하고 있다고 밝혔다"고 썼다', id="whitespace-and-newline-differences"),
    pytest.param("기사는 “경찰은　A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다”고 썼다", id="fullwidth-space"),
    pytest.param("passage는 “" + PASS + "”라고 한다", id="passage-quote"),
    pytest.param('passage는 "시험용   자리표시\n문구다"라고 한다', id="passage-quote-whitespace-differences"),
    pytest.param("passage는 「" + PASS2 + "」라고 한다", id="passage-quote-corner"),
    pytest.param("기사는 “경찰은 A사 대표 김씨의 횡령 혐의를 … 아직 확인되지 않았다”고 썼다", id="ellipsis-joins-real-fragments"),
    pytest.param("기사는 “" + ART + ".”고 썼다", id="trailing-period-added"),
]


@pytest.mark.parametrize("text", CLEAN)
def test_quote_found_in_article_or_passage_does_not_warn(article, corpus, text):
    assert run(article, corpus, text) == []


# 오탐 방지 정상 문장: 강조·용어 표기에 쓴 짧은 인용부호는 대조하지 않는다(MIN_QUOTE_CP 미만)
NORMAL_SHORT = [
    "제목의 '단정' 표현이 본문의 '혐의' 단계보다 앞선다.",
    '"실명" 보도 여부는 별도로 확인한다.',
    "“공적 인물”에 대한 비판은 보호 범위가 넓다.",
    "「정정보도」 청구와 ‘반론보도’ 청구는 요건이 다르다.",
    "기사의 “수사 중”이라는 표현은 혐의 단계를 뜻한다.",
]


@pytest.mark.parametrize("text", NORMAL_SHORT)
def test_short_emphasis_quotes_are_not_checked(article, corpus, text):
    assert run(article, corpus, text) == []


def test_short_quote_threshold_boundary(article, corpus):
    n = review.MIN_QUOTE_CP
    assert n == 8
    assert run(article, corpus, '"가나다라마바사"고 했다') == []                       # n-1 자
    assert len(run(article, corpus, '"가나다라마바사아"고 했다')) == 1                  # n 자


def test_list_field_names_the_field_and_each_bad_quote(article, corpus):
    w = run(article, corpus, ["정상 문장이다", '대법원은 "원고의 청구는 이유 없다고 판시하였다"고 했다'], field="counter_arguments")
    assert len(w) == 1 and "explanations[finding-001].counter_arguments" in w[0]


def test_quote_from_a_passage_not_cited_in_this_report_warns(article, corpus):
    cited = corpus.get_passage("law-prec-194652", "summary-2")["text"]
    other = next(p for p in corpus.search("합성 요약", limit=9) if p["text"][-14:] not in cited)
    frag = other["text"][-14:]
    assert len(run(article, corpus, f"다른 판결은 “{frag}”라고 한다")) == 1


def test_fields_outside_the_three_prose_fields_are_not_checked(article, corpus):
    w = run(article, corpus, '"원고의 청구는 이유 없다고 판시하였다"', field="applicable_issue")
    assert w == []


def test_quote_warning_does_not_block_submit_and_keeps_report(article, corpus):
    f = copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])
    res = review.build_report_ex(article, [f], {"finding-001": {"support": '"원고의 청구는 이유 없다고 판시하였다"'}}, corpus, now=NOW)
    assert res.report is not None and res.errors == [] and res.explanations["finding-001"]["support"]
