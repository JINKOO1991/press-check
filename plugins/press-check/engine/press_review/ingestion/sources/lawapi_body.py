"""법제처 판례 본문 수집 CLI 진입점: ``python -m press_review.ingestion lawapi_body --tier T1 --limit N --max-seconds S``.

로직은 ``lawapi.py`` 의 '본문' 절(``run_body``)에 있다. 어댑터 이름 규칙(``^[a-z][a-z0-9_]*$``) 때문에 하이픈 이름(lawapi-body)은 쓸 수 없다.
"""
from __future__ import annotations

from . import lawapi


def add_arguments(parser) -> None:
    lawapi.add_body_arguments(parser)


def run(ctx, args) -> dict:
    return lawapi.run_body(ctx, args)
