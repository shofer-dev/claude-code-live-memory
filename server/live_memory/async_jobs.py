"""Fire-and-forget job registry for the optional async tool pair.

Claude Code (and MCP today) has no native async/background tool invocation — a
tool call blocks the turn until it returns. To let an agent submit a slow query
and collect the result later, the SERVER implements the submit/poll pattern: this
registry runs each job as a background asyncio task and hands back a `job_id`;
the agent polls `collect(job_id)` until it's done. Opt-in (LIVE_MEMORY_ASYNC_TOOLS).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable


class JobRunner:
    def __init__(self, ttl_s: float = 3600.0, max_jobs: int = 256):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._ttl = ttl_s
        self._max = max_jobs

    def _prune(self) -> None:
        now = time.time()
        for jid in [j for j, v in self._jobs.items() if now - v["ts"] > self._ttl]:
            self._jobs.pop(jid, None)
        # hard cap: drop the oldest if we somehow blow past it
        while len(self._jobs) > self._max:
            oldest = min(self._jobs, key=lambda j: self._jobs[j]["ts"])
            self._jobs.pop(oldest, None)

    def submit(self, make_coro: Callable[[], Awaitable[str]]) -> str:
        """Start `make_coro()` in the background; return a job_id immediately."""
        self._prune()
        job_id = uuid.uuid4().hex[:12]
        self._jobs[job_id] = {"status": "running", "ts": time.time()}
        task = asyncio.create_task(self._run(job_id, make_coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(self, job_id: str, make_coro: Callable[[], Awaitable[str]]) -> None:
        try:
            result = await make_coro()
            done = {"status": "done", "result": result, "ts": time.time()}
        except Exception as e:  # noqa: BLE001 — surface job failure to the poller
            done = {"status": "error", "error": str(e), "ts": time.time()}
        if job_id in self._jobs:  # not pruned/abandoned
            self._jobs[job_id] = done

    def collect(self, job_id: str) -> dict[str, Any] | None:
        """None = unknown/expired. {status:"running"} = not done. Otherwise the
        terminal {status:"done"|"error", ...}, which is POPPED (one-shot collect)."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job["status"] == "running":
            return {"status": "running"}
        return self._jobs.pop(job_id)
