import json

import pytest

from press_review import config, corpus as corpus_mod, review


def raw_corpus():
    return json.loads((config.FIXTURES_DIR / "corpus.json").read_text(encoding="utf-8"))


def test_fixture_file_invariants():
    d = raw_corpus()
    ps = d["passages"]
    assert d["is_fixture"] is True and 6 <= len(ps) <= 10
    assert all(p["is_fixture"] is True for p in ps)
    assert all(p["text"].startswith("합성 요약(원문 아님)") for p in ps)
    real = sorted(p["url"] for p in ps if "law.go.kr" in p["url"])
    assert real == ["https://www.law.go.kr/LSW/precInfoP.do?precSeq=194652", "https://www.law.go.kr/LSW/precInfoP.do?precSeq=241789"]
    assert all("example.invalid" in p["url"] for p in ps if "law.go.kr" not in p["url"])
    real_ps = [p for p in ps if "law.go.kr" in p["url"]]
    assert all(p["decision_date"] is None for p in real_ps)            # 사건번호·선고일을 지어내지 않는다
    assert len({(p["source_id"], p["passage_id"]) for p in ps}) == len(ps)


def test_interface_basics(corpus):
    assert corpus.is_fixture and corpus.snapshot_id == "fixture-synthetic-0.1"
    p = corpus.get_passage("law-prec-194652", "summary-2")
    assert p["authority_type"] == "judgment" and set(p) == set(corpus_mod.PUBLIC_KEYS)
    p["text"] = "mutated"
    assert corpus.get_passage("law-prec-194652", "summary-2")["text"] != "mutated"
    assert corpus.get_passage("law-prec-194652", "nope") is None
    assert corpus.get_passage("x", "y") is None


def test_search_ranks_and_filters(corpus):
    hits = corpus.search("기사 전체 인상 제목 본문 단정 혐의 수사", authority_types=["judgment"], risk_channel="civil", limit=1)
    assert [(h["source_id"], h["passage_id"]) for h in hits] == [("law-prec-194652", "summary-2")]
    all_hits = corpus.search("합성 요약 원문 아님", limit=50)
    assert len(all_hits) == 9 == len({(h["source_id"], h["passage_id"]) for h in all_hits})
    assert len(corpus.search("합성 요약 원문 아님", limit=3)) == 3
    assert {h["authority_type"] for h in corpus.search("합성 요약", authority_types=["statute", "research"])} == {"statute", "research"}
    assert all(h["source_id"] == "fixture-iendc-001" for h in corpus.search("합성 요약", risk_channel="iendc_election"))
    assert corpus.search("합성 요약", risk_channel="privacy") == []


def test_search_deterministic_tie_break(corpus):
    a = corpus.search("합성 요약 원문 아님", limit=10)
    b = corpus.search("합성 요약 원문 아님", limit=10)
    assert a == b


def test_as_of_excludes_later_decisions(corpus):
    q = "제목과 본문의 인상 차이를 다루는 가상 결정"
    assert any(h["source_id"] == "fixture-pac-decision-001" for h in corpus.search(q, as_of="2026-10-08"))
    assert not any(h["source_id"] == "fixture-pac-decision-001" for h in corpus.search(q, as_of="2025-01-01"))
    assert any(h["source_id"] == "fixture-pac-decision-001" for h in corpus.search(q, as_of="2025-03-01"))


def test_zero_results_is_stated(corpus):
    out = review.search_cases(corpus, query="zzzzqqqq")
    assert out["ok"] and out["count"] == 0 and out["passages"] == [] and "찾지 못했습니다" in out["message"]


def test_limit_capped_at_ten_and_bad_args(corpus):
    assert review.search_cases(corpus, query="합성", limit=999)["count"] <= 10
    for kw in [dict(query=""), dict(query="x", limit=0), dict(query="x", limit=True), dict(query="x", risk_channel="lawsuit"),
               dict(query="x", authority_types=["blog"]), dict(query="x", as_of="2026-13-01"), dict(query=5)]:
        out = review.search_cases(corpus, **kw)
        assert out["ok"] is False and out["errors"], kw


def test_search_result_shape_matches_d2(corpus):
    out = review.search_cases(corpus, query="합성 요약", limit=2)
    for p in out["passages"]:
        assert set(p) == {"source_id", "passage_id", "url", "locator", "authority_type", "decision_date", "text", "is_fixture"}
        assert p["is_fixture"] is True


def test_status_counts(corpus):
    st = review.corpus_status(corpus)
    assert st["ok"] and st["is_fixture"] and st["passage_count"] == 9 and st["source_count"] == 9
    assert st["by_authority_type"]["judgment"] == 2 and st["as_of"] == "2026-10-08"
    assert st["rights_hold"] and st["missing"] and "시험" in st["notice"] or "합성" in st["notice"]


def test_malformed_corpora_rejected(tmp_path):
    d = raw_corpus()

    def write(obj):
        p = tmp_path / "c.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.FixtureCorpus(tmp_path / "missing.json")
    bad = dict(d, is_fixture=False)
    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.FixtureCorpus(write(bad))
    bad = json.loads(json.dumps(d))
    bad["passages"][0]["is_fixture"] = False
    with pytest.raises(corpus_mod.CorpusError, match="is_fixture"):
        corpus_mod.FixtureCorpus(write(bad))
    bad = json.loads(json.dumps(d))
    bad["passages"].append(bad["passages"][0])
    with pytest.raises(corpus_mod.CorpusError, match="duplicate"):
        corpus_mod.FixtureCorpus(write(bad))
    bad = json.loads(json.dumps(d))
    del bad["passages"][0]["url"]
    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.FixtureCorpus(write(bad))


def test_load_default_is_fixture_in_p0():
    assert corpus_mod.load_default().is_fixture
