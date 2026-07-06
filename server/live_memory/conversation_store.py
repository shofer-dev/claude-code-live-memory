"""Per-workspace persistence for Window B.

Versioned JSON snapshot under ${CLAUDE_PLUGIN_DATA}/<workspace-hash>.json. On
load, file-context entries are validated by SHA-256 against the current file on
disk: a hash mismatch or missing file silently drops the entry (stale reference),
so the manifest never claims knowledge of a file the on-disk bytes no longer match.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .models import ChatMessage, FileContext, LedgerFact

VERSION = 1


class ConversationStore:
    def __init__(self, cwd: str, path: Path):
        self.cwd = Path(cwd).resolve()
        self.path = path
        self._hash_cache: dict[str, str | None] = {}

    def load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "messages": [], "file_contexts": [], "knowledge_ledger": "", "ledger_facts": [],
            "last_compaction": None, "summaries_written": 0,
            "questions_answered": 0, "invocations": 0, "cost_usd": 0.0,
            "created_at": int(time.time() * 1000),
        }
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty
        except OSError:
            return empty
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return empty
        if data.get("version") != VERSION:
            return empty
        self._hash_cache = {}  # fresh per load: reflect current on-disk bytes
        return {
            "messages": [ChatMessage.from_dict(m) for m in data.get("messages", [])],
            "file_contexts": self._validate_file_contexts(data.get("file_contexts", [])),
            "knowledge_ledger": data.get("knowledge_ledger", ""),
            "ledger_facts": self._validate_ledger_facts(data.get("ledger_facts", [])),
            "last_compaction": data.get("last_compaction"),
            "summaries_written": data.get("summaries_written", 0),
            "questions_answered": data.get("questions_answered", 0),
            "invocations": data.get("invocations", 0),
            "cost_usd": data.get("cost_usd", 0.0),
            "created_at": data.get("created_at", empty["created_at"]),
        }

    def save(self, state: dict[str, Any]) -> None:
        data = {
            "version": VERSION,
            "cwd": str(self.cwd),
            "updated_at": int(time.time() * 1000),
            "created_at": state.get("created_at", int(time.time() * 1000)),
            "knowledge_ledger": state.get("knowledge_ledger", ""),
            "ledger_facts": [f.to_dict() for f in state.get("ledger_facts", [])],
            "messages": [m.to_dict() for m in state.get("messages", [])],
            "file_contexts": [fc.to_dict() for fc in state.get("file_contexts", [])],
            "last_compaction": state.get("last_compaction"),
            "summaries_written": state.get("summaries_written", 0),
            "questions_answered": state.get("questions_answered", 0),
            "invocations": state.get("invocations", 0),
            "cost_usd": state.get("cost_usd", 0.0),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic

    def _validate_file_contexts(self, entries: list[dict[str, Any]]) -> list[FileContext]:
        out: list[FileContext] = []
        for e in entries:
            fc = FileContext.from_dict(e)
            h = self._disk_hash(fc.path)
            if h is not None and h == fc.content_hash:
                out.append(fc)
            # mismatch/missing → drop (stale)
        return out

    def _validate_ledger_facts(self, entries: list[dict[str, Any]]) -> list[LedgerFact]:
        """Cross-session provenance check (FUTURE_DIRECTIONS §6): a fact whose cited
        source file changed or disappeared on disk since it was recorded is DEMOTED
        (kept, but flagged stale) — mirroring the freshness the file-context manifest
        already self-heals, but for the compacted ledger."""
        out: list[LedgerFact] = []
        for e in entries:
            f = LedgerFact.from_dict(e)
            for path, recorded in f.sources.items():
                if self._disk_hash(path) != recorded:  # changed or gone since recorded
                    f.stale = True
                    break
            out.append(f)
        return out

    def _disk_hash(self, path: str) -> str | None:
        """SHA-256 of the current on-disk bytes at `path` (workspace-relative), or
        None if missing/unreadable. Memoized per load so shared sources hash once."""
        cache = self._hash_cache
        if path in cache:
            return cache[path]
        try:
            full = (self.cwd / path).resolve()
            content = full.read_text(encoding="utf-8", errors="replace")
            h: str | None = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        except OSError:
            h = None
        cache[path] = h
        return h
