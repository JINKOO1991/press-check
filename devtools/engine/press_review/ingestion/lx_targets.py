"""법고을 수집 대상 번호 생성(결정적, 네트워크 없음): ``python -m press_review.ingestion.lx_targets --out-dir D [--index F] [--corpus C]``.

색인 passage(authority_type research·pac_decision·pac_settlement·pac_election_review·iendc_review·judgment)의 본문에서 법원 사건번호를 뽑아 그 번호를 인용한 **문서 수**
(passage 가 아니라 passages.source_id)를 센다. 색인에 없는 번호만 인용 문서 수 순으로 낸다. 헌재 번호('헌' 계열)는 사건부호 허용 목록에 없어 빠진다.
번호 분류(위에서부터 먼저 맞는 것):
  indexed        색인의 사건번호(case_numbers 표·passages.case_number)에 있다: 대상 아님
  structure_only 장부(lawgo_prec_supreme)에 본문이 이미 있다: 요청하지 않고 구조화만 필요(lx_prec 어댑터의 held_by_lawgo 와 같은 기준)
  lx_done        장부에 lx_prec 본문이 이미 있다: 대상 아님
  request        그 밖: 법고을에 요청할 대상
추출 규칙(잡음을 줄이는 쪽으로 보수적): 사건부호는 ``lx_prec.CASE_CODES`` 허용 목록, 연도와 부호·일련번호 사이에 공백 없음, 앞 글자가 숫자가 아님, 2자리 연도는 1945~1999 만,
'2000다37524, 37531' 의 뒤 번호는 앞 번호의 연도·부호를 잇되 뒤에 년월일조항호%원·마침표·숫자가 붙으면 번호로 보지 않는다. 문서가 자기 사건번호를 적은 것은 인용으로 세지 않는다.
출력(--out-dir): request_min3.txt·request_min2.txt·request_all.txt(법고을 인쇄 형식, 줄당 하나, 인용 문서 수 내림차순)·structure_only.txt·targets.json(건수와 목록).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

from .sources.lx_prec import CODE_ALT, canon_year, to_lx_form

AUTHORITY_TYPES = ("research", "pac_decision", "pac_settlement", "pac_election_review", "iendc_review", "judgment")
_MAIN = re.compile(rf"(?<!\d)(\d{{2,4}})({CODE_ALT})(\d{{1,7}})(?!\d)")
_MORE = re.compile(r"\s*[,·ㆍ]\s*(\d{1,7})(?!\d|\s*[년월일조항호%원.]|[가-힣])")


def extract_numbers(text: Any) -> list:
    """본문에서 법원 사건번호를 4자리 연도 표기로 뽑는다(순서 유지·중복 제거)."""
    s = text if isinstance(text, str) else ""
    out: list = []
    for m in _MAIN.finditer(s):
        y = canon_year(m.group(1))
        if y is None:
            continue
        prefix = y + m.group(2)
        for n in [prefix + m.group(3)] + _more_numbers(s, m.end(), prefix):
            if n not in out:
                out.append(n)
    return out


def _more_numbers(s: str, pos: int, prefix: str) -> list:
    out = []
    while True:
        m = _MORE.match(s, pos)
        if not m:
            return out
        out.append(prefix + m.group(1))
        pos = m.end()


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:" + urllib.parse.quote(str(Path(path).resolve())) + "?mode=ro", uri=True)


def _by_threshold(rows: list) -> dict:
    return {"min3": sum(1 for r in rows if r["docs"] >= 3), "min2": sum(1 for r in rows if r["docs"] >= 2), "all": len(rows)}


def build_targets(index_path, manifest, *, authority_types: tuple = AUTHORITY_TYPES) -> dict:
    """색인(읽기 전용으로 연다)과 장부(Manifest, 읽기만)에서 대상 번호를 만든다. 반환은 JSON 으로 쓸 수 있는 dict."""
    cites: dict = defaultdict(set)
    own: dict = defaultdict(set)
    indexed: set = set()
    scanned = 0
    conn = _open_ro(index_path)
    try:
        for sid, cn in conn.execute("SELECT DISTINCT source_id, case_number FROM passages WHERE case_number IS NOT NULL"):
            for n in extract_numbers(cn):
                own[sid].add(n)
                indexed.add(n)
        for (norm,) in conn.execute("SELECT DISTINCT norm FROM case_numbers"):
            indexed.update(extract_numbers(norm))
        marks = ",".join("?" * len(authority_types))
        docs = set()
        for sid, text in conn.execute(f"SELECT source_id, text FROM passages WHERE authority_type IN ({marks})", authority_types):
            docs.add(sid)
            for n in extract_numbers(text):
                cites[n].add(sid)
        scanned = len(docs)
    finally:
        conn.close()
    held: set = set()
    for r in manifest._q("SELECT json_extract(extra_json,'$.case_no') AS cn, json_extract(extra_json,'$.body.case_nos') AS bn FROM documents "
                         "WHERE source_id='lawgo_prec_supreme' AND status NOT IN ('quarantined','withheld') AND json_extract(extra_json,'$.body.text_available')=1"):
        held.update(extract_numbers(r["cn"]))
        held.update(extract_numbers(" ".join(json.loads(r["bn"]))) if r["bn"] else [])
    lx_done: set = set()
    for r in manifest._q("SELECT json_extract(extra_json,'$.case_no') AS cn FROM documents WHERE source_id='lx_prec' AND json_extract(extra_json,'$.body.text_available')=1"):
        lx_done.update(extract_numbers(r["cn"]))
    request, structure_only = [], []
    for n, ds in cites.items():
        if n in indexed:
            continue
        docs_n = len({d for d in ds if n not in own.get(d, ())})
        if docs_n == 0:
            continue
        row = {"number": n, "lx": to_lx_form(n), "docs": docs_n}
        if n in held:
            structure_only.append(row)
        elif n not in lx_done:
            request.append(row)
    order = lambda r: (-r["docs"], r["number"])
    request.sort(key=order)
    structure_only.sort(key=order)
    lx = lambda rows, k: [r["lx"] for r in rows if r["docs"] >= k]
    return {"request": request, "structure_only": structure_only,
            "lx_numbers": {"min3": lx(request, 3), "min2": lx(request, 2), "all": lx(request, 1)},
            "counts": {"request": _by_threshold(request), "structure_only": _by_threshold(structure_only), "indexed_numbers": len(indexed),
                       "citing_docs_scanned": scanned, "numbers_detected": len(cites), "lx_done": len(lx_done), "ledger_text_numbers": len(held)}}


def default_index() -> Path:
    from .. import config
    d = config.data_dir() / "index"
    return d / (d / "CURRENT").read_text(encoding="utf-8").strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m press_review.ingestion.lx_targets", description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", required=True, help="출력 폴더(새로 만든다)")
    ap.add_argument("--index", default=None, help="색인 sqlite(기본: 데이터 디렉터리의 CURRENT)")
    ap.add_argument("--corpus", default=None, help="장부 폴더(기본: 수집 코퍼스 폴더). 읽기 전용 메모리 사본으로 연다")
    a = ap.parse_args(argv)
    from .context import corpus_dir
    from .manifest import Manifest
    root = Path(a.corpus) if a.corpus else corpus_dir()
    m = Manifest(root / "manifest.sqlite", in_memory_copy=True)
    try:
        res = build_targets(Path(a.index) if a.index else default_index(), m)
    finally:
        m.close()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=False)
    for k in ("min3", "min2", "all"):
        (out / f"request_{k}.txt").write_text("".join(n + "\n" for n in res["lx_numbers"][k]), encoding="utf-8")
    (out / "structure_only.txt").write_text("".join(r["lx"] + "\n" for r in res["structure_only"]), encoding="utf-8")
    (out / "targets.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"counts": res["counts"], "out_dir": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
