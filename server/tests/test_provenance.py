"""Provenance-tagged compaction (FUTURE_DIRECTIONS §6).

Ledger facts carry the source files they were distilled from (path → content hash
at write time), so an out-of-band change to a cited file DEMOTES the fact (renders
it under a warning heading) instead of letting stale prose be trusted silently.
In-session demotion rides the existing invalidate/delete path; cross-session
demotion is validated against disk on load, mirroring the file-context manifest.
"""
from __future__ import annotations

import hashlib

from live_memory.context_window import ContextWindow
from live_memory.conversation_store import ConversationStore
from live_memory.models import FileContext, LedgerFact
from live_memory.prompts import STALE_LEDGER_HEADING


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _win() -> ContextWindow:
    return ContextWindow(200_000)


# ── attribution ──
def test_set_ledger_splits_facts_and_attributes_sources():
    w = _win()
    w.upsert_file_context(FileContext("src/auth/session.ts", _sha("x"), token_estimate=5))
    w.upsert_file_context(FileContext("api/middleware.ts", _sha("y"), token_estimate=5))
    w.set_ledger_from_summary(
        "SessionManager (src/auth/session.ts) issues tokens\n"
        "Middleware in api/middleware.ts consumes them\n"
        "General convention: 2-space indent"
    )
    f0, f1, f2 = w.ledger_facts
    assert f0.sources == {"src/auth/session.ts": _sha("x")}
    assert f1.sources == {"api/middleware.ts": _sha("y")}
    assert f2.sources == {}                       # cites no known path → nothing to validate
    assert not any(f.stale for f in w.ledger_facts)


def test_attributes_by_basename_not_just_full_path():
    # the model usually cites files by basename ("models.py"), not the full
    # workspace-relative path — attribution must catch both (regression: a real
    # benchmark produced 0 sources when only full paths were matched)
    w = _win()
    w.upsert_file_context(FileContext("server/live_memory/models.py", _sha("m"), token_estimate=5))
    w.set_ledger_from_summary(
        "`ChatMessage` (models.py): one turn in the conversation\n"           # basename only
        "server/live_memory/models.py holds the dataclasses\n"                # full path
        "This mentions submodels.python which is not a real file")            # must NOT match
    f0, f1, f2 = w.ledger_facts
    assert f0.sources == {"server/live_memory/models.py": _sha("m")}          # basename hit
    assert f1.sources == {"server/live_memory/models.py": _sha("m")}          # full-path hit
    assert f2.sources == {}                                                    # boundary: no false match


def test_single_line_summary_renders_verbatim():
    # legacy invariant: a one-line summary must round-trip to the exact ledger text
    w = _win()
    w.set_ledger_from_summary("LEDGER v2: facts.")
    assert w.knowledge_ledger == "LEDGER v2: facts."


# ── in-session demotion ──
def test_invalidate_demotes_only_citing_fact_and_orders_render():
    w = _win()
    w.upsert_file_context(FileContext("a.py", _sha("a"), token_estimate=5))
    w.upsert_file_context(FileContext("b.py", _sha("b"), token_estimate=5))
    w.set_ledger_from_summary("Fact about a.py here\nFact about b.py here")
    assert w.invalidate_file_context("a.py") is True
    a, b = w.ledger_facts
    assert a.stale and not b.stale                # only the a.py fact demoted
    kl = w.knowledge_ledger
    assert kl.index("Fact about b.py") < kl.index(STALE_LEDGER_HEADING) < kl.index("Fact about a.py")


def test_mark_deleted_demotes_citing_fact():
    w = _win()
    w.upsert_file_context(FileContext("gone.py", _sha("g"), token_estimate=5))
    w.set_ledger_from_summary("Something about gone.py")
    assert w.mark_file_deleted("gone.py") is True
    assert w.ledger_facts[0].stale and STALE_LEDGER_HEADING in w.knowledge_ledger


def test_mark_stale_noop_does_not_clobber_direct_ledger():
    # legacy / direct assignment (no provenance facts) must survive an invalidate
    w = _win()
    w.knowledge_ledger = "directly set, no facts"
    w.upsert_file_context(FileContext("a.py", _sha("a"), token_estimate=5))
    assert w.invalidate_file_context("a.py") is True
    assert w.knowledge_ledger == "directly set, no facts"


def test_ledger_for_summary_strips_heading_keeps_all_fact_text():
    w = _win()
    w.upsert_file_context(FileContext("a.py", _sha("a"), token_estimate=5))
    w.set_ledger_from_summary("Fresh fact about a.py\nOther fact")
    w.invalidate_file_context("a.py")            # demote the a.py fact
    s = w.ledger_for_summary()
    assert STALE_LEDGER_HEADING not in s          # heading is presentation, not a durable fact
    assert "Fresh fact about a.py" in s and "Other fact" in s


def test_clone_isolates_ledger_facts():
    w = _win()
    w.upsert_file_context(FileContext("a.py", _sha("a"), token_estimate=5))
    w.set_ledger_from_summary("Fact about a.py")
    c = w.clone()
    c.invalidate_file_context("a.py")
    assert c.ledger_facts[0].stale and not w.ledger_facts[0].stale


# ── cross-session (on-disk) validation ──
def test_store_roundtrips_and_validates_ledger_provenance(tmp_path):
    (tmp_path / "a.py").write_text("current bytes")
    store = ConversationStore(str(tmp_path), tmp_path / "snap.json")
    store.save({
        "knowledge_ledger": "Fact about a.py\nFact about b.py",
        "ledger_facts": [
            LedgerFact("Fact about a.py", sources={"a.py": _sha("current bytes")}),
            LedgerFact("Fact about b.py", sources={"b.py": _sha("older")}),  # b.py absent on disk
        ],
    })
    facts = store.load()["ledger_facts"]
    a = next(f for f in facts if "a.py" in f.text)
    b = next(f for f in facts if "b.py" in f.text)
    assert not a.stale                            # on-disk bytes still match → fresh
    assert b.stale                                # cited file missing → demoted


def test_store_ledger_provenance_stale_on_out_of_band_change(tmp_path):
    (tmp_path / "a.py").write_text("v1")
    store = ConversationStore(str(tmp_path), tmp_path / "snap.json")
    store.save({"ledger_facts": [LedgerFact("Fact about a.py", sources={"a.py": _sha("v1")})]})
    (tmp_path / "a.py").write_text("v2 changed")  # edited out of band between sessions
    assert store.load()["ledger_facts"][0].stale


def test_legacy_snapshot_without_ledger_facts_loads(tmp_path):
    store = ConversationStore(str(tmp_path), tmp_path / "snap.json")
    store.save({"knowledge_ledger": "legacy text"})
    loaded = store.load()
    assert loaded["knowledge_ledger"] == "legacy text" and loaded["ledger_facts"] == []
