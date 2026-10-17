"""검색 하네스(evals/run_search_evals.py): 변형 원복, 자동 gold 판정, 지연 집계, 회귀 세트 tune 표시. 합성 색인만 쓰고 실제 색인은 열지 않는다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import test_eng_search_rank as sr  # noqa: E402
import run_search_evals as rs  # noqa: E402
from press_review import corpus_sqlite as cs  # noqa: E402


def test_variants_apply_and_restore_ranking_constants(tmp_path):
    idx, ids = sr.build(tmp_path, [("case", "신청인은 반론보도를 청구하였다.")], [("list", "2012서울조정100 반론보도 취하 ")])
    try:
        before = (dict(cs.AUTHORITY_WEIGHTS), cs.LIST_CHUNK_FAMILIES, cs._LIST_SQL, idx._fts)
        for name in rs.VARIANTS:
            with rs.variant(name, idx):
                assert idx.search("반론보도", limit=5)           # 변형 안에서도 SQL 이 깨지지 않는다
        assert (dict(cs.AUTHORITY_WEIGHTS), cs.LIST_CHUNK_FAMILIES, cs._LIST_SQL, idx._fts) == before
        with rs.variant("no_demote", idx):
            assert sr.pids(idx.search("반론보도", limit=5))[0] in ids.values()
    finally:
        idx.close()


def test_gold_judges_case_cite_forbidden_and_absence():
    r = [{"case_number": "2020다1", "text": "형법 제307조", "authority_type": "judgment"}, {"case_number": "2021다2", "text": "금융실명제 실명", "authority_type": "judgment"}]
    assert rs.judge_q1({"gold": {"case_number": "2021다2"}}, r) == {"hit@1": False, "hit@10": True, "rr": 0.5, "top1_authority": "judgment"}
    assert rs.judge_q2({"gold": {"cite": "형법 제307조"}}, r)["hit@1"] is True
    assert rs.judge_q6({"gold": {"target": "실명", "hosts": ["금융실명"]}}, r)["forbidden@10"] == 1     # '실명' 이 숙주어 밖에도 있는 쪽은 금지 아님
    assert rs.judge_q7({"gold": {"absent_words": ["양궁"]}}, r)["with_query_word@10"] == 0


def test_latency_stats_median_and_nearest_rank_p95():
    s = [i / 1000 for i in range(1, 21)]           # 1..20 ms
    assert rs.latency_stats(s) == {"n": 20, "median_ms": 10.5, "p95_ms": 19.0}


def test_query_files_mark_regression_v0_as_tune_and_gold_as_dev():
    v0 = rs.load_queries(rs.SEARCH_DIR / "regression_v0.json")
    assert len(v0) == 24 and sum(q["kind"] == "general" for q in v0) == 21
    assert all(q["tune"] and q["split"] == "tune" for q in v0)
    gold = rs.load_queries(rs.GOLD_FILE)
    assert all(q["split"] == "dev" and not q["tune"] for q in gold)
    assert not {q["text"] for q in gold} & {q["text"] for q in v0 if q["kind"] == "exact_check"}      # tune 정확 검색 질의는 gold 에서 빠진다
    assert "gold" not in json.dumps([q for q in v0])
