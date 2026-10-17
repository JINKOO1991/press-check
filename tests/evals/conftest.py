import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals"))
import evalkit  # noqa: E402  (엔진 경로도 여기서 잡힌다)

from press_review import corpus as corpus_mod  # noqa: E402


@pytest.fixture(scope="session")
def corpus():
    return corpus_mod.FixtureCorpus()


@pytest.fixture(scope="session")
def articles():
    return evalkit.load_articles()


@pytest.fixture
def run_case(corpus, articles):
    """run_case(case_id, findings, explanations=None) -> evaluate_case 결과"""
    def _run(cid, findings, explanations=None):
        with evalkit.sandbox() as stats:
            return evalkit.evaluate_case(cid, copy.deepcopy(articles[cid]), findings, explanations or {}, corpus,
                                         evalkit.parse_now(evalkit.DEFAULT_NOW), stats)
    return _run


def span_of(article, bid, quote):
    t = {b["block_id"]: b["text"] for b in article["blocks"]}[bid]
    s = t.index(quote)
    return {"block_id": bid, "start": s, "end": s + len(quote), "quote": quote}


def finding(article, bid, quote, *, mode="replace", replacement=None, stars=2, channels=("civil",), evidence=(), ver=(), fid="finding-001",
            reason="시험용 주입 finding입니다."):
    return {
        "finding_id": fid, "span": span_of(article, bid, quote), "priority_stars": stars, "risk_channels": list(channels),
        "issue_category": "injected_test", "review_confidence": "medium", "reason_short": reason,
        "suggested_edit": {"mode": mode, "replacement": replacement, "related_edits": [], "required_verification": list(ver)},
        "evidence": [{"source_id": a, "passage_id": b} for a, b in evidence], "counter_considerations": [],
    }


@pytest.fixture
def mk(articles):
    def _mk(cid, bid, quote, **kw):
        return finding(articles[cid], bid, quote, **kw)
    return _mk
