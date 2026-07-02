"""Per-workspace state and the workspace registry.

One server, many workspaces: each `cwd` owns its own context window, FIFO queue,
on-disk snapshot, and path-jailed tool executor. The shared LLM client and
summarizer are injected. State is keyed by `cwd` from day one.
"""
from __future__ import annotations
from typing import Any

import asyncio
import time

from .config import Config, canonical_workspace
from .context_window import ContextWindow
from .conversation_store import ConversationStore
from .directory_tree import directory_tree_block
from .llm_client import LlmClient
from .models import CostSnapshot
from .question_queue import QuestionQueue
from .summarizer import Summarizer
from .tool_executor import ToolExecutor

from .constants import OBSERVE_INVALIDATE_GRACE_MS


class WorkspaceState:
    def __init__(self, cwd: str, cfg: Config, llm: LlmClient, summarizer: Summarizer):
        self.cwd = cwd
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        self.window = ContextWindow(cfg.max_context_tokens, cfg.compaction_threshold, cfg.compaction_floor)
        self._window_version = 0  # bumped on each committed window swap (optimistic concurrency)
        self.last_distill_at = 0.0  # wall time of the last observation-distillation (cooldown + fork dedupe)
        self.queue = QuestionQueue(cfg.max_queue_size, cfg.max_parallel_queries if cfg.is_parallel else 1)
        self.store = ConversationStore(cwd, cfg.snapshot_path(cwd))
        self.executor = ToolExecutor(cwd)
        self.recently_modified: set[str] = set()
        # bookkeeping (surfaced via /stats)
        self.last_compaction: int | None = None
        self.summaries_written = 0
        self.questions_answered = 0    # calls that produced an answer
        self.invocations = 0           # ALL tool calls reaching this workspace (incl. errors/timeouts)
        self.cost = CostSnapshot()
        self.created_at = int(time.time() * 1000)
        # keep-warm bookkeeping (seconds, monotonic-ish wall time)
        self.last_query_at = 0.0   # last REAL query (0 = never, this process)
        self.last_touch_at = 0.0   # last time the prefix was sent (query OR warm)
        self.keep_warms = 0
        self._dir_tree: str | None = None
        self._persist_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()  # guards fork-snapshot + commit-swap

    # ── fork / join (concurrency model) ──
    @property
    def commit_lock(self) -> asyncio.Lock:
        return self._commit_lock

    def fork_window(self) -> ContextWindow:
        """The window a question runs against. Parallel: an independent clone tagged
        with the current window version, so concurrent questions don't corrupt each
        other. Serial: the live window itself (mutated in place) — the queue admits
        one at a time."""
        if self.cfg.is_parallel:
            clone = self.window.clone()
            clone._base_version = self._window_version
            return clone
        return self.window

    def commit_window(self, candidate: ContextWindow) -> bool:
        """Adopt a finished question's window. Serial: it IS the live window → keep.
        Parallel: a LINEAR update (no other question committed since this fork was
        taken) is always adopted — including a net-shrinking COMPACTION (a compacted
        window has fewer tokens, so the 'most-exploring wins' tiebreak would wrongly
        discard it, redoing — and never persisting — compaction every question). The
        tiebreak applies only to a genuine RACE (a concurrent fork already committed),
        where comparing how much each explored is the right call."""
        if candidate is self.window:
            return True
        if candidate._base_version == self._window_version:        # linear → adopt (incl. compaction)
            self.window = candidate
            self._window_version += 1
            return True
        if candidate.exploration_score() > self.window.exploration_score():  # race → most-exploring wins
            self.window = candidate
            self._window_version += 1
            return True
        return False

    @property
    def directory_tree_block(self) -> str:
        if self._dir_tree is None:
            self._dir_tree = directory_tree_block(self.cwd, self.cfg.max_context_tokens, self.cfg.directory_tree_fraction)
        return self._dir_tree

    def refresh_directory_tree(self) -> None:
        self._dir_tree = None

    def add_cost(self, cost: CostSnapshot) -> None:
        """Accumulate spend. Dollars are only counted when **metered** (API key);
        in subscription mode the call is rate-limited, not $-metered, so its $ is
        zeroed (tokens still flow through)."""
        if not self.cfg.metered:
            cost.usd = 0.0
        self.cost.add(cost)

    async def load(self) -> None:
        data = await asyncio.to_thread(self.store.load)
        self.window.restore(data["messages"], data["file_contexts"], data["knowledge_ledger"])
        self.last_compaction = data["last_compaction"]
        self.summaries_written = data["summaries_written"]
        self.questions_answered = data["questions_answered"]
        self.invocations = data["invocations"]
        self.cost.usd = data["cost_usd"]
        self.created_at = data["created_at"]

    async def persist(self) -> None:
        async with self._persist_lock:
            state = {
                "messages": self.window.messages,
                "file_contexts": self.window.file_contexts,
                "knowledge_ledger": self.window.knowledge_ledger,
                "last_compaction": self.last_compaction,
                "summaries_written": self.summaries_written,
                "questions_answered": self.questions_answered,
                "invocations": self.invocations,
                "cost_usd": self.cost.usd,
                "created_at": self.created_at,
            }
            await asyncio.to_thread(self.store.save, state)

    # ── file-change feed ──
    def note_modified(self, path: str) -> bool:
        """Task/tool edit (PostToolUse): flag for the next question's hint — but
        ONLY if Live Memory has actually read the file (otherwise there is no
        prior knowledge that could be stale). The read set grows over time, so
        the longer it runs the more edits it tracks. Returns whether recorded.

        Superseded by `observe()` when passive ingestion is on: teeing the new
        bytes (fresh, authoritative) is strictly stronger than this stale hint."""
        if self.window.has_file(path):
            self.recently_modified.add(path)
            return True
        return False

    def observe(self, path: str, content: str) -> bool:
        """Passive ingestion (FUTURE_DIRECTIONS §1): the building agent's hook teed
        `path`'s current bytes (a Read/Edit/Write it just performed). Store them so
        the model answers without re-reading, and mark the file fresh/current. Unlike
        note_modified/invalidate this records even for never-read files (it IS the new
        knowledge), and clears any pending stale hint. Always returns True."""
        self.window.observe(path, content)
        self.recently_modified.discard(path)  # current now → no stale hint needed
        return True

    def invalidate(self, path: str) -> bool:
        """External edit (FileChanged 'change'/'add'): only matters if read. Skipped
        when the path was just teed in by `observe()` — that FileChanged is our own
        edit echoing back, and the teed bytes are already current."""
        if self.window.recently_observed(path, OBSERVE_INVALIDATE_GRACE_MS):
            return False
        if self.window.has_file(path):
            self.window.invalidate_file_context(path)
            return True
        return False

    def mark_deleted(self, path: str) -> bool:
        """External delete/move (FileChanged 'unlink'): flag the read file as gone
        so the next question's manifest tells the agent it no longer exists."""
        if self.window.has_file(path):
            self.window.mark_file_deleted(path)
            return True
        return False

    def drain_recently_modified(self) -> list[str]:
        out = sorted(self.recently_modified)
        self.recently_modified.clear()
        return out

    def set_clients(self, cfg: Config, llm: LlmClient, summarizer: Summarizer) -> None:
        """Hot-swap config + clients (model/provider change) without losing state."""
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        self.window.max_context_tokens = cfg.max_context_tokens
        self.window.fill_threshold = cfg.compaction_threshold
        self.window.compaction_floor = cfg.compaction_floor

    def stats(self) -> dict[str, Any]:
        u = self.window.get_usage()
        return {
            "cwd": self.cwd,
            "model": self.cfg.model,
            "endpoint": self.cfg.base_url,
            "metered": self.cfg.metered,
            "concurrency": self.cfg.concurrency,
            "auth": "oauth-subscription" if self.cfg.use_oauth else ("api-key" if self.cfg.api_key else "none"),
            "contextWindow": {
                "usedTokens": u.used_tokens,
                "maxTokens": u.max_tokens,
                "fillPct": round(u.fill_pct, 1),
                "qaMessages": u.qa_messages,
                "fileContexts": u.file_contexts,
                "staleFileContexts": u.stale_file_contexts,
            },
            "lastCompaction": self.last_compaction,
            "summariesWritten": self.summaries_written,
            "questionsAnswered": self.questions_answered,
            "invocations": self.invocations,
            "keepWarms": self.keep_warms,
            "lastTouchAt": int(self.last_touch_at) or None,  # cache last refreshed (query or warm), epoch s
            "queueDepth": self.queue.depth,
            "costUsd": round(self.cost.usd, 6),
            # cumulative cheap-model token usage (accumulates even under subscription,
            # where costUsd is null) — for benchmarking total/by-type token spend.
            "inputTokens": self.cost.input_tokens,
            "outputTokens": self.cost.output_tokens,
            "cacheReadTokens": self.cost.cache_read_tokens,
            "cacheWriteTokens": self.cost.cache_write_tokens,
        }


class WorkspaceRegistry:
    def __init__(self, cfg: Config, llm: LlmClient, summarizer: Summarizer):
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        self._states: dict[str, WorkspaceState] = {}
        self._lock = asyncio.Lock()

    def _key(self, cwd: str) -> str:
        return canonical_workspace(
            cwd, self.cfg.canonicalize_workspace, self.cfg.repo_root_mode == "outermost"
        )

    async def get(self, cwd: str) -> WorkspaceState:
        key = self._key(cwd)
        async with self._lock:
            ws = self._states.get(key)
            if ws is None:
                ws = WorkspaceState(key, self.cfg, self.llm, self.summarizer)
                await ws.load()
                self._states[key] = ws
            return ws

    def existing(self, cwd: str) -> WorkspaceState | None:
        return self._states.get(self._key(cwd))

    def all(self) -> list[WorkspaceState]:
        return list(self._states.values())

    def clear(self, cwd: str) -> bool:
        """Forget ONE workspace: drop its in-memory state and delete its on-disk
        snapshot, so the next query starts from a blank slate. Returns whether
        anything existed (loaded state or a snapshot file)."""
        key = self._key(cwd)
        had_state = self._states.pop(key, None) is not None
        snap = self.cfg.snapshot_path(key)
        had_snap = snap.exists()
        try:
            snap.unlink(missing_ok=True)
        except OSError:
            pass
        return had_state or had_snap

    def clear_all(self) -> int:
        """Forget EVERY workspace: drop all in-memory state + delete all snapshot
        files (the 16-hex `<hash>.json`; config.json/oauth_state.json are kept).
        Returns the number of snapshots deleted."""
        self._states.clear()
        n = 0
        for f in self.cfg.data_dir.glob("*.json"):
            if len(f.stem) == 16 and all(c in "0123456789abcdef" for c in f.stem):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    def reload(self, cfg: Config, llm: LlmClient, summarizer: Summarizer) -> None:
        """Adopt a new config + clients: future workspaces use them, and every
        existing workspace hot-swaps (state preserved)."""
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        for ws in self._states.values():
            ws.set_clients(cfg, llm, summarizer)
