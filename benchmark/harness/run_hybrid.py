#!/usr/bin/env python3
"""HYBRID A/B — realistic understand-then-edit tasks (bug fixes + feature requests).

Each task requires tracing across files to locate/scope the work, then a small localized
edit, with OBJECTIVE fail->pass acceptance. This is the realistic middle between the
pure-understanding trace A/B (Live Memory wins big) and the pure-wiring feature A/B
(break-even). Per task, per arm: reset a pinned shofer worktree → apply the task's setup
patch (a bug injection, or a failing feature test) → run the building agent (`claude -p`,
±`ask_live_memory`) → run the acceptance command → measure premium tokens (analyze.py) and
whether it passed.

Tasks manifest: hybrid_tasks.json (schema: id,type,title,prompt,setup_patch,accept_cmd,
trace_files,edit_files). Patches are applied via `git apply` from stdin.

Usage: RUNS=/tmp/pilot/hybrid server/.venv/bin/python benchmark/harness/run_hybrid.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

HARNESS = Path(__file__).resolve().parent
AN = HARNESS / "analyze.py"
WO_WT = os.environ.get("WO_WT", "/tmp/pilot/pwt1")   # without-arm worktree
WI_WT = os.environ.get("WI_WT", "/tmp/pilot/pwt2")   # with-arm worktree
RUNS = Path(os.environ.get("RUNS", "/tmp/pilot/hybrid")); RUNS.mkdir(parents=True, exist_ok=True)
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
TASKS_FILE = os.environ.get("TASKS", str(HARNESS / "hybrid_tasks.json"))
BASE = os.environ.get("SHOFER_BASE", "32cdefcba07ee9afde9bf65b373a75531f015d96")  # pinned commit — reproducibility
HANG = os.environ.get("HANG_GUARD", "2400")
LMCFG = '{"mcpServers":{"live-memory":{"type":"http","url":"http://127.0.0.1:7711/mcp"}}}'
LM_BASE = "http://127.0.0.1:7711"
SYS = ("When working in this codebase you have a 'live_memory' companion via the ask_live_memory tool "
       "(when wired). For ANY question about how the existing code works, where something is defined, "
       "what calls what, or how a subsystem fits together, ASK live_memory FIRST - before "
       "reading/grepping/globbing source yourself. Pass the absolute repo root as cwd. Read a file "
       "directly only when you need its exact current contents to edit it. If the tool is not "
       "available, fall back to reading files.")
SON = dict(pin=3, pout=15, pcr=.30, pcw=3.75)   # $/1e6 (Sonnet, imputed)
HAI = dict(i=1, o=5, cr=.10, cw=1.25)           # $/1e6 (Haiku, imputed)


def sh(cmd, cwd=None, timeout=3000):
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)


def reset(wt):
    sh(["git", "checkout", "--", "."], cwd=wt); sh(["git", "clean", "-fd"], cwd=wt)


def ensure_pinned_worktree(wt):
    """Reproducibility guard: the worktree must be the PINNED shofer commit. Build it if
    missing (setup_worktree.sh), then hard-assert HEAD == BASE — abort otherwise so results
    can never be produced against an unpinned tree."""
    if not (Path(wt) / "src").is_dir():
        print(f"  [setup] building pinned worktree at {wt} …")
        r = sh([str(HARNESS / "setup_worktree.sh"), wt], timeout=1800)
        if r.returncode != 0:
            sys.exit(f"FATAL: setup_worktree.sh failed for {wt} (need shofer repo; set SHOFER_DIR).\n{r.stderr[-500:]}")
    head = sh(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
    if head != BASE:
        sys.exit(f"FATAL: {wt} is at {head[:12]}, not the pinned base {BASE[:12]}. "
                 f"Reproducibility requires the pinned commit — recreate with setup_worktree.sh.")
    reset(wt)
    print(f"  [ok] {wt} @ pinned {BASE[:12]}")


def apply_patch(wt, patch_text):
    p = subprocess.run(["git", "apply", "-"], cwd=wt, input=patch_text, text=True, capture_output=True)
    return p.returncode == 0, p.stderr.strip()


def clear_lm(cwd):
    req = urllib.request.Request(f"{LM_BASE}/clear", data=json.dumps({"cwd": cwd}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try: urllib.request.urlopen(req, timeout=20).close()
    except Exception: pass


def lm_stats(cwd):
    try:
        with urllib.request.urlopen(f"{LM_BASE}/stats?cwd={quote(cwd)}", timeout=10) as r:
            d = json.loads(r.read())
        return (d["inputTokens"], d["outputTokens"], d["cacheReadTokens"], d["cacheWriteTokens"])
    except Exception:
        return (0, 0, 0, 0)


def to(): return ["timeout", HANG] if int(HANG) > 0 else []


def warmup(wt):
    q = (f"Use ask_live_memory once: question='Explore and understand this codebase and give a "
         f"high-level summary of its architecture, main subsystems and conventions', cwd='{wt}', "
         f"timeout=240. Then report it.")
    sh(to() + ["claude", "-p", q, "--model", MODEL, "--mcp-config", LMCFG, "--strict-mcp-config",
               "--allowedTools", "mcp__live-memory__ask_live_memory", "--dangerously-skip-permissions",
               "--max-turns", "6", "--output-format", "json"], cwd=wt)


def run_agent(wt, prompt, with_lm, out):
    argv = to() + ["claude", "-p", prompt, "--model", MODEL, "--append-system-prompt", SYS,
                   "--strict-mcp-config", "--dangerously-skip-permissions", "--max-turns", "200",
                   "--output-format", "stream-json", "--verbose"]
    if with_lm: argv += ["--mcp-config", LMCFG]
    p = sh(argv, cwd=wt)
    Path(out).write_text(p.stdout)


def analyze(stream):
    p = sh([sys.executable, str(AN), stream])
    try: return json.loads(p.stdout)
    except Exception: return {"turns": 0, "read_tok": 0, "lm_calls": 0, "pin": 0, "pout": 0, "pcr": 0, "pcw": 0, "status": "KILLED"}


def prem_usd(m): return (m["pin"]*3 + m["pout"]*15 + m["pcr"]*.30 + m["pcw"]*3.75) / 1e6


def run_task(task):
    rows = []
    for arm, wt in (("without", WO_WT), ("with", WI_WT)):
        reset(wt)
        if task.get("setup_patch", "").strip():
            ok, err = apply_patch(wt, task["setup_patch"])
            if not ok:
                print(f"  [{task['id']}/{arm}] SETUP PATCH FAILED: {err[:200]}");
                rows.append({**base_row(task, arm), "status": "setup_fail"}); continue
        cheap0 = lm_stats(wt)
        if arm == "with":
            clear_lm(wt); warmup(wt); cheap0 = lm_stats(wt)  # count only the task's cheap tokens, not warm-up
        out = RUNS / f"{task['id']}_{arm}.jsonl"
        run_agent(wt, task["prompt"], with_lm=(arm == "with"), out=str(out))
        passed = sh(task["accept_cmd"], cwd=wt).returncode == 0
        m = analyze(str(out))
        cheap = tuple(lm_stats(wt)[i] - cheap0[i] for i in range(4)) if arm == "with" else (0, 0, 0, 0)
        row = {**base_row(task, arm), "passed": passed, "read_tok": m["read_tok"], "turns": m["turns"],
               "lm_calls": m.get("lm_calls", 0), "prem_usd": round(prem_usd(m), 4), "status": m["status"],
               "cheap_usd": round((cheap[0]*1 + cheap[1]*5 + cheap[2]*.10 + cheap[3]*1.25) / 1e6, 4)}
        rows.append(row)
        print(f"  [{task['id']}/{arm:7}] pass={passed} read_tok={m['read_tok']} turns={m['turns']} "
              f"lm_calls={m.get('lm_calls',0)} prem=${row['prem_usd']}")
    return rows


def base_row(task, arm):
    return {"id": task["id"], "type": task["type"], "arm": arm, "passed": False, "read_tok": 0,
            "turns": 0, "lm_calls": 0, "prem_usd": 0.0, "cheap_usd": 0.0, "status": "?"}


def main():
    tasks = json.load(open(TASKS_FILE))
    # Reproducibility: both arms run against the PINNED shofer commit (set up if needed).
    ensure_pinned_worktree(WO_WT); ensure_pinned_worktree(WI_WT)
    print(f"pinned base = {BASE}  ·  tasks = {TASKS_FILE}")
    all_rows = []
    for t in tasks:
        print(f"###### {t['id']} ({t['type']}): {t['title']} ######")
        all_rows += run_task(t)
    # write CSV
    cols = ["id", "type", "arm", "passed", "read_tok", "turns", "lm_calls", "prem_usd", "cheap_usd", "status"]
    with open(RUNS / "results.csv", "w") as f:
        f.write(",".join(cols) + "\n")
        for r in all_rows:
            f.write(",".join(str(r[c]) for c in cols) + "\n")
    # summary
    wo = [r for r in all_rows if r["arm"] == "without"]; wi = [r for r in all_rows if r["arm"] == "with"]
    print("\n================ HYBRID A/B (bug fixes + feature requests) ================")
    print(f"{'task':8}{'WO pass':>9}{'WI pass':>9}{'WO read_tok':>13}{'WI read_tok':>13}{'WO prem$':>10}{'WI prem$':>10}")
    for a, b in zip(wo, wi):
        print(f"{a['id']:8}{str(a['passed']):>9}{str(b['passed']):>9}{a['read_tok']:>13}{b['read_tok']:>13}{a['prem_usd']:>10.4f}{b['prem_usd']:>10.4f}")
    rt_wo = sum(r["read_tok"] for r in wo); rt_wi = sum(r["read_tok"] for r in wi)
    u_wo = sum(r["prem_usd"] for r in wo); u_wi = sum(r["prem_usd"] for r in wi)
    print(f"\ncumulative read_tok: without {rt_wo} -> with {rt_wi} ({100*(rt_wi-rt_wo)/rt_wo if rt_wo else 0:+.0f}%)")
    print(f"cumulative premium$: without {u_wo:.3f} -> with {u_wi:.3f} ({100*(u_wi-u_wo)/u_wo if u_wo else 0:+.0f}%)")
    print(f"acceptance: without {sum(r['passed'] for r in wo)}/{len(wo)}  with {sum(r['passed'] for r in wi)}/{len(wi)}")
    print(f"with-arm cheap$ (Haiku, excl warm-up): {sum(r['cheap_usd'] for r in wi):.4f}")


if __name__ == "__main__":
    main()
