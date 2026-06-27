#!/usr/bin/env python3
"""Backs /live-memory-empty — wipe the Live Memory's accumulated knowledge.

  /live-memory-empty           empty THIS workspace (cwd)
  /live-memory-empty all       empty ALL workspaces

Drops the in-memory state and deletes the on-disk snapshot(s), so the next
question starts from a blank slate. Handy for a clean start (e.g. a benchmark).
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("LIVE_MEMORY_URL", "http://127.0.0.1:7711")


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    body = {"all": True} if arg == "all" else {"cwd": os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())}
    req = urllib.request.Request(
        URL + "/clear", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"Live Memory: could not reach the server at {URL} ({e}).")
        return 0
    if not res.get("ok"):
        print(f"Live Memory: empty failed: {res.get('error')}")
    elif res.get("scope") == "all":
        print(f"Live Memory emptied — cleared {res.get('cleared')} workspace snapshot(s).")
    else:
        print(f"Live Memory emptied for: {res.get('cwd')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
