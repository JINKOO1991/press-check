"""G-D14 추출기 오프라인 비교(DeepSeek 재호출 없음). docs/coverage/P2_extractor_compare.md '## G-D14 오프라인 비교' 의 수치를 만든다.

단계
  extract : 표본 PDF 의 쪽마다 pdftotext / pdftotext -layout / kordoc(-p N) 텍스트를 --out 아래 임시 위치에 쓴다(있으면 건너뛴다, --max-seconds 로 끊고 다시 돌리면 이어받는다).
            장부(manifest.sqlite)는 읽기 전용으로만 연다. ``extract`` 서브명령은 쓰지 않는다.
  table   : match.json 을 마크다운 표로 낸다.
  match   : 창 응답 캐시(structure_jobs.status=done 의 output_path)에서 제안 인용을 모두 모아(채택·기각 구분 없이) 각 텍스트에 G-D13 규칙(공백 제거 대조)으로 맞춰 본다.
            결과 JSON 한 개(--out/match.json)와 표시용 표(stdout)를 낸다.

한계: 모델이 본 텍스트는 장부에 남은 쪽 텍스트(대부분 pdftotext -layout, iendc 는 kordoc)다. 이 비교는 '인용이 어느 텍스트에서 연속 문자열인가'를 잰 것이고
모델이 그 텍스트를 보고 쓴 인용이므로 모델이 본 추출기에 유리하게 기운다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "press-check" / "engine"))

from press_review import config  # noqa: E402
from press_review.ingestion import stage  # noqa: E402
from press_review.ingestion import structure_records as sr  # noqa: E402
from press_review.ingestion.extract import hwp as hwp_mod  # noqa: E402
from press_review.ingestion.extract import pdf as pdf_mod  # noqa: E402
from press_review.ingestion.extract.tools import ToolRunner  # noqa: E402

# P2_minigate.md 부록의 표본 중 PDF (HWP 3건 bn1307·bn1355·iendc:2105 는 kordoc 뿐이라 비교 대상이 아니다. 판례·chunk 전용·1차 게이트 표본은 제외).
SAMPLES = """
pac_cases:662#7827 pac_cases:793#7884 pac_cases:1013#7918 pac_cases:1084#8041 pac_cases:1169#8418
pac_election:801#8022 pac_election:778#7977 pac_election:779#7982 pac_election:1070#7721 pac_election:489#7997
pac_cases:1009#7952 pac_cases:1138#8287 pac_cases:1168#8410 pac_cases:1208#8571
pac_cases:bn2466#63223 pac_cases:bn2822#74045 pac_cases:bn3335#76744
pac_other:1003#6871 pac_other:700#5599 pac_other:702#5608 pac_other:703#5588 pac_other:704#5566
iendc:1949#1 iendc:2027#1 iendc:2287#1
""".split()
HWP_SAMPLES = ["pac_cases:bn1307#4246", "pac_cases:bn1355#4294", "iendc:2105#1"]
VARIANTS = ("pdftotext_layout", "pdftotext", "kordoc", "kordoc_br")
BR = re.compile(r"<br\s*/?>", re.I)


def corpus_dir() -> Path:
    return config.data_dir() / "corpus"


def open_ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{corpus_dir() / 'manifest.sqlite'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def safe(att_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", att_id)


def att_rows(c: sqlite3.Connection, ids: list) -> list:
    out = []
    for i in ids:
        r = c.execute("select a.att_id, a.raw_path, a.page_count, d.source_id, d.doc_id from attachments a join documents d using(doc_id) where a.att_id=?", (i,)).fetchone()
        if r:
            out.append(dict(r))
    return out


def ledger_pages(c: sqlite3.Connection, att_id: str) -> dict:
    """{page_id: (pdf_page_no, method, text_sha256, text_path)}"""
    return {r["page_id"]: (r["pdf_page_no"], r["method"], r["text_sha256"], r["text_path"])
            for r in c.execute("select page_id, pdf_page_no, method, text_sha256, text_path from pages where att_id=?", (att_id,))}


# ------------------------------------------------------------------ extract
def _one(job: tuple) -> tuple:
    out_dir, raw, n = job
    runner = ToolRunner()
    res = {}
    for name in ("pdftotext_layout", "pdftotext", "kordoc"):
        p = out_dir / f"{n:04d}.{name}.txt"
        if p.exists():
            continue
        try:
            if name == "kordoc":
                text = hwp_mod.extract_pdf_page(runner, raw, n).text
            else:
                text, err = pdf_mod.text_layer(runner, raw, n, layout=(name == "pdftotext_layout"))
                if err:
                    res[name] = err
                    continue
        except Exception as e:  # noqa: BLE001 - 쪽 하나의 실패는 기록만 하고 계속한다
            res[name] = f"{type(e).__name__}:{getattr(e, 'error_class', '')}"
            continue
        p.write_text(text, encoding="utf-8")
    return n, res


def cmd_extract(a: argparse.Namespace) -> int:
    c = open_ro()
    cd = corpus_dir()
    out = Path(a.out)
    jobs = []
    for att in att_rows(c, SAMPLES):
        led = ledger_pages(c, att["att_id"])
        d = out / safe(att["att_id"])
        d.mkdir(parents=True, exist_ok=True)
        raw = cd / att["raw_path"]
        for _pid, (n, method, _sha, _tp) in sorted(led.items(), key=lambda kv: kv[1][0] or 0):
            if method == "ocr" or n is None:
                continue                      # OCR 쪽은 세 추출기 모두 같은 글을 만들지 못한다
            if not all((d / f"{n:04d}.{x}.txt").exists() for x in ("pdftotext_layout", "pdftotext", "kordoc")):
                jobs.append((d, raw, n))
    t0 = time.time()
    done = errs = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        it = ex.map(_one, jobs)
        for _n, res in it:
            done += 1
            errs += bool(res)
            if res:
                print("page_error", _n, res, file=sys.stderr)
            if time.time() - t0 > a.max_seconds:
                ex.shutdown(wait=True, cancel_futures=True)
                break
    print(json.dumps({"pending_jobs": len(jobs), "done_now": done, "errors_now": errs, "stopped_by_time": time.time() - t0 > a.max_seconds}))
    return 0


# ------------------------------------------------------------------ match
def harvest(obj, hint=None) -> list:
    """응답 JSON 에서 {quote: 문자열} 객체를 모두 모은다: [(quote, page_id|None, hint_first_page|None)]."""
    out = []
    if isinstance(obj, dict):
        h = obj.get("first_page_id") if isinstance(obj.get("first_page_id"), str) else hint
        if isinstance(obj.get("quote"), str):
            out.append((obj["quote"], obj.get("page_id") if isinstance(obj.get("page_id"), str) else None, h))
        for k, v in obj.items():
            if k == "quote" and isinstance(v, str):
                continue
            out += harvest(v, h)
    elif isinstance(obj, list):
        for v in obj:
            out += harvest(v, hint)
    return out


def _pid(s):
    if not isinstance(s, str):
        return None
    m = re.search(r"<<PAGE\s+(.+?)>>", s)
    return m.group(1).strip() if m else s


def cmd_match(a: argparse.Namespace) -> int:
    c = open_ro()
    cd = corpus_dir()
    base = Path(a.out)
    result = {"attachments": {}, "hwp_na": HWP_SAMPLES}
    for att in att_rows(c, SAMPLES):
        aid = att["att_id"]
        led = ledger_pages(c, aid)
        by_n = {v[0]: pid for pid, v in led.items()}
        d = base / safe(aid)
        texts = {v: {} for v in VARIANTS}          # page_id -> text
        stored = {}
        ctrl = {"pages": 0, "layout_same": 0, "kordoc_same": 0, "ocr_pages": 0, "missing_variant_pages": 0}
        for pid, (n, method, sha, tp) in led.items():
            stored[pid] = stage.resolve(cd, tp).read_text(encoding="utf-8")
            if method == "ocr":
                ctrl["ocr_pages"] += 1
                for v in VARIANTS:
                    texts[v][pid] = ""
                continue
            ok = True
            for name in ("pdftotext_layout", "pdftotext", "kordoc"):
                p = d / f"{n:04d}.{name}.txt"
                if not p.exists():
                    ok = False
                    texts[name][pid] = ""
                else:
                    texts[name][pid] = p.read_text(encoding="utf-8")
            texts["kordoc_br"][pid] = BR.sub("\n", texts["kordoc"][pid])
            if not ok:
                ctrl["missing_variant_pages"] += 1
            ctrl["pages"] += 1
            ctrl["layout_same"] += texts["pdftotext_layout"][pid] == stored[pid]
            ctrl["kordoc_same"] += texts["kordoc"][pid] == stored[pid]
        pt = {v: {pid: sr.PageText(t) for pid, t in texts[v].items()} for v in VARIANTS}
        pt["ledger"] = {pid: sr.PageText(t) for pid, t in stored.items()}
        methods = {v[1] for v in led.values()}
        ledger_extractor = "kordoc" if methods == {"kordoc"} else ("pdftotext_layout" if "pdftotext" in methods else "ocr")
        # 응답
        jobs = c.execute("select unit, output_path, structurer_version from structure_jobs where target_id=? and status='done'", (aid,)).fetchall()
        rows = []
        files = 0
        for j in jobs:
            op = j["output_path"]
            if not op or not Path(op).is_file():
                continue
            doc = json.loads(Path(op).read_text(encoding="utf-8"))
            resp = doc.get("response")
            if not isinstance(resp, dict) or not isinstance(resp.get("cases"), list):
                continue
            files += 1
            wp = [p for p in doc.get("page_ids", []) if p in led]
            for q, pid, hint in harvest(resp):
                rows.append((wp, q, _pid(pid) or _pid(hint)))
        stats = {"files": files, "proposed": 0, "short": 0, "long": 0, "ocr_origin": 0, "eligible": 0, "ledger_match": 0, **{v: 0 for v in VARIANTS},
                 "gain_vs_ledger": {v: 0 for v in VARIANTS}, "lost_vs_ledger": {v: 0 for v in VARIANTS}}
        for wp, q, hint in rows:
            stats["proposed"] += 1
            cq = sr.collapse(q)
            if len(cq) < sr.MIN_QUOTE_CHARS:
                stats["short"] += 1
                continue
            if len(cq) > sr.MAX_QUOTE_CHARS:
                stats["long"] += 1
                continue
            order = ([hint] if hint in wp else []) + [p for p in wp if p != hint]
            origin = next((p for p in order if pt["ledger"][p].find(cq)), None)
            if origin is None and hint in led and led[hint][1] == "ocr":
                origin = hint
            if origin is not None and led[origin][1] == "ocr":
                stats["ocr_origin"] += 1
                continue
            stats["eligible"] += 1
            in_led = origin is not None
            stats["ledger_match"] += in_led
            for v in VARIANTS:
                hit = any(pt[v][p].find(cq) for p in order)
                stats[v] += hit
                if hit and not in_led:
                    stats["gain_vs_ledger"][v] += 1
                if in_led and not hit:
                    stats["lost_vs_ledger"][v] += 1
        result["attachments"][aid] = {"source_id": att["source_id"], "pages": att["page_count"], "ledger_extractor": ledger_extractor, "control": ctrl, "stats": stats}
    Path(base / "match.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"attachments": len(result["attachments"]), "out": str(base / "match.json")}))
    return 0


def cmd_table(a: argparse.Namespace) -> int:
    """match.json -> 마크다운 표(소스별 합계, 첨부별)."""
    r = json.loads((Path(a.out) / "match.json").read_text(encoding="utf-8"))
    agg: dict = {}
    keys = ("proposed", "short", "long", "ocr_origin", "eligible", "ledger_match", *VARIANTS)
    for aid, v in r["attachments"].items():
        g = agg.setdefault(v["source_id"], dict.fromkeys(keys, 0))
        for k in keys:
            g[k] += v["stats"][k]
    pct = lambda n, d: f"{100 * n / d:.1f}%" if d else "-"  # noqa: E731
    print("| 소스 | 제안 | 짧음 | OCR 쪽 인용 | 대조 대상 | 모델이 본 텍스트 | pdftotext -layout | pdftotext | kordoc | kordoc(`<br>`→개행) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for sid, g in agg.items():
        e = g["eligible"]
        print(f"| `{sid}` | {g['proposed']} | {g['short']} | {g['ocr_origin']} | {e} | {pct(g['ledger_match'], e)} ({g['ledger_match']}) | "
              + " | ".join(f"{pct(g[v], e)} ({g[v]})" for v in VARIANTS) + " |")
    print()
    print("| 첨부 | 소스 | 모델이 본 추출기 | 쪽(비OCR) | 대조 대상 | 모델이 본 텍스트 | -layout | pdftotext | kordoc | kordoc(`<br>`→개행) | OCR 쪽 인용(제외) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for aid, v in r["attachments"].items():
        s, c = v["stats"], v["control"]
        e = s["eligible"]
        print(f"| `{aid}` | {v['source_id']} | {v['ledger_extractor']} | {c['pages']} | {e} | {pct(s['ledger_match'], e)} ({s['ledger_match']}) | "
              + " | ".join(f"{pct(s[x], e)} ({s[x]})" for x in VARIANTS) + f" | {s['ocr_origin']} |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("extract", cmd_extract), ("match", cmd_match)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--max-seconds", type=float, default=500)
        p.add_argument("--workers", type=int, default=4)
        p.set_defaults(fn=fn)
    t = sub.add_parser("table")
    t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_table)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
