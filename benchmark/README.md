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

See [`harness/`](harness/) for the runnable scripts. The operating agents must
actually *have* `ask_live_memory` — wired per-invocation via `--mcp-config`.

> **CRITICAL — arm isolation (`--strict-mcp-config`).** If Live Memory is installed
> as a **global plugin** (`live-memory@shofer`), *every* `claude -p` session gets
> `ask_live_memory` — including the "without" arm. That silently confounds the A/B
> (both arms have Live Memory). **Both arms must pass `--strict-mcp-config`**, which
> disables the built-in/plugin MCP set:
>
> | | with Live Memory (arm I) | without (arm II) |
> |---|---|---|
> | invocation | `claude -p … --mcp-config <ours> --strict-mcp-config` | `claude -p … --strict-mcp-config` |
> | live-memory tools | **only** our wired instance | **none** (verified: lists `NONE`) |
>
> The early runs (`results/run3,run5,replicates`) predate this and are **confounded**
> (the without-arm had the plugin) — kept only as harness-iteration artifacts.

Per **feature × arm**: fresh **faithful build-env worktree** at the pinned base
(`32cdefc`, via `harness/setup_worktree.sh`) → `claude -p` (± `--mcp-config`,
always `--strict-mcp-config`) capturing the **`stream-json`** transcript →
acceptance (`tsc` green + specs + feature-present) → snapshot `/stats` → record.

**Bounds:** **no OS `timeout`** (a SIGKILL corrupts the JSON ledger); `--max-turns`
is the only, clean bound. API failures (`ConnectionRefused`/`api_retry`) are
detected → run marked **INVALID**, not scored.

## 6. Reproducibility & inspectability

For agents, "reproducible" means **deterministic setup + fully recorded runs + a
re-runnable harness yielding consistent statistics** — *not* bit-identical reruns
(impossible for LLMs; claiming otherwise is dishonest).

- **Pin**: shofer commit SHA, model IDs (building + Live Memory), prompts
  (verbatim), config/tool-sets, lockfiles. **Note:** `claude -p` exposes **no
  temperature/seed flag**, so the building agent's sampling **cannot be pinned** —
  runs are inherently stochastic. This is the root of the variance (§7); we counter
  it with the mechanism metric + per-turn normalization + replicates, not determinism.
- **Record** (per run): full transcripts, per-agent **premium** token ledgers +
  `/stats` **cheap** tokens, the produced diffs, acceptance logs, and Live
  Memory's **Q&A log** — as raw data, so the headline % is *recomputable*.
- **Caveat**: server-side models drift → the *recorded runs* are the durable
  artifact; re-running is best-effort against then-current models.
- **Publish**: harness + pinned inputs + recorded runs + aggregation, so reviewers
  can inspect *or* replay.

## 7. Measurement

**Why total-$ alone is the wrong lens.** A single run's premium cost is dominated
by **cache-read tokens** — the building agent re-reading its own growing
conversation each turn, ~`O(turns²)`. Turn count is highly stochastic, so total-$
swings wildly (observed: the *without*-arm baseline alone varied ~2×; the with-vs-
without delta ranged −53%…+59% across runs) and **buries** Live Memory's actual
effect, which is only on *codebase* reading. Single-feature total-$ is therefore
near-useless on its own.

**Primary metric — the mechanism (low variance, directly attributable).** From the
`stream-json` transcript ([`harness/analyze.py`](harness/analyze.py)) measure what
Live Memory actually *replaces*: **premium tokens the building agent spends reading
the codebase itself** = Σ tool-result tokens of `Read`/`Grep`/`Glob`. The without-
arm should read a lot; the with-arm offloads to `ask_live_memory` and reads little.
Also record **turns**, read/edit/lm call counts, and **premium-$ per turn**
(normalizes out path length). These isolate the treatment from the `O(turns²)`
context-churn nuisance.

**Secondary — the full token matrix** — {building model, Live Memory model} ×
{input, output, cache-read, cache-write} — per arm:

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

**Sanity gate — Live Memory `invocations`** (`/stats`): a with-arm run where the
operator barely calls `ask_live_memory` (invocations ≈ 0) is **invalid** — it
measured a no-op, not Live Memory. Track invocations per run; if usage is too low,
the fix is the **shared** operator prompt / MCP tool description (bias toward
"ask first, before reading files"), applied identically to both arms so the
*only* between-arm difference stays tool availability. **Never** restrict the
with-arm's own file tools to force usage — that confounds the comparison.

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
