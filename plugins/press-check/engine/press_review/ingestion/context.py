"""실행 컨텍스트, 시간 상한, 경로. 어댑터 계약은 ``sources/__init__.py`` 독스트링에 있다."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .. import config


class TimeBudgetExceeded(Exception):
    """--max-seconds 상한을 넘었다. 어댑터는 잡지 말고 올려 보낸다(CLI가 정상 중단으로 처리)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def data_root() -> Path:
    """수집 CLI의 데이터 루트. ``config.data_dir()`` 과 같은 함수다(경로 규칙은 그쪽 독스트링)."""
    return config.data_dir()


def corpus_dir() -> Path:
    return data_root() / "corpus"


class Budget:
    """단조 시계 기준 시간 상한. max_seconds가 None이면 무제한."""

    def __init__(self, max_seconds: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        self.max_seconds = max_seconds
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> Optional[float]:
        if self.max_seconds is None:
            return None
        return self.max_seconds - self.elapsed()

    def check(self) -> None:
        r = self.remaining()
        if r is not None and r <= 0:
            raise TimeBudgetExceeded(f"time budget of {self.max_seconds}s exhausted")


@dataclass
class RunContext:
    http: Any                      # HttpClient
    manifest: Any                  # Manifest
    budget: Budget
    logger: logging.Logger
    corpus_dir: Path
    now: Callable[[], datetime] = utcnow
    refresh: bool = False
    limit_pages: Optional[int] = None
    dry_run: bool = False
    extras: dict = field(default_factory=dict)

    @property
    def inventory_dir(self) -> Path:
        return self.corpus_dir / "inventory"

    def check_time(self) -> None:
        self.budget.check()
        g = self.extras.get("disk_guard")          # 디스크 가드(diskguard.DiskGuard): 페이지·문서 루프마다 부르는 이 지점에서 주기 검사
        if g is not None:
            g.tick()

    def time_left(self) -> Optional[float]:
        return self.budget.remaining()
