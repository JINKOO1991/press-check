"""U8 보강: 원문(기사·근거 passage)이 인용부호 안에 작은따옴표(‘…’)를 품고 있을 때, 그것을 그대로 옮긴 인용은 경고하지 않는다.
설명문 쪽만 따옴표를 ASCII 로 바꾸고 원문은 바꾸지 않아 생기던 오탐의 회귀 시험."""
import copy
import datetime as dt

from press_review import review
from press_review.providers import MockProvider

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
TAG = "[인용대조]"
NESTED = "피고는 ‘합성 중첩 인용 문구’라고 주장했다고 한다"


def run(article, corpus, explanation):
    f = copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])
    res = review.build_report_ex(article, [f], {"finding-001": {"support": explanation}}, corpus, now=NOW)
    assert res.report is not None, res.errors
    return [w for w in res.warnings if TAG in w]


def with_nested(article):
    article["blocks"][-1]["text"] += " " + NESTED
    return article


def test_verbatim_nested_quote_from_the_article_does_not_warn(article, corpus):
    assert run(with_nested(article), corpus, f'기사는 "{NESTED}"라고 썼다') == []
    assert run(with_nested(copy.deepcopy(article)), corpus, f"기사는 “{NESTED}”라고 썼다") == []


def test_altered_nested_quote_still_warns(article, corpus):
    w = run(with_nested(article), corpus, '기사는 "피고는 ‘합성 중첩 인용 문구’라고 반박했다고 한다"라고 썼다')
    assert len(w) == 1
