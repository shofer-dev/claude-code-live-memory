# Results & findings

Raw evidence is alongside this file: per-run `orchestrator.log`, `*.tok` (premium
token ledgers), `*.accept` (acceptance), `cheap.txt` (/stats deltas), `results.csv`
(replicates), and gzipped `*.stream.jsonl` (full agent transcripts). Costs are
**imputed** at published API rates (Sonnet building model, Haiku Live Memory model);
billing is via subscription, so `$` is notional, not invoiced.

## PRE-LAUNCH: quality, freshness, persistence (the claims users will attack)

Token savings say nothing about whether answers are *right*, *current*, or *survive a
restart*. Three harnesses target exactly those (`harness/accuracy.py`, `freshness.py`,
`steady_state.py`; LM = Haiku via subscription; accuracy graded by an LLM judge).

**#1 Accuracy (15 author-verified Q&A incl. 3 negative/"not present" questions).** The
finding is that **accuracy is coupled to warmth**:

| memory state | correct | hallucinated (ungrounded) | answered with 0 reads |
|---|---|---|---|
| COLD, no guard (old behavior) | 10/15 (67%) | 3/15 (20%) | 10/15 |
| **COLD + explore-guard** (shipped, default on) | **~91% mean** (13–15/15 across 3 reps) | ~7% (0–13%) | reads to ground (~1/q) |
| **WARM** (files passively ingested — the normal in-use state) | **15/15 (100%)** | **0/15 (0%)** | 15/15 (1 rep) |

A **cold** memory confabulates specific constants it never read — every wrong answer
(`compaction_floor=10000`, `max_context_tokens=180000`, model=`Sonnet`) was produced with
**0 file reads**: it guessed from the directory tree instead of reading `config.py`. A
**warm** memory (which passive ingestion produces automatically from the agent's own I/O)
answered **everything correctly with zero reads and zero hallucination**. Negatives were
3/3 both arms (it did not invent a Redis backend / a write capability / a `model` param).
**Implication for launch:** the quality claim holds *for a warmed memory* (real usage);
the cold-start hallucination risk is real. **Prompt hardening alone did NOT fix it** — adding
a "verify exact values by reading" rule to the system prompt left cold at 67% (ungrounded
20%→27%): Haiku kept answering specifics with 0 reads despite the instruction (reverted).

**FIXED structurally (shipped, default on): `force_explore_when_cold`.** When the memory has no
grounding (`ContextWindow.is_cold()` — no observed file content in-window and an ~empty ledger)
and the model tries to answer with **zero tool calls**, the loop rejects that first answer once
and forces it to explore (Grep/Read) before answering. Result over 3 reps: **cold 67% → ~91%
(87–100%), hallucination 20% → ~7% (0–13%)** — the cold memory now reads to ground (~1 read/q)
instead of guessing. The residual misses are **nuanced multi-part questions** (compaction-tier
*order*, auth-vs-model Haiku/Sonnet), not blind constant confabulation. Warm memory (content
in-window) is untouched → still 100% at 0 reads. So the honest cost/quality picture is: **warm =
accurate & free; cold = ~91% accurate but pays a read** (the whole point of a memory that
explores). Env: `LIVE_MEMORY_FORCE_EXPLORE_WHEN_COLD`. Known limit: `is_cold` is coarse (any
observed content → "not cold"), so a *partially*-warm memory can still answer an un-observed
file's specifics from priors; broad passive ingestion covers that in practice. (cold+guard 3
reps, warm 1 rep, LLM judge — spot-checked.)

**#2 Freshness after edits (does it reflect the current code?).** Across 3 reps:

- **Agent-edit path (PostToolUse tees new content): 6/6 fresh (100%)** — teed edits are
  authoritative, reflected immediately, no re-read. The core passive-learning claim holds.
- **Out-of-band path (FileChanged, no content): 4/6 (~67%)** — the file is flagged stale, but a
  fact already baked into an earlier Q&A can persist (a run answered `MAX_RETRIES=3` after it
  changed to 7, from a prior answer, without re-reading). **So "never stale" is too strong for
  out-of-band changes** — they're flagged and re-read *at the model's discretion*, and accumulated
  Q&A/ledger isn't invalidated by a file change. Honest copy (now in the slide + launch doc):
  *agent edits are immediate/authoritative; external changes are flagged and usually re-read.* (3 reps.)

**#3 Steady-state (no warm-up) + persistence across restart.** Same 6-question batch, own
server subprocess, live-memory repo:

| phase | file reads | answered | cheap $ (imputed) |
|---|---|---|---|
| COLD (cleared → must explore) | 6 | 6/6 | 0.043 |
| WARM (passively pre-populated, no warm-up) | **0** | 6/6 | 0.069 |
| RESTART (kill server → reload from snapshot) | **0** | 6/6 | **0.018** |

- **Persistence: PASS** — after a restart, previously-answered questions returned **0
  reads, 6/6 correct, and the *cheapest* of all** — memory reloaded from the on-disk
  snapshot.
- **Steady state eliminates reading** (6 → 0 vs cold).
- **Cost nuance:** freshly-teed *raw content* resident in-window (WARM) is *heavier* per
  query than cold's lean lazy manifest on a small repo that fits the window (+60%); once
  it's persisted/distilled to the lean manifest+ledger (RESTART), it's the **cheapest**
  (−59% vs cold). So the cheap-side win shows up in the *distilled/persisted* steady state,
  not the raw-teed moment — more motivation for retrieval/projection (FUTURE_DIRECTIONS §2).

**Net for the announcement:** quality is strong **when warm** (100%/0-hallucination) —
lead with that and be explicit that warming is what makes it accurate; don't claim
"never stale" for out-of-band edits; persistence across restarts is a clean, demoable win.

## 0. Confound correction — the early runs are INVALID for the A/B

Live Memory is installed as a **global plugin** (`live-memory@shofer`), so every
`claude -p` session — *including the "without" arm* — was offered `ask_live_memory`,
and the without-arm used it. So `run3`, `run5`, and `replicates/` did **not** compare
with-vs-without Live Memory; **both arms had it**. The with-arm's only real difference
was the warm-up, which *adds* cost — which is why the with-arm was frequently *more*
expensive (delta ranged −53%…+59%). Those dirs are kept only as harness-iteration
artifacts; their A/B deltas mean nothing.

**Fix:** both arms now pass **`--strict-mcp-config`** (disables the plugin/built-in
MCP set). Without-arm gets no `--mcp-config` → zero Live Memory (verified: tool list
returns `NONE`); with-arm gets only our wired instance. The corrected, instrumented
harness is `harness/run_reps2.sh` (+ `harness/analyze.py`); results land in
`results/reps2/`.

## DEFINITIVE single-feature result (K=20, isolated, P=4 parallel)

20 reps/arm, 40/40 valid, 0 invalid, 0 API failures (`results/parallel/`).

| metric | without (n=20) | with (n=20) | Δ |
|---|---|---|---|
| **read_tok** (codebase) | 41,208 ± 12,344 | 14,446 ± 6,694 | **−65%** |
| edit_calls | 12 | 12 | **0** |
| turns | 38 ± 9 | 40 ± 8 | +5% |
| **premium $** (Sonnet) | 0.703 ± 0.296 | 0.713 ± 0.221 | **+1.4%** |
| cheap $ (warm-up+feature, Haiku) | — | 0.235 ± 0.092 | — |

**Conclusion: on an edit-bound single feature, Live Memory offloads reading (−65%,
clean) but does NOT reduce premium tokens (break-even, +1.4% = noise in ±0.3 CV).**
Three consequences, each proven with the data:

1. **The reading offload doesn't reach the premium budget.** Read file content is
   <2% of the premium footprint (dominated by conversation/edits/system/tool-defs
   re-read every turn as cache-reads). Offloading it is invisible in total-$.
2. **Cost is invariant to the building model's price.** Per-token footprints are
   equal (with even +4%), so repricing both arms at Opus rates gives the *same*
   +3% — a pricier model can't widen a gap that doesn't exist at the token level.
3. **A free/local cheap model only reaches break-even, not a win.** The bottleneck
   is the premium side (flat), not the cheap-model cost.

**Why "reads less" ≠ "fewer premium tokens":** turns are driven by the **edit loop**
(12 edits → ~38–40 edit-check-iterate turns), which the feature fixes; reads batch
*into* turns rather than driving them. Live Memory makes *understanding* cheaper but
leaves *execution* untouched, and this feature is execution-bound. The intuition
("good answer → builder finishes faster") only chains through when the task is
**understanding-bound** (many search/read turns, few edits) — `count_lines` is the
opposite. **Next experiments that can show a premium win: (a) an understanding-heavy
feature; (b) the sequence (hot memory → fewer turns via accumulation).**

## UNDERSTANDING-BOUND task (the regime where it wins)

Read-only trace task (synthesize the tool-call code path across ~4k lines, no edits),
P=4, K=12, 23/24 valid (`results/understanding/`; with-arm n=11 accepted). Cost is Claude Code's own
reported `total_cost_usd`, time is `duration_ms` (no imputation), compared **per task, run to completion**:

| per task | without (n=12) | with (n=11) | Δ |
|---|---|---|---|
| read_tok (codebase reading) | 48,393 ± 18,035 | 3,456 ± 3,093 | **−93%** |
| cache_read (the bill driver) | 544,038 ± 416,728 | 307,217 ± 136,840 | −44% |
| **cost — building model** | $0.639 ± 0.153 | $0.247 ± 0.098 | **−61%** (Welch t=7.0) |
| cost — all-in (+ Haiku companion) | $0.639 | $0.477 | **−25%** |
| **wall-clock time** | 174 s ± 33 | 136 s ± 24 | **−22%** |

Opposite of the edit-bound feature: with no edits to backfill the window, the offloaded
reading stays gone, so the building model's **cost drops −61% per task** and it finishes **~22% faster**.

**Net (the two conditions for a real win), per task:**
| | total $ / task | vs without |
|---|---|---|
| without | 0.639 | — |
| with — building-model cost only (free/local companion, or already-warm) | 0.247 | **−61% WIN** |
| with — all-in incl. Haiku companion + a cold warm-up | 0.477 | **−25% WIN** |

1. **Task must be understanding-bound** — premium reading to offload (edit-bound moves the building cost less).
2. **Cheap-model cost < premium saved.** Here the building model saves **$0.39/task** while the Haiku
   companion costs **$0.23** (incl. a cold warm-up) → still a **−25% net win**; a near-free/local model or
   an already-warm memory widens it toward the full −61%. (On a *lighter* task where the without-arm is
   cheaper — e.g. the K=6 passive re-run below — the fixed Haiku cost can exceed the smaller saving and
   all-in goes break-even; the win scales with how much reading the task demands.)

> **Live Memory cuts the building model's cost on comprehension/exploration-heavy work (−61%/task here,
> highly significant), finishes ~22% faster, and stays net-positive all-in as long as the companion is
> cheap enough — which on a genuinely understanding-heavy task it is.** On edit-heavy work the building
> cost moves less, so the win shrinks (see the sequence below).

### Cheap-model capability — is a *cheaper* companion still accurate enough? (deepseek-v4-flash vs Haiku)

The all-in win widens as the companion gets cheaper **only if the cheap model stays accurate** — a
cheap-but-dumb model that confabulates would be worse than useless. Head-to-head on the 15-question
accuracy set (12 factual + 3 hallucination-traps, **warm**, LLM-judged, same repo), **×3 reps (n=45)**,
swapping **only** the companion model behind the identical `ask_live_memory` path (`results/cheap_model/`):

| companion (warm, 45 = 15 Q × 3 reps) | correct | hallucinated | negative traps | price $/M (in·out) | cheap-side $/task¹ | all-in¹ |
|---|---|---|---|---|---|---|
| **deepseek-v4-flash** | **44/45 (98%)** | **1 (2%)** | 9/9 | 0.14 · 0.28 | **$0.029** | **−57%** |
| claude-haiku-4-5 | 41/45 (91%) | 4 (9%) | 9/9 | 1.00 · 5.00 | $0.229 | −25% |

¹ cheap-side priced on the understanding-bound run's **actual** token profile at each model's rate;
all-in = building-model $0.247 + cheap-side, vs without $0.639. (deepseek reached via an OpenAI-compatible
router; Haiku via subscription OAuth — the real default. Per-rep correct: deepseek 15/15/14, Haiku 14/13/14.)

**deepseek-v4-flash (~8× cheaper per token) matches — and slightly edges — Haiku's accuracy** over 3 reps
(**98% vs 91%**, and **fewer hallucinations: 1 vs 4**), with **both perfect on the negative traps** (9/9,
the real capability risk). The single shared failure mode is one hard numeric fact (`max_context_tokens`)
that either model can miss when it answers warm without re-reading — not a harness fault (the reruns had
0 errors). Its ~8× lower price pushes the **all-in saving from −25% → −57%**, nearly the full **−61%**
building-model saving. This is the measured basis for condition #2: a cheap-but-capable companion
(deepseek-flash — or a local model, ≈ free) makes the all-in number approach the building-model win. The
companion model is server config (env / `/live-memory-config`), not per-call.

## UNDERSTANDING-BOUND task — RE-RUN with passive ingestion ON (+ compaction fixes)

Same read-only trace task, after landing **passive (organic) population** (the agent's
own file I/O tees into Live Memory) and the **compaction reliability fixes** (hysteresis
high/low watermark + parallel-commit versioning). P=2, K=6, **6/6 valid both arms, 0 API
failures** (`results/understanding_passive/`). Live Memory model = Haiku via subscription.

| metric | without (n=6) | with (n=6) | Δ mean |
|---|---|---|---|
| **read_tok** (premium tok reading codebase) | 38,709 ± 8,250 | 1,300 ± 1,699 | **−97%** |
| read_calls | 16 ± 3 | 2 ± 3 | −85% |
| turns | 20 ± 13 | 14 ± **3** | −28% |
| **cost / task — building model** (imputed Sonnet) | **$0.356** ± 0.309 | **$0.156** ± 0.034 | **−56%** |
| premium tok total | 653,402 ± **734,546** | 238,610 ± **58,644** | −63% |
| cheap-side $ / task (Haiku companion, incl. cold warm-up) | — | $0.292 ± 0.066 | — |
| cost / task — all-in (building **+** companion) | $0.356 | $0.447 | +26%¹ |
| acceptance (trace correct) | 6/6 | 6/6 | — |

**Per task, run-to-completion** (this replaces the old per-turn normalization — every run finishes the
task, so we compare **$ and wall-time per task**, not per turn): the building model's cost drops **−56%**
($0.356 → $0.156) and its codebase-reading **−97%**, answering from a single `ask_live_memory` call with
the trace correct every time. ¹The cheap Haiku companion adds **$0.29/task** *here* because this harness
pays a **cold warm-up on every task**, so **all-in** is roughly break-even (+26%) on a *single cold*
question — the fixed warm-up is only amortized in a real session. Two findings the bold spreads make
obvious:

1. **Live Memory collapses VARIANCE, not just the mean.** The without-arm is wild (premium total ±735k —
   one run spiraled to 42 turns / 2.15M cache-read; another finished in 1 turn) because it carries ~40k of
   file reads in its window and re-reads them every turn (`O(turns²)`). The with-arm holds almost no file
   content (offloaded to the one LM query) → bounded window → building-model cost **±$0.03** and turns
   **±3**. Predictable cost is itself the product win.
2. **The mechanism is unambiguous and low-variance:** the building agent's codebase-reading premium
   tokens drop **−97%** (38.7k → 1.3k); it reads 2 files instead of 16.

(The K=6 $ figures here are **imputed** premium — a lower-fidelity proxy used before streams were
retained; the K=12 run in "UNDERSTANDING-BOUND task" above reports the same task on Claude Code's **real**
reported cost + wall-time — **−61% building / −25% all-in / −22% time**, Welch t=7.0 — and is the
authoritative per-task number. This passive re-run's contribution is the **variance** and **mechanism**
findings, not a second cost estimate.)

Net: on understanding-bound work Live Memory cuts the **building model's cost per task ~56–61%** and
**finishes ~22% faster**, while making cost *predictable*. Counting the cheap companion's own cost, a
**single cold** question ranges from break-even (light task, imputed K=6 here) to **−25% all-in** (heavier
task, real K=12 above) — the fixed warm-up then **amortizes across the session** (the sequence A/B below
shows later questions reading ~0 tokens, so all-in savings compound the longer a session runs). The edit-bound regime
shows a **smaller** win (see below) — Live Memory makes *understanding* cheaper more than *execution*.

## COMPOUNDING SEQUENCE A/B — does accumulation compound across a sequence?

Both variants keep Live Memory **persisting + accumulating across the whole sequence** (warmed
for free), with the cold arm re-exploring at every step.

### Edit-bound sequence (3 reps) — a real but smaller win; understanding still wins bigger
`harness/run_sequence.sh` — 4 read-only tools added one per feature (`count_lines → count_chars →
count_bytes → count_words`), **cold agent per feature**, accumulating pinned shofer worktree. The
with-arm **warms once** and reuses across all 4 features (compounding). **3 reps, 24/24 features
accepted** (`tsc` green + tool wired). Cost is Claude Code's reported `total_cost_usd`, time is
`duration_ms`; the with-arm cost is **all-in** (building model + companion). `results/sequence/`.

| per task (mean of 4 features × 3 reps) | without | with | Δ |
|---|---|---|---|
| **cost / task — building model** | $0.889 | $0.676 | **−24%** |
| cost / task — all-in (+ companion)¹ | $0.889 | $0.760 | **−15%** |
| **wall-time / task** | 377 s | 276 s | **−27%** |
| codebase reading / task (read_tok) | 22,388 | 13,865 | −38% |

¹ companion cost is the marginal per-feature query cost; the one-time warm-up (~$0.3, amortized across
the 4 features) is excluded — folding it in trims the cumulative saving to ~−6% cost / unchanged time.

**Smaller than the understanding-bound win** (building-model **−24%/task** here vs **−61%** there):
feature work is execution-bound — the agent must read the exact files it edits (sometimes with 0 LM
calls), and turns are driven by the edit-check-iterate loop. It's also **noisy** — both in reading
(with-arm read_tok across the 3 reps: 62k / 39k / 65k) and, importantly, **in wall-time**: the aggregate
**−27%** is **not** uniform across reps — rep1 was actually *slower* with memory (151 s → 183 s) and the
mean is carried mostly by rep3 (680 s → 344 s). So treat **−27% wall-time / −15% cost** as a *directional*
per-task figure with wide rep spread (3 reps), not a tight estimate. But it is **not** break-even on the
mean: because the sequence **warms once and reuses**, the companion cost amortizes and the all-in saving
holds at **−15% cost / −27% wall-time per task** — which is why a *warm-amortized* edit sequence can
out-save the *cold-per-task* hybrid A/B above. Confirms the structural finding: Live Memory makes
*understanding* cheaper more than *execution*.

### Understanding-bound sequence (1 rep) — compounding DOES show (on the mechanism)
`harness/run_understanding_sequence.sh` — 6 **distinct**, read-only comprehension questions across
different shofer subsystems (tool-call path, schema validation, permissions, persistence,
cancellation, model routing), asked in order; with-arm's LM persists + accumulates, cold arm
re-reads each. `results/understanding_sequence/`.

| q | WO read_tok | WI read_tok | WI lm_calls |
|---|---|---|---|
| 0 | 39,372 | 49,642 | 1 |
| 1 | 6,502 | **0** | 1 |
| 2 | 41,544 | **0** | 1 |
| 3 | 35,353 | **0** | 1 |
| 4 | 34,725 | **0** | 1 |
| 5 | 1,811 | **0** | 1 |
| **cumulative** | **159,307** | **49,642 (−69%)** | |

**The flywheel is visible:** after warming on q0, the with-arm answers **every** later question with a
single `ask_live_memory` call and reads **zero** codebase tokens (q1–q5: **−100%**), while the cold arm
re-reads 35–41k each time. The more questions in a session, the more the q0 warm-up amortizes.

**Honest caveat:** premium **$ total is flat (+3%)** in this run — each question is a *separate, short*
`claude -p` session (~5 turns), so its premium is dominated by fixed prompt/tool/turn cache-read
(~93k/question in *both* arms) and the offloaded codebase-reading is a smaller $ slice. So compounding
shows cleanly in the **mechanism** (read_tok — the low-variance, attributable metric) but doesn't move
total-$ here — the recurring pattern that total-$ is cache-read-dominated and noisy. (1 rep; read_tok
is the reliable signal.)

**Bottom line across both:** accumulation across a sequence **compounds the reading-offload**, and more
cleanly in the understanding-bound regime (reads → 0 and stay there) than the edit-bound one (noisy
−38%). Converting that to $ still depends on the read-fraction of premium — which is why the proven
headline remains the single understanding-bound A/B (**−61% building-model cost / −22% wall-time per task**).

## HYBRID A/B — realistic understand-then-edit tasks (the middle regime)

A real session is rarely pure Q&A — the user asks for a **bug fixed** or a **feature built**, which
means *understand the codebase, then edit it*. `harness/run_hybrid.py` + `harness/hybrid_tasks.json`
encode **4 such tasks** on pinned shofer `@32cdefc`, each with **objective fail→pass acceptance**
(a hidden failing test, green at baseline, red under the task's setup patch — see the manifest):

- **bug1** — long-context pricing wrongly applied *exactly at* the token threshold (`cost.ts`).
- **bug2** — command auto-approval stops inspecting `$(...)` subshells, so `echo $(whoami)` auto-approves (`parse-command.ts`).
- **feat1** — allow tool-**group** names in a mode's `tools_denied` (`validateToolUse.ts`).
- **feat2** — bash-style `${key:-default}` default-value syntax in support-prompt templates (`support-prompt.ts`).

Prompts are **symptom/behaviour-first and location-blind** (they describe the wrong behaviour or the
desired API, never the file, the mechanism, or the test) so the agent must *genuinely locate and
understand* before editing. Each task, each arm: reset pinned worktree → apply setup patch → run the
building agent (`claude -p`, ±`ask_live_memory`, soft system-prompt nudge — the honest real-world
config) → run the acceptance command → measure. **3 reps, 24 runs. `results/hybrid_*/`.**

| task | adopted¹ | reading WO→WI | cost / task WO→WI² | wall-time WO→WI |
|---|---|---|---|---|
| bug1 | 0/3 | 2,612 → 1,377 (−47%) | $0.207 → $0.163 (−21%) | 75 s → 73 s (−2%) |
| bug2 | 1/3 | 7,758 → 5,782 (−25%) | $0.504 → $0.416 (−17%) | 200 s → 176 s (−12%) |
| **feat1** | **3/3** | **18,252 → 7,359 (−60%)** | **$0.669 → $0.488 (−27%)** | **247 s → 183 s (−26%)** |
| feat2 | 3/3 | 5,195 → 4,477 (−14%) | $0.233 → $0.373 (**+60%**) | 102 s → 121 s (+19%) |
| **per-task mean** | 7/12 | **−44%** | **$0.403 → $0.360 (−11%)** | **156 s → 138 s (−11%)** |

¹ *adopted* = with-arm reps that actually called `ask_live_memory` (of 3).
² cost/task = Claude Code's own reported `total_cost_usd`; the with-arm figure is **all-in** — building model **+** the Haiku companion's own query cost. Wall-time = `duration_ms` (with-arm includes the `ask_live_memory` round-trip; excludes the one-time warm-up).

**Acceptance: 12/12 both arms** — Live Memory never broke a task.

**Two honest caveats that reshape the headline:**

1. **Adoption is a real variable, and it tracks understanding burden.** Under a *soft* nudge (the plugin
   approximates this, it doesn't force calls), the agent consults memory on **features 6/6** but on
   **bugs only 1/6** — because these bug symptoms are one-keyword-greppable (`grep thresholdTokens`),
   so the agent just greps. Where it doesn't adopt (bug1 0/3), the with/without deltas are two
   independent runs = **noise, not signal** (bug1's read_tok is byte-identical across arms in the
   adopted-nowhere reps). The clean signal is **feat1**: the cold arm *thrashes* to locate the
   permission logic (18k reads, up to 28k in one rep) while the warmed arm goes straight there
   (7k, **−60% reading, −31% premium, 24→20 turns**).

2. **All-in cost is a modest win, and it's entirely the hard task — it can go negative on trivial ones.**
   Using Claude Code's own reported cost, all-in (building model **+** the Haiku companion), the per-task
   mean is **−11%** ($0.403 → $0.360) — but **not uniform**: **feat1 −27%** (the companion pays for itself
   several times over on a genuine cross-file hunt) while **feat2 is +60%** — on a trivial two-file task
   the companion's query round-trip costs *more* than the tiny reading it saves. This is the **worst case
   for Live Memory**: the harness **re-warms every task** (zero session compounding), so the saving is real
   but small precisely because these are short, single-file edits.

**Bottom line.** On realistic hybrid tasks measured **cold and one-shot**, Live Memory cuts the agent's
codebase-reading **−44%**, trims **per-task cost ~11%** and **wall-time ~11%**, and **never hurts
correctness** (12/12) — with the value concentrated in the understanding-heavy task (feat1: **−27% cost,
−26% time**) and slightly negative on the trivial one. It scales exactly where the earlier benchmarks
prove it does — as a **session lengthens**: the one-time warm amortizes and the `cache_read` offload
compounds (understanding-bound A/B: **−61% building-model cost / −22% time per task**; the
understanding-bound *sequence*: later questions read **0 tokens**). The hybrid regime is the honest
**middle**: understanding-heavy work (features) reliably pulls value from memory; greppable single-file
edits (bugs) are break-even and often don't even invoke it. Reproduce: `RUNS=/tmp/pilot/hybrid_r1
server/.venv/bin/python -u benchmark/harness/run_hybrid.py`.

## 1. Live Memory prefix-cache fix (verified, landed)

The cheap-model cost of every query was dominated by the **directory tree** (~30k
tokens) being **re-written** (cache-write 1.25×) on every question instead of read
(0.1×), because the volatile knowledge-ledger + file-manifest were baked into the
same cached system block, busting the cache key each question.

**Fix:** split the system prompt into a stable cached prefix (instructions + tree)
and a volatile uncached suffix (ledger + manifest). Verified by probe — 2nd-question
cache-write dropped **~30,000 → 735**; the tree became a cross-question cache-read.
In-benchmark (run5), the feature query's cheap tokens were **419k cache-read vs 26k
cache-write** (~16:1), vs ~1:1 before.

## 2. Lead-feature A/B (new `count_lines` tool) — single runs

| run | premium $ without | premium $ with | Δ | note |
|-----|------|------|------|------|
| run3 (pre-fix) | ~$0.85 | ~$0.79 | **−7%** | first clean run |
| run5 (post-fix) | $0.878 | $0.409 | **−53%** | |
| rep1 | $0.844 | $0.723 | **−14%** | with-arm emitted *more* output |
| rep2 | $0.435 | (running) | — | baseline alone ½ of rep1 |

Both arms always reach identical acceptance (`tsc` green + specs green + feature
present). Live Memory is used (1–2 invocations/feature, gate VALID).

## 3. The headline finding: **per-feature result is dominated by variance**

The premium delta swings **−7% … −53%** across runs, and the *without-arm baseline
alone swings ~2×* ($0.44 … $0.88). No single run is meaningful.

### Source of the fluctuation
- The metric is **dominated by cache-read tokens** — the agent re-reading its own
  growing conversation each turn, ~`O(turns²)`. This has nothing to do with Live
  Memory; it is the agent's own context churn. A run that takes 11 vs 74 turns
  differs ~quadratically in cost.
- **Turn count is highly stochastic** (the building agent samples a different path
  each run: different files read, different implementation — rep1 used 6 files,
  another run 3). So the nuisance term dwarfs and masks the treatment.
- **Live Memory's actual effect is small per feature** — it offloads *codebase*
  reading, a small slice of total premium; the warm-up (~$0.15–0.27) doesn't
  amortize on one feature. So signal ≪ noise per run.

### Plan to get stable, reliable results (ranked)
1. **Pin the building agent's sampling** (temperature 0 / fixed seed if `claude -p`
   exposes it) — the single biggest lever: makes the with/without paths comparable
   instead of two draws from wide distributions.
2. **Measure the mechanism, not just the outcome.** Add a metric for *premium
   tokens the building agent spends reading the codebase itself* (Read/Grep/Glob
   tool-result tokens) — that is exactly what Live Memory replaces, and it has far
   less variance than total-$ (which is swamped by context churn). Report turns and
   files-read too.
3. **Normalize per task, run-to-completion** — every run finishes the task, so report
   **$ and wall-time per task** (from Claude Code's reported `total_cost_usd` / `duration_ms`),
   not per-turn. This is turn-count-independent and is the normalization now used throughout
   (superseding the earlier premium-$/turn framing).
4. **Run the sequence (compounding), not single features** — over N cumulative
   features the warm-up amortizes and the with-arm's accumulated advantage grows
   *above* the per-feature noise floor; per-feature noise also partially averages.
   This is both the real claim and a more stable measurement.
5. **More replicates** — but understand variance is `O(turns²)`; prioritize (1)–(3)
   over brute-forcing N.

## 4. Status
- Cache fix: landed + verified.
- Faithful build-env worktree: solved (`harness/setup_worktree.sh`).
- Harness hardened: no OS-kill, API-failure → INVALID, feature-presence acceptance.
- Replicate batch (K=4): running; `replicates/results.csv` is refreshed on
  completion with the aggregate mean ± spread.
