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


class WorkspaceState:
    def __init__(self, cwd: str, cfg: Config, llm: LlmClient, summarizer: Summarizer):
        self.cwd = cwd
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        self.window = ContextWindow(cfg.max_context_tokens, cfg.compaction_threshold)
        self.queue = QuestionQueue(cfg.max_queue_size, cfg.max_parallel_queries if cfg.is_parallel else 1)
        self.store = ConversationStore(cwd, cfg.snapshot_path(cwd))
        self.executor = ToolExecutor(cwd)
        self.recently_modified: set[str] = set()
        # bookkeeping (surfaced via /stats)
        self.last_compaction: int | None = None
        self.summaries_written = 0
        self.questions_answered = 0
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
        """The window a question runs against. Parallel: an independent clone, so
        concurrent questions don't corrupt each other. Serial: the live window
        itself (mutated in place, as before) — the queue admits one at a time."""
        return self.window.clone() if self.cfg.is_parallel else self.window

    def commit_window(self, candidate: ContextWindow) -> bool:
        """Adopt a finished question's window. Serial: it IS the live window → keep.
        Parallel: replace the committed window only if this fork explored more of
        the codebase (longest/most-exploring wins) — otherwise its work is dropped
        (the caller still got its answer)."""
        if candidate is self.window:
            return True
        if candidate.exploration_score() > self.window.exploration_score():
            self.window = candidate
            return True
        return False

    @property
    def directory_tree_block(self) -> str:
        if self._dir_tree is None:
            self._dir_tree = directory_tree_block(self.cwd, self.cfg.max_context_tokens)
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
                "cost_usd": self.cost.usd,
                "created_at": self.created_at,
            }
            await asyncio.to_thread(self.store.save, state)

    # ── file-change feed ──
    def note_modified(self, path: str) -> bool:
        """Task/tool edit (PostToolUse): flag for the next question's hint — but
        ONLY if Live Memory has actually read the file (otherwise there is no
        prior knowledge that could be stale). The read set grows over time, so
        the longer it runs the more edits it tracks. Returns whether recorded."""
        if self.window.has_file(path):
            self.recently_modified.add(path)
            return True
        return False

    def invalidate(self, path: str) -> bool:
        """External edit (FileChanged 'change'/'add'): only matters if read."""
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
            "keepWarms": self.keep_warms,
            "lastTouchAt": int(self.last_touch_at) or None,  # cache last refreshed (query or warm), epoch s
            "queueDepth": self.queue.depth,
            "costUsd": round(self.cost.usd, 6),
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

    def reload(self, cfg: Config, llm: LlmClient, summarizer: Summarizer) -> None:
        """Adopt a new config + clients: future workspaces use them, and every
        existing workspace hot-swaps (state preserved)."""
        self.cfg = cfg
        self.llm = llm
        self.summarizer = summarizer
        for ws in self._states.values():
            ws.set_clients(cfg, llm, summarizer)
