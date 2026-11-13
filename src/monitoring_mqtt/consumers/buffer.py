"""Bounded buffer that hands out batches (no I/O; thread-safe).

The network thread adds events as they arrive; the delivery thread takes batches. A batch
that could not be delivered goes back to the front, in order, and is retried. When the
buffer is full the **oldest** events are dropped and counted: recent data is worth more to
a live system, and the drop shows up as sequence gaps in the stored history.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class BufferStats:
    buffered: int
    dropped: int


class BatchBuffer[T]:
    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self._items: deque[T] = deque()
        self._lock = threading.Lock()
        self._clock = clock
        self._oldest_at: float | None = None  # when the oldest waiting item arrived
        self.dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, item: T) -> None:
        with self._lock:
            if not self._items:
                self._oldest_at = self._clock()
            self._items.append(item)
            while len(self._items) > self.limit:
                self._items.popleft()
                self.dropped += 1

    def ready(self, batch_size: int, max_wait: float) -> bool:
        """A full batch is waiting, or the oldest item has waited ``max_wait`` seconds."""
        with self._lock:
            if not self._items:
                return False
            oldest = self._clock() if self._oldest_at is None else self._oldest_at
            waited = self._clock() - oldest
            return len(self._items) >= batch_size or waited >= max_wait

    def take(self, batch_size: int) -> list[T]:
        with self._lock:
            count = min(batch_size, len(self._items))
            batch = [self._items.popleft() for _ in range(count)]
            self._oldest_at = self._clock() if self._items else None
            return batch

    def give_back(self, batch: list[T]) -> None:
        """Return an undelivered batch to the front, keeping the order."""
        with self._lock:
            self._items.extendleft(reversed(batch))
            if self._oldest_at is None:
                self._oldest_at = self._clock()
            while len(self._items) > self.limit:
                self._items.popleft()
                self.dropped += 1

    def stats(self) -> BufferStats:
        with self._lock:
            return BufferStats(buffered=len(self._items), dropped=self.dropped)
