"""번들 색인 만들기(개발자 전용): 활성 색인에서 재배포 가능한 passage 만 담은 배포용 색인과 판본 정보를 만든다.

실행: ``PYTHONPATH=plugins/press-check/engine:devtools/engine python3 -m press_review.bundle_build --index <색인 파일 또는 <data>/index> --out <새 폴더>
[--exclude-docs 제외문서목록.txt]``

- 입력 색인은 읽기 전용(``mode=ro``)으로만 열고 ``CURRENT`` 와 운영 색인 폴더를 바꾸지 않는다. 출력 폴더에 ``CURRENT`` 가 있으면 거부한다.
- 담는 것: ``passages.redistributable=1`` 이고 ``passages.source_id`` 가 제외 목록에 없는 passage 와 그 보조 표 행, 그 passage 를 가리키는 별칭.
  제외 목록(UDEC-26: 자연인 실명이 남은 문서를 전수 검수해 뺀다): 한 줄에 문서 id 하나(``passages.source_id``, 예: ``lawgo_prec:900001``), ``#`` 줄은 주석.
  실명을 치환하지 않는다(치환하면 인용 원문 일치 검증이 깨진다). 문서 단위로 통째로 뺀다.
- 새 파일에 행을 복사하므로 제외한 본문이 빈 페이지·FTS 조각에 남지 않는다. snapshot_id·passage_count·날짜 범위·stats_json 은 담은 행으로 다시 센다.
  ``cases_by_authority_type`` 은 색인에 사건 키가 없어 담은 passage 의 서로 다른 사건번호 수로 센다(사건번호 없는 사건은 빠지는 하한).
- 출력: ``corpus-<snapshot>.sqlite``, ``corpus-<snapshot>.sqlite.sha256``, ``bundle-info.json``. 번들은 Git 이력 밖의 릴리스 자산으로 올린다(UDEC-26).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import corpus_sqlite as cs

BUNDLE_FORMAT = 1
_SIDE_TABLES = ("passage_risk", "case_numbers", "cites", "bigram_terms")
CASES_METHOD = "distinct case_number among kept passages (lower bound)"


class BundleBuildError(Exception):
    pass


def read_exclusions(path: Path | None) -> list[str]:
    if path is None:
        return []
    ids: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            ids.append(s)
    return list(dict.fromkeys(ids))


def _resolve_source(index: Path) -> Path:
    index = Path(index)
    if index.is_dir():
        c = cs.open_current(index)
        if c is None:
            raise BundleBuildError("index folder has no index")
        p = c._path
        c.close()
        return p
    return index


def build_bundle(index: Path, out_dir: Path, *, exclude_docs: list[str] | None = None, now: _dt.datetime | None = None) -> dict:
    src = _resolve_source(index)
    out_dir = Path(out_dir)
    if (out_dir / cs.CURRENT_POINTER).exists():
        raise BundleBuildError("out dir has a CURRENT pointer (refusing to write into an index folder)")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    excluded_ids = list(exclude_docs or [])
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"bundle-build-{os.getpid()}.tmp"
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp.resolve().as_uri() + "?mode=rwc", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (src.resolve().as_uri() + "?mode=ro",))
        smeta = {k: v for k, v in conn.execute("SELECT key, value FROM src.meta")}
        if smeta.get("format") != cs.INDEX_FORMAT:
            raise BundleBuildError(f"source index format {smeta.get('format')!r} is not {cs.INDEX_FORMAT} (rebuild it first)")
        use_fts = smeta.get("fts_trigram") == "1"
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        conn.executescript(cs._INDEX_SCHEMA)
        if use_fts:
            conn.executescript(cs._FTS_SCHEMA)
        conn.execute("CREATE TEMP TABLE excl(source_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO excl VALUES (?)", [(i,) for i in excluded_ids])
        total = conn.execute("SELECT COUNT(*) FROM src.passages").fetchone()[0]
        not_redis = conn.execute("SELECT COUNT(*) FROM src.passages WHERE redistributable=0").fetchone()[0]
        conn.execute("CREATE TEMP TABLE kept(rid INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO kept SELECT rid FROM src.passages WHERE redistributable=1 AND source_id NOT IN (SELECT source_id FROM excl)")
        matched = {r[0] for r in conn.execute("SELECT DISTINCT p.source_id FROM src.passages p JOIN excl e ON e.source_id=p.source_id")}
        dropped_excluded = conn.execute(
            "SELECT COUNT(*) FROM src.passages p JOIN excl e ON e.source_id=p.source_id WHERE p.redistributable=1").fetchone()[0]
        conn.execute("INSERT INTO main.passages SELECT * FROM src.passages WHERE rid IN (SELECT rid FROM kept)")
        for t in _SIDE_TABLES:
            conn.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t} WHERE rid IN (SELECT rid FROM kept)")
        if use_fts:
            conn.execute("INSERT INTO main.passages_fts(rowid, text, tags) SELECT rowid, text, tags FROM src.passages_fts WHERE rowid IN (SELECT rid FROM kept)")
        conn.execute("INSERT INTO main.passage_alias SELECT * FROM src.passage_alias a WHERE a.source_id NOT IN (SELECT source_id FROM excl) "
                     "AND a.passage_id IN (SELECT passage_id FROM main.passages)")
        n = conn.execute("SELECT COUNT(*) FROM main.passages").fetchone()[0]
        if n == 0:
            raise BundleBuildError("no redistributable passages to bundle")
        rows = conn.execute("SELECT * FROM main.passages ORDER BY passage_id").fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA main.table_info(passages)")]
        ix = {c: i for i, c in enumerate(cols)}
        by_auth: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        by_family: dict[str, int] = {}
        docs: dict[str, set] = {}
        cases: dict[str, set] = {}
        dates: list[str] = []
        undated = 0
        h = hashlib.sha256()
        h.update(f"{cs.INDEX_FORMAT}\nbundle-{BUNDLE_FORMAT}\n".encode())
        for r in rows:
            h.update(json.dumps([r[ix[c]] for c in cols if c != "rid"], ensure_ascii=False).encode("utf-8") + b"\n")
            a = r[ix["authority_type"]]
            by_auth[a] = by_auth.get(a, 0) + 1
            by_status[r[ix["verification_status"]]] = by_status.get(r[ix["verification_status"]], 0) + 1
            by_kind[r[ix["chunk_kind"]]] = by_kind.get(r[ix["chunk_kind"]], 0) + 1
            fam = r[ix["source_family"]]
            by_family[fam] = by_family.get(fam, 0) + 1
            docs.setdefault(fam, set()).add(r[ix["source_id"]])
            if r[ix["case_number"]]:
                cases.setdefault(a, set()).add(r[ix["case_number"]])
            if r[ix["reference_date"]] is None:
                undated += 1
            if r[ix["decision_date"]]:
                dates.append(r[ix["decision_date"]])
        for r in conn.execute("SELECT * FROM main.passage_alias ORDER BY alias_id, source_id"):
            h.update(b"alias\n" + json.dumps(list(r), ensure_ascii=False).encode("utf-8") + b"\n")
        n_alias = conn.execute("SELECT COUNT(*) FROM main.passage_alias").fetchone()[0]
        snapshot = cs.SNAPSHOT_PREFIX + h.hexdigest()[:cs.SNAPSHOT_HEX_LEN]
        cases_by_auth = {k: len(v) for k, v in sorted(cases.items())}
        stats = {"by_authority_type": dict(sorted(by_auth.items())), "by_verification_status": dict(sorted(by_status.items())),
                 "by_chunk_kind": dict(sorted(by_kind.items())), "aliases": n_alias, "cases_by_authority_type": cases_by_auth,
                 "date_unknown_passages": undated, "by_source_family": dict(sorted(by_family.items())),
                 "sources": {f: {"documents": len(d), "documents_with_passages": len(d), "passages": by_family[f], "documents_by_status": {},
                                 "redistribute_raw": "allowed", "redistribute_derivatives": "allowed"} for f, d in sorted(docs.items())},
                 "excluded": {"not_redistributable": not_redis, "excluded_document": dropped_excluded}}
        meta = {
            "format": cs.INDEX_FORMAT, "ne_stem_rules": smeta.get("ne_stem_rules", ""), "case_number_rules": smeta.get("case_number_rules", ""),
            "snapshot_id": snapshot, "built_at": now.isoformat(timespec="seconds"), "passage_count": str(n),
            "fts_trigram": "1" if use_fts else "0", "latest_decision_date": max(dates) if dates else "",
            "earliest_decision_date": min(dates) if dates else "", "display_limit_chars": smeta.get("display_limit_chars", str(cs.DISPLAY_QUOTE_MAX_CP)),
            "stats_json": json.dumps(stats, ensure_ascii=False, sort_keys=True),
            "bundle_format": str(BUNDLE_FORMAT), "bundle_source_snapshot": smeta.get("snapshot_id", ""), "bundle_cases_method": CASES_METHOD,
        }
        conn.executemany("INSERT INTO main.meta(key, value) VALUES (?,?)", sorted(meta.items()))
        conn.commit()
        conn.execute("DETACH DATABASE src")
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.chmod(tmp, 0o600)
    name = f"corpus-{snapshot}.sqlite"
    target = out_dir / name
    os.replace(tmp, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out_dir / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
    info = {"bundle_format": BUNDLE_FORMAT, "index_format": cs.INDEX_FORMAT, "snapshot_id": snapshot, "file": name, "sha256": digest,
            "size_bytes": target.stat().st_size, "passage_count": n, "built_at": meta["built_at"], "source_snapshot_id": meta["bundle_source_snapshot"],
            "latest_decision_date": meta["latest_decision_date"] or None, "by_authority_type": stats["by_authority_type"],
            "by_source_family": stats["by_source_family"], "cases_by_authority_type": cases_by_auth, "cases_count_method": CASES_METHOD,
            "dropped_not_redistributable": not_redis, "excluded_documents_listed": len(excluded_ids),
            "excluded_documents_matched": len(matched), "dropped_passages_of_excluded_documents": dropped_excluded}
    (out_dir / "bundle-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**info, "path": str(target), "unmatched_exclusions": sorted(set(excluded_ids) - matched)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="press_review.bundle_build", description="재배포 가능 passage 만 담은 번들 색인 만들기(개발자 전용)")
    ap.add_argument("--index", required=True, help="색인 파일(corpus-idx-*.sqlite) 또는 색인 폴더(CURRENT 를 읽기만 함)")
    ap.add_argument("--out", required=True, help="출력 폴더(색인 폴더가 아니어야 함)")
    ap.add_argument("--exclude-docs", help="제외할 문서 id 목록 파일(한 줄 하나, # 주석)")
    a = ap.parse_args(argv)
    try:
        res = build_bundle(Path(a.index), Path(a.out), exclude_docs=read_exclusions(Path(a.exclude_docs) if a.exclude_docs else None))
    except (BundleBuildError, OSError, sqlite3.Error) as e:
        sys.stderr.write(f"번들 생성 실패: {e}\n")
        return 1
    sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
