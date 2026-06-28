"""Measurement hinge for passive ingestion (FUTURE_DIRECTIONS §1):
does a passively-warmed window make ask_live_memory cheaper, net of the bloat the
observations add?  COLD (today's behavior) vs WARM (files teed via /notify first),
same questions, real Haiku via subscription. Cheap-side tokens from /stats deltas.

Run against a Live Memory server (defaults to a local instance on :7712 and this
repo as the target codebase; override with LM_BENCH_BASE / LM_BENCH_TARGET):

    # isolated server with the new code, subscription OAuth + Haiku, own data dir
    LIVE_MEMORY_PORT=7712 LIVE_MEMORY_DATA_DIR=/tmp/lm-bench LIVE_MEMORY_KEEP_WARM=false \
        server/.venv/bin/python -m live_memory &
    server/.venv/bin/python benchmark/harness/passive_hinge.py

The mechanism metric (tool_calls / files_read) is the low-variance signal; the
imputed $ is noisier (provider cache TTL between reps) but consistently negative.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import os
BASE = os.environ.get("LM_BENCH_BASE", "http://127.0.0.1:7712")
TARGET = os.environ.get("LM_BENCH_TARGET", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
TIMEOUT = 90

FILES = [
    "server/live_memory/context_window.py",
    "server/live_memory/keep_warm.py",
    "server/live_memory/manager.py",
    "server/live_memory/config.py",
    "server/live_memory/models.py",
]
QUESTIONS = [
    "In the context window module, which method evicts file contexts and what eviction policy does it use?",
    "How does the keep-warm loop decide a workspace is eligible to be warmed? List the conditions.",
    "What are the compaction tiers in _maybe_compact, in order?",
    "How is a workspace cwd canonicalized into its partition key, and what happens for a subdirectory of a git repo?",
    "Which FileContext fields are excluded from the persisted snapshot, and why?",
]

# imputed Haiku rates ($/1e6): input, output, cache-read, cache-write
HAI = dict(i=1.0, o=5.0, cr=0.10, cw=1.25)


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _stats() -> dict:
    from urllib.parse import quote
    with urllib.request.urlopen(f"{BASE}/stats?cwd={quote(TARGET)}", timeout=10) as r:
        return json.loads(r.read())


def _toks(s: dict) -> tuple[int, int, int, int]:
    return (s["inputTokens"], s["outputTokens"], s["cacheReadTokens"], s["cacheWriteTokens"], s["invocations"])  # type: ignore


def _cost(d: tuple[int, int, int, int]) -> float:
    i, o, cr, cw = d[:4]
    return (i * HAI["i"] + o * HAI["o"] + cr * HAI["cr"] + cw * HAI["cw"]) / 1e6


_TRAILER = re.compile(r"tool_calls=(\d+).*?files_read=(\d+).*?context=(\d+)/\d+\((\d+\.\d+)%\)")
_LAT = re.compile(r"latency=([\d.]+)s")


async def ask(session: ClientSession, q: str) -> tuple[str, dict]:
    res = await session.call_tool("ask_live_memory", {"question": q, "cwd": TARGET, "timeout": TIMEOUT})
    text = res.content[0].text
    m = _TRAILER.search(text.replace("\n", " "))
    lat = _LAT.search(text)
    meta = {
        "tool_calls": int(m.group(1)) if m else -1,
        "files_read": int(m.group(2)) if m else -1,
        "ctx_tokens": int(m.group(3)) if m else -1,
        "fill_pct": float(m.group(4)) if m else -1.0,
        "latency": float(lat.group(1)) if lat else -1.0,
    }
    return text, meta


async def run_arm(name: str, warm: bool) -> dict:
    _post("/clear", {"cwd": TARGET})
    if warm:
        contents = {}
        for rel in FILES:
            p = Path(TARGET) / rel
            contents[rel] = p.read_text(encoding="utf-8")
        resp = _post("/notify", {"kind": "edited", "cwd": TARGET,
                                 "paths": FILES, "contents": contents})
        teed_tokens = sum(len(c) for c in contents.values()) // 4
        print(f"[{name}] teed {resp['applied']}/{len(FILES)} files (~{teed_tokens} tok of observations)")

    base = _toks(_stats())
    rows = []
    async with streamable_http_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for i, q in enumerate(QUESTIONS):
                before = _toks(_stats())
                _, meta = await ask(s, q)
                after = _toks(_stats())
                delta = tuple(after[j] - before[j] for j in range(5))
                rows.append((i, meta, delta))
                print(f"[{name}] Q{i+1}: tools={meta['tool_calls']} reads={meta['files_read']} "
                      f"lat={meta['latency']:.1f}s fill={meta['fill_pct']:.1f}% | "
                      f"in={delta[0]} out={delta[1]} cr={delta[2]} cw={delta[3]} ${_cost(delta):.5f}")
    total = tuple(_toks(_stats())[j] - base[j] for j in range(5))
    tool_sum = sum(r[1]["tool_calls"] for r in rows)
    read_sum = sum(r[1]["files_read"] for r in rows)
    lat_sum = sum(r[1]["latency"] for r in rows)
    return {"name": name, "rows": rows, "total": total, "tool_sum": tool_sum,
            "read_sum": read_sum, "lat_sum": lat_sum, "cost": _cost(total)}


async def main() -> None:
    cold = await run_arm("COLD", warm=False)
    warm = await run_arm("WARM", warm=True)
    print("\n================ PASSIVE INGESTION — measurement hinge (Haiku, 1 rep) ================")
    hdr = f"{'arm':6}{'tool_calls':>12}{'files_read':>12}{'in':>9}{'out':>8}{'cacheR':>9}{'cacheW':>9}{'lat(s)':>9}{'$imp':>10}"
    print(hdr)
    for a in (cold, warm):
        t = a["total"]
        print(f"{a['name']:6}{a['tool_sum']:>12}{a['read_sum']:>12}{t[0]:>9}{t[1]:>8}{t[2]:>9}{t[3]:>9}{a['lat_sum']:>9.1f}{a['cost']:>10.5f}")
    dc = warm["cost"] - cold["cost"]
    print(f"\nNET cheap-side: WARM ${warm['cost']:.5f} vs COLD ${cold['cost']:.5f} "
          f"-> {dc:+.5f} ({100*dc/cold['cost'] if cold['cost'] else 0:+.1f}%)")
    print(f"tool round-trips: COLD {cold['tool_sum']} -> WARM {warm['tool_sum']} "
          f"({warm['tool_sum']-cold['tool_sum']:+d}); files_read: {cold['read_sum']} -> {warm['read_sum']} "
          f"({warm['read_sum']-cold['read_sum']:+d})")


if __name__ == "__main__":
    asyncio.run(main())
