---
name: live-memory
description: When you need to understand this codebase — where something lives, how a component works, what calls what, conventions — ask the Live Memory instead of re-reading files yourself. It is a persistent, cheap, large-context companion that has already accumulated knowledge of this repo across sessions.
---

# Live Memory

The **Live Memory** is a long-running companion that holds an ever-growing,
read-only understanding of *this* codebase. Use it to answer "where / how / what
calls what" questions cheaply, without loading large files into your own context.

## When to use `ask_live_memory`

Prefer it for **codebase-knowledge** questions:

- "Where is X implemented / configured / registered?"
- "How does component Y work, and what does it depend on?"
- "What calls Z / what would break if I change it?"
- "What's the convention for doing W in this repo?"

It can read files, ripgrep, and glob on its own to answer — you do not need to
pre-load context for it.

## When NOT to use it

- For writing or editing code (it is **read-only** — it answers, it never changes files).
- For facts unrelated to this repository.
- When you already have the answer in your own context.

## How to call it

Call the `ask_live_memory` MCP tool with:

- `question` (required) — a specific, self-contained question.
- `cwd` (required) — the **absolute** path of the current project/repository
  root (your session's working directory). Must be absolute — a relative path is
  rejected, since the server cannot resolve it against your session. The memory
  is keyed per repository, so always pass the repo root, not a subdirectory.
- `timeout` (required) — seconds you are willing to wait. The Live Memory is told
  this budget and returns its best answer by the deadline; pick a value that fits
  your latency tolerance (e.g. 30–120s for a normal lookup).

Ask **one focused question per call**. The answer string is what you get back —
the Live Memory's own working context is private and does not enter yours.
