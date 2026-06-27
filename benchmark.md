# Live Memory — token-reduction benchmark

**Status: plan finalized; pilot pending.** This is the living record — the
methodology now, the results later (§9).

## 1. What we measure (and what we claim)

Live Memory offloads codebase Q&A from the expensive **building** model to a
cheap, large-context companion. We capture the **full token breakdown** — per
**model** (expensive building model vs cheap Live Memory model) × per **type**
(input / output, plus cache read/write) — for both arms, so we can report:

> **−N% premium-model tokens**, **−M% cost**, and **−K% total tokens** (where the data supports it).

Premium-token and cost reduction is the **core, guaranteed-direction** claim
(Live Memory moves the heavy reading to a cheap model). **Total-token reduction
is a hypothesis we test, not assume**: Live Memory *adds* cheap-model tokens, but
the no-Live-Memory arm wastes premium *input* tokens re-reading the same code, so
the total may well drop too. We measure it and report whichever way it goes —
never hiding the cheap tokens.

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

**The features** are four bounded, dependency-ordered slices of shofer
modernization work — all **internal** (no server/UI → headless-testable, no
extension-host flakiness) and **comprehension-heavy** (each requires
understanding existing shofer subsystems), which is exactly Live Memory's
leverage. Each is scoped to a one-run slice. **Acceptance per feature: existing
shofer tests stay green + the feature's new test passes** (no regressions =
objective "it works").

### F1 — Schema-as-contract for tools (`src/core/tools/`)
A tool's shape is re-declared in several places (parameter/parser definition, UI
wiring, i18n strings, auto-approval), and per-model tool availability/naming is
hardcoded in the integrator layer — parallel sources of truth that drift and
cause *silent* tool failures (a tool half-registered, failing quietly). **Build:**
define each tool once as a typed `{ name, description, inputSchema, execute,
render? }` and derive *everything* from it — the LLM tool definition, runtime arg
validation/decoding, the UI label/inputs, and the auto-approval surface; delete
the hand-maintained mirrors; make malformed args fail *loudly* with a typed
`ToolFailure` instead of silently no-op'ing. **Slice:** convert **one tool at a
time** (naturally sequential, read-heavy on the shared tool machinery — ideal as
the lead/pilot feature). **Acceptance:** the tool's existing tests stay green + a
new test asserting (a) the LLM tool-def is derived from the single schema and
(b) malformed args raise `ToolFailure`.

### F2 — One permission engine
shofer has three overlapping systems — tool access, tool categories/groups, and
per-model tool preferences — plus a separate auto-approval mechanism: more
surface, three-way drift. **Build:** one ruleset — ordered `allow | ask | deny`
rules over `(action, resourceGlob)`, last-match-wins, allow-by-default, evaluated
both at tool *materialization* (is it available?) and per *invocation* (is this
call allowed?). Re-express categories/groups as rule patterns, and auto-approval
+ per-agent/mode gating as rulesets over the same engine. **Slice:** the engine +
migrate **one** of the three systems behind an adapter. **Acceptance:**
rule-evaluation unit tests + the migrated system's behavior preserved.

### F3 — Event-sourced persistence
shofer persists to flat files, which is *why* it needs bespoke performance
machinery (debounced saves, append logs, incremental IPC) — accidental complexity
from the storage choice. **Build:** move conversation/session state into an
embedded DB (SQLite/WAL) modeled as append-only events + projections; once writes
are cheap/incremental, the custom save/IPC layer is unnecessary, and durable
queues, windowed history (cursor queries), and revert/checkpoint fall out of the
data model. **Slice:** migrate **one** state slice to the DB with a one-time
importer. **Acceptance:** round-trip persistence tests + existing tests green.

### F4 — Structured cancellation
shofer threads cancellation by hand (AbortSignals, best-effort SIGINT) — error-
prone, leaking orphan processes and half-settled tools. **Build:** a
structured-concurrency primitive (a cancellation *scope*) so interrupting a run
deterministically cancels everything it spawned (provider stream, tool set),
binds child processes to the scope and kills the process *group*
(SIGTERM→SIGKILL) on cancel/timeout, and reconciles tool/message state on
interruption (no perpetually-"running" tool). **Slice:** the cancellation-scope
utility + wire one run path through it. **Acceptance:** cancellation/timeout tests
(no orphan processes, tool state reconciled) + existing tests green.

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
- **Warm-up (with-arm only)**: the with-arm opens with one fixed exploratory query
  — *"explore and understand this codebase and give a high-level summary"* — to
  pre-populate Live Memory, since in normal use it's already populated, not cold.
  This models realistic steady state — but the warm-up's cheap tokens are
  **counted**, not free: we report results **both** *including* the warm-up
  (conservative, cold-session) and *excluding* it (steady-state, the
  already-populated number). The without-arm has no warm-up (nothing to populate).

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

Capture the full **token matrix** — {expensive building model, cheap Live Memory
model} × {input, output, cache-read, cache-write} — per arm:

- **Expensive (building) model**: from each `claude -p --output-format json`
  `usage` (input/output + cache fields), summed over the subagents.
- **Cheap (Live Memory) model**: from `/stats`
  (`inputTokens`/`outputTokens`/`cacheReadTokens`/`cacheWriteTokens`), which
  accumulate even under subscription (where `costUsd` is null).
- **Totals** = expensive + cheap, per type and overall.
- **Cost** computed at **published API rates** per model — billing is via the
  Claude subscription, so the `$` is *imputed* (stated as such), not invoiced.

Report **per-feature and cumulative**, and — for the with-arm — **both including
and excluding the warm-up** query's cheap tokens (§4). **≥ 3–5 runs/arm** (agents
are stochastic) → mean ± spread, never one run. Both arms must pass the **same
acceptance**; failures reported honestly.

## 8. Status & plan

- [x] Live Memory deployed (systemd); `/live-memory-empty` added; `/stats` exposes
      per-type token totals.
- [x] Harness feasibility validated (`claude -p --mcp-config` smoke).
- [ ] Cheap model = **Haiku via Claude subscription** (OAuth) for the runs.
- [ ] **Pilot**: convert one tool (F1), 1×1, from shofer `32cdefc` → token delta.
- [ ] If positive: full **F1 → F4** sequence, N runs, on the CLI harness.
- [ ] Fill §9.

## 9. Results

_To be filled after runs: per-feature and cumulative premium-token reduction,
cost, the with/without divergence chart, and links to `runs/`._
