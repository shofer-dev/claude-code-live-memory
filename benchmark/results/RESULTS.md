# Results & findings

Raw evidence is alongside this file: per-run `orchestrator.log`, `*.tok` (premium
token ledgers), `*.accept` (acceptance), `cheap.txt` (/stats deltas), `results.csv`
(replicates), and gzipped `*.stream.jsonl` (full agent transcripts). Costs are
**imputed** at published API rates (Sonnet building model, Haiku Live Memory model);
billing is via subscription, so `$` is notional, not invoiced.

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
