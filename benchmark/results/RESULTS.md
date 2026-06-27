# Results & findings

Raw evidence is alongside this file: per-run `orchestrator.log`, `*.tok` (premium
token ledgers), `*.accept` (acceptance), `cheap.txt` (/stats deltas), `results.csv`
(replicates), and gzipped `*.stream.jsonl` (full agent transcripts). Costs are
**imputed** at published API rates (Sonnet building model, Haiku Live Memory model);
billing is via subscription, so `$` is notional, not invoiced.

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
P=4, K=12, 23/24 valid (`results/understanding/`):

| token dimension | without (n=12) | with (n=11) | Δ |
|---|---|---|---|
| read_tok | 48,393 ± 18,035 | 3,456 ± 3,093 | **−93%** |
| **cache_read** (the bill) | 544,038 ± 416,728 | 307,217 ± 136,840 | **−44%** |
| output | 4,856 | 4,378 | −10% |
| **premium $** | 0.366 ± 0.228 | 0.213 ± 0.087 | **−42%** (t=2.15, p≲.05) |

Opposite of the edit-bound feature: with no edits to backfill the window, the offloaded
reading stays gone, so **cache_read genuinely drops −44%** and premium **−42%**.

**Net (the two conditions for a real win):**
| | total $ | vs without |
|---|---|---|
| without | 0.366 | — |
| with, **free/local** cheap model | 0.213 | **−42% WIN** |
| with, Haiku incl. warm-up | 0.443 | +21% |

1. **Task must be understanding-bound** — premium reading to offload (edit-bound = no premium moves).
2. **Cheap-model cost < premium saved (~$0.15).** Haiku ($0.229) *exceeds* it → +21% even here;
   a near-free model (deepseek-flash/local) or a hot memory (no warm-up) flips it to a win.

> **Live Memory cuts premium cost only on comprehension/exploration-heavy work, and only
> nets positive when the weak model is cheap enough (or already hot) to stay under the
> premium it saves.** On edit-heavy work the premium doesn't move, so no model choice helps.

Caveat: marginally significant (the without-arm's exploration cost is wildly variable,
CV ~62%); direction is clear, magnitude needs more reps.

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
3. **Normalize out path length** — report premium-$ per turn and control for turn
   count (covariate), so "Live Memory needs fewer turns / less reading" shows even
   when total-$ is noisy.
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
