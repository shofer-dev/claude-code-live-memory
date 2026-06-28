"""Longer, realistic passive-ingestion benchmark that OVERFLOWS the window and
forces repeated compaction (tier-0 observation distillation + tier-2 Q&A summary).

A simulated interactive session: progressively tee 20 files (the agent's own I/O)
and ask 22 understanding questions. The small window (set on the server) can't hold
all observations → Live Memory compacts. Late "survival" questions re-probe files
teed early (long since distilled into the ledger, raw bytes gone) to test whether
knowledge SURVIVES summarization, and at what cost (answered from the ledger with
0 reads, or re-read?).

COLD (today's behavior — no teeing, LM explores) vs WARM (passive). Real Haiku.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = os.environ.get("LM_BENCH_BASE", "http://127.0.0.1:7712")
TARGET = os.environ.get("LM_BENCH_TARGET", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
TIMEOUT = 90
P = "server/live_memory"

# 20 files teed over the first 10 rounds (2/round); also the probe set.
FILES = [
    f"{P}/models.py", f"{P}/context_window.py", f"{P}/manager.py", f"{P}/summarizer.py",
    f"{P}/config.py", f"{P}/llm_client.py", f"{P}/keep_warm.py", f"{P}/workspace.py",
    f"{P}/server.py", f"{P}/tool_executor.py", f"{P}/question_queue.py", f"{P}/conversation_store.py",
    f"{P}/oauth.py", f"{P}/pricing.py", f"{P}/directory_tree.py", f"{P}/async_jobs.py",
    f"{P}/prompts.py", f"{P}/logging_setup.py", f"{P}/__main__.py", f"{P}/__init__.py",
]

# (question, is_survival). The first 16 probe FILES[0..15] in order; 17-22 re-probe
# files teed in the FIRST rounds (now distilled) + one cross-cutting trace.
PROBES = [
    ("In the models module, what are the two evictable kinds of content, and how is a stale FileContext represented?", False),
    ("In the context window, which method evicts file contexts and with what policy? What does clear_content do?", False),
    ("List the compaction tiers in _maybe_compact, in order.", False),
    ("What is the transcript size cap in the summarizer, and which end of the transcript does it keep?", False),
    ("How is a cwd canonicalized into a workspace key, and what is the default provider and model?", False),
    ("Where are the Anthropic cache_control breakpoints placed in the LLM client?", False),
    ("What conditions make a workspace eligible for keep-warm?", False),
    ("What does observe() do, and how does it differ from note_modified()?", False),
    ("Which HTTP routes does the server expose besides the MCP tool?", False),
    ("Which read-only tools does the executor offer, and which ones count as file reads?", False),
    ("How does the question queue bound concurrency and enforce timeouts?", False),
    ("How are persisted file contexts validated when a snapshot is loaded?", False),
    ("How does subscription OAuth obtain and refresh its token?", False),
    ("How is per-model cost estimated?", False),
    ("How is the directory-tree block kept within a size budget?", False),
    ("How does the fire-and-forget async job runner work?", False),
    # ── survival re-probes of early-teed (now-distilled) files ──
    ("Back to the data model: which FileContext fields are excluded from the persisted snapshot, and why?", True),
    ("In the context window again: how does enforce_limit choose what to evict, and when does it report still-over?", True),
    ("In the manager: after _distill_observations summarizes observations, what happens to their raw bytes?", True),
    ("Is the knowledge-ledger summary query-agnostic, and why does that matter for a shared memory?", True),
    ("Which env var toggles passive ingestion, and what is its default?", True),
    ("Trace end-to-end what happens when a file is teed via /notify and that file's bytes later overflow the window.", True),
]

HAI = dict(i=1.0, o=5.0, cr=0.10, cw=1.25)  # imputed $/1e6


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _stats() -> dict:
    with urllib.request.urlopen(f"{BASE}/stats?cwd={quote(TARGET)}", timeout=10) as r:
        return json.loads(r.read())


def _toks(s: dict) -> tuple:
    return (s["inputTokens"], s["outputTokens"], s["cacheReadTokens"], s["cacheWriteTokens"])


def _cost(d: tuple) -> float:
    i, o, cr, cw = d
    return (i * HAI["i"] + o * HAI["o"] + cr * HAI["cr"] + cw * HAI["cw"]) / 1e6


_TRAILER = re.compile(r"tool_calls=(\d+).*?files_read=(\d+).*?context=(\d+)/\d+\(([\d.]+)%\)")
_LAT = re.compile(r"latency=([\d.]+)s")


async def ask(session: ClientSession, q: str) -> tuple:
    res = await session.call_tool("ask_live_memory", {"question": q, "cwd": TARGET, "timeout": TIMEOUT})
    text = res.content[0].text
    flat = text.replace("\n", " ")
    m, lat = _TRAILER.search(flat), _LAT.search(flat)
    meta = {"tool_calls": int(m.group(1)) if m else -1, "files_read": int(m.group(2)) if m else -1,
            "fill": float(m.group(4)) if m else -1.0, "latency": float(lat.group(1)) if lat else -1.0}
    answer = text.split("\n\n---\n[live-memory]")[0]
    return answer, meta


def tee(files: list[str]) -> int:
    contents = {f: (Path(TARGET) / f).read_text(encoding="utf-8") for f in files}
    return _post("/notify", {"kind": "edited", "cwd": TARGET, "paths": files, "contents": contents})["applied"]


async def run_arm(name: str, warm: bool) -> dict:
    _post("/clear", {"cwd": TARGET})
    rows, tee_i = [], 0
    base = _toks(_stats())
    async with streamable_http_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for qi, (q, surv) in enumerate(PROBES):
                if warm and qi < 10:  # tee 2 files/round for the first 10 rounds (20 files)
                    tee(FILES[tee_i:tee_i + 2]); tee_i += 2
                before = _toks(_stats())
                ans, meta = await ask(s, q)
                st = _stats()
                after = _toks(st)
                delta = tuple(after[j] - before[j] for j in range(4))
                rows.append({"qi": qi, "surv": surv, "meta": meta, "delta": delta,
                             "sw": st["summariesWritten"], "fc": st["contextWindow"]["fileContexts"],
                             "stale": st["contextWindow"]["staleFileContexts"], "ans": ans})
                tag = "SURV" if surv else "    "
                print(f"[{name}] {tag} Q{qi+1:2d}: tools={meta['tool_calls']} reads={meta['files_read']} "
                      f"fill={meta['fill']:4.1f}% summaries={st['summariesWritten']} fc={st['contextWindow']['fileContexts']} "
                      f"lat={meta['latency']:4.1f}s ${_cost(delta):.5f}")
    total = tuple(_toks(_stats())[j] - base[j] for j in range(4))
    return {"name": name, "rows": rows, "total": total, "cost": _cost(total),
            "summaries": _stats()["summariesWritten"],
            "tool_sum": sum(r["meta"]["tool_calls"] for r in rows),
            "read_sum": sum(r["meta"]["files_read"] for r in rows),
            "lat_sum": sum(r["meta"]["latency"] for r in rows),
            "peak_fill": max(r["meta"]["fill"] for r in rows)}


async def main() -> None:
    cold = await run_arm("COLD", warm=False)
    print()
    warm = await run_arm("WARM", warm=True)

    print("\n================ PASSIVE INGESTION under COMPACTION (Haiku, 24k window, 22 Q) ================")
    hdr = f"{'arm':6}{'tools':>7}{'reads':>7}{'summaries':>11}{'peakfill%':>11}{'lat(s)':>9}{'$imp':>10}"
    print(hdr)
    for a in (cold, warm):
        print(f"{a['name']:6}{a['tool_sum']:>7}{a['read_sum']:>7}{a['summaries']:>11}{a['peak_fill']:>11.1f}{a['lat_sum']:>9.1f}{a['cost']:>10.5f}")
    dc = warm["cost"] - cold["cost"]
    print(f"\nNET cheap-side: WARM ${warm['cost']:.5f} vs COLD ${cold['cost']:.5f} -> "
          f"{dc:+.5f} ({100*dc/cold['cost'] if cold['cost'] else 0:+.1f}%)")
    print(f"tool round-trips COLD {cold['tool_sum']} -> WARM {warm['tool_sum']}; "
          f"files_read COLD {cold['read_sum']} -> WARM {warm['read_sum']}; "
          f"compactions COLD {cold['summaries']} / WARM {warm['summaries']}")

    # survival: did WARM answer the late re-probes of early-distilled files cheaply?
    print("\n---- SURVIVAL re-probes (early files, long since distilled into the ledger) ----")
    cold_s = {r["qi"]: r for r in cold["rows"] if r["surv"]}
    for r in warm["rows"]:
        if not r["surv"]:
            continue
        c = cold_s.get(r["qi"], {})
        print(f"Q{r['qi']+1}: WARM reads={r['meta']['files_read']} tools={r['meta']['tool_calls']} | "
              f"COLD reads={c.get('meta',{}).get('files_read','?')} tools={c.get('meta',{}).get('tool_calls','?')}")
        print(f"   WARM answer: {r['ans'][:240].strip()}")


if __name__ == "__main__":
    asyncio.run(main())
