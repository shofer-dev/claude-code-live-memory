"""Per-workspace question admission (asyncio).

A `Semaphore(max_parallel)` bounds how many questions run at once per workspace:
`max_parallel=1` (default) serializes (one at a time, FIFO); higher values run
the fork-join parallel model. The caller's soft `timeout` is passed to the
processor as an absolute deadline so it returns a best-effort answer *before* the
deadline; a slightly-larger `wait_for` is the hard backstop that cancels a
runaway. Total in-flight + waiting is bounded by `max_size`.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .constants import HARD_BACKSTOP_MARGIN_S
from .models import QuestionResult

# Processor: (question, cwd, deadline_monotonic) -> QuestionResult
Processor = Callable[[str, str, float], Awaitable[QuestionResult]]


class QueueFull(Exception):
    pass


class QuestionTimeout(Exception):
    pass


class QuestionQueue:
    def __init__(self, max_size: int = 100, max_parallel: int = 1):
        self.max_size = max_size
        self.max_parallel = max(1, max_parallel)
        self._sem = asyncio.Semaphore(self.max_parallel)
        self._depth = 0

    @property
    def depth(self) -> int:
        return self._depth

    async def submit(self, question: str, cwd: str, timeout_s: float, processor: Processor) -> QuestionResult:
        if self._depth >= self.max_size:
            raise QueueFull(f"Live Memory question queue is full (max {self.max_size}). Try again later.")
        self._depth += 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        async def run() -> QuestionResult:
            async with self._sem:  # admit up to max_parallel at once (1 = serial)
                return await processor(question, cwd, deadline)

        try:
            return await asyncio.wait_for(run(), timeout=timeout_s + HARD_BACKSTOP_MARGIN_S)
        except asyncio.TimeoutError as e:
            raise QuestionTimeout(
                f"Live Memory question exceeded its hard timeout ({timeout_s:.0f}s + backstop)."
            ) from e
        finally:
            self._depth -= 1
