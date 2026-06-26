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

from .models import ChatMessage, FileContext

VERSION = 1


class ConversationStore:
    def __init__(self, cwd: str, path: Path):
        self.cwd = Path(cwd).resolve()
        self.path = path

    def load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "messages": [], "file_contexts": [], "knowledge_ledger": "",
            "last_compaction": None, "summaries_written": 0,
            "questions_answered": 0, "cost_usd": 0.0,
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
        return {
            "messages": [ChatMessage.from_dict(m) for m in data.get("messages", [])],
            "file_contexts": self._validate_file_contexts(data.get("file_contexts", [])),
            "knowledge_ledger": data.get("knowledge_ledger", ""),
            "last_compaction": data.get("last_compaction"),
            "summaries_written": data.get("summaries_written", 0),
            "questions_answered": data.get("questions_answered", 0),
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
            "messages": [m.to_dict() for m in state.get("messages", [])],
            "file_contexts": [fc.to_dict() for fc in state.get("file_contexts", [])],
            "last_compaction": state.get("last_compaction"),
            "summaries_written": state.get("summaries_written", 0),
            "questions_answered": state.get("questions_answered", 0),
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
            try:
                full = (self.cwd / fc.path).resolve()
                content = full.read_text(encoding="utf-8", errors="replace")
                if hashlib.sha256(content.encode("utf-8", "replace")).hexdigest() == fc.content_hash:
                    out.append(fc)
                # mismatch → drop (stale)
            except OSError:
                pass  # missing/unreadable → drop
        return out
