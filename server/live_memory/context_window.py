"""ContextWindow — Window B utilization.

Two evictable kinds, handled differently (DESIGN.md §Compaction):
  - **file contexts** are re-readable (the file is the source of truth) → evicted
    LRU by `enforce_limit()` synchronously at its 3 call sites.
  - **message history** is the accumulated reasoning → NOT hard-dropped; when the
    window is still over budget after file-context eviction, the manager runs a
    batched **neutral summarization** (async) that folds the oldest pairs into the
    `knowledge_ledger` and then drops them via `pop_oldest_pair()`.
"""
from __future__ import annotations

import copy
import hashlib

from .models import ChatMessage, ContextUsage, FileContext, estimate_tokens, now_ms

TRUNCATED_MESSAGE_PAIR_TOKEN_COST = 100


class ContextWindow:
    def __init__(self, max_context_tokens: int, fill_threshold: float = 0.85,
                 compaction_floor: float = 0.6):
        self.messages: list[ChatMessage] = []
        self.file_contexts: list[FileContext] = []
        self.knowledge_ledger: str = ""
        self.max_context_tokens = max_context_tokens
        self.fill_threshold = fill_threshold       # high watermark: compaction TRIGGER
        self.compaction_floor = compaction_floor   # low watermark: compact DOWN to this
        self._evicted_tokens = 0
        self._base_version = 0  # the workspace window-version this fork was taken from

    # ── accounting ──
    def estimated_token_count(self) -> int:
        msg = sum(estimate_tokens(m.content) for m in self.messages)
        fcs = sum(fc.token_estimate for fc in self.file_contexts)
        return msg + fcs + estimate_tokens(self.knowledge_ledger)

    def is_nearly_full(self) -> bool:
        return self.estimated_token_count() > self.max_context_tokens * self.fill_threshold

    def get_usage(self) -> ContextUsage:
        return ContextUsage(
            used_tokens=self.estimated_token_count(),
            max_tokens=self.max_context_tokens,
            qa_messages=len(self.messages),
            file_contexts=len(self.file_contexts),
            stale_file_contexts=sum(1 for fc in self.file_contexts if not fc.content_hash),
        )

    # ── fork / join (parallel concurrency model) ──
    def clone(self) -> ContextWindow:
        """A deep, independent copy. In the parallel model each in-flight question
        runs against its own fork, so concurrent questions can't corrupt one
        another; the richest fork is committed back (see `exploration_score`)."""
        c = ContextWindow(self.max_context_tokens, self.fill_threshold, self.compaction_floor)
        c.messages = copy.deepcopy(self.messages)
        c.file_contexts = copy.deepcopy(self.file_contexts)
        c.knowledge_ledger = self.knowledge_ledger
        return c

    def exploration_score(self) -> tuple[int, int]:
        """How much of the codebase this window embodies — the tiebreaker that
        picks the 'most-exploring' fork on commit: (# files read into knowledge,
        total estimated tokens), compared lexicographically. Files dominate so a
        fork that actually read more source wins over one that merely answered at
        length."""
        files = sum(1 for fc in self.file_contexts if fc.content_hash)
        return (files, self.estimated_token_count())

    # ── file contexts ──
    def upsert_file_context(self, entry: FileContext) -> None:
        for fc in self.file_contexts:
            if fc.path == entry.path:
                fc.content_hash = entry.content_hash
                fc.token_estimate = entry.token_estimate
                fc.last_referenced_at = entry.last_referenced_at
                if entry.content:  # an observation: adopt teed bytes (don't wipe on a manifest-only re-read)
                    fc.content = entry.content
                    fc.observed_at = entry.observed_at
                    fc.deleted = False  # observing current bytes un-deletes the path
                return
        self.file_contexts.append(entry)

    def observe(self, path: str, content: str) -> None:
        """Passive ingestion (FUTURE_DIRECTIONS §1): record `path`'s current bytes
        (teed from the building agent's I/O) as a fresh, content-bearing entry so
        the model can answer without re-reading. Records even for never-read files."""
        self.upsert_file_context(FileContext(
            path=path,
            content_hash=hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
            token_estimate=estimate_tokens(content),
            last_referenced_at=now_ms(),
            content=content,
            observed_at=now_ms(),
        ))

    def recently_observed(self, path: str, grace_ms: int) -> bool:
        """True if `path`'s bytes were teed in within `grace_ms` — used to ignore a
        FileChanged event that is just our own teed edit echoing back."""
        for fc in self.file_contexts:
            if fc.path == path:
                return fc.observed_at > 0 and (now_ms() - fc.observed_at) <= grace_ms
        return False

    def content_contexts_lru(self) -> list[FileContext]:
        """Content-bearing (observed) entries, least-recently-referenced first —
        the order compaction distills + sheds them in."""
        return sorted((fc for fc in self.file_contexts if fc.has_content),
                      key=lambda fc: fc.last_referenced_at)

    def clear_content(self, path: str) -> bool:
        """Drop an observation's raw bytes, leaving a manifest-only entry (still
        'known', re-readable on demand). Its token weight collapses to the manifest
        one-liner so the freed budget is actually reclaimed."""
        for fc in self.file_contexts:
            if fc.path == path and fc.content:
                fc.content = ""
                fc.token_estimate = estimate_tokens(f"[Read into your knowledge: {path}]")
                return True
        return False

    def invalidate_file_context(self, path: str) -> bool:
        for fc in self.file_contexts:
            if fc.path == path:
                fc.content_hash = ""  # "" → stale → re-read / dropped on next validate
                fc.content = ""       # teed bytes no longer match disk → drop them
                return True
        return False

    def mark_file_deleted(self, path: str) -> bool:
        """Flag a read file as GONE from this path (deleted, or moved/renamed
        away). Distinct from stale: there's nothing to re-read here."""
        for fc in self.file_contexts:
            if fc.path == path:
                fc.content_hash = ""
                fc.content = ""
                fc.deleted = True
                return True
        return False

    def remove_file_context(self, path: str) -> None:
        self.file_contexts = [fc for fc in self.file_contexts if fc.path != path]

    @property
    def file_context_paths(self) -> list[str]:
        return [fc.path for fc in self.file_contexts]

    def has_file(self, path: str) -> bool:
        return any(fc.path == path for fc in self.file_contexts)

    def is_cold(self, min_ledger_chars: int = 160) -> bool:
        """True when there's no grounding to answer FROM: no observed file content in the
        window and an essentially-empty knowledge ledger. Prior Q&A is deliberately
        excluded (it may itself be a guess). Used to force exploration before a cold, cheap
        model answers exact values from priors."""
        if any(fc.has_content for fc in self.file_contexts):
            return False
        return len(self.knowledge_ledger.strip()) < min_ledger_chars

    # ── messages ──
    def append_message(self, m: ChatMessage) -> None:
        self.messages.append(m)

    def message_count(self) -> int:
        return len(self.messages)

    def pop_oldest_pair(self) -> list[ChatMessage]:
        """Remove and return the oldest user+assistant pair (for summarization).
        Returns up to 2 messages; [] when ≤2 remain (keep a minimal tail)."""
        if len(self.messages) <= 2:
            return []
        pair = [self.messages.pop(0)]
        if self.messages:
            pair.append(self.messages.pop(0))
        return pair

    # ── eviction (file contexts only; sync) ──
    def enforce_limit(self, target: int | None = None) -> bool:
        """Evict the least-recently-referenced file contexts until at/under
        `target` (default: the hard max) or none remain. Returns True if STILL
        over `target` afterward (i.e. message-history compaction is needed)."""
        limit = self.max_context_tokens if target is None else target
        while self.estimated_token_count() > limit and self.file_contexts:
            self.file_contexts.sort(key=lambda fc: fc.last_referenced_at)
            evicted = self.file_contexts.pop(0)
            self._evicted_tokens += evicted.token_estimate
        return self.estimated_token_count() > limit

    def consume_evicted_tokens(self) -> int:
        n, self._evicted_tokens = self._evicted_tokens, 0
        return n

    # ── restore / clear ──
    def restore(self, messages: list[ChatMessage], file_contexts: list[FileContext], ledger: str) -> None:
        self.messages = list(messages)
        self.file_contexts = list(file_contexts)
        self.knowledge_ledger = ledger

    def clear(self) -> None:
        self.messages.clear()
        self.file_contexts.clear()
        self.knowledge_ledger = ""
