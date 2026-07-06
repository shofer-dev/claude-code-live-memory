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

from .constants import CHARS_PER_TOKEN

Role = Literal["user", "assistant", "system"]


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
    """A file the Live Memory has read. Manifest by default — no content stored;
    the bytes are re-read on demand. `content_hash == ""` marks it STALE (force
    re-read / drop on next validate); `deleted` marks it GONE from this path
    (deleted or moved/renamed away).

    Passive ingestion (FUTURE_DIRECTIONS §1): when the building agent's hook tees a
    file's current bytes, they are held in `content` so the Live Memory can answer
    WITHOUT a re-read. `content == ""` is the classic manifest-only entry; a
    non-empty `content` is an "observed" entry rendered inline. Raw content is
    in-memory only — never persisted (snapshots stay lean and re-warm from real
    work) and is distilled into the knowledge ledger when the window compacts."""
    path: str  # relative to workspace
    content_hash: str
    token_estimate: int
    loaded_at: int = field(default_factory=now_ms)
    last_referenced_at: int = field(default_factory=now_ms)
    deleted: bool = False
    content: str = ""      # teed bytes (in-memory only; "" == manifest-only)
    observed_at: int = 0   # ms when content was teed (for the invalidate grace guard)

    @property
    def has_content(self) -> bool:
        return bool(self.content)

    def to_dict(self) -> dict[str, Any]:
        d = vars(self).copy()
        d.pop("content", None)      # never persist raw content → lean snapshots
        d.pop("observed_at", None)  # in-memory grace bookkeeping only
        return d

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
class LedgerFact:
    """One durable fact in the knowledge ledger, tagged with its provenance
    (FUTURE_DIRECTIONS §6). `sources` maps each cited file path → the content hash
    it had when the fact was recorded, so an out-of-band change to a cited file can
    be detected and the fact **demoted** (`stale=True`) rather than silently trusted.
    Attribution is mechanical (a fact cites the manifest paths it mentions); an
    empty `sources` means nothing to validate against, so the fact is never
    auto-demoted (it falls back to precedence, as before)."""
    text: str
    sources: dict[str, str] = field(default_factory=dict)  # path → content_hash at write time
    written_at: int = field(default_factory=now_ms)
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "sources": dict(self.sources),
                "written_at": self.written_at, "stale": self.stale}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LedgerFact":
        return cls(
            text=d.get("text", ""),
            sources=dict(d.get("sources", {})),
            written_at=d.get("written_at", now_ms()),
            stale=bool(d.get("stale", False)),
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
