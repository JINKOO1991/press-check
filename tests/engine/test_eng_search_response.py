"""search_cases 응답 다듬기(U4 1단계): limit<=5 는 정보 손실 없이 중복·기본값만 줄이고, limit>5 는 긴 passage 를 발췌한다.

설계: private_docs/search_design_20261106/design.md §7·§8 U4·§9 응답, 정정 절. corpus 계약(PUBLIC_KEYS)은 바꾸지 않는다.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import corpus as corpus_mod, corpus_sqlite as cs, review  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eng_search_rank import NOW, build  # noqa: E402

EXTRAS = ("reference_date", "reference_date_basis", "date_unknown", "case_number", "institution", "outcome_type", "issue_tags", "risk_channels",
          "opinion_kinds", "opinion_kind", "includes_dissent", "counter_signals", "verification_status", "source_family", "redistributable",
          "chunk_kind", "display_text", "display_truncated", "display_limit_chars")


def est_tokens(obj) -> float:
    s = json.dumps(obj, ensure_ascii=False)
    han = sum("가" <= ch <= "힣" for ch in s)
    return han * 1.45 + (len(s) - han) * 0.35


@pytest.fixture()
def live(tmp_path):
    data = tmp_path / "data"
    sl.build_ledger(data / "corpus")
    cs.build_index(data / "corpus" / "manifest.sqlite", data / "index", now=NOW)
    c = cs.open_current(data / "index")
    yield c
    c.close()


def test_default_table_covers_only_non_public_extras():
    assert set(review.PASSAGE_DEFAULTS) <= set(EXTRAS)
    assert not set(review.PASSAGE_DEFAULTS) & set(corpus_mod.PUBLIC_KEYS)


@pytest.mark.parametrize("query", ["정정보도 요건", "명예훼손", "2021다20003", "공적 인물 비판 상당한 이유", "합성"])
def test_limit_5_response_loses_no_information(live, query):
    for stance in ("both", "counter"):
        full = live.search(query, limit=5, stance=stance)
        out = review.search_cases(live, query=query, limit=5, stance=stance)
        assert out["ok"] and out["count"] == len(full) >= 1
        assert [review.restore_passage(p) for p in out["passages"]] == full          # 기본값·중복을 되살리면 코퍼스 응답과 같다
        for p in out["passages"]:
            assert set(corpus_mod.PUBLIC_KEYS) <= set(p)                              # 공개 키는 그대로
            assert "excerpts" not in p and "text_truncated" not in p


def test_limit_5_trims_duplicates_and_defaults_but_keeps_rights_fields(live):
    full = {p["passage_id"]: p for p in live.search("정정보도 반론보도", limit=5, authority_types=["judgment"])}
    out = review.search_cases(live, query="정정보도 반론보도", limit=5, authority_types=["judgment"])
    assert out["count"] == len(full)
    got = {p["passage_id"]: p for p in out["passages"]}
    j4, j1 = got[sl.J4], got[sl.J1]
    assert "display_text" not in j4 and full[sl.J4]["display_text"] == full[sl.J4]["text"]          # 재배포 허락 passage: display_text 는 text 와 같아 뺀다
    assert j1["display_text"] == full[sl.J1]["display_text"] and j1["display_truncated"] is True     # 화면 인용 상한이 걸린 passage 는 그대로
    assert j1["redistributable"] is False and j1["display_limit_chars"] == cs.DISPLAY_QUOTE_MAX_CP
    assert sum(len(json.dumps(p, ensure_ascii=False)) for p in out["passages"]) < sum(len(json.dumps(p, ensure_ascii=False)) for p in full.values())


def test_citation_rule_explains_missing_display_text(live):
    out = review.search_cases(live, query="정정보도 반론보도", limit=5, authority_types=["judgment"])
    assert any(p.get("display_truncated") for p in out["passages"])
    assert "display_text 가 없는 passage" in out["citation_rule"] and "display_text 만" in out["citation_rule"]


def test_unknown_extras_and_public_none_values_are_kept():
    ps = [{k: None for k in corpus_mod.PUBLIC_KEYS} | {"source_id": "a:1", "passage_id": "p1", "text": "합성", "is_fixture": False, "future_key": [], "match": "text"}]

    class Fake:
        snapshot_id, is_fixture = "idx-fake", False

        def search(self, query, **kw):
            return copy.deepcopy(ps)

    out = review.search_cases(Fake(), query="x")
    assert out["passages"] == ps                                                    # None 인 공개 키·모르는 키는 값이 비어 있어도 지우지 않는다


# ------------------------------------------------------------------ limit>5 발췌

LONG_TEXT = "합성 문장이다. " * 300 + "표적어휘가 여기에 있다. " + "끝 문장이다. " * 60


@pytest.fixture()
def long_idx(tmp_path):
    idx, ids = build(tmp_path, [("long", LONG_TEXT), ("short", "표적어휘가 짧은 본문에 있다.")])
    yield idx, ids
    idx.close()


def test_limit_over_5_excerpts_long_passage_as_exact_substrings(long_idx):
    idx, ids = long_idx
    out = review.search_cases(idx, query="표적어휘", limit=6)
    got = {p["passage_id"]: p for p in out["passages"]}
    p = got[ids["long"]]
    assert "text" not in p and p["text_truncated"] is True and p["text_len"] == len(LONG_TEXT)
    ex = p["excerpts"]
    assert 1 <= len(ex) <= 3 and sum(e["end"] - e["start"] for e in ex) <= 1200
    assert ex[0]["start"] == 0 and ex[0]["end"] == 300
    for e in ex:
        assert e["text"] == LONG_TEXT[e["start"]:e["end"]]                           # 조각은 원문 부분문자열(이어 붙인 가짜 인용 방지)
    assert any("표적어휘" in e["text"] for e in ex)                                   # 질의어 주변 창
    assert [e["start"] for e in ex] == sorted(e["start"] for e in ex) and all(a["end"] <= b["start"] for a, b in zip(ex, ex[1:]))
    s = got[ids["short"]]
    assert s["text"] == "표적어휘가 짧은 본문에 있다." and "excerpts" not in s       # 발췌 예산 안의 짧은 passage 는 전문


def test_limit_5_or_less_never_excerpts(long_idx):
    idx, ids = long_idx
    for limit in (1, 5):
        out = review.search_cases(idx, query="표적어휘", limit=limit)
        assert all(p["text"] for p in out["passages"]) and not any("excerpts" in p for p in out["passages"])


def fake_corpus(passages):
    class Fake:
        snapshot_id, is_fixture = "idx-fake", False

        def search(self, query, **kw):
            return copy.deepcopy(passages)
    return Fake()


def fake_passage(pid, text, **kw):
    base = {"source_id": "lawgo_prec:" + pid, "passage_id": pid, "url": "https://www.law.go.kr/x", "locator": {"kind": "text_range", "value": "d#1:0-9"},
            "authority_type": "judgment", "decision_date": "2020-01-01", "text": text, "is_fixture": False,
            "reference_date": "2020-01-01", "reference_date_basis": "decision_date", "date_unknown": False, "case_number": "2020다1", "institution": "supreme_court",
            "outcome_type": "judgment", "issue_tags": [], "risk_channels": [], "opinion_kinds": ["majority"], "opinion_kind": "majority",
            "includes_dissent": False, "counter_signals": [], "verification_status": "verified", "source_family": "lawgo_prec_supreme",
            "redistributable": True, "chunk_kind": "case", "display_text": text, "display_truncated": False, "display_limit_chars": None, "match": "text"}
    base.update(kw)
    return base


def test_mixed_opinion_passage_keeps_full_text_even_when_limit_over_5():
    long = "다수의견이다. " * 400
    mixed = fake_passage("m1", long, opinion_kinds=["majority", "dissenting"], opinion_kind="mixed", includes_dissent=True)
    dissent = fake_passage("m2", long, opinion_kinds=["dissenting"], opinion_kind="dissenting", includes_dissent=True)
    plain = fake_passage("m3", long)
    out = review.search_cases(fake_corpus([mixed, dissent, plain]), query="다수의견", limit=10)
    by = {p["passage_id"]: p for p in out["passages"]}
    assert by["m1"]["text"] == long and by["m2"]["text"] == long and "excerpts" not in by["m1"]       # 반대의견이 든 passage 는 전문 유지
    assert "text" not in by["m3"] and by["m3"]["excerpts"]


def test_non_redistributable_long_passage_keeps_display_text_rule():
    long = "가나다라마바사 " * 300
    p = fake_passage("n1", long, redistributable=False, display_text=long[:300], display_truncated=True, display_limit_chars=300)
    out = review.search_cases(fake_corpus([p]), query="가나다라마바사", limit=7)
    got = out["passages"][0]
    assert got["display_text"] == long[:300] and got["display_truncated"] is True and "text" not in got and "citation_rule" in out


def test_limit_10_response_stays_under_25000_estimated_tokens():
    ps = [fake_passage(f"t{i}", (f"{i}번째 판결의 이유 문장이다. " * 400)) for i in range(10)]
    raw = {"passages": ps}
    assert est_tokens(raw) > 25_000                                           # 다듬기 전에는 상한을 넘는다
    out = review.search_cases(fake_corpus(ps), query="판결", limit=10)
    assert est_tokens(out) < 25_000


# ------------------------------------------------------------------ submit_report 왕복(url·locator 서버 채움)

def test_submit_report_fills_url_locator_from_ids_only(article, corpus):
    import datetime as dt
    from press_review.providers import MockProvider
    f =copy.deepcopy(MockProvider().analyze(article, corpus)[0][0])
    hit = review.search_cases(corpus, query="합성 요약", limit=2)["passages"][0]
    f["evidence"] = [{"source_id": hit["source_id"], "passage_id": hit["passage_id"]}]          # url·locator·authority_type 없이 id 두 개만
    now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
    rep = review.build_report_ex(article, [f], {}, corpus, now=now).report
    ev = rep["findings"][0]["evidence"][0]
    src = corpus.get_passage(hit["source_id"], hit["passage_id"])
    assert (ev["url"], ev["locator"], ev["authority_type"], ev["verified"]) == (src["url"], src["locator"], src["authority_type"], True)
