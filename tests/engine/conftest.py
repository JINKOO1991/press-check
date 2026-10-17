import copy
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdr_engine_paths import ARTICLE_FIXTURE, CONTRACTS, ENGINE, REPO  # noqa: E402

from press_review import corpus as corpus_mod  # noqa: E402
from press_review import logsafe  # noqa: E402



@pytest.fixture(autouse=True)
def _clean_logsafe():
    logsafe.reset()
    yield
    logsafe.reset()


@pytest.fixture(scope="session")
def repo():
    return REPO


@pytest.fixture(scope="session")
def article_bytes():
    return ARTICLE_FIXTURE.read_bytes()


@pytest.fixture()
def article(article_bytes):
    return json.loads(article_bytes.decode("utf-8"))


@pytest.fixture(scope="session")
def example_review_obj():
    return json.loads((CONTRACTS / "review.example.json").read_text(encoding="utf-8"))


@pytest.fixture()
def example_review(example_review_obj):
    return copy.deepcopy(example_review_obj)


@pytest.fixture(scope="session")
def corpus():
    return corpus_mod.FixtureCorpus()


def make_article(blocks, article_id="t-001", date="2026-10-05"):
    return {
        "schema_version": "0.1",
        "article_id": article_id,
        "publication_date": date,
        "blocks": [{"block_id": b, "section": s, "text": t} for b, s, t in blocks],
        "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False},
    }


@pytest.fixture()
def mk_article():
    return make_article
