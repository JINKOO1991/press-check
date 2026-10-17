#!/usr/bin/env python3
"""합성 시나리오 12개를 공급자 출력에 적용해 결과 표를 evals/out/ 에 json 으로 쓴다.

배선 검증용이다. 법률 정확도나 모델 성능을 재지 않는다. 네트워크와 실제 비밀값을 쓰지 않는다.
사용: python evals/run_evals.py [--source mock|golden] [--now RFC3339] [--out-dir DIR]
종료 코드: 0 실행 완료(fail 이 있어도 결과 표를 쓰면 0), 2 사용 오류. --strict 면 fail 이 있을 때 1.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalkit  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=["mock", "golden"], default="mock",
                    help="mock: MockProvider 결과. golden: 규칙 자체 점검용 손으로 쓴 합성 기준 출력")
    ap.add_argument("--now", default=evalkit.DEFAULT_NOW)
    ap.add_argument("--out-dir", default=str(evalkit.HERE / "out"))
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    try:
        result = evalkit.run_all(a.source, a.now)
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 2
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{a.source}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    c = result["counts"]
    for r in result["cases"]:
        bad = [x["name"] for x in r["checks"] if x["status"] == "fail"]
        sys.stdout.write(f"{r['status']:6} {r['case_id']:28} findings={r['finding_count']} {('fail: ' + ','.join(bad)) if bad else ''}\n")
    sys.stdout.write(f"source={a.source} pass={c['pass']} fail={c['fail']} manual={c['manual']} -> {path}\n")
    return 1 if a.strict and c["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
