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
- **`setup_worktree.sh [path]`** — creates a *faithful build-env* git worktree of
  shofer at the pinned base commit: `pnpm install --offline` (correct workspace
  links to the worktree's own packages), builds the sibling `@shofer/*` packages
  so their `dist` exists, links `@shofer/vscode-shim`, and verifies the base
  `pnpm check-types` is clean. Cross-package edits propagate to `tsc` — required
  for the tool-wiring feature. Without this, `tsc` resolves `@shofer/types` from a
  stale prebuilt `dist` and any new-tool feature fails acceptance spuriously.
- **`ab_single.sh`** — one A/B run of the lead feature (new `count_lines` tool):
  arm II (no Live Memory) then arm I (clear → warm-up → feature). Captures premium
  tokens (`stream-json`, kill-safe) and cheap tokens (`/stats` deltas) with cache
  read/write split, imputes cost at published rates, and prints a table.
- **`run_reps.sh [K]`** — K independent A/B replicates → `results.csv` →
  aggregate mean ± spread over the **valid** reps only (a rep is valid iff
  `status=complete`, `api_error=no`, and acceptance `FEATURE=yes`).

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
bash setup_worktree.sh /tmp/pilot/shofer     # build the faithful worktree once
bash run_reps.sh 4                            # 4 replicates + aggregate
```
