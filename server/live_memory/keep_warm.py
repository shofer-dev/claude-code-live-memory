"""KV / prompt-cache keep-warm loop (opt-out via LIVE_MEMORY_KEEP_WARM).

A provider's prefix cache (Anthropic explicit `cache_control`, OpenAI/DeepSeek
implicit prefix caching) expires after some idle TTL; once cold, the next real
query re-reads the whole prefix at full rate (slow + expensive). To avoid that,
this loop periodically issues a **minimal** request (the same system + history
prefix, `max_tokens=1`, output discarded) against each recently-active workspace,
re-reading the cached prefix and refreshing its TTL. The interval comes from
provider knowledge (config); a workspace idle longer than `max_idle` is left to
go cold (don't keep abandoned ones hot forever).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .constants import KEEP_WARM_PING_MAX_TOKENS
from .manager import _build_system, _history_to_messages
from .tool_executor import TOOL_SCHEMAS

if TYPE_CHECKING:
    from .config import Config
    from .workspace import WorkspaceRegistry, WorkspaceState

log = logging.getLogger("live_memory.keep_warm")


def _eligible(ws: "WorkspaceState", now: float, interval: float, max_idle: float) -> bool:
    """Warm only a workspace that has content, isn't mid-query, has had a real
    query this process within max_idle, and whose prefix was last sent ≥ interval
    ago (so it's about to/just went cold)."""
    if ws.window.message_count() == 0 and not ws.window.knowledge_ledger.strip():
        return False
    if ws.queue.depth > 0:
        return False  # an in-flight query already keeps the cache warm
    if ws.last_query_at <= 0 or (now - ws.last_query_at) > max_idle:
        return False  # never queried here, or abandoned
    return (now - ws.last_touch_at) >= interval


async def warm_one(ws: "WorkspaceState", now: float) -> None:
    """Send the workspace's current prefix with a 1-token completion; discard it."""
    # Warm the SAME cached prefix real queries use: identical stable system + tools
    # (tools are part of the cache key), so the breakpoint stays hot across idle gaps.
    system_stable, system_volatile = _build_system(ws, ws.window)
    conversation = _history_to_messages(ws.window.messages)
    conversation.append({"role": "user", "content": "(keep-warm ping — reply with a single token)"})
    result = await ws.llm.chat(system_stable, conversation, tools=TOOL_SCHEMAS, max_tokens=KEEP_WARM_PING_MAX_TOKENS, system_volatile=system_volatile)
    ws.add_cost(result.cost)          # the warm read is real (cheap) spend
    ws.last_touch_at = now
    ws.keep_warms += 1


async def keep_warm_loop(registry: "WorkspaceRegistry", cfg: "Config") -> None:
    interval = max(30.0, cfg.keep_warm_interval_s)
    log.info("Keep-warm loop started (interval=%.0fs, max_idle=%.0fs).", interval, cfg.keep_warm_max_idle_s)
    try:
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            for ws in registry.all():
                if _eligible(ws, now, interval, cfg.keep_warm_max_idle_s):
                    try:
                        await warm_one(ws, now)
                        log.debug("kept warm: %s", ws.cwd)
                    except Exception as e:  # noqa: BLE001 — best-effort; never kill the loop
                        log.debug("keep-warm failed for %s: %s", ws.cwd, e)
    except asyncio.CancelledError:
        log.info("Keep-warm loop stopped.")
        raise
