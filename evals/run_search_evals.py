"""검색 품질 평가 하네스(P4_DESIGN §5, 작업 S1·S2·S3).

현재 색인을 읽기 전용(``immutable=1``)으로 열어 질의 세트를 돌린다. 엔진 코드는 바꾸지 않는다.
- ``run``: 질의 세트 실행 -> 라벨 없는 분포 지표 + 자동 gold 판정 + 순위 상수 변형 비교 + (선택) 지연 측정 -> JSON.
- ``gen-gold``: 색인에서 자동 gold 질의(Q1 사건번호·Q2 조문·Q6 동형어 금지 사건·Q7 무답)를 고정 seed 로 뽑아 ``evals/search/gold_auto_dev.json`` 에 쓴다.

한계(결과 JSON 의 ``limits`` 에도 적는다)
- 사건 단위 관련성 등급(E4)이 없어 Recall@10·nDCG 는 계산하지 않는다. 여기 지표는 라벨 없는 분포와 자동 gold(사건번호 적중, 조문 포함, 금지 사건 수, 무답)뿐이며
  법률 정확도나 검색 품질을 인증하지 않는다.
- ``search()`` 의 limit 상한은 MAX_LIMIT=10 이라 §5.3 의 '변형 4개 상위 20 풀링'은 지금 코드로는 만들 수 없다.
- 자동 gold 는 dev 전용이다. test 분할은 §3.4 그룹 분할(V1) 뒤에 만든다.
- 커밋되는 질의·결과 파일에는 passage 본문·발췌를 넣지 않는다(ID·유형·순위·지표만).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalkit  # noqa: E402,F401  (엔진 경로를 잡는다)
from press_review import corpus_sqlite as cs  # noqa: E402
from press_review.corpus import MAX_LIMIT  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SEARCH_DIR = HERE / "search"
GOLD_FILE = SEARCH_DIR / "gold_auto_dev.json"
SEED = 20261010
ORIG_LIST_FAMILIES = tuple(cs.LIST_CHUNK_FAMILIES)     # 변형이 상수를 덮어써도 라벨 없는 지표는 원래 정의로 센다
ORIG_WEIGHTS = dict(cs.AUTHORITY_WEIGHTS)
WARMUP, REPEATS = 3, 20
VARIANTS = ("current", "no_weights", "no_demote", "bigram_only")
TUNE_EXACT = {"99나27325", "2011서울조정1462"}        # 순위 작업 기록에 있던 정확 검색 질의: gold 표본에서 뺀다
Q2_CITES = ["언론중재법 제16조", "언론중재법 제17조", "형법 제307조", "형법 제310조", "형법 제309조", "헌법 제21조"]   # 제14조는 tune 기록에 있어 뺐다
Q6_SPECS = [  # (질의, 목표어, 금지 숙주어): 숙주어를 지우면 목표어가 남지 않는 passage 가 금지 사건 후보. 자동 후보이며 사람 확인 전이다
    ("실명", "실명", ["금융실명"]), ("공인", "공인", ["공인중개사", "공인인증", "공인회계사", "공인노무사"]),
    ("사진", "사진", ["사진기"]), ("사과", "사과", ["사과나무"]), ("초상", "초상", ["초상집"]), ("보도", "보도", ["보도블록"]),
]
Q7_TOPICS = ["양자역학", "양궁 교향곡", "중성자 인슐린", "소믈리에"]    # 낱말마다 색인 text 에 부분문자열로 0건인 것(LIKE 로 확인; 2글자는 trigram FTS 로 셀 수 없다)


# ------------------------------------------------------------------ 입출력

def load_queries(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    qs = doc["queries"]
    ids = [q["id"] for q in qs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate query id in {path.name}")
    for q in qs:
        if q["tune"] and q["split"] != "tune":
            raise ValueError(f"{q['id']}: tune query must have split=tune")
        if q["params"].get("limit", 5) > MAX_LIMIT:
            raise ValueError(f"{q['id']}: limit above engine cap")
    return qs


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def git_info() -> dict:
    def g(*a):
        return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True, timeout=20).stdout.strip()
    dirty = [l for l in g("status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
    return {"commit": g("rev-parse", "HEAD"), "dirty_tracked_files": len(dirty)}


# ------------------------------------------------------------------ 변형(순위 상수 덮어쓰기, 항상 원복)

@contextlib.contextmanager
def variant(name: str, corpus: cs.SqliteCorpus):
    saved_w, saved_fam, saved_sql, saved_fts = dict(cs.AUTHORITY_WEIGHTS), cs.LIST_CHUNK_FAMILIES, cs._LIST_SQL, corpus._fts
    saved_kind, saved_sql3 = cs.LIST_KIND, cs._LIST_SQL_V3
    try:
        if name == "no_weights":            # 키는 두고 값만 1.0: 빈 표는 _rank_sql 의 CASE 문법을 깨뜨린다
            for k in cs.AUTHORITY_WEIGHTS:
                cs.AUTHORITY_WEIGHTS[k] = 1.0
        elif name == "no_demote":           # _LIST_SQL 은 import 시점에 만든 문자열이라 둘 다 덮어쓴다(리터럴 "0"은 ORDER BY 열 번호가 된다)
            cs.LIST_CHUNK_FAMILIES = ("__none__",)
            cs._LIST_SQL_V3 = "(p.source_family IN ('__none__') AND p.verification_status='not_applicable')"      # 3번 형식 색인용
            cs._LIST_SQL = "(p.chunk_kind='__none__')"          # 4번 형식 색인용
            cs.LIST_KIND = "__none__"
        elif name == "bigram_only":
            corpus._fts = False
        elif name != "current":
            raise ValueError(name)
        yield
    finally:
        cs.AUTHORITY_WEIGHTS.clear()
        cs.AUTHORITY_WEIGHTS.update(saved_w)
        cs.LIST_CHUNK_FAMILIES, cs._LIST_SQL, corpus._fts = saved_fam, saved_sql, saved_fts
        cs.LIST_KIND, cs._LIST_SQL_V3 = saved_kind, saved_sql3


# ------------------------------------------------------------------ 지표

def unlabeled(results: list[dict]) -> dict:
    """라벨 없는 분포 지표(질의 1개). 목록 장 수는 변형과 무관하게 원래 정의로 센다."""
    texts = [hashlib.sha1(r["text"].encode("utf-8")).hexdigest() for r in results]
    return {
        "n": len(results), "zero": not results,
        "list_chunks": sum(r["source_family"] in ORIG_LIST_FAMILIES and r["verification_status"] == "not_applicable" for r in results),
        "authority": dict(Counter(r["authority_type"] for r in results)),
        "research": sum(r["authority_type"] == "research" for r in results),
        "duplicate_texts": len(texts) - len(set(texts)),
        "date_unknown": sum(bool(r["date_unknown"]) for r in results),
        "match": dict(Counter(r["match"] for r in results)),
    }


def judge_q1(q: dict, results: list[dict]) -> dict:
    want = q["gold"]["case_number"]
    own = [want in cs.case_numbers(r["case_number"]) for r in results]
    first = own.index(True) + 1 if any(own) else None
    return {"hit@1": bool(own[:1] and own[0]), "hit@10": any(own), "rr": 1 / first if first else 0.0, "top1_authority": results[0]["authority_type"] if results else None}


def judge_q2(q: dict, results: list[dict]) -> dict:
    cite = q["gold"]["cite"]
    has = [cite in cs.cites_in(r["text"]) for r in results]
    first = has.index(True) + 1 if any(has) else None
    return {"hit@1": bool(has[:1] and has[0]), "contains@10": sum(has), "rr": 1 / first if first else 0.0}


def judge_q6(q: dict, results: list[dict]) -> dict:
    t, hosts = q["gold"]["target"], q["gold"]["hosts"]
    forb = 0
    for r in results:
        s = r["text"]
        for h in hosts:
            s = s.replace(h, "")
        forb += t not in s
    return {"forbidden@10": forb, "n": len(results)}


def judge_q7(q: dict, results: list[dict]) -> dict:
    words = q["gold"]["absent_words"]
    return {"results": len(results), "with_query_word@10": sum(any(w in r["text"] for w in words) for r in results)}


JUDGES = {"Q1": judge_q1, "Q2": judge_q2, "Q6": judge_q6, "Q7": judge_q7}


def summarize_gold(kind: str, rows: list[dict]) -> dict:
    if not rows:
        return {}
    keys = {"Q1": ["hit@1", "hit@10", "rr"], "Q2": ["hit@1", "rr"], "Q6": ["forbidden@10"], "Q7": ["results", "with_query_word@10"]}[kind]
    out = {"queries": len(rows)}
    for k in keys:
        out[k + "_mean"] = round(sum(float(r[k]) for r in rows) / len(rows), 4)
    if kind == "Q6":
        out["queries_with_forbidden"] = sum(r["forbidden@10"] > 0 for r in rows)
    return out


def percentile(sorted_vals: list[float], p: float) -> float:
    """nearest-rank: sorted[ceil(p*n)-1]. n=20 에서 p95 는 19번째 값."""
    return sorted_vals[max(0, math.ceil(p * len(sorted_vals)) - 1)]


def latency_stats(samples: list[float]) -> dict:
    s = sorted(samples)
    return {"n": len(s), "median_ms": round(statistics.median(s) * 1000, 1), "p95_ms": round(percentile(s, 0.95) * 1000, 1)}


# ------------------------------------------------------------------ 실행

def run_set(corpus: cs.SqliteCorpus, queries: list[dict], pool: int | None = None) -> dict:
    """pool 이 있으면 search_eval 로 pool 건을 받아 case_keys 에 싣고, 지표·판정은 상위 10 만으로 계산한다(기준선과 같은 정의)."""
    out = {}
    for q in queries:
        if pool:
            full = corpus.search_eval(q["text"], **{**q["params"], "limit": pool})
        else:
            full = corpus.search(q["text"], **q["params"])
        res = full[:10]
        row = {"ids": [r["passage_id"] for r in res], "unlabeled": unlabeled(res)}
        if pool:
            row["pool_ids"] = [r["passage_id"] for r in full]
            row["case_keys"] = [r["case_number"] or r["source_id"] for r in full]
        if q["kind"] in JUDGES:
            row["gold"] = JUDGES[q["kind"]](q, res)
        out[q["id"]] = row
    return out


def pooling_burden(variants: dict, queries: list[dict]) -> dict:
    """§5.3: 질의별 판정 후보 = 변형들의 상위 pool 건을 합친 고유 사건(case_number, 없으면 source_id) 수."""
    per = {q["id"]: len({k for v in variants.values() for k in v["per_query"][q["id"]]["case_keys"]}) for q in queries}
    vals = sorted(per.values())
    return {"per_query_unique_cases": per, "mean": round(sum(vals) / len(vals), 1), "median": statistics.median(vals), "max": vals[-1]}


def aggregate(queries: list[dict], per_q: dict) -> dict:
    by_set: dict[str, dict] = {}
    for sname in dict.fromkeys(q["set"] for q in queries):
        qs = [q for q in queries if q["set"] == sname]
        rows = [per_q[q["id"]]["unlabeled"] for q in qs]
        n = len(rows)
        authority: Counter = Counter()
        for r in rows:
            authority.update(r["authority"])
        entry = {"queries": n, "zero_result_queries": sum(r["zero"] for r in rows), "top10_authority_total": dict(authority),
                 "list_chunks_top10_total": sum(r["list_chunks"] for r in rows), "research_top10_total": sum(r["research"] for r in rows),
                 "duplicate_texts_top10_total": sum(r["duplicate_texts"] for r in rows), "date_unknown_top10_total": sum(r["date_unknown"] for r in rows),
                 "results_total": sum(r["n"] for r in rows)}
        kinds = {q["kind"] for q in qs}
        for kd in kinds & JUDGES.keys():
            entry["gold_" + kd] = summarize_gold(kd, [per_q[q["id"]]["gold"] for q in qs if q["kind"] == kd])
        by_set[sname] = entry
    return by_set


def compare(base: dict, other: dict) -> dict:
    """질의별 짝 차이(라벨 없음): 상위 10 겹침과 목록 장·연구 수 차이."""
    ov, dl, dr = [], [], []
    for qid, b in base.items():
        o = other[qid]
        a, c = set(b["ids"]), set(o["ids"])
        ov.append(len(a & c) / max(1, len(a | c)))
        dl.append(o["unlabeled"]["list_chunks"] - b["unlabeled"]["list_chunks"])
        dr.append(o["unlabeled"]["research"] - b["unlabeled"]["research"])
    n = len(ov)
    return {"queries": n, "jaccard_top10_mean": round(sum(ov) / n, 4), "queries_changed": sum(x < 1.0 for x in ov),
            "list_chunks_delta_total": sum(dl), "research_delta_total": sum(dr)}


def measure_latency(index: Path, queries: list[dict]) -> dict:
    per_q, cold, all_samples = {}, {}, []
    for q in queries:
        c = cs.SqliteCorpus(index)       # 질의마다 새로 연다: 프로세스 콜드(OS 페이지 캐시는 비우지 않는다)
        try:
            t = time.perf_counter()
            c.search(q["text"], **q["params"])
            cold[q["id"]] = round((time.perf_counter() - t) * 1000, 1)
            for _ in range(WARMUP):
                c.search(q["text"], **q["params"])
            samples = []
            for _ in range(REPEATS):
                t = time.perf_counter()
                c.search(q["text"], **q["params"])
                samples.append(time.perf_counter() - t)
        finally:
            c.close()
        per_q[q["id"]] = latency_stats(samples)
        all_samples += samples
    return {"method": f"질의마다 색인을 새로 열어 콜드 1회, 예열 {WARMUP}회 버림, {REPEATS}회 측정(time.perf_counter, search() 전후). p95=nearest-rank. "
                      "콜드는 프로세스 콜드이며 디스크 콜드가 아니다. MCP 왕복은 측정하지 않았다.",
            "per_query": per_q, "cold_ms": cold, "set_pooled": latency_stats(all_samples),
            "set_median_of_query_medians_ms": round(statistics.median(v["median_ms"] for v in per_q.values()), 1),
            "set_median_of_query_p95_ms": round(statistics.median(v["p95_ms"] for v in per_q.values()), 1)}


def phrase_caps(corpus: cs.SqliteCorpus, queries: list[dict]) -> dict:
    out = {}
    for q in queries:
        stems = {corpus._stem(w) for w in cs.runs(q["text"])}
        out[q["id"]] = {"phrases_ge3": sum(len(s) >= 3 for s in stems), "cap": cs.MAX_PHRASES, "at_cap": sum(len(s) >= 3 for s in stems) >= cs.MAX_PHRASES}
    return out


def resolve_index(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    od = cs.index_dir()
    return od / (od / cs.CURRENT_POINTER).read_text(encoding="utf-8").strip()


def cmd_run(a) -> int:
    index = resolve_index(a.index)
    corpus = cs.SqliteCorpus(index)
    try:
        if a.expect_snapshot and corpus.snapshot_id != a.expect_snapshot:
            sys.stderr.write(f"snapshot mismatch: {corpus.snapshot_id}\n")
            return 2
        files = [SEARCH_DIR / "regression_v0.json", GOLD_FILE, SEARCH_DIR / "latency_load.json"]
        sets = {f.stem: load_queries(f) for f in files if f.exists()}
        rank_queries = [q for k in ("regression_v0", "gold_auto_dev") for q in sets.get(k, [])]
        variants = {}
        for v in a.variants:
            with variant(v, corpus):
                per_q = run_set(corpus, rank_queries, a.pool)
            variants[v] = {"per_query": per_q, "by_set": aggregate(rank_queries, per_q)}
        base = variants["current"]["per_query"]
        result = {
            "what": "검색 하네스 실행 결과. 법률 정확도·모델 성능을 재지 않는다. 라벨 없는 분포 지표와 자동 gold(dev) 판정이며 사건 단위 관련성 등급은 없다.",
            "snapshot_id": corpus.snapshot_id, "index_file": index.name, "git": git_info(),
            "rank_constants": {"AUTHORITY_WEIGHTS": ORIG_WEIGHTS, "CANDIDATE_CAP": cs.CANDIDATE_CAP, "MAX_PHRASES": cs.MAX_PHRASES, "MAX_GRAMS": cs.MAX_GRAMS,
                               "BIGRAM_K1": cs.BIGRAM_K1, "LIST_CHUNK_FAMILIES": list(ORIG_LIST_FAMILIES), "KEEP_INDEXES": cs.KEEP_INDEXES, "INDEX_FORMAT": cs.INDEX_FORMAT},
            "env": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "fts_trigram_available": cs.fts_trigram_available(),
                    "index_has_trigram": corpus._index_has_fts, "platform": platform.platform(), "cpus": os.cpu_count(), "note": a.env_note},
            "query_files": {f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:16] for f in files if f.exists()},
            "variants": {v: {"by_set": variants[v]["by_set"]} for v in variants},
            "paired_vs_current": {v: compare(base, variants[v]["per_query"]) for v in variants if v != "current"},
            "per_query_current": base,
            "pool": a.pool,
            "limits": ["Recall@10·nDCG@10 은 사건 단위 등급(E4)이 없어 계산하지 않았다.", "기본 실행은 search() limit 상한 10 이다. 상위 20 풀링은 --pool 20(search_eval, 평가 전용)로만 돈다.",
                       "자동 gold 는 dev 전용이며 Q3·Q4·Q5·as_of 변형과 test 분할은 만들지 않았다(V1 분할 뒤).",
                       "Q6 금지 사건 목록은 자동 후보이며 사람 확인 전이다. Q7 은 질의어가 색인 text 에 0건임을 확인한 주제이고, 상위 결과가 관련 0이라는 판정은 아니다."],
        }
        if a.pool:
            result["pooling_burden"] = pooling_burden(variants, rank_queries)
            result["pooling_burden_general21"] = pooling_burden(variants, [q for q in rank_queries if q["kind"] == "general"])
        q7 = [q for q in sets.get("gold_auto_dev", []) if q["kind"] == "Q7"]
        if q7:
            con = open_ro(index)
            try:
                result["q7_absence_check"] = {q["id"]: {w: con.execute("SELECT count(*) FROM passages WHERE text LIKE ?", ("%" + w + "%",)).fetchone()[0]
                                                        for w in q["text"].split()} for q in q7}
            finally:
                con.close()
        if "latency_load" in sets:
            result["load_set_caps"] = phrase_caps(corpus, sets["latency_load"])
    finally:
        corpus.close()
    if a.pool and a.latency:
        sys.stderr.write("--pool 과 --latency 는 같이 쓰지 않는다\n")
        return 2
    if a.latency:     # 패치 없는 현행 설정에서만 잰다
        lat_queries = [q for k in ("regression_v0", "gold_auto_dev", "latency_load") for q in sets.get(k, [])]
        result["latency"] = measure_latency(index, lat_queries)
        result["latency"]["queries"] = len(lat_queries)
    out = Path(a.out) if a.out else HERE / "out" / f"search_{result['snapshot_id']}_{result['git']['commit'][:8]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(json.dumps({"out": str(out), "snapshot_id": result["snapshot_id"], "commit": result["git"]["commit"][:8]}) + "\n")
    return 0


# ------------------------------------------------------------------ 자동 gold 생성(S2)

def gen_gold(index: Path) -> dict:
    con = open_ro(index)
    try:
        rng = random.Random(SEED)
        qs = []
        for auth in ("judgment", "pac_decision", "pac_settlement", "pac_election_review"):
            rows = sorted(r[0] for r in con.execute(
                "SELECT c.norm FROM case_numbers c JOIN passages p ON p.rid=c.rid WHERE p.authority_type=? AND p.case_number IS NOT NULL "
                "GROUP BY c.norm HAVING count(*) BETWEEN 1 AND 3", (auth,)) if r[0] not in TUNE_EXACT and cs.query_case_numbers(r[0]))
            for norm in rng.sample(rows, 2):
                qs.append({"id": f"Q1-{len(qs) + 1:02d}", "set": "gold_auto_dev", "kind": "Q1", "text": norm, "params": {"limit": 10}, "split": "dev", "tune": False,
                           "label_source": "auto", "gold": {"case_number": norm, "stratum": auth}})
        for i, cite in enumerate(Q2_CITES, 1):
            qs.append({"id": f"Q2-{i:02d}", "set": "gold_auto_dev", "kind": "Q2", "text": cite, "params": {"limit": 10}, "split": "dev", "tune": False,
                       "label_source": "auto", "gold": {"cite": cite, "cites_rows": con.execute("SELECT count(*) FROM cites WHERE cite=?", (cite,)).fetchone()[0]}})
        for i, (q, t, hosts) in enumerate(Q6_SPECS, 1):
            n = sum(con.execute("SELECT count(*) FROM passages WHERE text LIKE ?", (f"%{h}%",)).fetchone()[0] for h in hosts)
            qs.append({"id": f"Q6-{i:02d}", "set": "gold_auto_dev", "kind": "Q6", "text": q, "params": {"limit": 10}, "split": "dev", "tune": False,
                       "label_source": "auto", "tune_overlap": q in {"실명", "공인", "사진", "사과", "초상"},
                       "gold": {"target": t, "hosts": hosts, "host_passages": n, "needs_human_check": True}})
        for i, topic in enumerate(Q7_TOPICS, 1):
            absent = {w: con.execute("SELECT count(*) FROM passages WHERE text LIKE ?", ("%" + w + "%",)).fetchone()[0] for w in topic.split()}
            if any(absent.values()):
                raise RuntimeError(f"Q7 topic not absent: {topic} {absent}")
            qs.append({"id": f"Q7-{i:02d}", "set": "gold_auto_dev", "kind": "Q7", "text": topic, "params": {"limit": 10}, "split": "dev", "tune": False,
                       "label_source": "auto", "gold": {"absent_words": topic.split(), "text_hits": absent}})
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key IN ('snapshot_id','passage_count')"))
    finally:
        con.close()
    return {"schema_version": 1, "_note": "자동 gold 질의(P4_DESIGN §5.2 Q1·Q2·Q6·Q7, dev 전용). `run_search_evals.py gen-gold` 로 고정 seed 에서 재생성한다. "
            "Q1 은 사건번호를 가진 passage 가 1~3개인 사건번호에서 판례·결정·조정·선거심의 유형별 2개씩, tune 기록의 정확 검색 질의는 뺐다. Q6 금지 사건은 자동 후보이며 사람 확인 전이다. "
            "test 분할과 Q3·Q4·Q5는 V1 분할 뒤에 만든다. 본문·발췌를 담지 않는다.",
            "generated_from_snapshot": meta.get("snapshot_id"), "seed": SEED, "queries": qs}


def cmd_gen_gold(a) -> int:
    doc = gen_gold(resolve_index(a.index))
    GOLD_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(f"{len(doc['queries'])} queries -> {GOLD_FILE}\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--index", default=None, help="색인 파일(기본: CURRENT). 읽기 전용으로만 연다")
    r.add_argument("--expect-snapshot", default=None)
    r.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=VARIANTS)
    r.add_argument("--latency", action="store_true")
    r.add_argument("--pool", type=int, default=None, help="search_eval 로 질의당 상위 N건을 받아 풀링한다(평가 전용, <=EVAL_MAX_LIMIT). 지표는 상위 10 기준 그대로")
    r.add_argument("--out", default=None)
    r.add_argument("--env-note", default="")
    g = sub.add_parser("gen-gold")
    g.add_argument("--index", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "run" and "current" not in a.variants:
        a.variants = ["current", *a.variants]
    return cmd_run(a) if a.cmd == "run" else cmd_gen_gold(a)


if __name__ == "__main__":
    sys.exit(main())
