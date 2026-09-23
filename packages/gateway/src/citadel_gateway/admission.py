"""Admission: a bounded gate in front of scarce hardware (ADR-0001 §Q12, ADR-0004).

Task concurrency is unbounded; GPU admission is bounded. Workers run freely and queue
only at the model call, and the time a call spends queued is measured and handed back
so the runtime can show a waiting task as *waiting* -- "waiting on GPU" rather than
apparently hung. The capacity comes from the profile registry (`gpu_admission`), never
from a constant here.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional


class AdmissionGate:
    def __init__(self, name: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"{name} admission capacity must be at least 1, got {capacity}")
        self.name = name
        self.capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._in_use = 0
        self._waiting = 0
        self._admitted = 0
        self._total_wait_ms = 0
        self._max_wait_ms = 0

    @contextmanager
    def acquire(self, on_wait: Optional[Callable[[int], None]] = None) -> Iterator[int]:
        """Yields the milliseconds spent queued. `on_wait` is called once, with the
        queue depth, if the call cannot be admitted immediately."""
        started = time.perf_counter()
        if not self._semaphore.acquire(blocking=False):
            with self._lock:
                self._waiting += 1
                depth = self._waiting
            if on_wait is not None:
                on_wait(depth)
            try:
                self._semaphore.acquire()
            finally:
                with self._lock:
                    self._waiting -= 1
        wait_ms = int((time.perf_counter() - started) * 1000)
        with self._lock:
            self._in_use += 1
            self._admitted += 1
            self._total_wait_ms += wait_ms
            self._max_wait_ms = max(self._max_wait_ms, wait_ms)
        try:
            yield wait_ms
        finally:
            with self._lock:
                self._in_use -= 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "capacity": self.capacity,
                "in_use": self._in_use,
                "waiting": self._waiting,
                "admitted": self._admitted,
                "avg_wait_ms": round(self._total_wait_ms / self._admitted) if self._admitted else 0,
                "max_wait_ms": self._max_wait_ms,
            }


__all__ = ["AdmissionGate"]
