"""Passive (organic) population — FUTURE_DIRECTIONS §1.

The building agent's Read/Edit/Write hooks tee file content into the window via
`observe()`; the model answers from it without re-reading, freshness/manifest
machinery treats it as current, and compaction distills it into the ledger under
pressure. Content is in-memory only (never persisted)."""
from __future__ import annotations

import asyncio

import pytest

from conftest import SERVER_DIR, far_deadline, make_ws, FakeLlm

from live_memory.config import Config
from live_memory.manager import _build_system, _maybe_compact, process_question
from live_memory.models import ChatResult, FileContext, ToolCall, now_ms
from live_memory.summarizer import Summarizer
from live_memory.workspace import OBSERVE_INVALIDATE_GRACE_MS, WorkspaceState


def test_observe_records_and_marks_fresh_even_for_unread_files(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    # observe a file Live Memory never actively read — it IS the new knowledge
    assert ws.observe("brand/new.py", "def hello(): ...") is True
    fc = next(f for f in ws.window.file_contexts if f.path == "brand/new.py")
    assert fc.has_content and fc.content_hash and not fc.deleted
    assert fc.content == "def hello(): ..."


def test_observe_clears_pending_stale_hint(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.window.upsert_file_context(FileContext("a.py", "h", token_estimate=5))
    assert ws.note_modified("a.py") is True             # old stale-hint path
    ws.observe("a.py", "new current bytes")              # teeing supersedes the hint
    assert ws.drain_recently_modified() == []            # hint cleared (file is current now)


def test_observed_content_rendered_inline_in_volatile_not_stable(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.observe("svc/auth.py", "SECRET_MARKER = 42  # the teed body")
    stable, volatile = _build_system(ws, ws.window)
    assert "SECRET_MARKER = 42" in volatile               # the model can answer without re-reading
    assert "Files observed live from the building agent's session" in volatile
    assert "svc/auth.py" in volatile
    assert "SECRET_MARKER" not in stable                  # stays OUT of the cached tree prefix


def test_invalidate_ignored_within_grace_then_honored(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.observe("x.py", "fresh bytes just teed")
    # a FileChanged right after our own teed edit is our echo → not stale
    assert ws.invalidate("x.py") is False
    fc = next(f for f in ws.window.file_contexts if f.path == "x.py")
    assert fc.has_content and fc.content_hash             # untouched, still current
    # once the grace window passes, a genuine external change invalidates it
    fc.observed_at = now_ms() - (OBSERVE_INVALIDATE_GRACE_MS + 1000)
    assert ws.invalidate("x.py") is True
    assert not fc.has_content and not fc.content_hash     # stale: bytes dropped, hash cleared


def test_delete_drops_observed_content(tmp_cfg):
    ws = make_ws(tmp_cfg, FakeLlm())
    ws.observe("gone.py", "soon to be deleted")
    assert ws.mark_deleted("gone.py") is True
    fc = next(f for f in ws.window.file_contexts if f.path == "gone.py")
    assert fc.deleted and not fc.has_content


@pytest.mark.asyncio
async def test_compaction_distills_observations_into_ledger_and_frees_budget(tmp_cfg):
    # Tier 0: when observed bytes overflow the window, distill them into the durable
    # ledger (one summarizer call), drop the raw bytes, keep the manifest entry.
    tmp_cfg.max_context_tokens = 1000
    tmp_cfg.compaction_threshold = 0.85                  # soft target = 850
    llm = FakeLlm(complete_text="LEDGER: auth lives in svc/auth.py")
    ws = WorkspaceState(SERVER_DIR, tmp_cfg, llm, Summarizer(llm))
    for i in range(6):                                    # 6 × ~250 tokens of content ≈ 1500 > target
        ws.observe(f"f{i}.py", "x" * 1000)               # ~250 tokens each
    assert ws.window.estimated_token_count() > 850
    await _maybe_compact(ws, ws.window)
    assert ws.window.estimated_token_count() <= 850       # budget actually reclaimed
    assert llm.complete_calls >= 1 and ws.summaries_written >= 1
    assert ws.window.knowledge_ledger == "LEDGER: auth lives in svc/auth.py"
    # at least some observations downgraded to manifest-only (content shed)
    assert any(not fc.has_content for fc in ws.window.file_contexts)


@pytest.mark.asyncio
async def test_cold_memory_forces_exploration_before_answering(tmp_cfg):
    # A cold memory that tries to answer with NO tool calls is rejected once and forced to
    # explore, so it grounds instead of confabulating from priors.
    llm = FakeLlm([
        ChatResult(answer="the default is 10000 tokens"),                                  # guess, no tools
        ChatResult(tool_calls=[ToolCall("t1", "Read", '{"file_path": "live_memory/config.py"}')]),
        ChatResult(answer="grounded from config.py"),
    ])
    ws = make_ws(tmp_cfg, llm)                       # fresh window → is_cold()
    assert ws.window.is_cold() is True
    r = await process_question(ws, "what is the default max_context_tokens?", far_deadline())
    assert r.answer == "grounded from config.py"      # the guess was NOT accepted
    assert llm.chat_calls == 3 and r.files_read == 1


@pytest.mark.asyncio
async def test_warm_memory_answers_without_forced_exploration(tmp_cfg):
    # Warm memory (observed content in-window) is NOT cold → the direct answer is accepted,
    # no forced read (preserves the 0-read win).
    llm = FakeLlm([ChatResult(answer="128000, from the observed file")])
    ws = make_ws(tmp_cfg, llm)
    ws.observe("live_memory/config.py", "max_context_tokens = 128000")
    assert ws.window.is_cold() is False
    r = await process_question(ws, "what is max_context_tokens?", far_deadline())
    assert r.answer == "128000, from the observed file" and llm.chat_calls == 1 and r.files_read == 0


@pytest.mark.asyncio
async def test_cold_guard_can_be_disabled(tmp_cfg):
    tmp_cfg.force_explore_when_cold = False
    llm = FakeLlm([ChatResult(answer="a guess")])
    ws = make_ws(tmp_cfg, llm)
    r = await process_question(ws, "what is X?", far_deadline())
    assert r.answer == "a guess" and llm.chat_calls == 1


@pytest.mark.asyncio
async def test_compaction_hysteresis_compacts_to_floor_not_threshold(tmp_cfg):
    # Compaction TRIGGERS at the high watermark (threshold) but compacts all the way
    # DOWN to the low watermark (floor) — leaving headroom so it doesn't re-fire (and
    # bust the prompt cache) on the next question. Regression guard for the overflow
    # thrash that made passive ingestion 10x costlier before hysteresis.
    from live_memory.manager import _maybe_compact
    tmp_cfg.max_context_tokens = 1000
    tmp_cfg.compaction_threshold = 0.85   # trigger above 850
    tmp_cfg.compaction_floor = 0.5        # but compact down to 500
    ws = make_ws(tmp_cfg, FakeLlm())
    for i in range(9):                     # 9 × 100 = 900 tokens (over the 850 trigger)
        ws.window.upsert_file_context(FileContext(f"f{i}.py", "h", token_estimate=100, last_referenced_at=i))
    await _maybe_compact(ws, ws.window)
    assert ws.window.estimated_token_count() <= 500   # to the FLOOR, not back to 850
    assert ws.window.file_contexts                      # but not emptied (LRU, just enough)


@pytest.mark.asyncio
async def test_distillation_cooldown_sheds_instead_of_resummarizing(tmp_cfg):
    # Under the per-workspace cooldown, an over-budget compaction with observations SHEDS
    # raw bytes (free) instead of calling the summarizer again — bounding cost under heavy
    # multi-session teeing. Distillation resumes once the interval elapses.
    from live_memory.manager import _maybe_compact
    tmp_cfg.max_context_tokens = 1000
    tmp_cfg.compaction_threshold = 0.85
    tmp_cfg.compaction_floor = 0.6
    tmp_cfg.distill_min_interval_s = 1000  # long cooldown for the test
    llm = FakeLlm(complete_text="LEDGER")
    ws = WorkspaceState(SERVER_DIR, tmp_cfg, llm, Summarizer(llm))

    for i in range(6):                                   # ~1500 tok of observations > 850 trigger
        ws.observe(f"a{i}.py", "x" * 1000)
    await _maybe_compact(ws, ws.window)                  # first: distills (1 summarizer call)
    assert llm.complete_calls == 1 and ws.window.estimated_token_count() <= 600

    for i in range(6):
        ws.observe(f"b{i}.py", "y" * 1000)
    await _maybe_compact(ws, ws.window)                  # within cooldown: SHED, no new call
    assert llm.complete_calls == 1                       # still 1 — did not re-summarize
    assert ws.window.estimated_token_count() <= 600      # …but budget still reclaimed

    ws.last_distill_at = 0.0                              # simulate cooldown elapsed
    for i in range(6):
        ws.observe(f"c{i}.py", "z" * 1000)
    await _maybe_compact(ws, ws.window)                  # distills again
    assert llm.complete_calls == 2


@pytest.mark.asyncio
async def test_observed_content_is_not_persisted(tmp_cfg):
    # Raw teed bytes stay in-memory; the snapshot keeps only the lean manifest, and
    # the entry survives reload only because the on-disk file still hashes the same.
    ws = make_ws(tmp_cfg, FakeLlm())
    real = "live_memory/models.py"
    content = (__import__("pathlib").Path(SERVER_DIR) / real).read_text(encoding="utf-8")
    ws.observe(real, content)
    assert next(f for f in ws.window.file_contexts if f.path == real).has_content
    await ws.persist()
    ws2 = WorkspaceState(SERVER_DIR, tmp_cfg, FakeLlm(), Summarizer(FakeLlm()))
    await ws2.load()
    fc = next((f for f in ws2.window.file_contexts if f.path == real), None)
    assert fc is not None and not fc.has_content          # manifest survived; raw bytes did not


def test_passive_ingestion_on_by_default(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith("LIVE_MEMORY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_MEMORY_PROVIDER", "anthropic")
    assert Config().passive_ingestion is True
    monkeypatch.setenv("LIVE_MEMORY_PASSIVE_INGESTION", "false")
    assert Config().passive_ingestion is False


@pytest.mark.asyncio
async def test_observation_lets_model_answer_without_a_read(tmp_cfg):
    # End-to-end: after observing a file, the model answers directly (no tool calls),
    # proving the teed bytes reached its context.
    ws = make_ws(tmp_cfg, FakeLlm([ChatResult(answer="answered from observed bytes")]))
    ws.observe("config.py", "DEBUG = True")
    r = await process_question(ws, "is DEBUG on?", far_deadline())
    assert r.answer == "answered from observed bytes" and r.tool_calls == 0
