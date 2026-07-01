"""The agent loop — process one question against a workspace's Window B.

The window persists only the Q&A pairs (user question, assistant answer); each
question's tool round-trips live in a transient conversation array and are
discarded once the answer is produced. Compaction (file-context eviction +
neutral summarization) happens once on entry; file-context eviction also runs
in-loop. The soft deadline bounds the loop and yields a best-effort answer.
"""
from __future__ import annotations
from typing import Any

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .models import ChatMessage, CostSnapshot, FileContext, QuestionResult, estimate_tokens, now_ms
from .prompts import LIVE_MEMORY_SYSTEM_PROMPT, empty_ledger_text
from .tool_executor import FILE_READING_TOOLS, TOOL_SCHEMAS

if TYPE_CHECKING:
    from .context_window import ContextWindow
    from .workspace import WorkspaceState


def _safe_json(s: str) -> dict[str, Any]:
    try:
        v = json.loads(s) if s and s.strip() else {}
        return v if isinstance(v, dict) else {"_raw": s}
    except json.JSONDecodeError:
        return {"_raw": s}


def _build_system(ws: "WorkspaceState", window: "ContextWindow") -> tuple[str, str]:
    """Return (stable, volatile). STABLE = instructions + directory tree — byte-
    identical across questions, so the Anthropic cache breakpoint on it is written
    once and read cross-question (the tree dominates per-call input). VOLATILE =
    knowledge ledger + file manifest — changes per question, so it is kept OUT of
    the cached prefix (it follows the breakpoint) and never busts the tree cache."""
    stable = LIVE_MEMORY_SYSTEM_PROMPT.replace("{directory_tree}", ws.directory_tree_block)

    ledger = window.knowledge_ledger.strip() or empty_ledger_text()
    sections = [f"## Accumulated knowledge (durable facts distilled from earlier questions)\n{ledger}"]
    # Files Live Memory has read into its knowledge. The content hash is internal
    # (staleness tracking) and is NOT shown to the model. A changed-but-not-yet-
    # re-read file is flagged informationally (the model decides whether to act).
    # Observed entries (passive ingestion, FUTURE_DIRECTIONS §1) carry the file's
    # current bytes teed from the building agent's session → rendered inline so the
    # model answers without a re-read; the rest stay one-line manifest references.
    manifest: list[str] = []
    observed: list[str] = []
    for fc in window.file_contexts:
        if fc.deleted:
            manifest.append(f"[Previously read: {fc.path} — DELETED or moved/renamed; it no longer exists at this path, so your knowledge of it is obsolete. If you still need it, locate its new path (Glob/Grep/find_paths) or report that it is gone.]")
        elif fc.has_content:
            observed.append(f"#### {fc.path}\n{fc.content}")
        elif fc.content_hash:
            manifest.append(f"[Read into your knowledge: {fc.path} (~{fc.token_estimate} tokens)]")
        else:
            manifest.append(f"[Read into your knowledge: {fc.path} — CHANGED since you read it; your knowledge of it may be out of date]")
    if observed:
        sections.append(
            "## Files observed live from the building agent's session\n"
            "These are current, authoritative file contents teed from the agent's own "
            "Read/Edit/Write — treat them as up to date and answer from them without "
            "re-reading.\n\n" + "\n\n".join(observed)
        )
    if manifest:
        sections.append("\n".join(manifest))
    return stable, "\n\n".join(sections)


def _build_hints(recently_modified: list[str], remaining_s: float) -> str:
    parts: list[str] = []
    if recently_modified:
        parts.append(
            "[Heads-up: these files you've previously read have changed since you read them: "
            f"{', '.join(recently_modified)}. Your knowledge of them may be out of date — decide "
            f"for yourself whether any of this is relevant to the question.]"
        )
    parts.append(
        f"[Soft budget for this question (advisory, not enforced): aim to answer within "
        f"~{remaining_s:.0f}s of wall time, using fewer tool round-trips when possible, and keep the "
        f"answer concise. If the question genuinely requires more, exceed it rather than answer wrongly.]"
    )
    return "\n\n".join(parts)


def _history_to_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": m.content} for m in messages if m.role in ("user", "assistant")]


async def _distill_observations(ws: "WorkspaceState", window: "ContextWindow", target: int) -> None:
    """Compaction tier 0 (passive ingestion, FUTURE_DIRECTIONS §1): the raw bytes
    teed in by `observe()` are the bulk of any window bloat. Distill the least-
    recently-used observations into the durable knowledge ledger (one batched
    neutral summary), then drop their raw bytes — leaving manifest-only entries
    that stay re-readable on demand. This is the retention knob: raw while it fits,
    summarized into the lean prefix under pressure."""
    pending: list[tuple[str, str]] = []
    for fc in window.content_contexts_lru():
        if window.estimated_token_count() <= target:
            break
        pending.append((fc.path, fc.content))
        window.clear_content(fc.path)  # collapses its token weight → frees budget now
    if not pending:
        return
    observed = [ChatMessage(role="user", content=f"[Observed file: {p}]\n{c}") for p, c in pending]
    new_ledger, cost = await ws.summarizer.summarize(window.knowledge_ledger, observed)
    window.knowledge_ledger = new_ledger
    ws.add_cost(cost)
    ws.last_compaction = now_ms()
    ws.summaries_written += 1


async def _maybe_compact(ws: "WorkspaceState", window: "ContextWindow") -> None:
    # Hysteresis: TRIGGER once over the high watermark (compaction_threshold, 0.85),
    # then compact all the way down to the low watermark (compaction_floor, 0.6) —
    # NOT back to the trigger. The headroom keeps compaction rare/batched so the next
    # questions hit a stable, cached window instead of re-compacting every turn (which
    # busts the prompt cache — the failure mode passive ingestion otherwise hits when
    # observations overflow). Tier 0: distill+shed observed file bytes. Tier 1: evict
    # re-readable file contexts (LRU). Tier 2: neutrally summarize oldest Q&A.
    if window.estimated_token_count() <= int(window.max_context_tokens * window.fill_threshold):
        return
    target = int(window.max_context_tokens * window.compaction_floor)
    await _distill_observations(ws, window, target)
    if window.estimated_token_count() <= target:
        return
    still_over = window.enforce_limit(target)
    if not still_over:
        return
    dropped: list[ChatMessage] = []
    while window.estimated_token_count() > target and window.message_count() > 2:
        pair = window.pop_oldest_pair()
        if not pair:
            break
        dropped.extend(pair)
    if dropped:
        new_ledger, cost = await ws.summarizer.summarize(window.knowledge_ledger, dropped)
        window.knowledge_ledger = new_ledger
        ws.add_cost(cost)
        ws.last_compaction = now_ms()
        ws.summaries_written += 1


def _track_file_read(ws: "WorkspaceState", window: "ContextWindow", args_json: str) -> None:
    """After a successful content-reading tool (FILE_READING_TOOLS) pulled a file
    into context, record a file-context manifest entry so the loaded-files set
    (for change notifications) and staleness tracking cover it. Expects a
    `file_path` (or legacy `path`) arg — the convention for content-reading tools."""
    try:
        args = _safe_json(args_json)
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str) or not path:
            return
        full = (Path(ws.cwd) / path).resolve()
        if Path(ws.cwd).resolve() not in full.parents and full != Path(ws.cwd).resolve():
            return
        content = full.read_text(encoding="utf-8", errors="replace")
        window.upsert_file_context(FileContext(
            path=path,
            content_hash=hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
            token_estimate=estimate_tokens(content),
            last_referenced_at=now_ms(),
        ))
    except OSError:
        pass


async def process_question(ws: "WorkspaceState", question: str, deadline: float) -> QuestionResult:
    loop = asyncio.get_running_loop()
    start = time.time()

    # Take the window this question runs against — a fork (parallel) or the live
    # window (serial) — atomically with draining the change-notification hints.
    async with ws.commit_lock:
        window = ws.fork_window()
        recently = ws.drain_recently_modified()

    await _maybe_compact(ws, window)
    system_stable, system_volatile = _build_system(ws, window)
    conversation = _history_to_messages(window.messages)
    hints = _build_hints(recently, max(0.0, deadline - loop.time()))
    conversation.append({"role": "user", "content": f"{question}\n\n{hints}" if hints else question})

    answer = ""
    prompt_tok = completion_tok = 0
    tool_calls_total = files_read_total = 0
    cost = CostSnapshot()
    timed_out = False
    forced_explore = False  # cold-start guard: force one exploration before answering

    for _ in range(ws.cfg.max_iterations):
        if loop.time() >= deadline:
            timed_out = True
            if not answer:
                answer = "(I reached the time budget before producing a final answer. Ask again with a larger timeout or a narrower question.)"
            break

        result = await ws.llm.chat(system_stable, conversation, tools=TOOL_SCHEMAS, system_volatile=system_volatile)
        prompt_tok += result.prompt_tokens
        completion_tok += result.completion_tokens
        cost.add(result.cost)

        if not result.tool_calls:
            # Cold-start grounding guard: the memory has no basis to answer (no observed file
            # content, ~empty ledger) and the model answered WITHOUT consulting the code —
            # make it explore instead of guessing from priors. Fires at most once per
            # question; warm memory (content in-window) and a non-empty ledger are untouched.
            if (ws.cfg.force_explore_when_cold and not forced_explore
                    and tool_calls_total == 0 and window.is_cold() and loop.time() < deadline):
                forced_explore = True
                if result.answer:
                    conversation.append({"role": "assistant", "content": result.answer})
                conversation.append({"role": "user", "content": (
                    "[You have no stored knowledge of this codebase covering that question, and you "
                    "answered without consulting the code. Do NOT answer from general/prior knowledge "
                    "or by guessing from file/symbol names. Use Grep/Glob/find_paths to locate the "
                    "relevant file(s), Read them, then answer grounded in what you actually read.]")})
                continue
            answer = result.answer
            break
        tool_calls_total += len(result.tool_calls)

        assistant_blocks: list[dict[str, Any]] = []
        if result.answer:
            assistant_blocks.append({"type": "text", "text": result.answer})
        for tc in result.tool_calls:
            assistant_blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": _safe_json(tc.arguments)})
        conversation.append({"role": "assistant", "content": assistant_blocks})

        tool_result_blocks: list[dict[str, Any]] = []
        for tc in result.tool_calls:
            ex = await ws.executor.execute(tc.name, tc.arguments)
            tool_result_blocks.append({
                "type": "tool_result", "tool_use_id": tc.id,
                "content": ex.content, "is_error": ex.is_error,
            })
            if tc.name in FILE_READING_TOOLS and not ex.is_error:
                _track_file_read(ws, window, tc.arguments)
                files_read_total += 1
        conversation.append({"role": "user", "content": tool_result_blocks})

        window.enforce_limit()  # shed file contexts if the manifest pushed us over
    else:
        if not answer:
            answer = "(Reached the maximum tool iterations without a final answer.)"

    # Persist only the Q&A pair into the window (tool round-trips are transient).
    window.append_message(ChatMessage(role="user", content=question))
    window.append_message(ChatMessage(role="assistant", content=answer))
    window.enforce_limit()

    # Commit this fork back (serial: in place; parallel: only if it explored more),
    # then count the question and account its cost regardless of whether it won.
    async with ws.commit_lock:
        ws.commit_window(window)
        ws.questions_answered += 1
        ws.add_cost(cost)
        ws.last_query_at = ws.last_touch_at = time.time()  # for keep-warm idle tracking
    await ws.persist()

    return QuestionResult(
        answer=answer,
        tokens_used=prompt_tok + completion_tok,
        context_usage=window.get_usage(),
        cost_snapshot=cost,
        timed_out=timed_out,
        duration_ms=int((time.time() - start) * 1000),
        prompt_tokens=prompt_tok,
        completion_tokens=completion_tok,
        tool_calls=tool_calls_total,
        files_read=files_read_total,
    )
