#!/usr/bin/env python3
"""Backs the /live-memory-config slash command.

`show`                       — print the current config + live model in use.
`set key=value [key=value…]` — write keys into ${CLAUDE_PLUGIN_DATA}/config.json
                               and hot-reload the running server (no restart).

Keys: provider (anthropic|openai), model, base_url, api_key, use_oauth.
Examples:
  /live-memory-config set provider=openai base_url=https://api.deepseek.com model=deepseek-chat api_key=sk-...
  /live-memory-config set provider=anthropic use_oauth=true model=claude-haiku-4-5-20251001
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

URL = os.environ.get("LIVE_MEMORY_URL", "http://127.0.0.1:7711")


def data_dir() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("LIVE_MEMORY_DATA_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude" / "plugins" / "data" / "live-memory"


def _coerce(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _post(path: str) -> dict:
    try:
        req = urllib.request.Request(URL + path, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def main() -> int:
    args = sys.argv[1:]
    action = args[0] if args else "show"
    cfg_path = data_dir() / "config.json"

    if action == "show":
        try:
            conf = json.loads(cfg_path.read_text())
        except Exception:  # noqa: BLE001
            conf = {}
        print("config.json:", json.dumps(conf, indent=2) if conf else "(none — using env/defaults)")
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        try:
            with urllib.request.urlopen(URL + "/stats?cwd=" + urllib.parse.quote(cwd), timeout=4) as r:
                s = json.loads(r.read())
            print(f"in use : model={s.get('model')}  endpoint={s.get('endpoint')}  "
                  f"auth={s.get('auth')}  metered={s.get('metered')}")
        except Exception as e:  # noqa: BLE001
            print(f"(server not reachable at {URL}: {e})")
        return 0

    if action == "set":
        try:
            conf = json.loads(cfg_path.read_text())
        except Exception:  # noqa: BLE001
            conf = {}
        changed = []
        for kv in args[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                conf[k] = _coerce(v)
                changed.append(k)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(conf, indent=2))
        print(f"updated {', '.join(changed) or '(nothing)'} in {cfg_path}")
        res = _post("/reload")
        if res.get("ok"):
            print("server reloaded:", json.dumps(res.get("config", {})))
        else:
            print(f"reload failed (restart the server to apply): {res.get('error')}")
        return 0

    print("usage: /live-memory-config [show | set key=value ...]")
    print("  keys: provider (anthropic|openai), model, base_url, api_key, use_oauth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
