"""I/O units: conversation store (SHA-256 validation), tool executor (path jail),
question queue (serialization + bounds + timeout)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from live_memory.conversation_store import ConversationStore
from live_memory.models import (
    ChatMessage, ContextUsage, CostSnapshot, FileContext, QuestionResult, estimate_tokens,
)
from live_memory.question_queue import QuestionQueue, QueueFull
from live_memory.tool_executor import ToolExecutor

SERVER_DIR = str(Path(__file__).resolve().parents[1])


# ── conversation store ──
def test_store_roundtrip_and_sha256_validation(tmp_path):
    (tmp_path / "real.txt").write_text("hello world")
    good = hashlib.sha256(b"hello world").hexdigest()
    store = ConversationStore(str(tmp_path), tmp_path / "snap.json")
    store.save({
        "messages": [ChatMessage("user", "q1"), ChatMessage("assistant", "a1")],
        "file_contexts": [
            FileContext("real.txt", good, estimate_tokens("hello world")),  # valid → kept
            FileContext("gone.txt", good, 5),                                # missing → dropped
            FileContext("real.txt", "WRONGHASH", 5),                         # stale → dropped
        ],
        "knowledge_ledger": "L", "summaries_written": 2, "questions_answered": 1,
        "cost_usd": 0.5, "created_at": 123,
    })
    loaded = store.load()
    assert [m.content for m in loaded["messages"]] == ["q1", "a1"]
    assert [fc.path for fc in loaded["file_contexts"]] == ["real.txt"]
    assert loaded["knowledge_ledger"] == "L" and loaded["cost_usd"] == 0.5


def test_store_missing_and_corrupt_return_empty(tmp_path):
    store = ConversationStore(str(tmp_path), tmp_path / "nope.json")
    assert store.load()["messages"] == []
    (tmp_path / "bad.json").write_text("{not json")
    assert ConversationStore(str(tmp_path), tmp_path / "bad.json").load()["messages"] == []


# ── tool executor ──
@pytest.mark.asyncio
async def test_read_file_and_path_jail():
    ex = ToolExecutor(SERVER_DIR)
    r = await ex.execute("Read", '{"file_path": "live_memory/models.py", "limit": 2}')
    assert not r.is_error and "models.py (lines 1-2 of" in r.content
    bad = await ex.execute("Read", '{"file_path": "../../../../etc/passwd"}')
    assert bad.is_error and "outside the workspace" in bad.content


@pytest.mark.asyncio
async def test_grep_output_modes():
    ex = ToolExecutor(SERVER_DIR)
    content = await ex.execute("Grep", '{"pattern": "class ToolExecutor", "glob": "*.py"}')
    assert not content.is_error and "tool_executor.py:" in content.content  # file:line:text
    files = await ex.execute("Grep", '{"pattern": "class ToolExecutor", "output_mode": "files_with_matches"}')
    assert "tool_executor.py" in files.content and ":class" not in files.content  # paths only
    # Build the needle by concatenation so the contiguous literal is absent from
    # the repo (rg searches the whole workspace, including this test file).
    needle = "zzz" + "AbsentSymbol" + "qqq"
    none = await ex.execute("Grep", json.dumps({"pattern": needle}))
    assert not none.is_error and "No matches found." in none.content


@pytest.mark.asyncio
async def test_glob_unknown_tool_and_bad_json():
    ex = ToolExecutor(SERVER_DIR)
    f = await ex.execute("Glob", '{"pattern": "live_memory/*.py"}')
    assert "server.py" in f.content
    assert (await ex.execute("delete_all", "{}")).is_error  # pruned/unknown tool
    assert (await ex.execute("Read", "{not json")).is_error


@pytest.mark.asyncio
async def test_find_paths_type_name_and_depth():
    ex = ToolExecutor(SERVER_DIR)
    # type=dir surfaces directories (trailing /); the package dir is at depth 1
    dirs = await ex.execute("find_paths", '{"type": "dir", "max_depth": 1}')
    assert "live_memory/" in dirs.content and "live_memory/models.py" not in dirs.content
    # name glob restricts to matching files
    py = await ex.execute("find_paths", '{"name": "models.py", "type": "file"}')
    assert "live_memory/models.py" in py.content and "server.py" not in py.content
    # max_depth=1 from root never reaches a nested file
    shallow = await ex.execute("find_paths", '{"type": "file", "max_depth": 1}')
    assert "live_memory/models.py" not in shallow.content
    # skip-dirs are pruned
    assert "__pycache__" not in (await ex.execute("find_paths", "{}")).content


@pytest.mark.asyncio
async def test_git_search_grep_path_and_no_match():
    ex = ToolExecutor(SERVER_DIR)
    # absurd grep term → deterministic empty result (build needle so it isn't in-repo)
    needle = "zzz" + "NoSuchCommit" + "qqq"
    none = await ex.execute("git_search", json.dumps({"query": needle}))
    assert not none.is_error and "No matching commits." in none.content
    # path scope resolves and runs without error (history content is non-deterministic)
    scoped = await ex.execute("git_search", '{"path": "live_memory", "max_results": 3}')
    assert not scoped.is_error
    # path jail: escaping the workspace is rejected
    assert (await ex.execute("git_search", '{"path": "../../../../etc"}')).is_error


# ── logging ──
def test_configure_logging_optional_rotating_file(tmp_path, monkeypatch):
    import logging
    from live_memory.logging_setup import configure_logging
    logf = tmp_path / "sub" / "live-memory.log"
    monkeypatch.setenv("LIVE_MEMORY_LOG_FILE", str(logf))
    monkeypatch.setenv("LIVE_MEMORY_LOG_LEVEL", "DEBUG")
    log = logging.getLogger("test_lm_logging_isolated")
    log.handlers.clear()
    log.propagate = False
    try:
        configure_logging(log)  # isolated logger → doesn't pollute the root/other tests
        assert log.level == logging.DEBUG
        log.info("hello-file-and-journald")
        for h in log.handlers:
            h.flush()
        assert logf.exists() and "hello-file-and-journald" in logf.read_text()
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)


# ── async job runner (fire-and-forget submit/poll) ──
@pytest.mark.asyncio
async def test_job_runner_submit_running_then_collect_once():
    from live_memory.async_jobs import JobRunner
    jr = JobRunner()

    async def work():
        await asyncio.sleep(0.02)
        return "the answer"

    jid = jr.submit(work)
    assert jr.collect(jid) == {"status": "running"}      # immediately: not done
    await asyncio.sleep(0.05)
    got = jr.collect(jid)
    assert got["status"] == "done" and got["result"] == "the answer"
    assert jr.collect(jid) is None                        # one-shot: consumed


@pytest.mark.asyncio
async def test_job_runner_captures_error():
    from live_memory.async_jobs import JobRunner
    jr = JobRunner()

    async def boom():
        raise RuntimeError("kaboom")

    jid = jr.submit(boom)
    await asyncio.sleep(0.02)
    got = jr.collect(jid)
    assert got["status"] == "error" and "kaboom" in got["error"]
    assert jr.collect("never-existed") is None


# ── question queue ──
def _qr() -> QuestionResult:
    return QuestionResult("ok", 0, ContextUsage(0, 100, 0, 0, 0), CostSnapshot())


@pytest.mark.asyncio
async def test_queue_serializes_fifo():
    q = QuestionQueue(max_size=4)
    order: list[str] = []

    async def proc(question, cwd, deadline):
        order.append(f"s{question}")
        await asyncio.sleep(0.02)
        order.append(f"e{question}")
        return _qr()

    res = await asyncio.gather(q.submit("A", "c", 5, proc), q.submit("B", "c", 5, proc))
    assert len(res) == 2
    assert order == ["sA", "eA", "sB", "eB"]  # B never starts before A ends


@pytest.mark.asyncio
async def test_queue_parallel_admits_concurrently():
    q = QuestionQueue(max_size=4, max_parallel=2)
    order: list[str] = []

    async def proc(question, cwd, deadline):
        order.append(f"s{question}")
        await asyncio.sleep(0.02)
        order.append(f"e{question}")
        return _qr()

    await asyncio.gather(q.submit("A", "c", 5, proc), q.submit("B", "c", 5, proc))
    assert order[:2] in (["sA", "sB"], ["sB", "sA"])  # both START before either ENDS
    assert set(order) == {"sA", "sB", "eA", "eB"}


@pytest.mark.asyncio
async def test_queue_full_raises():
    q = QuestionQueue(max_size=1)

    async def slow(question, cwd, deadline):
        await asyncio.sleep(0.1)
        return _qr()

    running = asyncio.create_task(q.submit("A", "c", 5, slow))
    await asyncio.sleep(0.01)  # let A occupy the slot
    with pytest.raises(QueueFull):
        await q.submit("B", "c", 5, slow)
    await running
