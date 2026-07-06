# Live Memory vs. other codebase-context approaches

How Live Memory relates to the other ways a coding agent avoids re-reading your
repo: GitHub Copilot's `@workspace`/`#codebase` retrieval, Cursor's codebase
index + memories, and plain Claude Code with no memory layer.

> **Scope & freshness.** This compares *approaches to giving a coding agent
> repo context*, not overall product quality. Competitor behavior reflects
> publicly documented designs **as of early 2026** and ships fast — verify
> specifics before quoting them publicly.

## TL;DR — same goal, different bets

Everyone wants the same thing: the model shouldn't burn its budget re-discovering
your codebase every turn. There are two families of answer:

- **Retrieve fresh every turn (RAG).** Copilot and Cursor build an *index*
  (embeddings + lexical + symbols) and pull relevant chunks *per request*,
  straight from the current files. Always fresh, cited to a live source — but
  **stateless between turns**: each query re-discovers relevance from scratch and
  builds no cumulative understanding.
- **Accumulate and reason (memory).** Live Memory runs a *persistent
  large-context model* that accumulates understanding across sessions, learns
  passively from the agent's own reads/edits, and answers one read-only tool.
  Builds durable understanding — at the cost of having to keep that memory fresh.

Live Memory is the only one of these that (a) **learns passively** from what your
agent already does, (b) **persists reasoning across sessions**, and (c) runs
**fully local, read-only, on a model you choose** (including a local one).

## At a glance

| Dimension | **Live Memory** | **Copilot `@workspace`** | **Cursor `@Codebase`** | **Claude Code (no layer)** |
|---|---|---|---|---|
| Mechanism | Persistent large-context model + neutral knowledge ledger | Per-request RAG over embeddings/lexical/symbol index | Per-request RAG over local embeddings index (+ Memories) | Agent re-reads files each session |
| Cumulative understanding | **Yes** — accumulates across sessions | No — re-retrieves each turn | Partial — Memories persist curated facts | No |
| Learns passively from your edits | **Yes** (hooks tee reads/edits, no extra reading) | No | No (Memories are model/user-authored) | No |
| Persists across sessions | **Yes** (local JSON snapshot per workspace) | Index persists; no reasoning state | Index + Memories persist | Only `CLAUDE.md` (hand-written) |
| Freshness / staleness | File contexts hash-pinned & watcher-invalidated; ledger uses precedence (see cons) | **Automatic** — re-retrieved from current files | **Automatic** — re-retrieved; Memories can drift | Always fresh (re-reads) but expensive |
| Source citation / provenance | Facts carry path refs (hashes on the roadmap) | **Strong** — shows References (file+range) | Shows retrieved files | Whatever the agent read |
| Where your code goes | **Local only**; egress only to *your* configured LLM | Remote index on GitHub/MS servers | Embeddings to Cursor servers (per privacy mode) | Stays in your Claude Code session |
| Model choice | **Pluggable** — Claude sub (Haiku, no key), local, or any OpenAI-compatible | Fixed (Copilot models) | Fixed (Cursor models) | Your Claude Code model |
| Read-only / safety | **Read-only, path-jailed** by construction | N/A (assistant, not a separate memory) | N/A | Agent has full tool access |
| Setup / ops | One MCP server per workspace (a singleton to supervise) | Zero (managed) | Zero (managed) | Zero |
| Cost shape | Cheap/local companion answers broad questions; premium model does the work | Included in Copilot subscription | Included in Cursor subscription | Premium model pays to re-read |

## The approaches in detail

### Live Memory (this plugin)

**How:** a separate, cheap or local large-context model runs as a long-lived MCP
server. It learns your repo passively (the agent's reads/edits are teed via hooks
— no extra reading), keeps raw file contexts as a hash-pinned manifest, and
compacts older Q&A into a neutral free-text *knowledge ledger*. Your agent asks
broad questions through one read-only tool, `ask_live_memory`, instead of
re-reading files.

**Pros**
- Only approach that **builds cumulative understanding** *and* **learns
  passively** — it gets better as you work, with no extra reads.
- **Persists across sessions** — a new session is bootstrapped, not cold.
- **Local & private** by default; **read-only and path-jailed** — it can never
  edit, create, or run anything.
- **Model-agnostic** — free on a Claude subscription (Haiku, no API key), or
  point it at a local model for ~free, or any OpenAI-compatible endpoint.
- Shifts broad "where/how/what-calls" work off the **premium** model onto a cheap
  companion (understanding-bound benchmark: premium coding-model bill −61%; see
  [`benchmark/results/RESULTS.md`](./benchmark/results/RESULTS.md)).

**Cons**
- **Cold start:** it must observe the repo before it's useful (mitigated by
  passive learning, but a brand-new checkout starts thin).
- **Ledger drift:** compacted free-text facts keep *path references* but not
  content hashes, so a fact can go stale after an out-of-band change. Today this
  is handled by *precedence* (fresh file bytes outrank ledger hints), not
  validation — a soft guarantee. Provenance-tagged compaction is the planned fix
  (see [`FUTURE_DIRECTIONS.md`](./FUTURE_DIRECTIONS.md)).
- **Answer quality is bounded by the companion model** you choose — a very cheap
  or small local model trades some accuracy for cost.
- **An extra moving part:** a per-workspace server to run/supervise.
- **Not a substitute for exact bytes:** when the agent needs a precise diff or
  literal file content, it should still read the file — Live Memory is for broad
  understanding, not line-exact retrieval.

### GitHub Copilot — `@workspace` / `#codebase`

**How:** builds an index of the repo — a GitHub-hosted **remote embeddings
index** for indexed repos (refreshed as you push), with a local fallback — and
per request blends **semantic + lexical search + the language-server symbol
graph** + recently-open files. Retrieved snippets are live file chunks shown as
**References**. Persistent context is added via `.github/copilot-instructions.md`
(instructions) and **Copilot Spaces** (user-curated context bundles).

**Pros**
- **Automatic freshness** — re-retrieves from current files every turn, so no
  distilled fact to go stale.
- **Strong source-linking** — every answer cites the file/range it used.
- **Zero setup / fully managed**; strong symbol-graph grounding via the LSP.

**Cons**
- **No cumulative understanding** — each query re-discovers relevance from
  scratch; quality is bounded by retrieval.
- **Doesn't learn passively** from your edit history into durable memory.
- **Code leaves your machine** — indexing/queries run on GitHub/Microsoft
  servers; fixed models; requires a Copilot subscription.

### Cursor — `@Codebase` + Memories

**How:** a local **codebase embeddings index** for retrieval (`@Codebase`), plus
a **Memories** feature that persists curated rules/project facts across sessions.

**Pros**
- Automatic freshness on retrieval; **Memories add some persistence** of
  project-specific facts (closest mainstream analog to Live Memory).
- Tight editor integration, zero setup.

**Cons**
- Memories are **model/user-authored rules**, not passively-learned repo
  understanding, and can themselves drift.
- Embeddings are computed/handled on **Cursor's servers** (subject to privacy
  mode); fixed models; subscription.

### Claude Code with no memory layer (the baseline)

**How:** the agent re-reads whatever it needs each session; `CLAUDE.md` provides
persistent *instructions*.

**Pros**
- Always exactly fresh; nothing to run; zero drift.
- `CLAUDE.md` captures durable conventions the author cares about.

**Cons**
- The **premium model pays, in tokens and latency, to re-discover the repo every
  session** — exactly the cost Live Memory targets.
- `CLAUDE.md` is **hand-written and static** — no passive learning, no accumulated
  understanding.

## When to use which

- **Reach for Live Memory** when you work in the same repo repeatedly, want the
  cost of broad "where/how/what-calls" questions off the premium model, and care
  about running **locally / read-only / on a model you control**. Its edge is
  *cumulative, passively-learned, private* understanding.
- **Copilot/Cursor's RAG is stronger** when you want **zero ops**, **line-exact
  cited retrieval every turn**, and don't mind code being indexed on a vendor's
  servers — and when you're already living in that editor.
- **Plain Claude Code** is fine for one-off tasks in an unfamiliar repo where
  there's nothing to accumulate yet.

These aren't mutually exclusive: Live Memory answers the *broad-understanding*
questions cheaply; the agent still reads exact files (and can use an editor's RAG)
when it needs literal bytes.

## The synthesis

The interesting direction is **combining the families**: keep Live Memory's
cumulative, passively-learned understanding, and borrow RAG's *automatic
freshness* by attaching source hashes to each compacted fact so stale prose is
detected and re-derived — **provenance-tagged compaction**. That would give Live
Memory both cumulative understanding (which pure RAG lacks) *and* a checkable
freshness guarantee (which it currently only approximates). See
[`FUTURE_DIRECTIONS.md`](./FUTURE_DIRECTIONS.md).
