#!/usr/bin/env python3
"""Claude Code hook feeder for the Live Memory server.

Invoked by `hooks.json` on PostToolUse(Write|Edit) and FileChanged. Reads the
hook payload (JSON on stdin), extracts the changed path + workspace cwd, and
best-effort POSTs a notification to the running Live Memory server.

Two kinds:
  - `edited`  : a task/tool edit (Write|Edit). Server adds it to
                recentlyModifiedFiles (append-only; no eviction). The hint is
                attached to the next question's trailing turn.
  - `changed` : an out-of-band file change (FileChanged). Server invalidates the
                file context IFF it has actually read that file (loaded-files
                set membership); otherwise it drops the event.

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
    body = json.dumps({"kind": kind, "event": event, "cwd": cwd, "paths": paths}).encode("utf-8")
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
