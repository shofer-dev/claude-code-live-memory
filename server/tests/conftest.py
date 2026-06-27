"""Shared test fixtures + a scripted fake LLM (duck-types LlmClient)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/

from live_memory.config import Config
from live_memory.models import ChatResult, CostSnapshot
from live_memory.summarizer import Summarizer
from live_memory.workspace import WorkspaceState

SERVER_DIR = str(Path(__file__).resolve().parents[1])


class FakeLlm:
    """Scripted, no-network stand-in for AnthropicClient/OpenAIClient."""

    def __init__(self, scripts: list[ChatResult] | None = None,
                 complete_text: str = "LEDGER: codebase facts.", delay: float = 0.0):
        self.scripts = list(scripts or [])
        self.complete_text = complete_text
        self.delay = delay
        self.chat_calls = 0
        self.complete_calls = 0

    async def chat(self, system_prompt: str, messages: list[dict], tools=None, max_tokens: int = 4096, system_volatile: str = "") -> ChatResult:
        self.chat_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.scripts.pop(0) if self.scripts else ChatResult(answer="(out of script)")

    async def complete(self, system_prompt: str, user_text: str, max_tokens: int = 2048):
        self.complete_calls += 1
        return self.complete_text, CostSnapshot(usd=0.001)


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    """A deterministic Config rooted at a temp data dir, env cleared."""
    for k in list(__import__("os").environ):
        if k.startswith("LIVE_MEMORY_") or k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_MEMORY_PROVIDER", "anthropic")  # avoid subscription auto-detect in tests
    cfg = Config()
    cfg.max_context_tokens = 200_000
    cfg.max_iterations = 6
    return cfg


def make_ws(cfg: Config, llm) -> WorkspaceState:
    return WorkspaceState(SERVER_DIR, cfg, llm, Summarizer(llm))


def far_deadline() -> float:
    return asyncio.get_event_loop().time() + 1000
