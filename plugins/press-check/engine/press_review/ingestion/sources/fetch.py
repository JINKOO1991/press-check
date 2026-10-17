"""다운로드 단계 어댑터(P2_DESIGN §1): ``python -m press_review.ingestion fetch [--source ID] [--tier A,B,C] [--limit N] [--max-seconds N]``.

어댑터 계약(sources/__init__.py)의 얇은 진입점이다. 구현은 ``ingestion/fetch.py``.
"""
from __future__ import annotations

from .. import fetch as fetch_stage


def add_arguments(parser) -> None:
    parser.add_argument("--source", default=None, help="source_id 하나만 처리(예: pac_adjustment_casebooks)")
    parser.add_argument("--tier", default=None, help="처리 순위 A,B,C (쉼표로 여러 개, 기본 전부 A->B->C 순)")
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 시도할 첨부 수 상한")
    parser.add_argument("--att", action="append", dest="atts", help="att_id 하나만(반복 가능). 순위·소스 지정 없이도 고른다(게이트 표본용)")
    parser.add_argument("--retry-exhausted", action="store_true", help="재시도 상한(3회)에 걸려 건너뛰던 실패 첨부도 다시 시도")


def run(ctx, args) -> dict:
    return fetch_stage.run_fetch(ctx, source=args.source, tier=args.tier, limit=args.limit, retry_exhausted=args.retry_exhausted,
                                 att_ids=args.atts)
