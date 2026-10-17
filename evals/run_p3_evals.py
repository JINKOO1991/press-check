#!/usr/bin/env python3
"""P3 평가 확장: 합성 기사 8건과 색인 passage 연결을 판정해 evals/out/eval_p3.json 에 쓴다.

배선·행동 규칙 시험용이다. 법률 정확도나 모델 성능을 재지 않는다. 네트워크와 실제 비밀값을 쓰지 않는다.
사용: python evals/run_p3_evals.py [--outputs-dir DIR] [--data-dir DIR] [--now RFC3339] [--out-dir DIR] [--strict]
- 코퍼스: 데이터 디렉터리(기본 config.data_dir())의 검색 색인. 색인이 없거나 연결 passage 가 빠졌으면 그 시나리오는 pending_corpus.
- 출력: 기본은 evals/p3_golden/ 의 합성 기준 출력(규칙 자체 점검용). --outputs-dir 에 <id>.json({findings, explanations}) 을 두면 그것을 판정한다.
종료 코드: 0 실행 완료, 2 사용 오류. --strict 면 fail 이 있을 때 1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalkit  # noqa: E402
import p3_kit  # noqa: E402
import p4_metrics  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outputs-dir", default=str(evalkit.HERE / "p3_golden"))
    ap.add_argument("--data-dir", default=None, help="검색 색인이 있는 데이터 디렉터리(기본: PRESS_DISPUTE_DATA_DIR 또는 사용자 데이터 위치)")
    ap.add_argument("--now", default=evalkit.DEFAULT_NOW)
    ap.add_argument("--out-dir", default=str(evalkit.HERE / "out"))
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--scenarios", default=None, help="시나리오 파일(기본 p3_scenarios.json). 스키마 v2 시나리오도 같은 러너로 돈다")
    ap.add_argument("--out-name", default="eval_p3.json")
    ap.add_argument("--host-model", default=None, help="평가 대상 호스트 모델 ID(실행마다 고정·기록, P4_DESIGN §10-8)")
    ap.add_argument("--host-effort", default=None)
    ap.add_argument("--host-skill-version", default=None)
    ap.add_argument("--require-host", action="store_true", help="호스트 모델·effort 를 안 주면 사용 오류(종료 코드 2). 호스트 출력(--outputs-dir)을 판정할 때 쓴다")
    ap.add_argument("--group-members", default=None, help="출처 그룹 id -> {case_numbers, source_ids, passage_ids} JSON(로컬 전용, V1 산출). 자기 사건 근거를 차단한다")
    a = ap.parse_args(argv)
    if a.data_dir:
        os.environ["PRESS_DISPUTE_DATA_DIR"] = a.data_dir
    try:
        from press_review import corpus as corpus_mod
        corpus = corpus_mod.load_default()
        host = p4_metrics.host_record(a.host_model, a.host_effort, a.host_skill_version)
        if a.require_host and not host["pinned"]:
            sys.stderr.write("--require-host: --host-model and --host-effort are required\n")
            return 2
        members = json.loads(Path(a.group_members).read_text(encoding="utf-8")) if a.group_members else None
        scen = p3_kit.load_scenarios(Path(a.scenarios) if a.scenarios else None)
        errs = p3_kit.validate_scenarios(scen)
        if errs:
            sys.stderr.write("scenario file problems: " + "; ".join(errs) + "\n")
            return 2
        outs = {sc["id"]: json.loads((Path(a.outputs_dir) / f"{sc['id']}.json").read_text(encoding="utf-8")) for sc in scen["cases"]}
        result = p3_kit.run_p3(corpus, scen, outputs=outs, now_s=a.now, host=host, group_members=members)
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 2
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / a.out_name
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for r in result["cases"]:
        bad = [x["name"] for x in r["checks"] if x["status"] == "fail"]
        extra = f"missing={len(r.get('missing_passages', []))}" if r["status"] == p3_kit.PENDING else (f"fail: {','.join(bad)}" if bad else "")
        sys.stdout.write(f"{r['status']:14} {r['case_id']:38} findings={r['finding_count']} {extra}\n")
    c = result["counts"]
    sys.stdout.write("derivation: " + " ".join(f"{k}={v}" for k, v in result["p4_summary"]["by_derivation"].items()) + "\n")
    sys.stdout.write(f"snapshot={result['meta']['corpus_snapshot_id']} pass={c['pass']} fail={c['fail']} pending_corpus={c['pending_corpus']} -> {path}\n")
    return 1 if a.strict and c["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
