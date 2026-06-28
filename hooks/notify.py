#!/usr/bin/env python3
"""Claude Code hook feeder for the Live Memory server.

Invoked by `hooks.json` on PostToolUse(Write|Edit) and FileChanged. Reads the
hook payload (JSON on stdin), extracts the changed path + workspace cwd, and
best-effort POSTs a notification to the running Live Memory server.

Three kinds:
  - `edited`  : a task/tool edit (Write|Edit|MultiEdit|NotebookEdit).
  - `read`    : a content read (Read).
  - `changed` : an out-of-band file change (FileChanged). Server invalidates the
                file context IFF it has actually read that file (loaded-files
                set membership); otherwise it drops the event.

For `edited`/`read`, when passive ingestion is enabled (FUTURE_DIRECTIONS §1) the
hook also tees the file's CURRENT bytes (read locally — free, no model tokens) in
a `contents` map, so the Live Memory learns the code as a side effect of real work
without paying to re-read it. The server falls back to the old stale-hint behavior
when no content is present or passive ingestion is off.

This must never break the hook: it always exits 0, swallows every error, and
uses a short connect timeout so a down server never stalls Claude Code.
"""
import json
import os
import sys
import urllib.request

# The server's notify endpoint. Override via env if the server moved.
NOTIFY_URL = os.environ.get("LIVE_MEMORY_NOTIFY_URL", "http://127.0.0.1:7711/notify")
CONNECT_TIMEOUT_S = 1.5
# Don't tee files larger than this (bytes) — keep the window lean and the POST small.
MAX_FILE_BYTES = int(os.environ.get("LIVE_MEMORY_PASSIVE_MAX_FILE_BYTES", str(256 * 1024)))


def _extract_paths(payload: dict) -> list[str]:
    """Best-effort pull of file path(s) from a hook payload across shapes."""
    paths: list[str] = []
    # FileChanged-style
    for key in ("path", "file_path", "filePath"):
        v = payload.get(key)
        if isinstance(v, str):
            paths.append(v)
    # PostToolUse-style: tool_input.{file_path,path,notebook_path} or edits[]
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(ti, dict):
        for key in ("file_path", "path", "notebook_path"):
            v = ti.get(key)
            if isinstance(v, str):
                paths.append(v)
        edits = ti.get("edits")
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                    paths.append(e["file_path"])
    return list(dict.fromkeys(p for p in paths if p))  # dedup, drop empties


def _read_text(path: str, cwd: str) -> str | None:
    """Best-effort local read of a workspace text file, capped. Returns None for
    paths outside the workspace, files over the cap, binaries, or any error — so
    teeing never blocks or breaks the hook."""
    try:
        root = os.path.realpath(cwd)
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        if full != root and not full.startswith(root + os.sep):
            return None  # outside the workspace jail
        if os.path.getsize(full) > MAX_FILE_BYTES:
            return None
        with open(full, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:  # over cap or binary
            return None
        return raw.decode("utf-8")
    except Exception:
        return None  # missing/unreadable/non-utf-8 → skip teeing this file


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else "changed"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    cwd = (
        payload.get("cwd")
        or payload.get("project_dir")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    paths = _extract_paths(payload)
    if not paths:
        return  # nothing actionable

    # FileChanged carries the change type: change | add | unlink. Forward it so the
    # server can treat a delete/move (unlink) differently from a plain modify.
    event = payload.get("event", "")
    # Passive ingestion: tee current bytes for the agent's own reads/edits. Reading
    # locally is free (no model tokens) — the cost the design avoids is the premium
    # re-read, not a disk read. The server ignores `contents` when passive is off.
    contents: dict[str, str] = {}
    if kind in ("edited", "read"):
        for p in paths:
            text = _read_text(p, cwd)
            if text is not None:
                contents[p] = text
    body = json.dumps({"kind": kind, "event": event, "cwd": cwd, "paths": paths, "contents": contents}).encode("utf-8")
    req = urllib.request.Request(
        NOTIFY_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S).close()
    except Exception:
        pass  # server down / slow: never block the hook


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
