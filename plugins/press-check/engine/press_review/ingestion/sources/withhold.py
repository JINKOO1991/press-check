"""보류 일괄 표시 어댑터(P2_DESIGN G-D8): ``python -m press_review.ingestion withhold [--reason viewer_shell] [--source ID] [--dry-run]``.

네트워크를 쓰지 않는다. 어댑터 계약(sources/__init__.py)의 얇은 진입점이고 구현은 ``ingestion/withhold.py``.
``--dry-run`` 은 장부를 바꾸지 않고 대상 수(``would_withhold``)만 센다.
"""
from __future__ import annotations

from .. import withhold as withhold_stage


def add_arguments(parser) -> None:
    parser.add_argument("--reason", default=withhold_stage.VIEWER_SHELL, choices=list(withhold_stage.REASONS), help="보류 사유(기본 viewer_shell)")
    parser.add_argument("--source", default=None, help="source_id 하나만 처리")


def run(ctx, args) -> dict:
    return withhold_stage.run_withhold(ctx.manifest, reason=args.reason, source=args.source, dry_run=ctx.dry_run)
