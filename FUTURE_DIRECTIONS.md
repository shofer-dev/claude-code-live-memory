# Live Memory — future directions

Parked design ideas for evolving Live Memory, grounded in the benchmark findings
(see [`benchmark/results/RESULTS.md`](benchmark/results/RESULTS.md)). Not committed
work — a captured brainstorm to pick up later.

## 0. What the benchmark established (the constraints these ideas must respect)

- **Live Memory's premium-token win is task-shaped.** On **understanding/exploration-
  heavy** work it cuts the building agent's premium cost (~**−42%** on a read-only
  "trace the code path" task: `cache_read` −44%). On **edit-heavy** work it's **flat**
  — reading-to-edit is irreducible (you must read a file's exact bytes to edit it) and
  the edits backfill the window, so the offloaded reading never reaches the bill.
- **The net win has two conditions:** (1) the task is understanding-bound, AND (2) the
  cheap model's cost stays **under the premium it saves** (~$0.15). Haiku's *exploration*
  cost (warm-up + per-query re-reading, ~$0.23) **exceeded** that — so even the winning
  regime was +21% net with metered Haiku. A near-free/local model **or a hot memory**
  flips it to a real win.
- **Two bottlenecks to attack:** (a) the **cheap-side exploration/warm-up cost**, and
  (b) **window bloat** (the building agent's premium is dominated by `cache_read` =
  re-reading its growing context; Live Memory's own context has the same dynamic).

The two ideas below attack exactly these: **passive learning** removes the cheap-side
exploration cost and keeps Live Memory hot for free; **denser representation** keeps the
window lean so warm knowledge doesn't reintroduce bloat.

## 1. Passive (organic) population

> **Status: IMPLEMENTED** (initial cut). Content-carrying hooks (`Read` +
> `Write|Edit|MultiEdit|NotebookEdit` → `notify.py` tees current bytes) → `/notify`
> `{paths, contents}` → `WorkspaceState.observe()` upserts a content-bearing file
> context, rendered inline so the model answers without re-reading; marked current
> (not stale), wired into the freshness/manifest machinery (decision 2). Raw bytes
> are in-memory only and distilled into the ledger by compaction **tier 0**
> (decision 1). Gated by `LIVE_MEMORY_PASSIVE_INGESTION` (default on; off = today's
> active fallback). See DESIGN.md §Data Flow 3c + §Compaction.
>
> **Measurement hinge — answered YES** (`benchmark/harness/passive_hinge.py`, Haiku,
> 5 understanding questions over 5 files, 3 reps). WARM (files teed via `/notify`
> before the questions) vs COLD (today's behavior): WARM answered **every** question
> with **0 tool round-trips and 0 file reads** (COLD: 10–11 tool calls, 6–7 reads
> per run), at **−3% to −21% cheap-side cost** (always cheaper, net of the ~10k-token
> observation bloat — the cache amortizes it) and **~30–40% lower latency**. So a
> passively-warmed window makes `ask_live_memory` cheaper net of the bloat, and
> eliminates re-reading on the warm path.
>
> **Overflow regime — exercised + two bugs fixed** (`benchmark/harness/passive_compaction.py`:
> 22 questions, 20 files teed progressively into a deliberately small 24k window so
> observations + Q&A overflow and force compaction; includes late "survival" re-probes
> of early files long since distilled). It surfaced two real bugs the fits-regime hid:
> (1) **compaction thrash** — compacting back to the *trigger* re-fired every question
> and busted the prompt cache → fixed with a high/low **watermark** (`compaction_floor`,
> DESIGN.md §Compaction); (2) **compaction never committed in parallel mode** — the
> "most-exploring fork wins" tiebreak discarded the (smaller) compacted window every
> time → fixed with optimistic **window versioning** (linear updates always commit;
> tiebreak only on genuine races). Same benchmark across the fixes: WARM compactions
> 18→18→**3**, WARM cost +889%→+443%→**+95%** vs COLD. **Survival recall is perfect**
> throughout — every re-probe of a distilled file answered correctly from the ledger
> with 0 reads (COLD had to re-read). Remaining gap: when the working set **exceeds**
> the window, WARM still carries a fuller context per call than COLD's lazy manifest,
> so cheap-side cost is ~2× (both pennies on Haiku) — this is precisely the §2
> (retrieval/projection) lever: hold a query-relevant projection, not the whole set.
> **Still open:** §2 projection, the premium-side win (full `claude -p` A/B), and
> active-read population.

**Idea.** Populate Live Memory not (only) via `ask_live_memory`, but **passively** from
the building agent's normal `Read`/`Edit`/`Write` I/O. The file contents are already
flowing; tee a copy into Live Memory so it learns the code as a side effect of real work,
**without paying to re-read it**. `ask_live_memory` is unchanged and **additive** — it can
still read actively when the passive layer hasn't covered something.

**Why it matters.** Directly removes the cheap-side exploration cost the benchmark showed
was eating the win, and realizes — for free, from real work — the "hot memory" / "free
cheap model" assumptions that the −42% win depends on. Floor = today's behavior (active
fallback); ceiling = warm, cheap, always-in-sync. **No regression risk.**

**Properties.**
- **No double-read** — population cost collapses from "re-explore" to "summarize what was
  already observed."
- **Always in-sync** — observing every `Edit`'s *new* content is strictly stronger than
  today's "mark stale → re-read later." Never stale on the hot path.
- **Cross-task flywheel** — even edit-heavy sessions (which can't win on premium) passively
  warm Live Memory for *future* understanding queries (which can).

**Implementation: tee, don't proxy.** Do **not** replace the agent's tools (matching native
Read/Edit/Write behavior — permissions, diffs, line numbering, partial reads — is a trap).
Extend the existing hook feed (`/notify` from `PostToolUse`/`FileChanged`) to carry the
**content**, not just the change event: a `PostToolUse` hook POSTs `{path, content}` to a
Live Memory ingestion endpoint. Native tools untouched; agent notices nothing.

**Load-bearing design decisions (now that two streams feed one window):**
1. **Ingest raw, let existing compaction digest it** → population is ~free at write-time
   (no model call to ingest; the compaction that already runs summarizes on overflow).
   But the **retention/compaction policy is now the key knob**: what stays raw (available,
   no re-read) vs gets summarized (lean prefix), and when.
2. **Wire passive obs into the freshness/manifest machinery so it *triggers* the active
   path.** Observed `Edit` → file content authoritative/current; observed `Read` →
   refresh. Then `ask_live_memory` reads actively **only when the manifest is missing or
   stale for the question** — precise, automatic, and the active reads also populate.

**Measurement hinge (run before building much):** does a passively-warmed window make
`ask_live_memory` **cheaper net of the bloat it adds**? Warm the window via observation,
then measure query cost vs cold. If compaction keeps the prefix lean → real, self-sustaining
win. If raw observations pile up faster than they're distilled → bloat could eat it.

## 2. Denser knowledge representation

**Goal, disciplined by the constraint.** The model ultimately consumes **tokens** and
reasons best over **fairly natural language**. So "denser than prose" splits into two
*different* levers — and the goal is **not** "max knowledge in the window" (that bloats
cost + dilutes attention) but "**the right knowledge at the right density for cheap
reasoning.**"

- **Retrieval** — don't hold it all; fetch the *right* subset per query (RAG, graph
  queries, hierarchy). The scale lever. It's *less* text, better chosen — not denser text.
- **Encoding** — make the included tokens carry more (structured facts, signatures/call-
  edges instead of prose/bodies). Real, but with a **sweet spot**: too terse/symbolic and
  the LLM reasons *worse*. The densest store is not the most useful one.

**Verdicts on specific representations:**

- **Binary / embeddings-into-context — no (with this stack).** Hosted LLMs can't read
  arbitrary vectors/blobs; "soft-prompt / KV-as-memory / memory-token" research needs
  model-side support you don't have over an API. Embeddings are excellent for the **index**
  (finding relevant knowledge); the retrieved item still renders to text. Don't chase
  feeding vectors to the model.
- **RAG — yes for scale, one caveat.** External store + top-k retrieval → unbounded
  knowledge, lean window. **Weak on multi-hop / "how does X flow end-to-end"** questions
  (the trace-task regime where Live Memory wins) — top-k chunks give fragments, not a
  traversal.
- **Code graph — strongest for code.** Code *is* a graph (calls, imports, types, data
  flow); most understanding questions are **traversals** over it ("trace the tool-call
  path" is literally a graph walk). Captures relationships prose flattens. Architecture:
  graph is the queryable **store** → extract the relevant **subgraph** per query → render
  *that* to compact text. Composes with passive sync (edits update nodes/edges). Tooling
  exists (tree-sitter, LSP, SCIP/stack-graphs) — real engineering, not magic.
- **API / symbol skeleton — the 80/20, lowest-regret first step.** Most "where/how/what-
  calls-what" questions are answered by the **shape** (signatures, types, docstrings, call
  edges), not the bodies. Store the skeleton densely (a fraction of full-file tokens,
  lossless about *structure*), fetch bodies on demand. A strict win over today's prose
  ledger for structural questions, without committing to a full graph-query engine.

**Synthesis — recommended target architecture:** a **hierarchical, graph-aware,
passively-synced index** as the store, with retrieval projecting a small dense subset into
the window:
- always-resident **core map** (repo architecture, entry points, conventions) — the
  genuine "wisdom" worth permanent tokens;
- **graph + symbol skeleton** as the structured layer;
- **bodies/details retrieved on demand** when a query needs them;
- the window holds a **query-relevant projection**, not a growing prose blob.

This is the direct lever on the window-bloat bottleneck (§0b): it keeps the prefix lean →
cheap `cache_read` → preserves/amplifies the understanding-task win, and it's what lets
passive population scale (ingest into a structured index, not a flat window).

**Hard parts / risks:**
1. **Retrieval/traversal quality is the whole game** — and hard for multi-hop; a graph
   helps but "which subgraph answers this" needs query planning, not just top-k.
2. **Incremental maintenance** of derived structure (graph/embeddings/skeleton) on every
   edit (ties to §1) — feasible but non-trivial, especially cross-language.
3. **Over-structuring backfires** — stripping natural-language scaffolding can make the
   LLM reason worse. Want dense facts *plus* prose glue; tuned empirically, not maximized.
4. **Scope** — this becomes a *code-intelligence platform*, not a context-window manager.
   Worth it if the payoff (cheap, always-hot, scalable understanding) holds — eyes open.

## 3. Sequencing (if/when we pick this up)

1. **Symbol/call-graph skeleton layer** — concrete, densest *faithful* code representation,
   directly serves the traversal questions where Live Memory wins, strict win over the
   prose ledger. Lowest regret.
2. **Passive ingestion via content-carrying hooks** + the manifest-triggered active path —
   plus the measurement hinge (warm vs cold query cost).
3. **Retrieval/subgraph projection** to keep the window lean as the store grows.
4. Only then consider a full graph-query engine / hierarchical multi-resolution store.

Each step is independently measurable against the benchmark harness (`benchmark/harness/`):
the metric that matters is **premium tokens on understanding-bound tasks**, and whether the
cheap-side cost stays under the premium saved.

## 4. Scaling & robustness on very large codebases

**Where we are (see DESIGN.md Appendix C).** Accumulation *cannot* overflow the context window —
compaction always sheds the persistent window to a small floor, and the knowledge ledger self-caps
at ~2048 tokens (each compaction regenerates it under `max_tokens=2048`). So a very large repo
degrades, it doesn't crash. But that same cap is the ceiling: the ledger can't represent a huge
codebase, so it becomes a **lossy, recency/frequency-biased** summary (older facts squeezed out).

**Three things to pursue (roughly in ROI order):**

1. **Retrieval, not a bigger ledger (the fundamental fix; = §2).** Ledger saturation is inherent to a
   flat single-ledger store. Raising the 2048 cap only delays it *and* re-inflates the window (the
   §0b bloat problem). The real answer is the §2 architecture — a hierarchical/graph/skeleton store
   with **per-query retrieval** projecting only the relevant subset into the window — so total known
   surface is unbounded while the in-window footprint stays lean. This is what lets Live Memory scale
   past "what fits in one 2k-token ledger."

2. **Per-question transient-token guard (cheap robustness; turns a crash into degradation).** The
   in-flight tool-result conversation is not budget-managed, so a single question that reads several
   large files can exceed the model's hard context → a "prompt too long" error. Add a **per-question
   cap on accumulated tool-result tokens** (stop reading + answer best-effort when near the limit)
   and/or a **reactive retry** (catch the context-length error → compact/trim → retry). Small, local
   to the agent loop; the one item that removes an actual hard-error edge on large repos. (Pairs with
   Appendix A's reactive-overflow note.)

3. **Bounded / lazy directory-tree scan.** `_scan` currently walks the entire workspace and builds the
   full tree in memory before truncating to ~10%. For giant monorepos, make the scan **stop after N
   entries** (or lazily expand) rather than scan-all-then-truncate, and consider leaning on
   Grep/Glob/find_paths instead of a large resident tree when the repo is huge.

**Measurement hinge:** run the understanding-bound A/B on a repo whose working set **exceeds** the
window (or shrink the window) and track recall + premium tokens as size grows — the point where recall
falls off is exactly where retrieval (§2) earns its keep.

## 5. Push-based orientation (SessionStart core-map injection)

**Idea.** Today the agent-facing surface is **pull-only**: it must decide to call `ask_live_memory`
(nudged by the skill + tool description). A brand-new/cold session gets no benefit until the model
thinks to ask. Add a **secondary, push** path that injects a tiny **"core map"** into a fresh
session's context so the agent starts *oriented* — it complements, never replaces, the pull path.

**Mechanism (verified against Claude Code docs).** MCP is pull-only (tools/resources/prompts enter
context only when Claude calls them). The supported push is a **`SessionStart` hook** — which a
plugin can ship in its `hooks/hooks.json` (matchers `startup` / `resume` / `clear` / `compact`).
The hook prints `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "…"}}`
to stdout and Claude Code injects that string into the session context, once, at start. (The
`Notification` hook is **human-facing** — terminal/desktop alerts — not context. `Channels`
(research preview, v2.1.80+, `--channels`) is the only true MCP push, but it's for reactive **event**
streams, not static context injection — not a fit here.)

**Shape.** A `SessionStart` hook script `curl`s a small new server route (e.g. `GET /coremap?cwd=…`)
that returns the **always-resident core map** from §2 — repo architecture, entry points, conventions —
*distinct from the full knowledge ledger* — and emits it as `additionalContext`. This is the §2 core
map made *pushable*; the deep Q&A stays pull (`ask_live_memory`).

**Load-bearing constraint (why this is easy to get wrong).** Whatever the hook injects lands in the
**premium main-agent context every session** — the exact cost Live Memory exists to *avoid*. So it
must be **tiny and high-value** (a ~100–300-token core map, never the ledger), and should:
- gate to `startup`/`clear` only (skip `resume`/`compact`, where the agent already has context);
- be **opt-in behind a config flag** (some users won't want unconditional premium tokens — consistent
  with the plugin's "nothing forced into context" stance);
- emit only once the workspace is **warm enough** to have a real core map (else it's noise).

Done carelessly it just reintroduces the §0b window-bloat it's meant to sidestep.

**Measurement hinge:** does a pushed core map cut the building agent's **cold-start exploration**
(fewer initial `Read`/`Grep`, earlier/fewer `ask_live_memory` calls) by enough to justify its
per-session premium tokens? A/B cold sessions with vs. without the injected core map, on the
understanding-bound task — the net is (premium exploration saved) − (core-map tokens spent each
session). Depends entirely on keeping the map small and only pushing it when it pays.

## 6. Ledger freshness (provenance-tagged compaction) — ✅ IMPLEMENTED (core)

> **Shipped.** `LedgerFact` (`models.py`) carries `sources: {path → content_hash}`;
> `ContextWindow.ledger_facts` is the source of truth and renders the `knowledge_ledger`
> text readers already consume. A cited file changing **in-session** (`invalidate_file_context` /
> `mark_file_deleted` → `mark_ledger_stale`) or **cross-session** (`conversation_store` re-hashes
> each fact's sources against disk on load) demotes citing facts under `STALE_LEDGER_HEADING`.
> Attribution is per-line/path-mention (each fact cites the manifest paths its text names → small
> source sets); the summarizer is fed the plain fact text (`ledger_for_summary`, heading stripped).
> **Remaining future:** background *re-derivation* of demoted facts (today they're flagged, not
> auto-refreshed); optional model-attribution to narrow source sets; subsumption by the §2 store.
> The rationale below records the design.

**The gap — asymmetric staleness across the two tiers.** Live Memory keeps two kinds of memory and
handles their staleness very differently. **Raw file contexts** are a hash-pinned manifest
(`FileContext.content_hash`, `models.py`) that *self-heals*: the watcher fires `FileChanged` →
`context_window.invalidate_file_context()` blanks the hash (→ stale → re-read/drop), and on load
`conversation_store` re-checks each hash against disk and drops mismatches. So even **out-of-band**
edits (a `git pull`, another editor, a delete) can't leave stale raw evidence. **The knowledge
ledger** has no such link: compaction (`summarizer.py`) distills dropped Q&A into free-text facts and
in doing so throws away the *checkable reference* between a fact and the file version it came from —
it keeps the path as **text** (`SessionManager lives in src/auth/session.ts`), not as a hash it can
re-validate. After an out-of-band change, that sentence is silently orphaned. Today the only defense
is **precedence** (the prompt declares fresh file bytes strictly stronger than ledger hints), which is
soft and only fires when the relevant file happens to be back in-window.

**Idea.** Carry provenance *through* compaction: tag each compacted fact with the `{path:
content_hash}` of the files it drew on — hashes we **already compute** for the manifest — then at
recall (or on `FileChanged`) compare stored vs current hashes and **demote or re-derive only the facts
whose sources changed**. Provenance is *advisory metadata riding alongside the prose* — never a schema
the model must fill, never an index it must trust. This imports RAG's automatic-freshness property
(every fact traceable to a live source) into Live Memory's accumulate-and-reason model, keeping the
cumulative understanding pure RAG lacks (see `COMPARISON.md` — "The synthesis").

**Mechanism.**
- `knowledge_ledger: str` → `list[FactRecord]` (`text`, `sources: [{path, content_hash}]`,
  `written_at`), with a `render_ledger()` that flattens back to prose so the model's view — and every
  downstream prompt — is unchanged. Provenance is a **sidecar**; the hashes never enter the
  token-budgeted prose the model reads.
- **Attribution: mechanical first.** Tag each fact from a compacted Q&A slice with the union of
  `{path: content_hash}` for the file contexts that were in-window when that Q&A was answered. It
  cannot hallucinate a link (worst case it *over*-tags). Optional model-attribution later to narrow
  the source set.
- **Validation reuses the existing path.** On `FileChanged` (already wired, `server.py`) or at recall,
  compare stored source hashes to current; on mismatch mark `stale=True` and render that fact under a
  `⚠ possibly out of date — re-verify against current files` heading (leaning on the precedence rule
  that already exists). Optional: a cheap background pass that re-summarizes just the stale facts
  against the changed file's new content — self-healing, mirroring what file contexts already do.

**Why it respects the §0 constraints.** The ledger the model reads/writes stays **free-text prose**
(§0: the model reasons best over natural language) — no knowledge graph, no per-sentence citation UI.
It reuses hashing already done; the check is **O(changed files)**, not O(ledger). The model never
*trusts* the tags — they only gate demotion/re-derivation — which preserves the core bet ("reason over
current context, not an index it trusts"). It composes with §1 (passive sync already produces the
FileChanged events and fresh hashes) and with §2 (when the store becomes a graph/skeleton, provenance
generalizes to node/edge → source-hash, same mechanism).

**Granularity tension (the one knob).** Too coarse (hash the whole transcript window per fact) →
over-demotion: a fact flagged because *some unrelated* file in that window changed. Too fine
(per-sentence / per-symbol source-linking) → brittle and expensive, drifting back toward the symbolic
index §2 explicitly warns against. **Sweet spot: per-fact with a small source set (1–3 files).**
Whole-file hashing still means an edit to an *irrelevant section* of a cited file demotes the fact —
acceptable, because **demote = re-check, not delete**.

**Hard parts / risks.**
1. **Attribution precision** — mechanical tagging over-tags (churn); model attribution can mislink.
   Start mechanical; only add model-narrowing if false demotions become annoying.
2. **Re-derivation cost/scheduling** — on-demand (blocks the answer) vs background (eventual). Prefer
   background, with demotion (cheap) as the always-on guardrail.
3. **Interaction with the §4 ledger self-cap (~2048 tokens).** Keep provenance strictly out of the
   rendered prose so it never competes with the token budget — hashes live in the snapshot sidecar
   only.
4. **Possible subsumption by §2.** Once a graph/skeleton store lands, nodes carry source refs
   natively and this may be redundant. So scope it as an **interim, additive** win for the *prose
   ledger* — cheap now, not a permanent subsystem.

**Sequencing.** Cheap and additive; **can ship before the §2 store**. Steps 1–3 (FactRecord +
mechanical tagging + FileChanged demotion + the prose heading) touch only existing code
(`summarizer.py`, `context_window.py`, `models.py`, `server.py`); background re-derive is a later,
optional layer.

**Measurement hinge.** During an understanding-bound run, mutate files the ledger holds facts about
**out-of-band** (edit/delete outside the agent) and measure the **stale-answer rate** with vs. without
demotion — alongside the **false-demotion rate** (facts demoted whose relevant content didn't actually
change). Net win = (wrong answers avoided) − (extra re-derivation cost + churn from false demotions).
