"""검색 어절 경계 가중(edge_soft25, UDEC-21): 복합 조사 8개, bigram_terms 의 ne 열(pdr-index-5), 구형 색인 롤백, 정확 검색 불변.

설계: private_docs/search_design_20261106/design.md §1·§6·§9, 정정 절(stem 규칙 버전).
"""
import math
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import corpus_sqlite as cs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eng_search_rank import NOW, build, pids  # noqa: E402

COMPOUND8 = ("이라는", "에서의", "으로서", "으로써", "에게서", "으로의", "한테", "께서")


# ------------------------------------------------------------------ U1 복합 조사

@pytest.mark.parametrize("josa", COMPOUND8)
def test_compound_josa_is_stripped(josa):
    assert cs.strip_josa("법원" + josa) == "법원"
    assert cs.strip_josa("대통령실" + josa) == "대통령실"


def test_compound_josa_keeps_existing_behaviour_and_two_char_floor():
    assert cs.strip_josa("전문가") == "전문"          # 기존: 끝 가/이/도 처리는 _stem 의 색인 증거가 맡는다
    assert cs.strip_josa("기자가") == "기자"
    assert cs.strip_josa("법원에서는") == "법원"
    assert cs.strip_josa("나한테") == "나한테" and cs.strip_josa("나께서") == "나께서"      # 남는 글자 2자 미만은 떼지 않는다
    assert cs.strip_josa("한테") == "한테"


# ------------------------------------------------------------------ U2 ne 계산

def counts(text):
    return cs.bigram_counts(text)


def test_ne_interior_and_edges():
    assert counts("원고의")["고의"] == (1, 0)                 # 원고의 -> '고의' 는 어절 안쪽(조사 뗀 어절 '원고' 끝이 아니다)
    assert counts("원고의")["원고"] == (1, 1)                 # 어절 첫머리
    assert counts("정정보도청구")["청구"] == (1, 1)           # 조사 없는 어절 끝
    assert counts("정정보도청구를")["청구"] == (1, 1)         # 조사를 뗀 어절 끝
    assert counts("정정보도청구권")["청구"] == (1, 0)         # 안쪽
    assert counts("금융실명법")["실명"] == (1, 0)
    assert counts("고의로")["고의"] == (1, 1)                 # 첫머리
    assert counts("법원으로서")["법원"] == (1, 1)


def test_ne_counts_runs_and_sums_both_kinds():
    c = counts("고의 원고의 정정보도청구 고의로")
    assert c["고의"] == (3, 2)                                # 첫머리 2(고의, 고의로) + 안쪽 1(원고의)
    assert c["청구"] == (1, 1)


def test_ne_non_hangul_gram_counts_as_edge():
    c = counts("ai 모델 ai")
    assert c["ai"] == (2, 2)                                  # 한글이 아닌 gram 은 ne=n
    c2 = counts("2019다12345")
    assert all(n == ne for g, (n, ne) in c2.items() if not all("가" <= ch <= "힣" for ch in g))


# ------------------------------------------------------------------ 순위(합성 색인)

CASES = [("interior", "원고의 청구를 기각한다는 취지의 사건이다"), ("head", "고의로 명예를 훼손한 사건이다")]
# C0(구 점수)에서는 두 passage 가 같은 n=1 이라 passage_id 순(interior 가 먼저)으로 나온다.


def test_interior_only_passage_ranks_after_head_but_is_not_dropped(tmp_path):
    idx, ids = build(tmp_path, CASES)
    try:
        assert pids(idx.search("고의", limit=5)) == [ids["head"], ids["interior"]]
    finally:
        idx.close()


def test_bigram_score_formula(tmp_path):
    idx, ids = build(tmp_path, CASES)
    try:
        total = idx._passage_total()
        rows = {r["rid"]: r["s"] for r in idx._bigram_scores(["고의"], "", [], "", edge={"고의"})}
        rid = {pid: r for r, pid in idx._db.execute("SELECT rid, passage_id FROM passages")}
        idf = math.log(1 + (total - 1 + 0.5) / (1 + 0.5))          # df = ne>0 인 행 1개(head)
        k = cs.BIGRAM_K1
        assert rows[rid[ids["head"]]] == pytest.approx(idf * 1.0 * (k + 1) / (1.0 + k))
        assert rows[rid[ids["interior"]]] == pytest.approx(idf * 0.25 * (k + 1) / (0.25 + k))
        assert cs.EDGE_INTERIOR_WEIGHT == 0.25
    finally:
        idx.close()


def test_index_format_5_and_stem_rules_recorded(tmp_path):
    idx, _ = build(tmp_path, CASES)
    try:
        meta = idx._meta
        assert meta["format"] == "pdr-index-5" == cs.INDEX_FORMAT
        assert meta["ne_stem_rules"] == cs.ne_stem_rules()
        cols = [r[1] for r in idx._db.execute("PRAGMA table_info(bigram_terms)")]
        assert cols == ["gram", "rid", "n", "ne"]
    finally:
        idx.close()


def test_stem_rule_change_changes_snapshot(tmp_path, monkeypatch):
    corpus = tmp_path / "c"
    sl.build_ledger(corpus)
    a = cs.build_index(corpus / "manifest.sqlite", tmp_path / "ia", now=NOW, dry_run=True)["snapshot_id"]
    monkeypatch.setattr(cs, "_JOSA2", cs._JOSA2 + ("시험",))
    b = cs.build_index(corpus / "manifest.sqlite", tmp_path / "ib", now=NOW, dry_run=True)["snapshot_id"]
    assert a != b            # strip_josa 를 고치면 같은 passage 라도 ne 가 달라지므로 재빌드가 unchanged 로 건너뛰면 안 된다


# ------------------------------------------------------------------ 구형 색인 롤백(pdr-index-3·4)

def downgrade(src: Path, dst: Path, fmt: str) -> Path:
    shutil.copy(src, dst)
    db = sqlite3.connect(dst)
    with db:
        if "ne" in [r[1] for r in db.execute("PRAGMA table_info(bigram_terms)")]:
            db.execute("ALTER TABLE bigram_terms DROP COLUMN ne")
        if fmt == "pdr-index-3":
            db.execute("DROP TABLE passage_alias")
            db.execute("ALTER TABLE passages DROP COLUMN chunk_kind")
        db.execute("UPDATE meta SET value=? WHERE key='format'", (fmt,))
    db.close()
    return dst


@pytest.mark.parametrize("fmt", ["pdr-index-3", "pdr-index-4"])
def test_old_index_formats_keep_current_scoring(tmp_path, fmt):
    idx, ids = build(tmp_path, CASES)
    path = Path(idx._path)
    idx.close()
    old = cs.SqliteCorpus(downgrade(path, tmp_path / f"old-{fmt}.sqlite", fmt))
    try:
        assert pids(old.search("고의", limit=5)) == [ids["interior"], ids["head"]]      # C0 순서: 같은 점수는 passage_id 순
        assert pids(old.search("원고의", limit=5))[0] == ids["interior"]
    finally:
        old.close()


def test_bigram_only_mode_keeps_c0_for_three_char_words(tmp_path):
    cases = [("a", "원고의적 사정이 없다는 판단"), ("b", "고의적으로 한 행위라는 판단")]
    idx, ids = build(tmp_path, cases)
    path = Path(idx._path)
    idx.close()
    new = cs.SqliteCorpus(path)
    old = cs.SqliteCorpus(downgrade(path, tmp_path / "old4.sqlite", "pdr-index-4"))
    try:
        for c in (new, old):
            c._fts = False                       # trigram 없는 환경: 3글자 낱말을 2글자 gram 으로 쪼갠다
        got = pids(new.search("고의적", limit=5))
        assert got == pids(old.search("고의적", limit=5)) == [ids["a"], ids["b"]]    # 쪼갠 gram 에는 경계 가중을 쓰지 않는다(C0 와 같다)
    finally:
        new.close()
        old.close()


# ------------------------------------------------------------------ 정확 검색 회귀(사건번호·조문 단독)

def test_case_number_and_cite_only_queries_equal_c0(tmp_path):
    data = tmp_path / "data"
    sl.build_ledger(data / "corpus")
    cs.build_index(data / "corpus" / "manifest.sqlite", data / "index", now=NOW)
    idx = cs.open_current(data / "index")
    path = Path(idx._path)
    keys = [r[0] for r in idx._db.execute("SELECT DISTINCT norm FROM case_numbers")] + [r[0] for r in idx._db.execute("SELECT DISTINCT cite FROM cites")]
    assert len(keys) >= 5
    new = {k: pids(idx.search(k, limit=10)) for k in keys}
    idx.close()
    old = cs.SqliteCorpus(downgrade(path, tmp_path / "c0.sqlite", "pdr-index-4"))
    try:
        for k in keys:
            assert new[k] == pids(old.search(k, limit=10)), k
            assert new[k], k
    finally:
        old.close()
