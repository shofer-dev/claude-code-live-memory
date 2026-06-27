"""LLM clients for Window B — provider-pluggable.

Two adapters cover essentially every model:
  - `AnthropicClient`  — Anthropic Messages API (+ Bedrock/Vertex/gateways).
    Expresses explicit `cache_control` prompt-caching breakpoints. Also supports
    a Claude Code subscription **OAuth** bearer token (opt-in, gray area).
  - `OpenAIClient`     — any OpenAI-compatible `/chat/completions` endpoint
    (OpenAI, **DeepSeek**, local models, gateways). Implicit prefix caching.

Both accept the same neutral conversation (the Anthropic-shaped message dicts the
manager builds) and return a `ChatResult`, so the agent loop is provider-agnostic.
Abort is via asyncio task cancellation.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from . import pricing
from .config import Config
from .models import ChatResult, CostSnapshot, ToolCall

OAUTH_BETA = "oauth-2025-04-20"


class LlmClient(Protocol):
    async def chat(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   max_tokens: int = 4096, system_volatile: str = "") -> ChatResult: ...
    async def complete(self, system_prompt: str, user_text: str, max_tokens: int = 2048) -> tuple[str, CostSnapshot]: ...


def _with_cache(block: dict[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": {"type": "ephemeral"}}


# ─────────────────────────── Anthropic ───────────────────────────
class AnthropicClient:
    def __init__(self, cfg: Config, oauth_cred: Any = None) -> None:
        from anthropic import AsyncAnthropic
        self.cfg = cfg
        self._AsyncAnthropic = AsyncAnthropic
        self._oauth = oauth_cred  # OAuthCredential with async token(); None → use API key
        self._cur_token: str | None = None
        self._client: Any = None
        if oauth_cred is None:
            self._client = AsyncAnthropic(base_url=cfg.base_url, api_key=cfg.api_key or "missing")

    async def _ac(self) -> Any:
        """Return an AsyncAnthropic bound to a current token (rebuilds on refresh)."""
        if self._oauth is None:
            return self._client
        tok = await self._oauth.token()
        if tok != self._cur_token or self._client is None:
            self._cur_token = tok
            self._client = self._AsyncAnthropic(
                base_url=self.cfg.base_url, auth_token=tok,
                default_headers={"anthropic-beta": OAUTH_BETA},
            )
        return self._client

    async def chat(self, system_prompt: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None, max_tokens: int = 4096,
                   system_volatile: str = "") -> ChatResult:
        client = await self._ac()
        # Cache breakpoint on the STABLE prefix (instructions + directory tree + tools)
        # so it is written once and read cross-question. The VOLATILE block (ledger +
        # file manifest) follows the breakpoint uncached, so it never busts the prefix.
        system = [_with_cache({"type": "text", "text": system_prompt})]
        if system_volatile:
            system.append({"type": "text", "text": system_volatile})
        msgs = [dict(m) for m in messages]
        if len(msgs) >= 2:  # cache the stable history tail (breakpoint 3)
            tail = msgs[-2]
            c = tail.get("content")
            if isinstance(c, list) and c:
                c = list(c); c[-1] = _with_cache(dict(c[-1])); tail["content"] = c
            elif isinstance(c, str):
                tail["content"] = [_with_cache({"type": "text", "text": c})]
        resp = await client.messages.create(
            model=self.cfg.model, max_tokens=max_tokens, system=system, messages=msgs, tools=tools or [],
        )
        answer: list[str] = []
        tool_calls: list[ToolCall] = []
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                answer.append(b.text)
            elif getattr(b, "type", None) == "tool_use":
                tool_calls.append(ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input or {})))
        u = resp.usage
        cr, cw = getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0
        it, ot = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
        cost = pricing.estimate_cost(self.cfg.model, input_tokens=it, output_tokens=ot,
                                     cache_read_tokens=cr, cache_write_tokens=cw)
        return ChatResult(answer="".join(answer), tool_calls=tool_calls,
                          prompt_tokens=it + cr + cw, completion_tokens=ot,
                          cache_read_tokens=cr, cache_write_tokens=cw, cost=cost)

    async def complete(self, system_prompt: str, user_text: str, max_tokens: int = 2048) -> tuple[str, CostSnapshot]:
        client = await self._ac()
        resp = await client.messages.create(
            model=self.cfg.model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system_prompt}],
            messages=[{"role": "user", "content": user_text}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        u = resp.usage
        cost = pricing.estimate_cost(self.cfg.model,
                                     input_tokens=getattr(u, "input_tokens", 0) or 0,
                                     output_tokens=getattr(u, "output_tokens", 0) or 0,
                                     cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        return text, cost


# ─────────────────────────── OpenAI-compatible (DeepSeek/OpenAI/gateways) ───────────────────────────
def _anthropic_msgs_to_openai(system_prompt: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for b in content:
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b["id"], "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                    })
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif role == "user":  # tool_result blocks → one OpenAI `tool` message each
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b.get("content", "")})
    return out


def _tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                          "parameters": t.get("input_schema", {})}}
        for t in (tools or [])
    ]


class OpenAIClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key or 'missing'}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(600.0, connect=15.0),
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._http.post("/chat/completions", json=payload)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    def _cost(self, usage: dict[str, Any]) -> CostSnapshot:
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) \
            or usage.get("prompt_cache_hit_tokens", 0) or 0
        prompt = usage.get("prompt_tokens", 0) or 0
        return pricing.estimate_cost(self.cfg.model, input_tokens=max(0, prompt - cached),
                                     output_tokens=usage.get("completion_tokens", 0) or 0,
                                     cache_read_tokens=cached)

    async def chat(self, system_prompt: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None, max_tokens: int = 4096,
                   system_volatile: str = "") -> ChatResult:
        full_system = f"{system_prompt}\n\n{system_volatile}" if system_volatile else system_prompt
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": _anthropic_msgs_to_openai(full_system, messages),
            "max_tokens": max_tokens,
        }
        ot = _tools_to_openai(tools)
        if ot:
            payload["tools"] = ot
        data = await self._post(payload)
        msg = (data.get("choices") or [{}])[0].get("message", {})
        tool_calls = [
            ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], arguments=tc["function"].get("arguments", "{}"))
            for tc in (msg.get("tool_calls") or [])
        ]
        usage = data.get("usage", {})
        cost = self._cost(usage)
        return ChatResult(answer=msg.get("content") or "", tool_calls=tool_calls,
                          prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                          completion_tokens=usage.get("completion_tokens", 0) or 0,
                          cache_read_tokens=cost.cache_read_tokens, cost=cost)

    async def complete(self, system_prompt: str, user_text: str, max_tokens: int = 2048) -> tuple[str, CostSnapshot]:
        data = await self._post({
            "model": self.cfg.model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}],
        })
        msg = (data.get("choices") or [{}])[0].get("message", {})
        return (msg.get("content") or ""), self._cost(data.get("usage", {}))


def make_client(cfg: Config, oauth_cred: Any = None) -> LlmClient:
    if cfg.provider == "openai":
        return OpenAIClient(cfg)
    return AnthropicClient(cfg, oauth_cred=oauth_cred)
