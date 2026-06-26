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


def _build_system(ws: "WorkspaceState", window: "ContextWindow") -> str:
    ledger = window.knowledge_ledger.strip() or empty_ledger_text()
    prompt = (
        LIVE_MEMORY_SYSTEM_PROMPT
        .replace("{knowledge_ledger}", ledger)
        .replace("{directory_tree}", ws.directory_tree_block)
    )
    # Files Live Memory has read into its knowledge. The content hash is internal
    # (staleness tracking) and is NOT shown to the model. A changed-but-not-yet-
    # re-read file is flagged informationally (the model decides whether to act).
    manifest = []
    for fc in window.file_contexts:
        if fc.deleted:
            manifest.append(f"[Previously read: {fc.path} — DELETED or moved/renamed; it no longer exists at this path, so your knowledge of it is obsolete. If you still need it, locate its new path (Glob/Grep/find_paths) or report that it is gone.]")
        elif fc.content_hash:
            manifest.append(f"[Read into your knowledge: {fc.path} (~{fc.token_estimate} tokens)]")
        else:
            manifest.append(f"[Read into your knowledge: {fc.path} — CHANGED since you read it; your knowledge of it may be out of date]")
    if manifest:
        prompt += "\n\n" + "\n".join(manifest)
    return prompt


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


async def _maybe_compact(ws: "WorkspaceState", window: "ContextWindow") -> None:
    # Compact down to the SOFT threshold (compaction_threshold, default 0.85) once
    # the window exceeds it — not the hard max. Tier 1: evict re-readable file
    # contexts (LRU). Tier 2: if that isn't enough, neutrally summarize oldest Q&A.
    target = int(window.max_context_tokens * window.fill_threshold)
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
    system = _build_system(ws, window)
    conversation = _history_to_messages(window.messages)
    hints = _build_hints(recently, max(0.0, deadline - loop.time()))
    conversation.append({"role": "user", "content": f"{question}\n\n{hints}" if hints else question})

    answer = ""
    prompt_tok = completion_tok = 0
    tool_calls_total = files_read_total = 0
    cost = CostSnapshot()
    timed_out = False

    for _ in range(ws.cfg.max_iterations):
        if loop.time() >= deadline:
            timed_out = True
            if not answer:
                answer = "(I reached the time budget before producing a final answer. Ask again with a larger timeout or a narrower question.)"
            break

        result = await ws.llm.chat(system, conversation, tools=TOOL_SCHEMAS)
        prompt_tok += result.prompt_tokens
        completion_tok += result.completion_tokens
        cost.add(result.cost)

        if not result.tool_calls:
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
