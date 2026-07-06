#!/usr/bin/env python3
"""Human-facing Live Memory status — backs the /live-memory-stats slash command.

GETs the server's read-only /stats endpoint for the given workspace and prints a
compact, formatted summary. Never enters the model's context (the slash command
displays this output verbatim).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

STATS_URL = os.environ.get("LIVE_MEMORY_STATS_URL", "http://127.0.0.1:7711/stats")
_BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.txt")


def _print_banner() -> None:
    try:
        with open(_BANNER_PATH, encoding="utf-8") as f:
            sys.stdout.write("\n" + f.read().rstrip("\n") + "\n\n")
    except OSError:
        pass  # banner is cosmetic; never fail stats over it


def _fmt_usd(v) -> str:
    try:
        return f"${float(v):.4f}"
    except Exception:
        return str(v)


def _dur(secs) -> str:
    try:
        s = int(secs)
    except Exception:
        return f"{secs}s"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if d or h:
        parts.append(f"{h:02d}h")
    parts.append(f"{m:02d}m")
    return " ".join(parts)


def _ago(epoch_s) -> str:
    if not epoch_s:
        return "never"
    secs = max(0, int(time.time() - int(epoch_s)))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s ago"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m ago"


def main() -> int:
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    _print_banner()
    url = STATS_URL + "?" + urllib.parse.urlencode({"cwd": cwd})
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            s = json.loads(resp.read())
    except Exception as e:
        print(f"Live Memory: unreachable at {STATS_URL} ({e}).")
        print("Is the server running? (it is an externally-supervised singleton)")
        return 0

    cw = s.get("contextWindow", {})
    fill = cw.get("fillPct")
    print("Live Memory — status")
    print(f"  workspace      : {s.get('cwd', cwd)}")
    print(f"  model          : {s.get('model', '?')}  via {s.get('endpoint', '?')}")
    print(f"  concurrency    : {s.get('concurrency', '?')}")
    print(f"  context window : {cw.get('usedTokens', '?')}/{cw.get('maxTokens', '?')} tokens"
          + (f" ({fill:.0f}%)" if isinstance(fill, (int, float)) else ""))
    print(f"  Q&A retained   : {cw.get('qaMessages', '?')} messages")
    print(f"  file contexts  : {cw.get('fileContexts', '?')} ({cw.get('staleFileContexts', 0)} stale)")
    print(f"  last compaction: {s.get('lastCompaction', 'never')}  (summaries: {s.get('summariesWritten', 0)})")
    print(f"  questions       : {s.get('questionsAnswered', 0)} answered  (queue: {s.get('queueDepth', 0)})")
    print(f"  cache refreshed : {_ago(s.get('lastTouchAt'))}  ({s.get('keepWarms', 0)} keep-warm pings)")
    if s.get("metered", True):
        print(f"  cost (cumulative): {_fmt_usd(s.get('costUsd', 0))}")
    else:
        print(f"  cost            : {s.get('costNote', 'subscription — rate-limited, not $-metered')}")
    print(f"  uptime          : {_dur(s.get('uptimeSeconds', '?'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
