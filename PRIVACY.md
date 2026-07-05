# Privacy Policy — Live Memory (Claude Code plugin)

_Last updated: 2026-07-05_

Live Memory is an open-source Claude Code plugin published by **shofer.dev**. It runs entirely on
**your own machine** as a local MCP server. This policy describes exactly what data it touches, what
leaves your machine, and what is stored — so you can decide with full information.

**Short version:** the plugin author (shofer.dev) collects **nothing**. There is no telemetry, no
analytics, and no server operated by shofer.dev that receives your data. The only data that leaves your
machine goes to the **LLM provider you configure** (Anthropic by default), under that provider's terms.

## What the plugin accesses

To answer your agent's codebase questions, Live Memory reads **source files in the workspace (repo)
you point it at**:

- **Passively:** hooks tee the *content of files your Claude Code agent already reads or edits* to the
  local server, so it learns from work you were doing anyway (no extra reading).
- **Actively:** when answering a question it can read files itself using **read-only, path-jailed**
  tools (Grep/Read) confined to the workspace. It **cannot** edit, create, delete, or execute anything.

It does not access files outside the workspace, your environment secrets, your browser, or any other
application.

## What leaves your machine, and to whom

To generate an answer, Live Memory sends the relevant **codebase content and your question** to the
**LLM provider you have configured** — and to no one else:

- **Default:** Anthropic (`api.anthropic.com`), using your existing **Claude subscription** via an OAuth
  token (it also contacts Anthropic's OAuth endpoint, `console.anthropic.com`, to refresh that token).
  No API key is required in this mode.
- **If you reconfigure it** (via `/live-memory-config` or environment variables): the provider you
  choose instead — e.g. DeepSeek or any OpenAI-compatible endpoint, or a **fully local model** (in which
  case nothing leaves your machine at all).

That data is processed by the chosen provider under **their** privacy policy and your account terms with
them (for Anthropic, see Anthropic's Privacy Policy and Commercial/Consumer Terms). shofer.dev is not a
party to that transfer and never receives a copy.

**No third-party analytics or telemetry** are included. The plugin makes no network calls other than to
the configured LLM provider (and Anthropic's OAuth endpoint for subscription auth).

## What is stored, and where

Live Memory keeps its accumulated codebase knowledge as **JSON snapshot files on your local disk**
(by default under `~/.claude/plugins/data/live-memory/`, or wherever you set `LIVE_MEMORY_DATA_DIR` /
`CLAUDE_PLUGIN_DATA`). This is local persistence so the memory survives across sessions. It is never
uploaded anywhere. Raw teed file bytes are held in memory only; snapshots keep a distilled summary and
file manifest, not full file contents.

## Your controls

- **Wipe the memory** for a workspace (or all) at any time: `/live-memory-empty`.
- **Inspect** what it holds and its cost: `/live-memory-stats`.
- **Change or localize the model/provider** (including running fully offline): `/live-memory-config`.
- **Stop it** by stopping the local server; **remove all stored data** by deleting the data directory
  above; **uninstall** the plugin with `/plugin uninstall`.

## Children

Live Memory is a developer tool and is not directed to children under 13.

## Changes

Material changes to this policy will be reflected in this file in the plugin's repository, with an
updated date.

## Contact

Questions or concerns: open an issue at
<https://github.com/shofer-dev/claude-code-live-memory/issues> or contact shofer.dev.
