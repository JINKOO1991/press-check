"""U4: 판례·헌재 결정(lawgo_prec·lawgo_detc) 출처 표시(search_cases·corpus_status)와 사건 단위 인입 평가 미반영 경고(build_index). 합성 장부만 쓴다."""
import datetime as dt
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import corpus as corpus_mod, corpus_sqlite as cs, review  # noqa: E402
from press_review.ingestion.__main__ import main as ingest_main  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
PHRASE = "헌법재판소 결정, 법제처 국가법령정보센터 제공"
PREC_PHRASE = "법원 판결, 법제처 국가법령정보센터 제공"
DETC_WORD = "합성헌재독자어휘"


def passage(source_id, passage_id, **kw):
    base = {k: None for k in corpus_mod.PUBLIC_KEYS}
    base.update({"source_id": source_id, "passage_id": passage_id, "text": "합성", "is_fixture": False})
    base.update(kw)
    return base


class FakeCorpus:
    snapshot_id = "idx-fake"
    is_fixture = False

    def __init__(self, passages):
        self._p = passages

    def search(self, query, **kw):
        return list(self._p)


def add_detc(path, *, quote_total, quote_ok):
    m = Manifest(path)
    try:
        m.ensure_source("lawgo_detc", "law_go_kr", "합성 헌재")
        m.upsert_document("lawgo_detc:900201", "lawgo_detc", canonical_url="https://www.law.go.kr/LSW/detcInfoP.do?detcSeq=900201", title="합성 결정")
        m.set_document_status("lawgo_detc:900201", "structured")
        cid = "lawgo_detc:900201:2099헌마1"
        rec = {"case_id": cid, "doc_id": "lawgo_detc:900201", "institution": "constitutional_court", "case_number": "2099헌마1",
               "decision_date": "2099-01-02", "authority_type": "judgment", "outcome_type": "judgment", "issue_tags": [], "legal_reasoning": []}
        m.upsert_case(cid, "lawgo_detc:900201", rec, quote_total=quote_total, quote_ok=quote_ok)
        m.upsert_chunk(cid + "#c1", "lawgo_detc:900201", f"{DETC_WORD} 합성 헌법재판소 결정 문장이다.", "text_range", "lawgo_detc:900201#s001:0-30",
                       case_id=cid, authority_type="judgment", decision_date="2099-01-02")
    finally:
        m.close()


def build(tmp_path, *, quote_ok=9, name="data"):
    d = tmp_path / name
    ledger = sl.build_ledger(d / "corpus")
    add_detc(ledger, quote_total=10, quote_ok=quote_ok)
    res = cs.build_index(ledger, d / "index", now=NOW)
    return d, ledger, res


# ------------------------------------------------------------------ search_cases 응답

def test_search_response_has_no_attribution_without_detc_passage():
    out = review.search_cases(FakeCorpus([passage("pac_cases:1", "p1"), passage("iendc_cases:2", "p2"), passage("pac_research_quarterly:3", "p3")]), query="x")
    assert "source_attribution" not in out
    assert set(out) == {"ok", "count", "snapshot_id", "is_fixture", "stance", "passages"}          # 기존 응답 키 불변


def test_search_response_attribution_only_for_detc_passages():
    ps = [passage("pac_cases:1", "p1"), passage("lawgo_detc:1", "d1"), passage("lawgo_detc:2", "d2")]
    out = review.search_cases(FakeCorpus(ps), query="x")
    assert out["source_attribution"] == {"text": PHRASE, "source_ids": ["lawgo_detc:1", "lawgo_detc:2"], "passage_ids": ["d1", "d2"]}
    assert out["passages"] == ps                                                                    # passage 공개 키·내용은 바뀌지 않는다
    assert all(set(p) == set(corpus_mod.PUBLIC_KEYS) for p in out["passages"])


def test_search_response_prec_passage_gets_prec_phrase():
    ps = [passage("lawgo_prec:7", "j1"), passage("pac_cases:1", "p1")]
    out = review.search_cases(FakeCorpus(ps), query="x")
    assert out["source_attribution"] == {"text": PREC_PHRASE, "source_ids": ["lawgo_prec:7"], "passage_ids": ["j1"]}
    assert PREC_PHRASE == "법원 판결, 법제처 국가법령정보센터 제공"


def test_search_response_mixed_prec_and_detc_has_both_phrases():
    ps = [passage("lawgo_prec:7", "j1"), passage("lawgo_detc:1", "d1"), passage("pac_cases:1", "p1")]
    out = review.search_cases(FakeCorpus(ps), query="x")
    sa = out["source_attribution"]
    assert sa["text"] == f"{PHRASE} / {PREC_PHRASE}"
    assert sa["source_ids"] == ["lawgo_detc:1", "lawgo_prec:7"] and sa["passage_ids"] == ["j1", "d1"]


def test_search_attribution_on_real_index(tmp_path):
    d, _, res = build(tmp_path)
    assert res["status"] == "built"
    c = cs.open_current(d / "index")
    try:
        hit = review.search_cases(c, query=DETC_WORD)
        assert hit["count"] >= 1 and hit["source_attribution"]["text"] == PHRASE
        assert hit["source_attribution"]["source_ids"] == ["lawgo_detc:900201"]
        assert set(hit["source_attribution"]["passage_ids"]) <= {p["passage_id"] for p in hit["passages"]}
        miss = review.search_cases(c, query="엉뚱한 사람의 초상 정정보도 조정", authority_types=["pac_settlement"])
        assert miss["count"] >= 1 and all(p["source_id"].startswith("pac_") for p in miss["passages"]) and "source_attribution" not in miss
        prec = review.search_cases(c, query="정정보도 청구는 언론중재법 제14조", authority_types=["judgment"], institution="supreme_court")
        assert prec["count"] >= 1 and prec["source_attribution"]["text"] == PREC_PHRASE
        assert all(s.startswith("lawgo_prec:") for s in prec["source_attribution"]["source_ids"])
    finally:
        c.close()


def test_attribution_for_matches_source_prefix_only():
    assert cs.attribution_for("lawgo_detc:137285") == PHRASE and cs.attribution_for("lawgo_detc") == PHRASE
    assert cs.attribution_for("lawgo_prec:1") == PREC_PHRASE and cs.attribution_for("lawgo_prec_supreme") == PREC_PHRASE
    assert cs.attribution_for("lawgo_prec_refs") == PREC_PHRASE
    assert cs.attribution_for("pac_cases:5001") is None and cs.attribution_for("lawgo_detc_x:1") is None and cs.attribution_for("lawgo_prec_x:1") is None
    assert cs.attribution_for(None) is None


def test_mcp_tool_description_mentions_attribution():
    from press_review import mcp_server
    desc = {t["name"]: t["description"] for t in mcp_server._tool_defs()}
    assert "source_attribution" in desc["search_cases"]


# ------------------------------------------------------------------ corpus_status

def test_corpus_status_source_line_has_attribution(tmp_path):
    d, _, _ = build(tmp_path)
    c = cs.open_current(d / "index")
    try:
        st = review.corpus_status(c)
    finally:
        c.close()
    assert st["sources"]["lawgo_detc"]["attribution"] == PHRASE
    assert st["sources"]["lawgo_prec_supreme"]["attribution"] == PREC_PHRASE
    assert all("attribution" not in v for k, v in st["sources"].items() if k not in ("lawgo_detc", "lawgo_prec_supreme"))
    assert st["case_admission_pending"] == 0
    assert not any("case_admission_pending" in w or "인입 평가" in w for w in st["warnings"])


def test_corpus_status_warns_when_pending(tmp_path):
    d, _, res = build(tmp_path, quote_ok=2)
    assert res["case_admission_pending"] == 1
    c = cs.open_current(d / "index")
    try:
        st = review.corpus_status(c)
    finally:
        c.close()
    assert st["case_admission_pending"] == 1
    assert any("인입 평가" in w and "1건" in w for w in st["warnings"])


# ------------------------------------------------------------------ build_index: 평가 미반영 경고

def test_build_index_no_pending_no_warning(tmp_path):
    _, _, res = build(tmp_path, quote_ok=9)
    assert res["case_admission_pending"] == 0 and "warnings" not in res


def test_build_index_pending_warns_and_keeps_snapshot_rules(tmp_path):
    d, ledger, res = build(tmp_path, quote_ok=2)
    assert res["case_admission_pending"] == 1
    assert res["warnings"] == ["case_admission_pending: 1 (run admission --apply)"]
    dry = cs.build_index(ledger, d / "index2", now=NOW, dry_run=True)
    assert dry["status"] == "dry_run" and dry["snapshot_id"] == res["snapshot_id"]                  # 경고는 snapshot 에 영향이 없다
    assert dry["case_admission_pending"] == 1 and dry["warnings"] == res["warnings"]
    assert {"status", "path", "passages", "excluded", "snapshot_id"} <= set(res)                      # 기존 키 유지


def test_build_index_ledger_untouched_and_pending_clears_after_apply(tmp_path):
    d, ledger, res = build(tmp_path, quote_ok=2)
    before = ledger.read_bytes()
    cs.build_index(ledger, d / "index3", now=NOW, dry_run=True)
    assert ledger.read_bytes() == before                                                            # 평가 계획 계산은 장부를 쓰지 않는다
    out = io.StringIO()
    assert ingest_main(["admission", "--source", "lawgo_detc", "--apply"], corpus=ledger.parent, stdout=out) == 0
    assert json.loads(out.getvalue())["summary"]["excluded"] == 1
    after = cs.build_index(ledger, d / "index4", now=NOW)
    assert after["case_admission_pending"] == 0 and "warnings" not in after
    assert after["excluded"].get("case_admission_excluded") == 1
