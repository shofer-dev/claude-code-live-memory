# Testing Live Memory

From fastest to full end-to-end. Paths below assume the plugin lives at
`claude-code/live-memory/`; adjust as needed.

## 0. One-time setup

```bash
cd claude-code/live-memory/server
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # runtime deps + mypy/pytest
```

**Prerequisites:** Python ≥ 3.10; **ripgrep** (`rg`) for `Grep`; `git`
(optional) for `git_search` / `get_changed_files`.

**Behind a private package proxy?** If `pip install` fails to reach an internal
mirror, point pip straight at PyPI for this command (env var, so the
build-isolation subprocess inherits it):

```bash
PIP_INDEX_URL=https://pypi.org/simple/ pip install -e ".[dev]"
```

## 0b. Pre-push gate (blocks a bad push)

Mirroring `extensions/shofer-router`, the full suite gates `git push`. `run-tests.sh`
runs `mypy --strict` + the whole `pytest` suite (bootstrapping the venv's dev deps
if needed) and exits non-zero on any failure:

```bash
./run-tests.sh                       # run the gate by hand
./git-hooks/install.sh               # install the pre-push hook (once per clone)
```

`git-hooks/install.sh` copies `git-hooks/pre-push` into the monorepo's
`.git/hooks/`. On `git push`, if the push includes changes under
`claude-code/live-memory/`, the hook runs `run-tests.sh` and **blocks the push if
anything fails**; pushes that don't touch live-memory are unaffected. Re-run
`install.sh` after editing the committed hook (it copies, not symlinks).

## 1. Automated checks (seconds, no network)

```bash
mypy live_memory/        # → "Success: no issues found in N source files"
pytest -q                # → all pass (unit + integration)
pytest -m "not integration" -q   # unit only (faster; no subprocess/ports)
```

Most are **mocked unit tests** (fake LLM, mocked httpx/credentials) covering token
budgeting, context-window eviction + threshold compaction, the SHA-256 store, the
path-jailed tools, queue/concurrency, fork-join commit, async jobs, keep-warm
eligibility, pricing overrides, OpenAI-compat conversion + OAuth refresh, config
layering, and the full agent loop. No API key or network required.

There is also one **integration test** (`tests/test_integration.py`, marked
`integration`) that launches the **real `python -m live_memory` server** pointed
at a **mock OpenAI endpoint** (no network/cost) and drives it over the **real
streamable-http MCP transport**: tool registration, `ask_live_memory` round-trip
+ metadata trailer, the async `submit`/`result` pair, relative-cwd rejection,
`/stats`, and that the keep-warm loop actually starts (the kind of wiring a
mocked-LlmClient unit test can't reach). It runs by default; skip with
`-m "not integration"`.

## 2. Run the server + smoke-test it (no Claude Code needed)

If you're logged into a Claude **subscription**, this is **zero-config** — it
reuses that credential (auto-refreshed) on Haiku. Otherwise set a key (see §3).

```bash
# terminal A — start the server (idempotent singleton)
cd claude-code/live-memory/server && source .venv/bin/activate
python -m live_memory
# → "Live Memory starting on http://127.0.0.1:7711/mcp (model=..., auth=...)"
```

```bash
# terminal B — endpoints
curl -s 127.0.0.1:7711/health ; echo
curl -s "127.0.0.1:7711/stats?cwd=$PWD" ; echo     # auth + metered + window stats
```

**Drive the real `ask_live_memory` tool over MCP** (the key test — it reads code
and answers):

```bash
python - <<'PY'
import asyncio
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
REPO = "/absolute/path/to/the/codebase/to/ask/about"
async def main():
    async with streamablehttp_client("http://127.0.0.1:7711/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("ask_live_memory", {
                "question": "Where is X implemented and what calls it?",
                "cwd": REPO, "timeout": 60,
            })
            print(res.content[0].text)
asyncio.run(main())
PY

curl -s "127.0.0.1:7711/stats?cwd=/absolute/path/to/the/codebase" | python3 -m json.tool
```

## 3. Pick a model / provider

Two equivalent ways:

```bash
# (a) env vars — restart the server. DeepSeek (cheap, recommended):
LIVE_MEMORY_PROVIDER=openai LIVE_MEMORY_BASE_URL=https://api.deepseek.com \
  LIVE_MEMORY_API_KEY=sk-... LIVE_MEMORY_MODEL=deepseek-v4-flash  python -m live_memory

# (b) hot-reload a RUNNING server (no restart) — same as the /live-memory-config command:
python ../commands/config.py set provider=openai base_url=https://api.deepseek.com \
                                 model=deepseek-v4-flash api_key=sk-...
python ../commands/config.py show
```

Providers: `anthropic` (Messages API + Bedrock/Vertex/gateways; API key **or**
subscription OAuth) and `openai` (any OpenAI-compatible endpoint: OpenAI,
DeepSeek, local models, gateways).

## 4. Full plugin inside Claude Code (the real deal)

1. Keep the server (step 2) **running** — Claude Code does not start `type:http` servers.
2. Install the plugin: `/plugin` → install from the local path
   `claude-code/live-memory/`, then enable it. Confirm with `/mcp` that
   **`live-memory`** connected and exposes `ask_live_memory`.
3. Ask a codebase question — the agent should call `ask_live_memory` on its own
   (guided by the skill), or invoke it directly.
4. **Human status:** `/live-memory-stats` and `/live-memory-config show`
   (these are user-facing, never seen by the agent).
5. **File-change notification** (informs, doesn't instruct):
   - Ask something that makes it read file `X` → `/live-memory-stats` shows
     `fileContexts` increment.
   - Edit `X` (any Write/Edit, or change it on disk).
   - Ask a related question again → the model is *informed* that `X` changed
     since it read it, and decides for itself whether to re-read (and which
     lines). Editing a file it has **not** read produces no notification.

## Notes

- **Cost:** on the subscription path `/stats` shows *"subscription — rate-limited,
  not $-metered"* (it draws on your subscription's rate-limit budget — a ToS gray
  area — not dollars). With a DeepSeek/OpenAI/Anthropic **API key** it shows a real
  `$` estimate.
- **Reset state:** `rm -rf ~/.claude/plugins/data/live-memory/` (per-workspace
  snapshots, `config.json`, and `oauth_state.json` live there).
- **Lifecycle:** the HTTP transport needs the server pre-running and supervised
  (systemd/container). If `/health` doesn't respond, check the server terminal for
  the startup model/auth line and any error.
- **CI gate:** `mypy live_memory/ && pytest`.
