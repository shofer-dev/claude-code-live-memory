# Benchmark harness

Reproducible A/B harness for the Live Memory token-reduction benchmark. See
[`../README.md`](../README.md) for the experiment design and
[`../results/RESULTS.md`](../results/RESULTS.md) for findings.

## Prerequisites
- Live Memory server running on `127.0.0.1:7711` (cheap model = Haiku via Claude
  subscription; `LIVE_MEMORY_KEEP_WARM=false` for clean accounting).
- `claude` CLI authenticated (the building model; pinned to `claude-sonnet-4-6`).
- A clone of `extensions/shofer`; the worktree is built at the pinned base SHA.

## Scripts

### Shared
- **`setup_worktree.sh [path]`** — creates a *faithful build-env* git worktree of
  shofer at the pinned base commit (`32cdefc`): `pnpm install --offline` (correct
  workspace links to the worktree's own packages), builds the sibling `@shofer/*`
  packages so their `dist` exists, links `@shofer/vscode-shim`, and verifies the base
  `pnpm check-types` is clean. Cross-package edits propagate to `tsc` — required for
  the tool-wiring feature. Without this, `tsc` resolves `@shofer/types` from a stale
  prebuilt `dist` and any new-tool feature fails acceptance spuriously. The worktree
  is **isolated** from the live `extensions/shofer` working tree (a detached checkout
  in `/tmp`), so it never disturbs another session working on that submodule.
- **`analyze.py <stream.jsonl>`** — the mechanism extractor: from a `claude -p`
  `stream-json` transcript, emits premium tokens the building agent spent **reading
  the codebase** (`read_tok` = Read/Grep/Glob tool-result tokens), `ask_live_memory`
  call/token counts, edit-call count, turns, the final premium token usage
  (in/out/cache-read/cache-write), and an API-failure flag. The low-variance signal.

### Premium-side A/B (building model = Sonnet; measures the value proposition)
- **`run_understanding.sh [P K]`** — the regime where Live Memory **wins**: a
  read-only "trace the tool-call code path" task (synthesize across ~4k lines, no
  edits → offloaded reading isn't backfilled by edit context). `P` parallel workers ×
  `K` reps, each in its own worktree + per-`cwd` LM workspace. Mechanism + full token
  matrix + grading (the trace must hit the real identifiers). → `results.csv`.
  *Result: −42% premium $/turn, −97% codebase-reading tokens (see RESULTS.md).*
- **`run_sequence.sh`** — the **compounding** A/B: a sequence of read-only tools
  (`count_lines → count_chars → count_bytes → count_words`) added one per feature by a
  **fresh cold agent**, on an **accumulating** worktree; the with-arm's Live Memory
  persists across the whole sequence (warmed for free by passive ingestion), the
  without-arm re-explores each time. Tests whether accumulation rescues edit-bound
  work. → per-feature `results.csv`. *Result: break-even net (edit work is execution-
  bound); see RESULTS.md.*
- **`ab_single.sh`** — one A/B run of the lead feature (new `count_lines` tool):
  arm II (no Live Memory) then arm I (clear → warm-up → feature). Captures premium
  tokens (`stream-json`, kill-safe) and cheap tokens (`/stats` deltas) with cache
  read/write split, imputes cost at published rates, prints a table.
- **`run_reps.sh [K]`** / **`run_reps2.sh`** / **`run_parallel.sh <P> <K>`** —
  feature-replicate batches → `results.csv` → aggregate mean ± spread over the
  **valid** reps only (`status=complete`, `api_error=no`, acceptance `FEATURE=yes`).
  `run_reps2.sh` adds **`--strict-mcp-config`** so the global `live-memory@shofer`
  plugin can't leak `ask_live_memory` into the *without* arm (the confound that
  invalidated the earliest runs). `run_parallel.sh` runs `P` workers each in its own
  worktree + LM workspace (per-`cwd` clear, not `clear_all`) for the K=20 batch.

### Cheap-side benchmarks (passive ingestion; LM's own Haiku tokens via the MCP transport)
- **`passive_hinge.py`** — the FUTURE_DIRECTIONS §1 measurement hinge: COLD (today's
  behavior) vs WARM (files teed via `/notify` first), same questions. Does a
  passively-warmed window make `ask_live_memory` cheaper net of the bloat it adds?
  *Result: −21%, 0 re-reads, ~40% lower latency when the working set fits the window.*
- **`passive_compaction.py`** — passive ingestion under **window overflow**: 22
  questions, 20 files teed progressively into a deliberately small window so
  observations + Q&A overflow and force repeated compaction; includes late "survival"
  re-probes of early (distilled) files. Surfaced + verified the compaction-hysteresis
  and parallel-commit fixes. *Result + diagnosis: see RESULTS.md.*
  Override target/server with `LM_BENCH_TARGET` / `LM_BENCH_BASE` env vars.

## Bounds & validity (learned the hard way)
- **No OS `timeout`** on agent runs — a SIGKILL corrupts the JSON ledger.
  `--max-turns` is the only (clean) bound; a hit still emits the final JSON.
- **API failures** (`ConnectionRefused` / `api_retry` storms — the env's flaky
  external DNS) are detected and mark a run **INVALID**, never scored.
- **Acceptance verifies the feature exists** (non-empty diff + `count_lines` in
  the tool-name schema + `tsc` green) — a no-op no longer passes (the base
  type-checks, so `tsc`-green alone is not sufficient).

## Run
```sh
# Premium-side A/B (the value-proposition tests)
bash setup_worktree.sh /tmp/pilot/pwt1                  # build a faithful worktree once
RUNS=/tmp/pilot/understanding bash run_understanding.sh 2 6   # understanding-bound, 2 workers × 6 reps
RUNS=/tmp/pilot/sequence      bash run_sequence.sh           # compounding 4-feature sequence
bash run_parallel.sh 4 20                               # edit-bound feature, K=20 replicates

# Cheap-side passive-ingestion benchmarks (need a server with the new code)
server/.venv/bin/python benchmark/harness/passive_hinge.py            # fits-in-window: warm vs cold
LIVE_MEMORY_MAX_CONTEXT_TOKENS=24000 …python -m live_memory &         # small window to force compaction
server/.venv/bin/python benchmark/harness/passive_compaction.py       # overflow + compaction
```
Results land in `results/` (per-task subdirs); see [`../results/RESULTS.md`](../results/RESULTS.md).
