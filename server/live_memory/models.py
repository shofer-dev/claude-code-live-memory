"""Core data structures for the Live Memory server (Window B).

Two deliberate design choices (see DESIGN.md):
  - File contexts are a **manifest** (path + content hash + token estimate), not
    stored content — the bytes are re-read on demand via the read tools; the
    manifest exists for budget accounting + staleness (stale == empty hash).
  - Compaction summarizes the oldest history into a neutral `knowledge_ledger`
    instead of hard-dropping it (the reference truncates).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "system"]

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Reference formula: ceil(len(text) / 4)."""
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""  # raw JSON string as emitted by the model


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ChatMessage:
    """One turn in Window B. `content` is the canonical flat text counted for
    the token budget; tool turns also carry `tool_calls`/`tool_results`."""
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    timestamp: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [vars(tc) for tc in self.tool_calls],
            "tool_results": [vars(tr) for tr in self.tool_results],
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatMessage":
        return cls(
            role=d.get("role", "user"),
            content=d.get("content", ""),
            tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls", [])],
            tool_results=[ToolResult(**tr) for tr in d.get("tool_results", [])],
            timestamp=d.get("timestamp", now_ms()),
        )


@dataclass
class FileContext:
    """A file the Live Memory has read. Manifest only — no content stored.
    `content_hash == ""` marks it STALE (force re-read / drop on next validate);
    `deleted` marks it GONE from this path (deleted or moved/renamed away)."""
    path: str  # relative to workspace
    content_hash: str
    token_estimate: int
    loaded_at: int = field(default_factory=now_ms)
    last_referenced_at: int = field(default_factory=now_ms)
    deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileContext":
        return cls(
            path=d["path"],
            content_hash=d.get("content_hash", ""),
            token_estimate=d.get("token_estimate", 0),
            loaded_at=d.get("loaded_at", now_ms()),
            last_referenced_at=d.get("last_referenced_at", now_ms()),
            deleted=d.get("deleted", False),
        )


@dataclass
class CostSnapshot:
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "CostSnapshot") -> None:
        self.usd += other.usd
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class ContextUsage:
    used_tokens: int
    max_tokens: int
    qa_messages: int
    file_contexts: int
    stale_file_contexts: int

    @property
    def fill_pct(self) -> float:
        return (100.0 * self.used_tokens / self.max_tokens) if self.max_tokens else 0.0


@dataclass
class ChatResult:
    """One LLM turn."""
    answer: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: CostSnapshot = field(default_factory=CostSnapshot)


@dataclass
class QuestionResult:
    """Returned to the caller of ask_live_memory."""
    answer: str
    tokens_used: int
    context_usage: ContextUsage
    cost_snapshot: CostSnapshot
    timed_out: bool = False
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    files_read: int = 0
