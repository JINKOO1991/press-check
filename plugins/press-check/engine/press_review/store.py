"""세션 메모리 보관소. 디스크에 아무것도 쓰지 않는다(파일·sqlite·pickle·임시파일 없음).

키는 호출자가 정한다(예: ``article:<handle>``, ``report:<report_id>``). 값은 만료 시각과 함께 dict에만 있다.
시계는 주입할 수 있다(테스트용). 기본은 ``time.monotonic``.

``on_expire(key)`` 를 주면 항목이 만료·삭제·비움으로 사라질 때마다 (락 밖에서) 호출한다. 원고에 딸린 로그 색인 같은
파생 사본을 함께 지우는 데 쓴다. 콜백이 예외를 내도 보관소 동작은 영향을 받지 않는다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import config


class EphemeralStore:
    def __init__(
        self,
        ttl_seconds: float = config.DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[str], None] | None = None,
        max_items: int | None = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_items is not None and max_items <= 0:
            raise ValueError("max_items must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._clock = clock
        self._on_expire = on_expire
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, Any]] = {}

    def _notify(self, keys: list[str]) -> None:
        if self._on_expire is None:
            return
        for k in keys:
            try:
                self._on_expire(k)
            except Exception:  # 파생 사본 정리 실패가 보관소를 깨뜨리지 않게 한다
                pass

    def put(self, key: str, value: Any, ttl_seconds: float | None = None) -> float:
        """저장하고 만료 시각(시계 기준)을 돌려준다. 같은 키는 덮어쓰며 TTL을 다시 시작한다."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        with self._lock:
            dead = self._purge_locked()
            expires = self._clock() + ttl
            self._items.pop(key, None)  # 덮어쓴 키는 가장 새 항목이 된다
            self._items[key] = (expires, value)
            while self.max_items is not None and len(self._items) > self.max_items:
                oldest = next(iter(self._items))  # 삽입 순서상 가장 오래된 항목
                del self._items[oldest]
                dead.append(oldest)
        self._notify(dead)
        return expires

    def remaining_ttl(self, key: str) -> float | None:
        """키의 남은 보관 시간(초). 없거나 만료됐으면 None."""
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            left = item[0] - self._clock()
            return left if left > 0 else None

    def get(self, key: str, default: Any = None) -> Any:
        """만료됐으면 지우고 default."""
        expired = False
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return default
            if item[0] <= self._clock():
                del self._items[key]
                expired = True
            else:
                return item[1]
        if expired:
            self._notify([key])
        return default

    def delete(self, key: str) -> bool:
        with self._lock:
            removed = self._items.pop(key, None) is not None
        if removed:
            self._notify([key])
        return removed

    def _purge_locked(self) -> list[str]:
        now = self._clock()
        dead = [k for k, (exp, _) in self._items.items() if exp <= now]
        for k in dead:
            del self._items[k]
        return dead

    def purge_expired(self) -> int:
        """만료 항목을 지우고 지운 개수."""
        with self._lock:
            dead = self._purge_locked()
        self._notify(dead)
        return len(dead)

    def clear(self) -> None:
        with self._lock:
            keys = list(self._items)
            self._items.clear()
        self._notify(keys)

    def __len__(self) -> int:
        with self._lock:
            dead = self._purge_locked()
            n = len(self._items)
        self._notify(dead)
        return n

    def __repr__(self) -> str:  # 내용을 노출하지 않는다
        return f"<EphemeralStore items={len(self._items)} ttl={self.ttl_seconds}s>"
