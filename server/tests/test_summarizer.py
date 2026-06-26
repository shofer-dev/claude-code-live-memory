"""Summarizer: neutral ledger update via a mock LLM, transcript rendering, fail-soft."""
from __future__ import annotations

import pytest

from conftest import FakeLlm

from live_memory.models import ChatMessage, ToolCall, ToolResult
from live_memory.summarizer import Summarizer, render_transcript


def test_render_transcript_shapes():
    msgs = [
        ChatMessage("user", "what is X?"),
        ChatMessage("assistant", "", tool_calls=[ToolCall("t1", "Grep", '{"regex":"X"}')]),
        ChatMessage("user", "", tool_results=[ToolResult("t1", "found in a.py")]),
        ChatMessage("assistant", "X lives in a.py"),
    ]
    t = render_transcript(msgs)
    assert "what is X?" in t and "Grep" in t and "found in a.py" in t and "X lives in a.py" in t


@pytest.mark.asyncio
async def test_summarize_updates_ledger():
    llm = FakeLlm(complete_text="LEDGER v2: auth lives in src/auth.")
    s = Summarizer(llm)
    new_ledger, cost = await s.summarize("LEDGER v1", [ChatMessage("user", "q"), ChatMessage("assistant", "a")])
    assert new_ledger == "LEDGER v2: auth lives in src/auth."
    assert llm.complete_calls == 1


@pytest.mark.asyncio
async def test_summarize_empty_batch_is_noop():
    llm = FakeLlm()
    s = Summarizer(llm)
    new_ledger, _ = await s.summarize("keep me", [])
    assert new_ledger == "keep me" and llm.complete_calls == 0


@pytest.mark.asyncio
async def test_summarize_fail_soft_keeps_existing():
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("provider down")

    s = Summarizer(Boom())
    new_ledger, _ = await s.summarize("existing", [ChatMessage("user", "q"), ChatMessage("assistant", "a")])
    assert new_ledger == "existing"  # dropped the batch but kept the ledger
