# Working agreements — Live Memory

Rules for anyone (human or agent) changing this repo. Keep it short and
high-signal — durable rules only, current-state. Full design: `DESIGN.md`.

## What this is

A Claude Code plugin: a persistent, **read-only**, large-context codebase-Q&A
companion. One long-running HTTP MCP server (Python/asyncio) exposes a single
tool, `ask_live_memory`, and accumulates repo knowledge across sessions. State
is keyed per workspace (`cwd`). See `DESIGN.md` for the full rationale.

## Stack & conventions

- **Python ≥ 3.10, asyncio.** Server code lives in `server/live_memory/`; it's a
  standalone package (`server/pyproject.toml`). Key deps: `mcp` (FastMCP,
  streamable-http), `anthropic`, `starlette`, `uvicorn`, `watchdog`.
- **Type checking is `mypy --strict`** (`pyproject.toml`, excludes `tests/`).
  **No ruff/black/flake8 is configured** — don't assume a formatter runs.
- **All tunable magic numbers live in `server/live_memory/constants.py`** — no
  scattered literals.
- Add a runtime dep = edit `pyproject.toml` `dependencies` + `pip install -e .`.
  New source files are auto-discovered (setuptools) — no manual srcs list.

## Test / gate

- **`./run-tests.sh`** (repo root) is the gate: `mypy live_memory/` then
  `pytest -q`, bootstrapping `server/.venv` with `.[dev]` if needed. It's the
  pre-push hook (`git-hooks/pre-push`; install once via `git-hooks/install.sh`).
  Leave the tree green before committing.
- Faster inner loop from `server/`: `mypy live_memory/` +
  `pytest -m "not integration" -q`. Tests are fully mocked (fake LLM, mocked
  httpx/OAuth) — no network or API key needed.

## Running / deploying the server

- **The server must already be running before Claude Code connects.** `.mcp.json`
  is `type: http` — Claude Code *connects*, never spawns it; if it's down you get
  a connection error. Dev: `cd server && pip install -e . && python -m live_memory`
  → `http://127.0.0.1:7711/mcp` (+ `/health`, `/stats`, `/notify`, `/reload`).
- **Prod = a *user* systemd service on :7711** via `deploy/install-service.sh`
  (defaults to a user service — required so zero-config subscription OAuth can
  read `~/.claude/.credentials.json`). It's a supervised singleton serving every
  session: **restart with `systemctl --user restart live-memory`; never kill it
  ad hoc** — that breaks every connected session (and systemd restarts it anyway).
- Model/provider: set via env (`LIVE_MEMORY_*`) or the `/live-memory-config`
  command, which writes `config.json` in the data dir and **hot-reloads via
  `POST /reload` — no restart**. Precedence: **env > config.json > defaults**.

## Non-obvious invariants (don't break these)

- **Read-only, path-jailed to `cwd`, forever.** The exploration tools are only
  `Read`/`Grep`/`Glob` + `find_paths`/`get_changed_files`/`git_search`. Never add
  `Write`/`Edit`/`Bash` — read-only-ness is the product's core guarantee.
- **`cwd` must be absolute**; it's the workspace partition key, canonicalized to
  the enclosing git repo root (a subdir and its repo root share one memory).
  Relative paths are rejected, not resolved.
- **Window B is append-only between compactions; compaction is batched, neutral
  (query-agnostic) summarization — never front-truncation.** Keep
  `compaction_floor` below `compaction_threshold` (floor == threshold reproduces
  a ~10× cache-thrash regression).
- **Two wire protocols only** (`anthropic` Messages + `openai`-compatible).
  `TOOL_SCHEMAS` are defined **Anthropic-native** (`input_schema`) and translated
  to OpenAI shape at request time — keep new tools Anthropic-shaped.
- **Token budget is a soft `chars/≈4` heuristic** reconciled against real `usage`;
  never a correctness input — do not add a `count_tokens` round-trip (DESIGN
  Appendix A).
- **Passive-ingestion bytes are in-memory only, never persisted** — snapshots
  keep a distilled ledger + file manifest, not raw file contents (also a privacy
  line — see `PRIVACY.md`).
- **Benchmarks:** the standalone Python probes (`benchmark/harness/*.py`) spin up
  their own server on a separate port (7712/7714) + scratch data dir; the A/B
  *orchestrator* shell scripts deliberately drive the prod :7711 server and call
  `POST /clear`. Don't point a probe at :7711.
- **Plugin files are cached under `~/.claude/plugins/`** after install — source
  edits aren't live. For dev, launch `claude --plugin-dir <path>` (picks up edits
  via `/reload-plugins`); otherwise `/plugin marketplace update` then
  `/reload-plugins`.

## Docs

- Current-state only: no changelogs, no "previously…". Keep `README.md` /
  `DESIGN.md` and each surface's doc in sync with the code in the same change.
