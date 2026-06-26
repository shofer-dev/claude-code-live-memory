"""End-to-end integration: REAL streamable-http MCP transport + REAL server
subprocess. Only the LLM endpoint is mocked (a local OpenAI-compatible stub), so
there's no network and no token cost — but everything else is the real thing:
the FastMCP server, tool registration, the queue + process_question, the
OpenAIClient HTTP path, the /health//stats//notify routes, the metadata trailer,
the async submit/poll pair, and the lazy keep-warm start (the wiring a unit test
with a mocked LlmClient can't exercise — and where the lifespan bug hid).

Run just these:  pytest -m integration       Skip them:  pytest -m 'not integration'
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER_DIR = str(Path(__file__).resolve().parents[1])
pytestmark = pytest.mark.integration


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


class _MockLLM(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /chat/completions stub — always answers."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "MOCK ANSWER"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:  # silence the stub
        pass


@pytest.fixture
def mock_llm():
    srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), _MockLLM)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@pytest.fixture
def server(mock_llm, tmp_path):
    """The real `python -m live_memory`, pointed at the mock LLM, on a free port."""
    port = _free_port()
    repo = tmp_path / "repo"           # a tiny git repo → small directory tree
    (repo / ".git").mkdir(parents=True)
    (repo / "hello.txt").write_text("hi")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LIVE_MEMORY_") and k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")}
    env.update({
        "LIVE_MEMORY_PROVIDER": "openai", "LIVE_MEMORY_BASE_URL": mock_llm,
        "LIVE_MEMORY_API_KEY": "test-key", "LIVE_MEMORY_MODEL": "test-model",
        "LIVE_MEMORY_HOST": "127.0.0.1", "LIVE_MEMORY_PORT": str(port),
        "LIVE_MEMORY_DATA_DIR": str(tmp_path / "data"),
        "LIVE_MEMORY_KEEP_WARM": "true", "LIVE_MEMORY_ASYNC_TOOLS": "true",
    })
    proc = subprocess.Popen([sys.executable, "-m", "live_memory"], cwd=SERVER_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    logs: list[str] = []
    Thread(target=lambda: logs.extend(iter(proc.stdout.readline, "")), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):  # wait for readiness
        try:
            if _get(f"{base}/health")["status"] == "ok":
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("server did not become healthy")
    try:
        yield base, str(repo), logs
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_mcp_round_trip_and_async_and_keepwarm(server):
    base, cwd, logs = server
    async with streamable_http_client(f"{base}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            assert "ask_live_memory" in names
            assert "ask_live_memory_submit" in names and "ask_live_memory_result" in names  # async on

            # sync round-trip: real transport → server → mock LLM → answer + metadata trailer
            res = await s.call_tool("ask_live_memory", {"question": "hi", "cwd": cwd, "timeout": 30})
            text = res.content[0].text
            assert "MOCK ANSWER" in text
            assert "[live-memory]" in text and "model=test-model" in text

            # async submit/poll round-trip
            sub = (await s.call_tool("ask_live_memory_submit", {"question": "q", "cwd": cwd, "timeout": 30})).content[0].text
            job_id = re.search(r'job "([^"]+)"', sub).group(1)
            for _ in range(50):
                got = (await s.call_tool("ask_live_memory_result", {"job_id": job_id})).content[0].text
                if "[running]" not in got:
                    break
                time.sleep(0.1)
            assert "MOCK ANSWER" in got

            # relative cwd is rejected by the real tool
            bad = (await s.call_tool("ask_live_memory", {"question": "x", "cwd": "rel/path", "timeout": 30})).content[0].text
            assert "absolute path" in bad

    # /stats over real HTTP reflects the answered questions + config
    stats = _get(f"{base}/stats?cwd={quote(cwd)}")
    assert stats["model"] == "test-model" and stats["questionsAnswered"] >= 2
    assert stats["concurrency"] == "parallel"

    # the keep-warm loop actually started under the real transport (the lifespan bug)
    for _ in range(50):
        if any("Keep-warm loop started" in line for line in logs):
            break
        time.sleep(0.1)
    assert any("Keep-warm loop started" in line for line in logs), "keep-warm loop did not start"
