"""End-to-end agent loop (manager.process_question) with a scripted fake model:
tool loop, read-file tracking, soft-deadline, neutral compaction, persistence."""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeLlm, SERVER_DIR, far_deadline, make_ws

from live_memory.manager import process_question
from live_memory.models import ChatMessage, ChatResult, CostSnapshot, FileContext, ToolCall
from live_memory.summarizer import Summarizer
from live_memory.workspace import WorkspaceState


@pytest.mark.asyncio
async def test_tool_loop_answers_and_persists(tmp_cfg):
    llm = FakeLlm([
        ChatResult(tool_calls=[ToolCall("t1", "Grep", '{"pattern": "class ContextWindow"}')]),
        ChatResult(answer="ContextWindow is in context_window.py."),
    ])
    ws = make_ws(tmp_cfg, llm)
    r = await process_question(ws, "where is ContextWindow?", far_deadline())
    assert r.answer == "ContextWindow is in context_window.py." and not r.timed_out
    assert llm.chat_calls == 2
    assert [m.content for m in ws.window.messages] == ["where is ContextWindow?", r.answer]
    assert tmp_cfg.snapshot_path(SERVER_DIR).exists()


@pytest.mark.asyncio
async def test_read_file_populates_loaded_set(tmp_cfg):
    llm = FakeLlm([
        ChatResult(tool_calls=[ToolCall("t1", "Read", '{"file_path": "live_memory/models.py", "limit": 5}')]),
        ChatResult(answer="models.py holds dataclasses."),
    ])
    ws = make_ws(tmp_cfg, llm)
    await process_question(ws, "what is in models.py?", far_deadline())
    assert ws.window.has_file("live_memory/models.py")
    assert ws.invalidate("live_memory/models.py") is True
    assert ws.invalidate("never/read.py") is False


@pytest.mark.asyncio
async def test_grep_does_not_count_as_a_read(tmp_cfg):
    # only content-reading tools (Read) join the read set; searches don't
    llm = FakeLlm([
        ChatResult(tool_calls=[ToolCall("t1", "Grep", '{"pattern": "class"}')]),
        ChatResult(answer="found some classes"),
    ])
    ws = make_ws(tmp_cfg, llm)
    await process_question(ws, "any classes?", far_deadline())
    assert ws.window.file_contexts == []  # Grep never populates the read set


@pytest.mark.asyncio
async def test_file_change_notifications_only_for_read_files(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.window.upsert_file_context(FileContext("a.py", "h", token_estimate=5))  # simulate a read
    # tool edit (note_modified) and external edit (invalidate): recorded only for read files
    assert ws.note_modified("a.py") is True
    assert ws.note_modified("never_read.py") is False
    assert ws.invalidate("a.py") is True
    assert ws.invalidate("never_read.py") is False
    assert ws.drain_recently_modified() == ["a.py"]


@pytest.mark.asyncio
async def test_metered_cost_accumulates(tmp_cfg):
    # API-key (metered) mode: the per-turn $ flows into the workspace total.
    tmp_cfg.metered = True
    llm = FakeLlm([ChatResult(answer="done", cost=CostSnapshot(usd=0.05))])
    ws = make_ws(tmp_cfg, llm)
    await process_question(ws, "q", far_deadline())
    assert ws.cost.usd == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_subscription_cost_is_zeroed(tmp_cfg):
    # Subscription (not metered): rate-limited, not $-metered → $ stays 0.
    tmp_cfg.metered = False
    llm = FakeLlm([ChatResult(answer="done", cost=CostSnapshot(usd=0.05))])
    ws = make_ws(tmp_cfg, llm)
    await process_question(ws, "q", far_deadline())
    assert ws.cost.usd == 0.0


@pytest.mark.asyncio
async def test_metadata_counts_tools_and_reads(tmp_cfg):
    llm = FakeLlm([
        ChatResult(tool_calls=[ToolCall("t1", "Read", '{"file_path": "live_memory/models.py", "limit": 3}')]),
        ChatResult(answer="done"),
    ])
    ws = make_ws(tmp_cfg, llm)
    r = await process_question(ws, "what's in models.py?", far_deadline())
    assert r.tool_calls == 1 and r.files_read == 1
    assert r.prompt_tokens >= 0 and r.completion_tokens >= 0 and r.duration_ms >= 0


@pytest.mark.asyncio
async def test_metadata_zero_when_no_tools(tmp_cfg):
    r = await process_question(make_ws(tmp_cfg, FakeLlm([ChatResult(answer="direct")])), "hi", far_deadline())
    assert r.tool_calls == 0 and r.files_read == 0


def test_stats_exposes_token_breakdown(tmp_cfg):
    # cumulative input/output tokens surface in /stats (needed for benchmarking,
    # and they accumulate even under subscription where costUsd is null/zeroed).
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.cost.add(CostSnapshot(usd=0.01, input_tokens=100, output_tokens=20, cache_read_tokens=50))
    s = ws.stats()
    assert s["inputTokens"] == 100 and s["outputTokens"] == 20
    assert s["cacheReadTokens"] == 50 and s["cacheWriteTokens"] == 0


@pytest.mark.asyncio
async def test_registry_clear_and_clear_all(tmp_cfg, tmp_path):
    from live_memory.workspace import WorkspaceRegistry
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    reg = WorkspaceRegistry(tmp_cfg, FakeLlm(), Summarizer(FakeLlm()))
    ws = await reg.get(str(repo))
    await ws.persist()                                  # write its snapshot
    snap = tmp_cfg.snapshot_path(ws.cwd)
    assert snap.exists() and reg.existing(str(repo)) is not None
    # clear ONE workspace → state dropped + snapshot deleted
    assert reg.clear(str(repo)) is True
    assert not snap.exists() and reg.existing(str(repo)) is None
    assert reg.clear(str(repo)) is False               # nothing left
    # recreate a snapshot, then clear_all wipes everything
    await (await reg.get(str(repo))).persist()
    assert snap.exists()
    assert reg.clear_all() >= 1 and not snap.exists() and reg.all() == []


@pytest.mark.asyncio
async def test_registry_canonicalizes_subdir_to_repo_root(tmp_cfg, tmp_path):
    from live_memory.workspace import WorkspaceRegistry
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    reg = WorkspaceRegistry(tmp_cfg, FakeLlm(), Summarizer(FakeLlm()))
    ws_root = await reg.get(str(repo))
    ws_sub = await reg.get(str(repo / "sub"))
    assert ws_root is ws_sub                              # subdir collapses to the same workspace
    assert ws_root.cwd == str(repo.resolve())             # keyed at the repo root
    assert reg.existing(str(repo / "sub")) is ws_root


def test_format_metadata_footer(tmp_cfg):
    from live_memory.models import ContextUsage, CostSnapshot, QuestionResult
    from live_memory.server import _format_metadata
    tmp_cfg.metered = True
    ws = make_ws(tmp_cfg, FakeLlm())
    r = QuestionResult("ans", tokens_used=120, context_usage=ContextUsage(120, 1000, 2, 1, 0),
                       cost_snapshot=CostSnapshot(usd=0.0012), prompt_tokens=100, completion_tokens=20,
                       tool_calls=2, files_read=1)
    foot = _format_metadata(r, ws)
    assert foot.startswith("---\n[live-memory] ")
    assert "tokens=120(in=100,out=20)" in foot and "tool_calls=2" in foot and "files_read=1" in foot
    assert "cost=$0.0012" in foot and "context=120/1000(12.0%)" in foot
    tmp_cfg.metered = False  # subscription → no notional dollars
    assert "cost=n/a(subscription)" in _format_metadata(r, ws)


def test_manifest_distinguishes_read_changed_deleted(tmp_cfg):
    from live_memory.manager import _build_system
    ws = make_ws(tmp_cfg, FakeLlm())
    w = ws.window
    for p in ("read.py", "changed.py", "gone.py"):
        w.upsert_file_context(FileContext(p, "h", token_estimate=5))
    assert ws.invalidate("changed.py") is True       # FileChanged 'change' → stale
    assert ws.mark_deleted("gone.py") is True          # FileChanged 'unlink' → deleted
    assert ws.mark_deleted("never_read.py") is False   # only files we actually read
    sp = _build_system(ws, w)
    assert "Read into your knowledge: read.py" in sp
    assert "changed.py — CHANGED since you read it" in sp
    assert "gone.py — DELETED or moved/renamed" in sp


@pytest.mark.asyncio
async def test_soft_deadline_best_effort(tmp_cfg):
    llm = FakeLlm([ChatResult(tool_calls=[ToolCall(f"t{i}", "get_changed_files", "{}")]) for i in range(10)], delay=0.1)
    ws = make_ws(tmp_cfg, llm)
    deadline = asyncio.get_event_loop().time() + 0.15
    r = await process_question(ws, "loop", deadline)
    assert r.timed_out is True
    assert "time budget" in r.answer.lower()
    assert llm.chat_calls < 10


@pytest.mark.asyncio
async def test_compaction_triggers_at_soft_threshold_not_hard_cap(tmp_cfg):
    # Regression: compaction must fire at compaction_threshold (0.85), not only at
    # the hard max. A window at 90% (over 85%, under 100%) should evict down to 85%.
    from live_memory.manager import _maybe_compact
    tmp_cfg.max_context_tokens = 1000
    tmp_cfg.compaction_threshold = 0.85          # soft target = 850
    ws = make_ws(tmp_cfg, FakeLlm())
    for i in range(9):                            # 9 × 100 = 900 tokens = 90% (file contexts)
        ws.window.upsert_file_context(FileContext(f"f{i}.py", "h", token_estimate=100, last_referenced_at=i))
    assert ws.window.estimated_token_count() == 900  # over 850 soft, under 1000 hard → OLD code did nothing
    await _maybe_compact(ws, ws.window)
    assert ws.window.estimated_token_count() <= 850   # evicted down to the soft threshold
    assert ws.window.file_contexts                     # but not emptied (LRU, just enough)


@pytest.mark.asyncio
async def test_neutral_compaction(tmp_cfg):
    tmp_cfg.max_context_tokens = 120
    llm = FakeLlm([ChatResult(answer="x" * 400) for _ in range(6)], complete_text="LEDGER v2: facts.")
    ws = WorkspaceState(SERVER_DIR, tmp_cfg, llm, Summarizer(llm))
    for i in range(5):
        await process_question(ws, f"q{i}", far_deadline())
    assert llm.complete_calls >= 1 and ws.summaries_written >= 1
    assert ws.window.knowledge_ledger == "LEDGER v2: facts."
    assert ws.last_compaction is not None


# ── concurrency model: serial (default) vs parallel fork-join ──
def test_serial_fork_is_live_window_and_commit_is_noop(tmp_cfg):
    tmp_cfg.concurrency = "serial"                # opt out of the parallel default
    ws = make_ws(tmp_cfg, FakeLlm())
    assert ws.cfg.is_parallel is False
    assert ws.fork_window() is ws.window          # serial: operate on the live window
    assert ws.commit_window(ws.window) is True     # commit-in-place is a no-op keep


def test_parallel_commit_keeps_most_exploring(tmp_cfg):
    tmp_cfg.concurrency = "parallel"
    ws = make_ws(tmp_cfg, FakeLlm())
    assert ws.fork_window() is not ws.window       # parallel: independent clone
    base = ws.window
    richer = base.clone()
    richer.upsert_file_context(FileContext("x.py", "h", token_estimate=10))  # read a file
    poorer = base.clone()
    poorer.append_message(ChatMessage("assistant", "z" * 400))               # just a long answer
    assert ws.commit_window(richer) is True and ws.window is richer          # 1 file > 0 files
    assert ws.commit_window(poorer) is False and ws.window is richer         # longer text doesn't win


@pytest.mark.asyncio
async def test_parallel_fork_join_keeps_most_exploring_end_to_end(tmp_cfg):
    import json as _json
    tmp_cfg.concurrency = "parallel"

    class RouterLlm:  # routes by which question is in the conversation (deterministic under gather)
        def __init__(self) -> None:
            self.chat_calls = 0

        async def chat(self, system, messages, tools=None, max_tokens=4096):
            self.chat_calls += 1
            await asyncio.sleep(0.005)  # yield so both questions fork from the same base
            blob = _json.dumps(messages)
            if "qA" in blob:  # A explores: read a file, then answer
                if "live_memory/models.py" in blob:
                    return ChatResult(answer="A done")
                return ChatResult(tool_calls=[ToolCall("a1", "Read", '{"file_path": "live_memory/models.py", "limit": 3}')])
            return ChatResult(answer="B done")  # B answers directly, reads nothing

        async def complete(self, *a, **k):
            return "LEDGER", CostSnapshot()

    ws = make_ws(tmp_cfg, RouterLlm())
    rA, rB = await asyncio.gather(
        process_question(ws, "qA", far_deadline()),
        process_question(ws, "qB", far_deadline()),
    )
    assert rA.answer == "A done" and rB.answer == "B done"   # both callers get their answer
    assert ws.questions_answered == 2                         # both counted
    # A explored a file → its fork wins the commit; B's Q&A is dropped from shared memory
    assert ws.window.has_file("live_memory/models.py")
    contents = [m.content for m in ws.window.messages]
    assert "qA" in contents and "A done" in contents and "qB" not in contents


@pytest.mark.asyncio
async def test_parallel_cumulative_cost_sums_all_forks(tmp_cfg):
    # In parallel mode only the richest fork's WINDOW commits, but EVERY fork's
    # cost must still accumulate into /stats (ws.cost), independent of who won.
    import json as _json
    tmp_cfg.concurrency = "parallel"
    tmp_cfg.metered = True  # so $ isn't zeroed

    class CostLlm:
        async def chat(self, system, messages, tools=None, max_tokens=4096):
            await asyncio.sleep(0.005)  # interleave: both fork from the same base
            a = "qA" in _json.dumps(messages)
            return ChatResult(answer="A" if a else "B", cost=CostSnapshot(usd=0.01 if a else 0.02))

        async def complete(self, *a, **k):
            return "L", CostSnapshot()

    ws = make_ws(tmp_cfg, CostLlm())
    await asyncio.gather(
        process_question(ws, "qA", far_deadline()),
        process_question(ws, "qB", far_deadline()),
    )
    assert ws.questions_answered == 2
    assert ws.cost.usd == pytest.approx(0.03)  # 0.01 + 0.02, both forks counted


@pytest.mark.asyncio
async def test_keep_warm_eligibility_and_warm(tmp_cfg):
    import time as _time
    from live_memory.keep_warm import _eligible, warm_one
    ws = make_ws(tmp_cfg, FakeLlm([ChatResult(answer="ok")]))
    now = _time.time()
    interval, max_idle = 240.0, 1800.0

    # empty + never-queried → not eligible
    assert _eligible(ws, now, interval, max_idle) is False
    ws.window.append_message(ChatMessage("user", "q"))      # give it content
    ws.last_query_at = ws.last_touch_at = now - 300         # queried 5 min ago, idle ≥ interval
    assert _eligible(ws, now, interval, max_idle) is True
    # in-flight query → skip (an active query already warms the cache)
    ws.queue._depth = 1
    assert _eligible(ws, now, interval, max_idle) is False
    ws.queue._depth = 0
    # touched recently → skip
    ws.last_touch_at = now - 10
    assert _eligible(ws, now, interval, max_idle) is False
    # abandoned (idle > max_idle) → skip
    ws.last_touch_at = now - 300
    ws.last_query_at = now - (max_idle + 100)
    assert _eligible(ws, now, interval, max_idle) is False

    # warm_one issues a 1-token ping and bumps the counter + last_touch
    ws.last_query_at = now
    before = ws.keep_warms
    await warm_one(ws, now)
    assert ws.keep_warms == before + 1 and ws.last_touch_at == now


@pytest.mark.asyncio
async def test_persistence_restores(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm([ChatResult(answer="forty-two")]))
    await process_question(ws, "the answer?", far_deadline())
    ws2 = WorkspaceState(SERVER_DIR, tmp_cfg, FakeLlm(), Summarizer(FakeLlm()))
    await ws2.load()
    assert [m.content for m in ws2.window.messages] == ["the answer?", "forty-two"]
    assert ws2.questions_answered == 1
