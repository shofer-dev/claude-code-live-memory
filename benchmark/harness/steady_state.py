"""#3 — STEADY-STATE (no warm-up) + PERSISTENCE across restart.

The −42% headline assumes an *already-warm* memory; our A/B paid a cold warm-up. This
isolates the honest already-in-use number, and proves the memory survives a restart.

Manages its OWN live-memory server subprocess (isolated port + data dir; it must, to
restart it). Three phases, same question batch each time, against the live-memory repo:

  COLD    — cleared memory; the model must explore (reads files) to answer.
  WARM    — steady state: memory pre-populated for FREE via passive ingestion (files
            teed to /notify, no model warm-up); should answer with ~0 reads, cheaper.
  RESTART — after WARM, kill + restart the server (same data dir) and re-ask; the
            accumulated memory reloads from its on-disk snapshot, so previously-answered
            questions stay cheap/correct WITHOUT re-reading — proving persistence.

Reports per phase: file reads, imputed cheap-model $, and answers-returned.

Usage:  server/.venv/bin/python benchmark/harness/steady_state.py
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO / "server"
VENV_PY = SERVER_DIR / ".venv" / "bin" / "python"
PORT = int(os.environ.get("LM_BENCH_PORT", "7714"))
BASE = f"http://127.0.0.1:{PORT}"
TARGET = str(REPO)
DATA_DIR = os.environ.get("LM_BENCH_DATA_DIR", "/tmp/lm-steady-data")
TIMEOUT = 90

FILES = [f"server/live_memory/{f}" for f in
         ("models.py", "context_window.py", "manager.py", "config.py", "workspace.py",
          "server.py", "summarizer.py", "keep_warm.py")]
QUESTIONS = [
    "Which method evicts file contexts and with what policy?",
    "What are the compaction tiers in _maybe_compact, in order?",
    "How is a cwd canonicalized into the workspace key?",
    "Which env var toggles passive ingestion and what is its default?",
    "What does observe() do and how does it differ from note_modified()?",
    "How does commit_window decide whether to adopt a fork in parallel mode?",
]
HAI = dict(i=1.0, o=5.0, cr=0.10, cw=1.25)  # imputed $/1e6


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def _post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def stats():
    s = _get(f"{BASE}/stats?cwd={quote(TARGET)}")
    return (s["inputTokens"], s["outputTokens"], s["cacheReadTokens"], s["cacheWriteTokens"])


def cost(d):
    i, o, cr, cw = d
    return (i * HAI["i"] + o * HAI["o"] + cr * HAI["cr"] + cw * HAI["cw"]) / 1e6


def start_server():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LIVE_MEMORY_") and k not in ("ANTHROPIC_API_KEY",)}
    env.update({"LIVE_MEMORY_PORT": str(PORT), "LIVE_MEMORY_DATA_DIR": DATA_DIR,
                "LIVE_MEMORY_KEEP_WARM": "false", "PYTHONUNBUFFERED": "1"})
    proc = subprocess.Popen([str(VENV_PY), "-m", "live_memory"], cwd=str(SERVER_DIR), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if _get(f"{BASE}/health")["status"] == "ok":
                return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("server did not become healthy")


def stop_server(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def tee_all():
    contents = {f: (REPO / f).read_text(encoding="utf-8") for f in FILES}
    return _post("/notify", {"kind": "edited", "cwd": TARGET, "paths": FILES, "contents": contents})["applied"]


async def ask_batch():
    reads = 0
    answered = 0
    async with streamable_http_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for q in QUESTIONS:
                res = await s.call_tool("ask_live_memory", {"question": q, "cwd": TARGET, "timeout": TIMEOUT})
                text = res.content[0].text
                ans = text.split("\n\n---\n[live-memory]")[0].strip()
                import re
                m = re.search(r"files_read=(\d+)", text)
                reads += int(m.group(1)) if m else 0
                if ans and not ans.lower().startswith("error"):
                    answered += 1
    return reads, answered


async def phase(label, prep=None):
    base = stats()
    if prep:
        prep()
    reads, answered = await ask_batch()
    d = tuple(stats()[j] - base[j] for j in range(4))
    print(f"[{label:8}] file_reads={reads:2d}  answered={answered}/{len(QUESTIONS)}  cheap$={cost(d):.5f}")
    return {"label": label, "reads": reads, "answered": answered, "cost": cost(d)}


async def main():
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    for f in Path(DATA_DIR).glob("*.json"):
        f.unlink()  # clean slate
    proc = start_server()
    try:
        # COLD: cleared → must explore
        _post("/clear", {"cwd": TARGET})
        cold = await phase("COLD")

        # WARM: steady state — pre-populated for free via passive ingestion, no warm-up query
        _post("/clear", {"cwd": TARGET})
        warm = await phase("WARM", prep=lambda: print(f"           (teed {tee_all()} files passively — no model warm-up)"))
    finally:
        stop_server(proc)

    # RESTART: same data dir; accumulated memory must reload from the snapshot
    print("           (server restarted — reloading memory from on-disk snapshot)")
    proc = start_server()
    try:
        restart = await phase("RESTART")
    finally:
        stop_server(proc)

    print("\n================ STEADY-STATE + PERSISTENCE ================")
    print(f"{'phase':10}{'file_reads':>12}{'answered':>10}{'cheap$':>10}")
    for p in (cold, warm, restart):
        print(f"{p['label']:10}{p['reads']:>12}{p['answered']:>10}/{len(QUESTIONS)}{p['cost']:>10.5f}")
    dr = 100 * (warm["reads"] - cold["reads"]) / cold["reads"] if cold["reads"] else 0
    dc = 100 * (warm["cost"] - cold["cost"]) / cold["cost"] if cold["cost"] else 0
    print(f"\nsteady-state (WARM vs COLD): file_reads {cold['reads']}→{warm['reads']} ({dr:+.0f}%) · cheap$ {dc:+.0f}%")
    print(f"persistence (after restart): file_reads={restart['reads']} answered={restart['answered']}/{len(QUESTIONS)} "
          f"— {'PASS (memory reloaded, stayed cheap)' if restart['reads'] <= warm['reads'] + 2 and restart['answered']==len(QUESTIONS) else 'CHECK'}")


if __name__ == "__main__":
    asyncio.run(main())
