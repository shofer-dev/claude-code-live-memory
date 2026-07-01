"""#1 — ANSWER ACCURACY (not just token savings).

The token-savings benchmarks say nothing about whether the cheap memory's answers are
*right*. This asks live-memory a fixed set of codebase questions with author-verified
ground truth (including negative/"not present" questions to catch hallucination), and
grades each answer with an LLM judge (claude -p, blind to how the answer was produced).

Reports: correct / partial / incorrect rates and a hallucination rate (ungrounded claims),
plus how often the memory had to read files to answer. Optional `--direct` arm runs the
premium model reading the repo itself, graded the same way, as an upper-bound reference.

Usage:
  LM_BENCH_BASE=http://127.0.0.1:7712 server/.venv/bin/python benchmark/harness/accuracy.py
  (add `--direct` to also grade a premium read-the-repo arm)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = os.environ.get("LM_BENCH_BASE", "http://127.0.0.1:7712")
TARGET = os.environ.get("LM_BENCH_TARGET", str(Path(__file__).resolve().parents[2]))
JUDGE_MODEL = os.environ.get("LM_BENCH_JUDGE_MODEL", "claude-sonnet-4-6")
TIMEOUT = 90

# Author-verified ground truth about the live-memory codebase. `ref` is the correct
# answer; `negative` marks questions whose correct answer is "no / not present" — the
# ones that expose hallucination.
QA = [
    {"q": "What is the default port the server listens on?", "ref": "7711 (LIVE_MEMORY_PORT default)."},
    {"q": "What is the default value of compaction_floor, and what is it for?", "ref": "0.6 — the low watermark compaction compacts DOWN to (hysteresis; the trigger is compaction_threshold 0.85)."},
    {"q": "What is the default max_context_tokens?", "ref": "128000."},
    {"q": "Which env var toggles passive ingestion, and what is its default?", "ref": "LIVE_MEMORY_PASSIVE_INGESTION, default true (on)."},
    {"q": "Which method evicts file contexts and with what eviction policy?", "ref": "ContextWindow.enforce_limit — LRU by last_referenced_at (oldest first)."},
    {"q": "List the compaction tiers in _maybe_compact, in order.", "ref": "Tier 0: distill observed file content into the ledger + drop raw bytes; Tier 1: evict re-readable file contexts (LRU); Tier 2: neutrally summarize oldest Q&A into the ledger."},
    {"q": "How is a cwd canonicalized into the workspace key?", "ref": "Expanded/resolved and snapped to the enclosing git repo root (so a subdir and the repo root share one workspace)."},
    {"q": "With no API key but a Claude subscription present, what auth and model does it use?", "ref": "Subscription OAuth token (auto-refreshed) on Haiku (claude-haiku-4-5)."},
    {"q": "Which FileContext fields are excluded from the persisted snapshot?", "ref": "content and observed_at (raw teed bytes are in-memory only; snapshots keep the manifest)."},
    {"q": "What does clear_content do to a file context?", "ref": "Drops the raw teed bytes (content='') and collapses its token_estimate to the manifest one-liner cost, leaving a re-readable manifest entry."},
    {"q": "Which read-only tool(s) count as a 'file read' for staleness tracking?", "ref": "Read (FILE_READING_TOOLS = {'Read'}); Grep/Glob/find_paths do not."},
    {"q": "What are the arguments to the ask_live_memory tool?", "ref": "question, cwd (absolute), and timeout."},
    # negatives (correct answer is 'no'):
    {"q": "Does live-memory support a Redis backend for storing its memory?", "ref": "No. Persistence is a versioned JSON snapshot per workspace on disk; there is no Redis backend.", "negative": True},
    {"q": "Can the live-memory agent modify or write files in the repo?", "ref": "No. It is strictly read-only and path-jailed; its tools cannot edit, create, or run commands.", "negative": True},
    {"q": "Does ask_live_memory take a `model` parameter to pick the model per call?", "ref": "No. Its parameters are question, cwd, timeout; the model is server config (set via env or /live-memory-config), not per call.", "negative": True},
]

_TRAILER = re.compile(r"tool_calls=(\d+).*?files_read=(\d+)")
_LAT = re.compile(r"latency=([\d.]+)s")

# Files whose facts the QA set asks about — teed to warm the memory in the --warm arm
# (models the realistic already-in-use state; passive ingestion would have done this).
WARM_FILES = [f"server/live_memory/{f}" for f in
              ("config.py", "context_window.py", "manager.py", "workspace.py", "models.py",
               "tool_executor.py", "server.py", "keep_warm.py")]


def _post(path, body):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _clear():
    _post("/clear", {"cwd": TARGET})


def _warm():
    """Passively ingest the relevant files (no model warm-up) — the realistic in-use state."""
    contents = {}
    for f in WARM_FILES:
        try:
            contents[f] = (Path(TARGET) / f).read_text(encoding="utf-8")
        except OSError:
            pass
    return _post("/notify", {"kind": "edited", "cwd": TARGET, "paths": list(contents), "contents": contents}).get("applied", 0)


async def ask_lm(session, q):
    res = await session.call_tool("ask_live_memory", {"question": q, "cwd": TARGET, "timeout": TIMEOUT})
    text = res.content[0].text
    ans = text.split("\n\n---\n[live-memory]")[0].strip()
    m = _TRAILER.search(text.replace("\n", " ")); lat = _LAT.search(text)
    return ans, (int(m.group(2)) if m else -1), (float(lat.group(1)) if lat else -1.0)


def ask_direct(q):
    """Premium model reads the repo itself (upper-bound reference)."""
    prompt = (f"In the codebase at {TARGET}, answer this question, grounded in the actual code "
              f"(read files as needed). Be concise.\n\nQ: {q}")
    p = subprocess.run(["claude", "-p", prompt, "--model", JUDGE_MODEL, "--strict-mcp-config",
                        "--dangerously-skip-permissions", "--max-turns", "30", "--output-format", "json"],
                       cwd=TARGET, capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout).get("result", "").strip()
    except Exception:
        return "(direct arm failed)"


def judge(q, ref, ans, negative):
    rubric = ("A correct answer AGREES with the reference on the key fact. For a negative question "
              "(reference says 'no'/'not present'), claiming the feature exists is INCORRECT and ungrounded. "
              "grounded=false if the answer asserts specifics not supported by the reference (a hallucination).")
    prompt = (f"You are grading a codebase Q&A answer against a reference answer. {rubric}\n\n"
              f"QUESTION: {q}\nREFERENCE (ground truth): {ref}\nANSWER TO GRADE: {ans}\n\n"
              f'Respond with ONLY a one-line JSON object: '
              f'{{"verdict": "correct|partial|incorrect", "grounded": true|false, "why": "<=12 words"}}')
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", JUDGE_MODEL, "--strict-mcp-config",
                            "--dangerously-skip-permissions", "--max-turns", "1", "--output-format", "json"],
                           capture_output=True, text=True, timeout=180)
        result = json.loads(p.stdout).get("result", "")
        j = json.loads(re.search(r"\{.*\}", result, re.S).group(0))
        return j.get("verdict", "?"), bool(j.get("grounded", False)), j.get("why", "")
    except Exception as e:
        return "ERROR", False, str(e)[:40]


async def run_arm(name, answer_fn, session=None):
    rows = []
    for i, item in enumerate(QA):
        if session is not None:
            ans, reads, lat = await answer_fn(session, item["q"])
        else:
            ans, reads, lat = answer_fn(item["q"]), -1, -1.0
        v, grounded, why = judge(item["q"], item["ref"], ans, item.get("negative", False))
        rows.append({"i": i, "neg": item.get("negative", False), "verdict": v, "grounded": grounded,
                     "reads": reads, "lat": lat, "why": why, "ans": ans})
        tag = "NEG " if item.get("negative") else "    "
        print(f"[{name}] {tag}Q{i+1:2d} {v:9} grounded={grounded!s:5} reads={reads} :: {item['q'][:56]}")
        if v in ("incorrect", "ERROR") or not grounded:
            print(f"        judge: {why} | ans: {ans[:120].strip()}")
    return rows


def summarize(name, rows):
    n = len(rows)
    c = sum(r["verdict"] == "correct" for r in rows)
    p = sum(r["verdict"] == "partial" for r in rows)
    inc = sum(r["verdict"] == "incorrect" for r in rows)
    halluc = sum(not r["grounded"] for r in rows)
    negs = [r for r in rows if r["neg"]]
    neg_ok = sum(r["verdict"] == "correct" for r in negs)
    reads = [r["reads"] for r in rows if r["reads"] >= 0]
    print(f"\n== {name} (n={n}) ==")
    print(f"  correct {c}/{n} ({100*c/n:.0f}%) · partial {p} · incorrect {inc} · hallucinated(ungrounded) {halluc} ({100*halluc/n:.0f}%)")
    print(f"  negatives (no-hallucination) {neg_ok}/{len(negs)} correct")
    if reads:
        print(f"  answered with 0 file reads: {sum(1 for x in reads if x==0)}/{len(reads)}; avg reads {sum(reads)/len(reads):.1f}")


async def main():
    direct = "--direct" in sys.argv
    warm = "--warm" in sys.argv
    _clear()
    if warm:
        print(f"(warmed: teed {_warm()} files passively — realistic already-in-use state)")
    async with streamable_http_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            lm_rows = await run_arm("LM", ask_lm, s)
    summarize(f"live-memory ({'WARM' if warm else 'COLD'})", lm_rows)
    if direct:
        print("\n--- direct (premium reads the repo) arm ---")
        d_rows = await run_arm("DIRECT", ask_direct, None)
        summarize("direct-read (reference)", d_rows)
    print("\n(judge is an LLM — treat verdicts as high-signal, not ground truth; spot-check incorrect/ungrounded rows.)")


if __name__ == "__main__":
    asyncio.run(main())
