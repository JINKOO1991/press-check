"""P4 V1: 사건 그룹(union-find)과 tune/dev/test/reserve 분할 산출(P4_DESIGN §3.4, §8.1 V1).

입력은 읽기 전용으로만 연다. 색인은 ``mode=ro&immutable=1``, manifest 는 ``mode=ro``(수신 프로세스가 WAL 로 쓰는 중이라 immutable 금지) + query_only + 단일 읽기 트랜잭션.
쓰기·잠금·ingestion 실행은 하지 않는다. 같은 입력이면 같은 결과다(고정 seed, 결정적 정렬, 입력 digest 기록).

그룹 묶기(결정적): 1) 같은 정규화 사건번호  2) 심급·관련 사건: record 의 appeal_or_later_history 사건번호, case_links(linked 는 대상 문서의 사건, 그 외는 번호로),
판결 본문 '원심' 인용 사건번호  3) 같은 원기사: disputed_spans·article_claims 인용문 5-gram Jaccard >= SIM_THRESHOLD.
매체(4)는 그룹을 묶는 규칙이 아니라 test 분리 제약이다(사용자 결정 2026-10-10: 매체 분리는 test 분할만).

분할: 그룹 id 해시 버킷으로 test 15%·dev 25%·reserve 10%·tune 50%. 매체명이 MEDIA_TEST_MAX_GROUPS 건보다 많은 그룹을 가진 매체의 그룹은 test 후보에서 dev 로 내린다.
test 에 매체 키가 있으면 같은 키를 가진 나머지 그룹은 reserve(media_conflict)로 옮긴다. 미래 기준일 T 는 정하지 않고 분포표만 낸다.

산출: 저장소 evals/split/split_v1_summary.json(작은 요약, 사건번호·매체명 없음), 로컬 데이터 폴더 아래 eval/ (그룹 멤버 매핑 split_v1_members.json 은 run_p3_evals --group-members 형식 + split,
case 배정 split_v1_assignment.json, 매체 키 목록 split_v1_media_keys.json). 법률 판단이나 성능 결과가 아니다.
"""
from __future__ import annotations

import argparse
import ast
import collections
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalkit  # noqa: E402,F401
import p4_metrics  # noqa: E402
from press_review import corpus_sqlite as cs  # noqa: E402

HERE = Path(__file__).resolve().parent
SEED = "p4-split-v1"
FRACTIONS = (("test", 0.15), ("dev", 0.25), ("reserve", 0.10), ("tune", 0.50))      # 버킷 구간 순서(test 부터)
SIM_THRESHOLD = 0.6
SHINGLE = 5
MIN_SPAN_CHARS = 30
GRAM_DF_CAP = 20                       # 이보다 많은 기록에 나오는 5-gram 은 후보 생성에 쓰지 않는다(상투 문구)
MEDIA_TEST_MAX_GROUPS = 5
GENERIC_MEDIA = {"언론사", "매체", "신문", "방송사", "일간지", "주간신문", "인터넷매체", "인터넷신문", "방송", "통신사", "잡지"}
EXCLUDED_STATUS = ("quarantined", "rejected")
_ORIG = re.compile(r"원심(?:판결|법원)?[^\n]{0,80}?(\d{2,4}\s?[가-힣]{1,4}\s?\d{1,7})")
_PAREN = re.compile(r"[(（][^)）]*[)）]")


def data_dir() -> Path:
    from press_review import config
    return config.data_dir()


def open_manifest(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    c.execute("PRAGMA query_only=1")
    c.execute("BEGIN")                  # 한 읽기 트랜잭션: 수신 중에도 일관된 시점
    return c


def open_index(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def norm_numbers(raw) -> list[str]:
    return cs.case_numbers(re.sub(r"\s+", "", unicodedata.normalize("NFKC", raw))) if isinstance(raw, str) and raw.strip() else []


def media_key(label: str, aliases: dict) -> str | None:
    s = unicodedata.normalize("NFKC", label or "")
    s = _PAREN.sub("", s)
    for t in ("주식회사", "㈜", "(주)"):
        s = s.replace(t, "")
    s = re.sub(r"\s+", "", s).lower()
    if s.endswith("사") and s[:-1].endswith(("일보", "신문", "방송", "뉴스", "통신")):
        s = s[:-1]
    if not s or s in GENERIC_MEDIA:
        return None
    return aliases.get(s, s)


def quote_text(item) -> str:
    """disputed_spans·article_claims 항목의 인용문 문자열. quote 가 {page_id,start,end,quote} 객체이면 안쪽 quote, 항목에 text 만 있으면 text."""
    if not isinstance(item, dict):
        return str(item or "")
    q = item.get("quote")
    if isinstance(q, dict):
        q = q.get("quote")
    return str(q or item.get("text") or "")


def _parse(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return ast.literal_eval(v)
    return v


def load_nodes(m: sqlite3.Connection, aliases: dict) -> tuple[list[dict], list[tuple], dict]:
    nodes = []
    stats = collections.Counter()
    raw_media_labels: set[str] = set()
    excerpts: dict[str, str] = {}
    media_raw: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for case_id, doc_id, auth, num, dd, status, rj in m.execute(
            "SELECT case_id, doc_id, authority_type, case_number, decision_date, verification_status, record_json FROM cases ORDER BY case_id"):
        stats["cases_all"] += 1
        if status in EXCLUDED_STATUS:
            continue
        rec = json.loads(rj)
        hist = [n for h in rec.get("appeal_or_later_history") or [] for n in norm_numbers(h.get("case_number"))]
        spans = [quote_text(s) for s in rec.get("disputed_spans") or []] + [quote_text(c) for c in rec.get("article_claims") or []]
        text = "".join(re.sub(r"\s+", "", t) for t in spans)
        if len(text) >= MIN_SPAN_CHARS:
            excerpts[case_id] = " ".join(t.strip() for t in spans if t.strip())[:200]
        media = []
        for p in rec.get("parties_redacted") or []:
            if p.get("role") == "media_outlet":
                raw_media_labels.add(p.get("label", ""))
                k = media_key(p.get("label", ""), aliases)
                if k:
                    media.append(k)
                    media_raw[k][p.get("label", "")] += 1
        nodes.append({"case_id": case_id, "doc_id": doc_id, "auth": auth, "numbers": norm_numbers(num), "decision_date": dd, "history": sorted(set(hist)),
                      "text": text if len(text) >= MIN_SPAN_CHARS else "", "media": sorted(set(media)), "has_media_role": any(p.get("role") == "media_outlet" for p in rec.get("parties_redacted") or [])})
    stats["cases_included"] = len(nodes)
    links = [tuple(r) for r in m.execute("SELECT case_id, rel, case_number, target_doc_id, status FROM case_links ORDER BY case_id, rel, target_doc_id")]
    for n in nodes:                     # 판결 본문 '원심' 인용 사건번호(판례만)
        n["orig"] = []
    jud = {n["case_id"]: n for n in nodes if n["auth"] == "judgment"}
    for cid, txt in m.execute("SELECT case_id, text FROM chunks WHERE authority_type='judgment' AND case_id IS NOT NULL ORDER BY chunk_id"):
        if cid in jud:
            for mm in _ORIG.finditer(txt):
                jud[cid]["orig"] += [x for x, _ in cs.query_case_numbers(re.sub(r"\s+", "", mm.group(1)))]
    for n in jud.values():
        n["orig"] = sorted(set(n["orig"]))
    return nodes, links, {"stats": dict(stats), "raw_media_labels": len(raw_media_labels), "excerpts": excerpts, "media_raw": media_raw}


class DSU:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)         # 대표는 항상 작은 case_id


def pair_scores(nodes: list[dict]) -> tuple[dict, int, int]:
    """후보쌍(상투 문구 5-gram 제외 후 공유 5-gram 이 있는 쌍)의 Jaccard. 0.4 이상만 담는다. 두 번째 값은 후보쌍 수."""
    def grams(t):
        return {int(hashlib.sha1(t[i:i + SHINGLE].encode()).hexdigest()[:12], 16) for i in range(len(t) - SHINGLE + 1)}
    sets = {n["case_id"]: grams(n["text"]) for n in nodes if n["text"]}
    inv: dict[int, list[str]] = collections.defaultdict(list)
    for cid in sorted(sets):
        for g in sets[cid]:
            inv[g].append(cid)
    cand = set()
    for ids in inv.values():
        if 1 < len(ids) <= GRAM_DF_CAP:
            cand.update((ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids)))
    out = {}
    for a, b in sorted(cand):
        inter = len(sets[a] & sets[b])
        j = inter / (len(sets[a]) + len(sets[b]) - inter)
        if j >= 0.4:
            out[(a, b)] = j
    return out, len(cand), len(sets)


def similar_pairs(nodes: list[dict]) -> tuple[list[tuple[str, str]], dict]:
    scores, n_cand, n_sets = pair_scores(nodes)
    pairs = [k for k, j in scores.items() if j >= SIM_THRESHOLD]
    return pairs, {"cases_with_span_text": n_sets, "candidate_pairs": n_cand, "linked_pairs": len(pairs),
                   "near_miss_pairs_0.4_to_threshold": len(scores) - len(pairs)}


def build_groups(nodes: list[dict], links: list[tuple]) -> tuple[dict, dict]:
    by_id = {n["case_id"]: n for n in nodes}
    dsu = DSU(by_id)
    by_num: dict[str, list[str]] = collections.defaultdict(list)
    for n in nodes:
        for x in n["numbers"]:
            by_num[x].append(n["case_id"])
    docs: dict[str, list[str]] = collections.defaultdict(list)
    for n in nodes:
        docs[n["doc_id"]].append(n["case_id"])
    rule = collections.Counter()
    def u(a, b, r):
        if dsu.find(a) != dsu.find(b):
            rule[r] += 1
        dsu.union(a, b)
    for ids in by_num.values():                                  # 1) 같은 사건번호
        for x in ids[1:]:
            u(ids[0], x, "same_case_number")
    for n in nodes:                                              # 2) 심급·관련
        for x in n["history"]:
            for t in by_num.get(x, []):
                u(n["case_id"], t, "appeal_history")
        for x in n.get("orig", []):
            for t in by_num.get(x, []):
                u(n["case_id"], t, "judgment_original_citation")
    for case_id, rel, num, target_doc, status in links:
        if case_id not in by_id:
            continue
        targets = docs.get(target_doc, []) if status == "linked" and target_doc else []
        targets += by_num.get(*norm_numbers(num)[:1], []) if norm_numbers(num) else []
        for t in targets:
            u(case_id, t, "case_links")
    pairs, sim_stats = similar_pairs(nodes)                      # 3) 같은 원기사
    for a, b in pairs:
        u(a, b, "same_source_article")
    return {cid: dsu.find(cid) for cid in by_id}, {"unions_by_rule": dict(rule), "similarity": sim_stats}


def bucket(gid: str) -> float:
    return int(hashlib.sha256(f"{SEED}|{gid}".encode()).hexdigest()[:8], 16) / 2 ** 32


def assign_splits(groups: dict[str, dict]) -> dict:
    media_groups: dict[str, int] = collections.Counter()
    for g in groups.values():
        for k in g["media"]:
            media_groups[k] += 1
    edges, acc = {}, 0.0
    for name, f in FRACTIONS:
        acc += f
        edges[name] = acc
    for gid in sorted(groups):
        g = groups[gid]
        b = bucket(gid)
        sp = next(name for name, e in edges.items() if b < e or name == "tune")
        if sp == "test" and any(media_groups[k] > MEDIA_TEST_MAX_GROUPS for k in g["media"]):
            sp = "dev"; g["demoted_from_test"] = True
        g["split"], g["media_conflict"] = sp, False
    test_media = {k for g in groups.values() if g["split"] == "test" for k in g["media"]}
    for gid in sorted(groups):
        g = groups[gid]
        if g["split"] in ("tune", "dev") and set(g["media"]) & test_media:
            g["split"], g["media_conflict"] = "reserve", True
    return {"test_media_keys": len(test_media), "media_conflict_groups": sum(g["media_conflict"] for g in groups.values()),
            "demoted_from_test": sum(bool(g.get("demoted_from_test")) for g in groups.values())}


def quarter(d: str) -> str:
    return f"{d[:4]}Q{(int(d[5:7]) - 1) // 3 + 1}"


def t_table(groups: dict[str, dict], precise_only: bool) -> dict:
    dated = [g["ref_date"] for g in groups.values() if g["ref_date"] and (not precise_only or g["ref_basis"] != "year_upper_bound")]
    rows = []
    for q in sorted({quarter(d) for d in dated}):
        start = f"{q[:4]}-{(int(q[5]) - 1) * 3 + 1:02d}-01"
        n = sum(d >= start for d in dated)
        rows.append({"quarter_start": start, "groups_from_then": n, "share": round(n / len(dated), 4)})
    ok = [r for r in rows if r["share"] >= 0.15]
    return {"dated_groups": len(dated), "recommended_quarter_start_rule_15pct": ok[-1]["quarter_start"] if ok else None, "recommended_row": ok[-1] if ok else None,
            "rows_recent": rows[-12:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--index", default=None)
    ap.add_argument("--local-out", default=None, help="그룹 멤버 매핑 등 큰 산출물 폴더(기본: <data>/eval)")
    ap.add_argument("--summary-out", default=str(HERE / "split" / "split_v1_summary.json"))
    ap.add_argument("--aliases", default=str(HERE / "split" / "media_aliases.json"))
    a = ap.parse_args(argv)
    dd = data_dir()
    manifest = Path(a.manifest) if a.manifest else dd / "corpus" / "manifest.sqlite"
    od = cs.index_dir()
    index = Path(a.index) if a.index else od / (od / cs.CURRENT_POINTER).read_text(encoding="utf-8").strip()
    local = Path(a.local_out) if a.local_out else dd / "eval"
    aliases_doc = json.loads(Path(a.aliases).read_text(encoding="utf-8")) if Path(a.aliases).exists() else {"aliases": {}}
    aliases = aliases_doc["aliases"]

    m = open_manifest(manifest)
    ix = open_index(index)
    try:
        queried_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        snapshot = dict(ix.execute("SELECT key, value FROM meta WHERE key IN ('snapshot_id','passage_count')"))
        nodes, links, load_info = load_nodes(m, aliases)
        counts = {"documents": m.execute("SELECT count(*) FROM documents").fetchone()[0], "chunks": m.execute("SELECT count(*) FROM chunks").fetchone()[0],
                  "cases_updated_max": m.execute("SELECT max(updated_at) FROM cases").fetchone()[0], "case_links": len(links)}
        chunk_of: dict[str, list[str]] = collections.defaultdict(list)
        for cid, pid in m.execute("SELECT case_id, chunk_id FROM chunks WHERE case_id IS NOT NULL ORDER BY chunk_id"):
            chunk_of[cid].append(pid)
        idx_ref = {pid: (rd, basis) for pid, rd, basis in ix.execute("SELECT passage_id, reference_date, reference_date_basis FROM passages WHERE is_fixture=0")}
        idx_total = len(idx_ref)
        idx_null = sum(v[0] is None for v in idx_ref.values())
    finally:
        m.rollback(); m.close(); ix.close()

    root, group_info = build_groups(nodes, links)
    by_id = {n["case_id"]: n for n in nodes}
    members: dict[str, list[str]] = collections.defaultdict(list)
    for cid, r in root.items():
        members[r].append(cid)
    groups = {}
    for r, cids in members.items():
        cids.sort()
        gid = "G-" + hashlib.sha1(cids[0].encode()).hexdigest()[:12]
        pids = sorted({p for c in cids for p in chunk_of.get(c, []) if p in idx_ref})
        refs = [(idx_ref[p][0], idx_ref[p][1]) for p in pids if idx_ref[p][0]]
        ref = max(refs) if refs else (None, None)
        precise = [r for r in refs if r[1] != "year_upper_bound"]          # 연도 상한만 있는 기록은 미래 구간 판정에서 뺀다
        future = (max(precise)[0] >= p4_metrics.FUTURE_T) if precise else None      # None = 판정 불가(date_unknown·연도 상한뿐)
        types = collections.Counter(by_id[c]["auth"] for c in cids)
        groups[gid] = {"cases": cids, "case_numbers": sorted({x for c in cids for x in by_id[c]["numbers"]}), "passage_ids": pids,
                       "primary_type": sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))[0][0], "mixed_types": len(types) > 1,
                       "media": sorted({k for c in cids for k in by_id[c]["media"]}), "ref_date": ref[0], "ref_basis": ref[1], "future_window": future}
    split_info = assign_splits(groups)

    # 입력 digest: 노드(파생 필드)·링크·색인 snapshot·별칭·매개변수
    h = hashlib.sha256()
    h.update(json.dumps([sorted(nodes, key=lambda n: n["case_id"]), links, snapshot.get("snapshot_id"), aliases, [SEED, SIM_THRESHOLD, FRACTIONS, MEDIA_TEST_MAX_GROUPS]],
                        ensure_ascii=False, sort_keys=True, default=str).encode())
    digest = h.hexdigest()

    split_names = [n for n, _ in FRACTIONS]
    by_split_type = {s: collections.Counter(g["primary_type"] for g in groups.values() if g["split"] == s) for s in split_names}
    by_split_year = {s: collections.Counter((g["ref_date"][:4] if g["ref_date"] else "undated") for g in groups.values() if g["split"] == s) for s in split_names}
    by_quarter = collections.Counter(quarter(g["ref_date"]) + ("*" if g["ref_basis"] == "year_upper_bound" else "") for g in groups.values() if g["ref_date"])
    n_cases = len(nodes)
    summary = {
        "what": "P4 V1 사건 그룹·분할 산출 요약. 법률 판단이나 성능 결과가 아니다. 사건번호·매체명은 담지 않는다(로컬 파일에 있다).",
        "input_snapshot": {"index_snapshot_id": snapshot.get("snapshot_id"), "index_passages": int(snapshot.get("passage_count", 0)), "manifest_queried_at_utc": queried_at,
                           "cases_all": load_info["stats"]["cases_all"], "cases_included": n_cases, "excluded_statuses": list(EXCLUDED_STATUS), **counts,
                           "input_digest": digest[:16], "note": "수신이 진행 중이라 입력이 늘면 digest 와 그룹이 바뀐다. 같은 digest 면 같은 결과다."},
        "parameters": {"seed": SEED, "fractions": dict(FRACTIONS), "sim_threshold": SIM_THRESHOLD, "shingle": SHINGLE, "min_span_chars": MIN_SPAN_CHARS,
                       "gram_df_cap": GRAM_DF_CAP, "media_test_max_groups": MEDIA_TEST_MAX_GROUPS, "aliases_need_human_confirmation": aliases_doc.get("needs_human_confirmation", True)},
        "groups": {"total": len(groups), "mixed_type_groups": sum(g["mixed_types"] for g in groups.values()),
                   "singleton_groups": sum(len(g["cases"]) == 1 for g in groups.values()), "max_group_cases": max(len(g["cases"]) for g in groups.values()),
                   **group_info, **split_info},
        "by_split": {s: {"groups": sum(g["split"] == s for g in groups.values()), "cases": sum(len(g["cases"]) for g in groups.values() if g["split"] == s)} for s in split_names},
        "by_split_type_groups": {s: dict(sorted(c.items())) for s, c in by_split_type.items()},
        "by_split_year_groups": {s: dict(sorted(c.items())) for s, c in by_split_year.items()},
        "by_quarter_groups": dict(sorted(by_quarter.items())),
        "by_quarter_note": "'*' 는 reference_date_basis 가 year_upper_bound(연도 12월 31일로 둔 상한)인 그룹이다.",
        "recheck_vs_doc": {
            "cases_decision_date_null": {"value": sum(n["decision_date"] is None for n in nodes), "of": n_cases, "doc": "9,241 / 13,343"},
            "index_reference_date_null": {"value": idx_null, "of": idx_total, "doc": "7,718 / 76,066"},
            "cases_with_media_outlet_role": {"value": sum(n["has_media_role"] for n in nodes), "of": n_cases, "doc": "1,768 (13%)"},
            "distinct_raw_media_labels": {"value": load_info["raw_media_labels"], "doc": 861},
            "groups_with_media_key": sum(bool(g["media"]) for g in groups.values()),
            "cases_in_groups_without_ref_date": sum(len(g["cases"]) for g in groups.values() if not g["ref_date"])},
        "future_window": {"T": p4_metrics.FUTURE_T, "as_of_for_future_eval": p4_metrics.FUTURE_AS_OF,
                          "decision": "사용자 결정 2026-10-10: T=2018-10-01(연도 상한만 있는 그룹 제외 기준). 그룹에 T 이후(연도 상한 아닌) 기록이 하나라도 있으면 미래 구간이다.",
                          "future_groups": sum(g["future_window"] is True for g in groups.values()),
                          "dated_precise_groups": sum(g["future_window"] is not None for g in groups.values()),
                          "undetermined_groups": sum(g["future_window"] is None for g in groups.values()),
                          "by_split_future_groups": {s_: sum(g["future_window"] is True and g["split"] == s_ for g in groups.values()) for s_ in split_names},
                          "note": "분할은 T 이전에 정해진 해시 배정 그대로이며 미래 그룹을 test 로 옮기지 않았다. 미래 구간 평가는 as_of 고정과 하네스 필터(p4_metrics.future_window_filter)로 한다."},
        "future_window_T": {"decision": "분포표(T 결정의 입력). 규칙: 그룹 단위로 T 이후 그룹이 15% 이상인 가장 늦은 분기.",
                            "all_dated_groups": t_table(groups, False), "precise_basis_only_groups": t_table(groups, True)},
    }
    Path(a.summary_out).write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    local.mkdir(parents=True, exist_ok=True)
    (local / "split_v1_members.json").write_text(json.dumps(
        {gid: {"case_numbers": g["case_numbers"], "source_ids": [], "passage_ids": g["passage_ids"], "split": g["split"], "future_window": g["future_window"]} for gid, g in sorted(groups.items())},
        ensure_ascii=False) + "\n", encoding="utf-8")
    (local / "split_v1_assignment.json").write_text(json.dumps(
        {"input_digest": digest[:16], "cases": {c: {"group": gid, "split": g["split"], "future_window": g["future_window"]} for gid, g in sorted(groups.items()) for c in g["cases"]}}, ensure_ascii=False) + "\n", encoding="utf-8")
    mk = collections.Counter(k for g in groups.values() for k in g["media"])
    (local / "split_v1_media_keys.json").write_text(json.dumps(dict(mk.most_common()), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"summary": a.summary_out, "local": str(local), "digest": digest[:16], "groups": len(groups)}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
