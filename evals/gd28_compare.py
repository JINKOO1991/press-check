"""G-D28 오프라인 비교(DeepSeek 재호출 없음, 네트워크 없음): 선거백서 표본의 이미 받은 창 응답 인용을 layout / raw / column-crop 텍스트에 다시 대조한다.
docs/coverage/P2_extractor_compare.md '## G-D28 비교' 의 수치를 만든다. 장부(manifest.sqlite)는 읽기 전용으로만 연다.

단계
  extract : 표본 PDF 의 비OCR 쪽마다 pdftotext -layout / pdftotext -raw / column_crop(엔진 함수 ``pdf.column_crop_text``) 텍스트를 --out 아래에 쓴다(있으면 건너뛴다).
  match   : 창 응답(structure_jobs.status=done)의 제안 인용을 모아 각 텍스트에 G-D13·G-D27 규칙으로 맞춰 본다. 쪽을 둘로 나눠 센다:
            2단 쪽(column_crop 이 실제로 자른 쪽)과 그 밖의 쪽. -layout 일치 중 V-FID-05(column_join)가 버릴 이어붙임은 따로 센다(맞는 인용이 아니라 틀린 인용이다).
            깨진 텍스트층(quality.assess 사유 broken_layer)이 걸리는 쪽 수와 '제→저' 오독 수도 첨부별로 센다.
  table   : match.json 을 마크다운 표로 낸다.

한계(문서에도 적는다): 인용을 쓴 모델은 -layout 텍스트를 봤다. 이 비교는 '인용이 어느 텍스트에서 연속 문자열인가'이므로 -layout 에 유리하게 기운다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "press-dispute-review" / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import extractor_compare as xc  # noqa: E402
from press_review.ingestion import stage  # noqa: E402
from press_review.ingestion import structure_records as sr  # noqa: E402
from press_review.ingestion.extract import pdf as pdf_mod  # noqa: E402
from press_review.ingestion.extract import quality  # noqa: E402
from press_review.ingestion.extract.tools import ToolRunner  # noqa: E402

# P2_minigate.md §7·재게이트 절의 선거백서 표본. 779 는 202쪽 중 200쪽이 OCR 이라 텍스트층 비교 대상이 아니다.
SAMPLES = ["pac_election:801#8022", "pac_election:778#7977", "pac_election:1070#7721", "pac_election:489#7997"]
OCR_SAMPLE = "pac_election:779#7982"
VARIANTS = ("layout", "raw", "crop")
JE = re.compile(r"저\s*[0-9lI↑]+\s*[조항호]")


def _job(job: tuple) -> tuple:
    out_dir, raw, n = job
    runner = ToolRunner()
    res = {}
    lay_p, raw_p, crop_p, info_p = (out_dir / f"{n:04d}.{x}" for x in ("layout.txt", "raw.txt", "crop.txt", "crop.json"))
    try:
        if not lay_p.exists():
            lay_p.write_text(pdf_mod.text_layer(runner, raw, n, True)[0], encoding="utf-8")
        if not raw_p.exists():
            raw_p.write_text(pdf_mod.text_layer(runner, raw, n, raw=True)[0], encoding="utf-8")
        if not crop_p.exists():
            layout_text = lay_p.read_text(encoding="utf-8")
            ctext, info = pdf_mod.column_crop_text(runner, raw, n, layout_text)
            crop_p.write_text(ctext if ctext is not None else layout_text, encoding="utf-8")
            info_p.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 쪽 하나의 실패는 기록만 하고 계속한다
        res["error"] = f"{type(e).__name__}:{getattr(e, 'error_class', '')}"
    return n, res


def cmd_extract(a: argparse.Namespace) -> int:
    c = xc.open_ro()
    cd = xc.corpus_dir()
    out = Path(a.out)
    jobs = []
    for att in xc.att_rows(c, SAMPLES):
        led = xc.ledger_pages(c, att["att_id"])
        d = out / xc.safe(att["att_id"])
        d.mkdir(parents=True, exist_ok=True)
        for _pid, (n, method, _sha, _tp) in sorted(led.items(), key=lambda kv: kv[1][0] or 0):
            if method == "ocr" or n is None:
                continue
            jobs.append((d, cd / att["raw_path"], n))
    errs = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for n, res in ex.map(_job, jobs):
            if res:
                errs += 1
                print("page_error", n, res, file=sys.stderr)
    print(json.dumps({"pages": len(jobs), "errors": errs}))
    return 0


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def cmd_match(a: argparse.Namespace) -> int:
    c = xc.open_ro()
    cd = xc.corpus_dir()
    base = Path(a.out)
    result = {"attachments": {}, "ocr_sample": OCR_SAMPLE}
    for att in xc.att_rows(c, SAMPLES):
        aid = att["att_id"]
        led = xc.ledger_pages(c, aid)
        d = base / xc.safe(aid)
        texts = {v: {} for v in VARIANTS}
        info = {}
        stored = {}
        ctrl = {"pages": 0, "layout_same_as_ledger": 0, "ocr_pages": 0, "two_column_cropped": 0, "crop_reasons": {}, "broken_layer_pages": 0, "je_misreads": 0}
        for pid, (n, method, sha, tp) in led.items():
            stored[pid] = stage.resolve(cd, tp).read_text(encoding="utf-8")
            if method == "ocr":
                ctrl["ocr_pages"] += 1
                for v in VARIANTS:
                    texts[v][pid] = ""
                continue
            ctrl["pages"] += 1
            for v, fn in (("layout", "layout.txt"), ("raw", "raw.txt"), ("crop", "crop.txt")):
                texts[v][pid] = _read(d / f"{n:04d}.{fn}")
            ctrl["layout_same_as_ledger"] += texts["layout"][pid] == stored[pid]
            inf = json.loads(_read(d / f"{n:04d}.crop.json") or "{}")
            info[pid] = inf
            ctrl["crop_reasons"][inf.get("reason", "?")] = ctrl["crop_reasons"].get(inf.get("reason", "?"), 0) + 1
            ctrl["two_column_cropped"] += inf.get("reason") == "cropped"
            reasons = quality.assess(texts["layout"][pid])[2]
            ctrl["broken_layer_pages"] += "broken_layer" in reasons
            ctrl["je_misreads"] += len(JE.findall(texts["layout"][pid]))
        pt = {v: {pid: sr.PageText(t) for pid, t in texts[v].items()} for v in VARIANTS}
        pt["ledger"] = {pid: sr.PageText(t) for pid, t in stored.items()}
        keys = ("proposed", "short", "long", "ocr_origin", "eligible", "ledger", *VARIANTS)
        stats = {cls: dict.fromkeys(keys, 0) for cls in ("twocol", "other")}
        extra = {cls: {"layout_joined": 0, "crop_gain_vs_layout": 0, "crop_lost_vs_layout": 0, "crop_lost_not_joined": 0, "raw_gain_vs_layout": 0, "raw_lost_vs_layout": 0}
                 for cls in ("twocol", "other")}
        jobs = c.execute("select unit, output_path from structure_jobs where target_id=? and status='done'", (aid,)).fetchall()
        for j in jobs:
            op = j["output_path"]
            if not op or not Path(op).is_file():
                continue
            doc = json.loads(Path(op).read_text(encoding="utf-8"))
            resp = doc.get("response")
            if not isinstance(resp, dict) or not isinstance(resp.get("cases"), list):
                continue
            wp = [p for p in doc.get("page_ids", []) if p in led]
            for q, pid, hint in xc.harvest(resp):
                hint = xc._pid(pid) or xc._pid(hint)
                cq = sr.collapse(re.sub(r"<br\s*/?>", " ", q))
                order = ([hint] if hint in wp else []) + [p for p in wp if p != hint]
                origin = next((p for p in order if pt["ledger"][p].find(cq)), None)
                twocol = origin is not None and info.get(origin, {}).get("reason") == "cropped"
                st, ex = stats["twocol" if twocol else "other"], extra["twocol" if twocol else "other"]
                st["proposed"] += 1
                if len(cq) < sr.MIN_QUOTE_CHARS:
                    st["short"] += 1
                    continue
                if len(cq) > sr.MAX_QUOTE_CHARS:
                    st["long"] += 1
                    continue
                if origin is None and hint in led and led[hint][1] == "ocr":
                    origin = hint
                if origin is not None and led[origin][1] == "ocr":
                    st["ocr_origin"] += 1
                    continue
                st["eligible"] += 1
                in_led = origin is not None
                st["ledger"] += in_led
                hits = {}
                joined = False
                for v in VARIANTS:
                    h = None
                    for p in order:
                        h = pt[v][p].find(cq)
                        if h:
                            if v == "layout" and pt["layout"][p].column_join(*h):
                                joined = True
                            break
                    hits[v] = bool(h)
                    st[v] += bool(h)
                ex["layout_joined"] += hits["layout"] and joined
                if hits["crop"] and not hits["layout"]:
                    ex["crop_gain_vs_layout"] += 1
                if hits["layout"] and not hits["crop"]:
                    ex["crop_lost_vs_layout"] += 1
                    ex["crop_lost_not_joined"] += not joined
                if hits["raw"] and not hits["layout"]:
                    ex["raw_gain_vs_layout"] += 1
                if hits["layout"] and not hits["raw"]:
                    ex["raw_lost_vs_layout"] += 1
        result["attachments"][aid] = {"source_id": att["source_id"], "pages": att["page_count"], "control": ctrl, "stats": stats, "extra": extra}
    Path(base / "match.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"attachments": len(result["attachments"]), "out": str(base / "match.json")}))
    return 0


def cmd_table(a: argparse.Namespace) -> int:
    r = json.loads((Path(a.out) / "match.json").read_text(encoding="utf-8"))
    pct = lambda n, d: f"{100 * n / d:.1f}% ({n}/{d})" if d else "-"  # noqa: E731

    def agg(cls):
        g = {k: 0 for k in ("eligible", "ledger", *VARIANTS)}
        x = {}
        for v in r["attachments"].values():
            for k in g:
                g[k] += v["stats"][cls][k]
            for k, n in v["extra"][cls].items():
                x[k] = x.get(k, 0) + n
        return g, x

    print("| 쪽 구분 | 대조 대상 | 모델이 본 텍스트(-layout) | pdftotext -layout | pdftotext -raw | column_crop | -layout 일치 중 column_join 대상 | crop 만 일치 | -layout 만 일치(crop 에서 빠짐) | 그중 이어붙임 아님 | raw 만 일치 | -layout 만 일치(raw 에서 빠짐) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cls, label in (("twocol", "2단 쪽(crop 이 자른 쪽)"), ("other", "그 밖의 쪽")):
        g, x = agg(cls)
        e = g["eligible"]
        print(f"| {label} | {e} | {pct(g['ledger'], e)} | {pct(g['layout'], e)} | {pct(g['raw'], e)} | {pct(g['crop'], e)} | {x['layout_joined']} | {x['crop_gain_vs_layout']} | "
              f"{x['crop_lost_vs_layout']} | {x['crop_lost_not_joined']} | {x['raw_gain_vs_layout']} | {x['raw_lost_vs_layout']} |")
    print()
    print("| 첨부 | 비OCR 쪽 | 자른 2단 쪽 | crop 불가 사유 | broken_layer 쪽 | '저N조' 오독 | 대조 대상 | -layout | -raw | column_crop | -layout 중 이어붙임 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for aid, v in r["attachments"].items():
        c, s, x = v["control"], v["stats"], v["extra"]
        e = s["twocol"]["eligible"] + s["other"]["eligible"]
        tot = lambda k: s["twocol"][k] + s["other"][k]  # noqa: E731
        reasons = ", ".join(f"{k} {n}" for k, n in sorted(c["crop_reasons"].items()) if k not in ("cropped", "not_two_column")) or "-"
        print(f"| `{aid}` | {c['pages']} | {c['two_column_cropped']} | {reasons} | {c['broken_layer_pages']} | {c['je_misreads']} | {e} | {pct(tot('layout'), e)} | {pct(tot('raw'), e)} | "
              f"{pct(tot('crop'), e)} | {x['twocol']['layout_joined'] + x['other']['layout_joined']} |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("extract", cmd_extract), ("match", cmd_match)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--workers", type=int, default=6)
        p.set_defaults(fn=fn)
    t = sub.add_parser("table")
    t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_table)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
