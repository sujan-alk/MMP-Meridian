"""
Time and rolling-window utilities.
"""

from __future__ import annotations

import time
from collections import deque
from typing import TypeVar

T = TypeVar("T")


def now_ms() -> int:
    """Current time as Unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def now_s() -> float:
    """Current time as Unix timestamp in seconds."""
    return time.time()


class RollingWindow:
    """
    A fixed-size rolling window of (timestamp_s, value) tuples.
    Automatically evicts entries older than max_age_s.
    """

    def __init__(self, max_size: int = 1000, max_age_s: float | None = None):
        self._data: deque[tuple[float, float]] = deque(maxlen=max_size)
        self._max_age_s = max_age_s

    def append(self, value: float, ts: float | None = None) -> None:
        self._data.append((ts if ts is not None else now_s(), value))
        if self._max_age_s:
            self._evict()

    def _evict(self) -> None:
        cutoff = now_s() - self._max_age_s  # type: ignore[operator]
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def values(self) -> list[float]:
        if self._max_age_s:
            self._evict()
        return [v for _, v in self._data]

    def __len__(self) -> int:
        return len(self._data)

    def is_ready(self, min_size: int = 2) -> bool:
        return len(self) >= min_size
