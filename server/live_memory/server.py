"""The Live Memory MCP server (HTTP transport) + human/ops HTTP endpoints.

MCP tool: `ask_live_memory(question, cwd, timeout)`. Plus four plain HTTP routes
the agent never sees:
  - GET  /health   — liveness for the supervisor.
  - GET  /stats    — human status (backs /live-memory-stats).
  - POST /notify   — the hooks' file-change feed (edited / changed).
  - POST /reload   — re-read config (env + config.json) and hot-swap the model/
                     provider (backs /live-memory-config). No restart needed.

Zero-config default: with no API key but a Claude subscription present, runs on
the subscription OAuth token + Haiku. Switch models via /live-memory-config.
"""
from __future__ import annotations

import asyncio
import os
import time

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .async_jobs import JobRunner
from .config import Config, is_absolute_cwd
from .constants import MAX_QUESTION_TIMEOUT_S, MIN_QUESTION_TIMEOUT_S
from .keep_warm import keep_warm_loop
from .llm_client import LlmClient, make_client
from .manager import process_question
from .models import QuestionResult
from .oauth import OAuthCredential
from .question_queue import QueueFull, QuestionTimeout
from .summarizer import Summarizer
from .workspace import WorkspaceRegistry, WorkspaceState

_START = time.time()


def _format_metadata(result: QuestionResult, ws: WorkspaceState) -> str:
    """A compact, clearly-delimited trailer the caller can parse or ignore —
    surfaces the per-question accounting the tool would otherwise hide."""
    u = result.context_usage
    cost = f"${result.cost_snapshot.usd:.4f}" if ws.cfg.metered else "n/a(subscription)"
    bits = [
        f"model={ws.cfg.model}",
        f"latency={result.duration_ms / 1000:.2f}s",
        f"tokens={result.tokens_used}(in={result.prompt_tokens},out={result.completion_tokens})",
        f"tool_calls={result.tool_calls}",
        f"files_read={result.files_read}",
        f"cost={cost}",
        f"context={u.used_tokens}/{u.max_tokens}({u.fill_pct:.1f}%)",
        f"qa_msgs={u.qa_messages}",
        f"file_ctx={u.file_contexts}",
    ]
    if result.timed_out:
        bits.append("timed_out=true")
    return "---\n[live-memory] " + " ".join(bits)


def _build_clients(cfg: Config) -> tuple[LlmClient, Summarizer]:
    oauth = OAuthCredential(cfg.oauth_state_path) if cfg.use_oauth else None
    llm = make_client(cfg, oauth_cred=oauth)
    return llm, Summarizer(llm)


def build_server(cfg: Config | None = None) -> FastMCP:
    cfg = cfg or Config()
    llm, summarizer = _build_clients(cfg)
    registry = WorkspaceRegistry(cfg, llm, summarizer)
    mcp = FastMCP("live-memory", host=cfg.host, port=cfg.port)

    # Background KV/prompt-cache keep-warm loop — started lazily on the first query
    # (when it first becomes relevant), since FastMCP's streamable-http app owns its
    # own ASGI lifespan and runs a custom lifespan per-MCP-session, not once at boot.
    _kw: dict[str, object] = {"task": None}

    def _ensure_keep_warm() -> None:
        if cfg.keep_warm and _kw["task"] is None:
            _kw["task"] = asyncio.create_task(keep_warm_loop(registry, cfg))

    def _validate(question: str, cwd: str) -> str | None:
        if not question or not question.strip():
            return "Error: 'question' is required."
        if not cwd:
            return "Error: 'cwd' is required."
        if not is_absolute_cwd(cwd):
            return ("Error: 'cwd' must be an absolute path to your project root. The Live "
                    "Memory runs as a separate, shared server and cannot resolve a path "
                    "relative to your session's working directory — pass the absolute path.")
        return None

    def _clamp_timeout(timeout: float) -> float:
        return max(MIN_QUESTION_TIMEOUT_S, min(float(timeout) if timeout else registry.cfg.default_timeout_s, MAX_QUESTION_TIMEOUT_S))

    async def _answer(question: str, cwd: str, timeout_s: float) -> str:
        """Run one question through the workspace queue and render answer + metadata.
        Raises QueueFull/QuestionTimeout/Exception — the caller decides how to report."""
        _ensure_keep_warm()  # idempotent; starts the warm loop on the first query
        ws = await registry.get(cwd)
        ws.invocations += 1  # count every well-formed call (answered or not), before processing

        async def proc(q: str, c: str, deadline: float) -> QuestionResult:
            return await process_question(ws, q, deadline)

        result = await ws.queue.submit(question, cwd, timeout_s, proc)
        answer = f"[partial — soft timeout reached]\n{result.answer}" if result.timed_out else result.answer
        return f"{answer}\n\n{_format_metadata(result, ws)}"

    @mcp.tool()
    async def ask_live_memory(question: str, cwd: str, timeout: float) -> str:
        """Ask the Live Memory about the codebase at `cwd`.

        The Live Memory is a persistent, read-only companion that accumulates
        knowledge of *this* repository across questions and sessions. It runs a
        separate, cheap, large-context model and explores on its own (read,
        grep, glob, git history), so it can answer "where is X / how does Y work
        / what calls Z / what's the convention for W" without you loading files
        into your own context — and it often already knows the answer from
        earlier questions. Expect a best-effort answer by your `timeout` from a
        smaller model: grounded in the actual code, but terser than your own
        reasoning. Its working context stays private; it never edits code.

        Args:
            question: A specific, self-contained question about the codebase.
            cwd: The ABSOLUTE path of your project root — your session's working
                directory / repository root, never a subdirectory and never a
                relative path. The Live Memory keys its persistent memory per
                repository, so always pass the repo root so questions about the
                same project share one accumulating memory.
            timeout: Seconds you are willing to wait; the Live Memory is told
                this budget and returns its best answer by the deadline.
        """
        err = _validate(question, cwd)
        if err:
            return err
        try:
            return await _answer(question, cwd, _clamp_timeout(timeout))
        except (QueueFull, QuestionTimeout) as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Error: the Live Memory failed to answer: {e}"

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "uptimeSeconds": int(time.time() - _START)})

    @mcp.custom_route("/stats", methods=["GET"])
    async def stats(req: Request) -> JSONResponse:
        cwd = req.query_params.get("cwd", os.getcwd())
        c = registry.cfg
        ws = registry.existing(cwd)
        if ws is None:
            auth = "oauth-subscription" if c.use_oauth else ("api-key" if c.api_key else "none")
            base = {"cwd": cwd, "model": c.model, "endpoint": c.base_url, "metered": c.metered, "auth": auth,
                    "concurrency": c.concurrency,
                    "uptimeSeconds": int(time.time() - _START),
                    "contextWindow": {"usedTokens": 0, "maxTokens": c.max_context_tokens, "fillPct": 0.0,
                                      "qaMessages": 0, "fileContexts": 0, "staleFileContexts": 0},
                    "lastCompaction": None, "summariesWritten": 0, "questionsAnswered": 0,
                    "invocations": 0,
                    "keepWarms": 0, "lastTouchAt": None, "queueDepth": 0, "costUsd": 0.0,
                    "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}
        else:
            base = ws.stats()
            base["uptimeSeconds"] = int(time.time() - _START)
        if not base.get("metered", True):
            base["costUsd"] = None  # subscription: not $-metered (don't show a notional number)
            base["costNote"] = "subscription — rate-limited, not $-metered"
        return JSONResponse(base)

    @mcp.custom_route("/notify", methods=["POST"])
    async def notify(req: Request) -> JSONResponse:
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        kind = body.get("kind", "changed")
        event = body.get("event", "")  # FileChanged: change | add | unlink
        cwd = body.get("cwd") or os.getcwd()
        contents = body.get("contents") or {}  # passive ingestion: {path: file content}
        passive = registry.cfg.passive_ingestion
        cap = registry.cfg.passive_max_file_bytes
        # Passive observations LAZY-LOAD the workspace: the agent's own I/O should warm
        # a repo even before its first query. Other change events keep the cheap
        # existing()-only path (don't spin up a workspace for arbitrary file churn).
        has_content = passive and isinstance(contents, dict) and any(isinstance(v, str) for v in contents.values())
        ws = await registry.get(cwd) if has_content else registry.existing(cwd)
        if ws is None:
            return JSONResponse({"ok": True, "applied": 0, "note": "workspace not loaded"})
        applied = 0
        for p in body.get("paths") or []:
            rel = os.path.relpath(p, cwd) if os.path.isabs(p) else p
            teed = contents.get(p) if isinstance(contents, dict) else None
            if passive and isinstance(teed, str):
                # Passive (organic) population: tee the file's current bytes into the
                # window (fresh, authoritative) instead of just flagging it stale.
                recorded = ws.observe(rel, teed[:cap])
            elif kind == "edited":
                recorded = ws.note_modified(rel)        # tool edit → next-question hint
            elif kind == "read":
                recorded = False                          # no content / passive off → nothing to track
            elif event == "unlink":
                recorded = ws.mark_deleted(rel)          # gone (deleted / moved away)
            else:
                recorded = ws.invalidate(rel)            # out-of-band modify → stale
            if recorded:
                applied += 1
        return JSONResponse({"ok": True, "applied": applied})

    @mcp.custom_route("/clear", methods=["POST"])
    async def clear(req: Request) -> JSONResponse:
        """Empty the Live Memory: `{"all": true}` wipes every workspace; otherwise
        `{"cwd": ...}` wipes that one. Backs /live-memory-empty — a clean slate."""
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("all"):
            return JSONResponse({"ok": True, "scope": "all", "cleared": registry.clear_all()})
        cwd = body.get("cwd") or os.getcwd()
        cleared = registry.clear(cwd)
        return JSONResponse({"ok": True, "scope": "workspace", "cwd": cwd, "cleared": int(cleared)})

    @mcp.custom_route("/reload", methods=["POST"])
    async def reload(_req: Request) -> JSONResponse:
        """Re-read config (env + config.json) and hot-swap the model/provider."""
        try:
            new_cfg = Config()
            new_llm, new_sum = _build_clients(new_cfg)
            registry.reload(new_cfg, new_llm, new_sum)
            return JSONResponse({"ok": True, "config": new_cfg.to_summary()})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # ── Optional async (fire-and-forget) tool pair — opt-in via LIVE_MEMORY_ASYNC_TOOLS.
    # MCP has no native async tool calls; this is the server-side submit/poll pattern.
    if cfg.async_tools:
        jobs = JobRunner()

        @mcp.tool()
        async def ask_live_memory_submit(question: str, cwd: str, timeout: float) -> str:
            """Submit a Live Memory question to run in the BACKGROUND and return a
            job_id immediately — use this instead of ask_live_memory when the query
            may be slow and you want to keep working, then collect the answer later.

            Returns a job_id. Do other work, then call ask_live_memory_result with
            that job_id to fetch the answer (it reports "[running]" until ready).
            Same answer quality as ask_live_memory; same args (see that tool for
            `cwd` rules — absolute repo root).
            """
            err = _validate(question, cwd)
            if err:
                return err
            ts = _clamp_timeout(timeout)
            job_id = jobs.submit(lambda: _answer(question, cwd, ts))
            return (f'Submitted as job "{job_id}". Continue with other work, then call '
                    f'ask_live_memory_result(job_id="{job_id}") to collect the answer.')

        @mcp.tool()
        async def ask_live_memory_result(job_id: str) -> str:
            """Collect the result of an ask_live_memory_submit job by its job_id.

            Returns the full answer (with metadata) when ready; "[running] …" if it
            is still working (do other work and poll again); or an error if the job
            failed or the id is unknown/expired. A successful/failed result is
            consumed once (collect it again only after a new submit).
            """
            got = jobs.collect(job_id)
            if got is None:
                return f'Error: no such job "{job_id}" (it may have expired, been collected already, or never existed).'
            if got["status"] == "running":
                return f'[running] job "{job_id}" is not finished yet — do other work and call ask_live_memory_result again shortly.'
            if got["status"] == "error":
                return f'Error: the Live Memory job failed: {got["error"]}'
            return str(got["result"])

    return mcp
