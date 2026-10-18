"""검색 색인 빌더 어댑터(P3_DESIGN §1): ``python -m press_review.ingestion build_index [--keep N] [--dry-run]``.

얇은 진입점이다. 구현은 ``press_review/corpus_sqlite.py``. 수집 장부는 읽기만 하고, 색인은 ``<data>/index/`` 에 쓴다
(``ctx.corpus_dir`` 의 부모 아래). 네트워크·비밀값·외부 API 를 쓰지 않는다. 시간 상한에 걸리면 부분 색인을 발행하지 않는다.
"""
from __future__ import annotations

from ... import corpus_sqlite


def add_arguments(parser) -> None:
    parser.add_argument("--keep", type=int, default=corpus_sqlite.KEEP_INDEXES, help="남길 색인 파일 수(현재 색인 포함, 기본 3)")


def run(ctx, args) -> dict:
    return corpus_sqlite.build_index(
        ctx.corpus_dir / "manifest.sqlite", ctx.corpus_dir.parent / corpus_sqlite.INDEX_DIR_NAME,
        now=ctx.now().astimezone(), check=ctx.check_time, keep=args.keep, dry_run=ctx.dry_run,
        force_disk=bool(ctx.extras.get("force_disk")), on_force=ctx.extras.get("on_force_disk"))
