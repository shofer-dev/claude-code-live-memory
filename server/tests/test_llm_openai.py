"""OpenAI-compatible adapter: Anthropic→OpenAI message/tool conversion, cost
mapping, and chat() parsing with a mocked HTTP layer (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from live_memory.llm_client import OpenAIClient, _anthropic_msgs_to_openai, _tools_to_openai


def test_msgs_conversion():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"regex": "x"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "found"}]},
    ]
    out = _anthropic_msgs_to_openai("SYS", messages)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "hi"}
    asst = out[2]
    assert asst["role"] == "assistant" and asst["content"] == "let me look"
    assert asst["tool_calls"][0]["function"]["name"] == "Grep"
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "found"}


def test_tools_conversion():
    tools = [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}]
    out = _tools_to_openai(tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"] == {"name": "Read", "description": "d", "parameters": {"type": "object"}}


def _client():
    cfg = SimpleNamespace(base_url="https://api.deepseek.com", api_key="k", model="deepseek-chat")
    return OpenAIClient(cfg)  # type: ignore[arg-type]


def test_cost_uses_cached_tokens():
    c = _client()
    # 1M prompt of which 600k cached → 400k uncached input + cache_read
    cost = c._cost({"prompt_tokens": 1_000_000, "completion_tokens": 0,
                    "prompt_tokens_details": {"cached_tokens": 600_000}})
    # 0.4M*0.27 (input) + 0.6M*0.27*0.10 (cache read)
    assert abs(cost.usd - (0.4 * 0.27 + 0.6 * 0.27 * 0.10)) < 1e-9


@pytest.mark.asyncio
async def test_chat_parses_tool_calls(monkeypatch):
    c = _client()

    async def fake_post(payload):
        # ensure conversion happened: tools present, system first
        assert payload["messages"][0]["role"] == "system"
        return {
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"path":"a.py"}'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }

    monkeypatch.setattr(c, "_post", fake_post)
    res = await c.chat("SYS", [{"role": "user", "content": "q"}], tools=[{"name": "Read", "input_schema": {}}])
    assert res.tool_calls[0].name == "Read"
    assert res.tool_calls[0].arguments == '{"path":"a.py"}'
    assert res.prompt_tokens == 100 and res.completion_tokens == 10


@pytest.mark.asyncio
async def test_chat_plain_answer(monkeypatch):
    c = _client()

    async def fake_post(payload):
        return {"choices": [{"message": {"content": "the answer"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}

    monkeypatch.setattr(c, "_post", fake_post)
    res = await c.chat("SYS", [{"role": "user", "content": "q"}])
    assert res.answer == "the answer" and not res.tool_calls
