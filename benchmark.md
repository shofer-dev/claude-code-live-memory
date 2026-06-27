# Live Memory — token-reduction benchmark

**Status: plan finalized; pilot pending.** This is the living record — the
methodology now, the results later (§9).

## 1. What we claim (and what we don't)

Live Memory offloads codebase Q&A from the expensive **building** model to a
cheap, large-context companion. So the defensible claim is:

> **−N% premium-model tokens** and **−M% cost** to reach the same working result.

**Not** "−X% total tokens" — Live Memory *adds* cheap-model tokens (it re-reads
files in its own context). We report **premium ↓, cheap ↑, net cost ↓**,
transparently. Claiming total-token reduction would be misleading: we'd just be
moving tokens to a cheaper model — which *is* the point, but we say so.

## 2. The mechanism we measure

When an agent must understand existing code:
- **Without Live Memory** → it `Read`/`Grep`s the files → bulky tokens in the
  *premium* model's context.
- **With Live Memory** → it asks `ask_live_memory` → a concise answer from the
  *cheap* model; the file-reading happens in Live Memory's context, and
  accumulated knowledge is reused across questions and agents.

Saving = premium tokens avoided − the (cheap) cost of the queries.

## 3. Task: feature-work on a real codebase (`extensions/shofer`)

A toy built from scratch would show ~nothing (little existing code to query).
Live Memory shines on a **large existing codebase repeatedly queried**, so we use
**shofer** — our own product, so we can define features *and* verify correctness
authoritatively, on a real codebase (more credible than a game).

**Base commit (pinned):** every run checks out shofer at exactly
`32cdefcba07ee9afde9bf65b373a75531f015d96` — never `master` (a moving target).

**Features**: Phase 1 of the shofer evolution roadmap
(`todos/opencode_inspired_work.md`), initiatives **#3 → #6**, each scoped to a
**bounded, headless-testable slice**:

| # | Slice | Why it fits |
|---|---|---|
| **#3** schema-as-contract | convert tools to one typed schema, **one tool at a time** (`src/core/tools/`) | naturally sequential + read-heavy on shared tool machinery → ideal; the doc itself says do it incrementally |
| **#4** unify permissions | one ordered allow/ask/deny engine; migrate one system behind an adapter | self-contained; read-heavy (3 existing systems) |
| **#5** event-sourced persistence | retire flat-file state for one slice | read-heavy (current state subsystem) |
| **#6** structured cancellation | reliable cancel/timeout | self-contained |

All are "internal, no server/UI" (headless-testable, no extension-host flakiness),
dependency-ordered (a legit sequence), and **comprehension-heavy** — exactly Live
Memory's leverage. Acceptance per feature = **existing shofer tests stay green +
the feature's new test passes** (no regressions = objective "it works").

## 4. Design: sequential, fresh subagent per feature

- **One fresh subagent per feature, run in sequence** — *not* the main session.
  An operator with accumulated context would skip reads/queries it already
  "knows," biasing both arms; a cold agent is the only fair operator.
- **Identical task prompt** in both arms (each says *"use `ask_live_memory` for
  codebase questions when available"*).
- **The only difference between arms**: whether `ask_live_memory` is wired — so
  that line is a no-op in the "without" arm. Same model, tools, prompts.
- **Accumulation lives in Live Memory, not the agent.** Each feature's agent
  starts cold; Live Memory persists across the sequence (with-arm), so feature C
  reuses what A and B taught it, while the without-arm re-reads every time. **That
  cumulative divergence is the headline.**
- **Clean slate per run**: `/live-memory-empty` (= `POST /clear {all:true}`) wipes
  accumulated memory before each run, so runs are independent.

## 5. Harness (runnable by us, reproducible by anyone)

The operating agents must actually *have* `ask_live_memory` — which a normal
agent harness/subagent does **not** by default. The public `claude` CLI solves
it via per-invocation MCP config:

| | with Live Memory (arm I) | without (arm II) |
|---|---|---|
| invocation | `claude -p "<prompt>" --mcp-config <live-memory>` | `claude -p "<prompt>"` |
| premium tokens | `--output-format json` → `usage` | same |
| cheap tokens / $ | Live Memory `/stats` | n/a |

Per **feature × arm**: fresh **shofer worktree** at the pinned base commit
(`32cdefc`) → reset
`LIVE_MEMORY_DATA_DIR` → `claude -p` (± `--mcp-config`) capturing the JSON usage +
transcript → run **`pnpm test`** acceptance → snapshot `/stats` → record under
`runs/<arm>/<feature>/`. Aggregate → per-feature + cumulative deltas.

**Validated (smoke):** a headless `claude -p --mcp-config` agent *called*
`ask_live_memory`, `--output-format json` returned usage, and Live Memory logged
the query (`/stats` `questionsAnswered` ticked). The harness is feasible by us and
replayable by anyone with the `claude` CLI.

## 6. Reproducibility & inspectability

For agents, "reproducible" means **deterministic setup + fully recorded runs + a
re-runnable harness yielding consistent statistics** — *not* bit-identical reruns
(impossible for LLMs; claiming otherwise is dishonest).

- **Pin**: shofer commit SHA, model IDs (building + Live Memory), prompts
  (verbatim), config/tool-sets, temperature 0, a Dockerfile + lockfiles.
- **Record** (per run): full transcripts, per-agent **premium** token ledgers +
  `/stats` **cheap** tokens, the produced diffs, acceptance logs, and Live
  Memory's **Q&A log** — as raw data, so the headline % is *recomputable*.
- **Caveat**: server-side models drift → the *recorded runs* are the durable
  artifact; re-running is best-effort against then-current models.
- **Publish**: harness + pinned inputs + recorded runs + aggregation, so reviewers
  can inspect *or* replay.

## 7. Measurement

- Premium tokens = Σ(subagents) input + output, from `--output-format json`.
- Cheap tokens / $ from Live Memory `/stats`.
- Cost = premium·rate + cheap·rate.
- **≥ 3–5 runs/arm** (agents are stochastic) → report mean ± spread, never one run.
- Both arms must pass the **same acceptance**; failures reported honestly.

## 8. Status & plan

- [x] Live Memory deployed on `deepseek-v4-flash` (systemd); `/live-memory-empty` added.
- [x] Harness feasibility validated (`claude -p --mcp-config` smoke).
- [ ] **Pilot**: convert one tool (#3), 1×1, from shofer `32cdefc` → premium-token delta.
- [ ] If positive: full **#3 → #6** sequence, N runs, on the CLI harness.
- [ ] Fill §9.

## 9. Results

_To be filled after runs: per-feature and cumulative premium-token reduction,
cost, the with/without divergence chart, and links to `runs/`._
