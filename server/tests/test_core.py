"""Pure-logic units: token estimation, pricing, context window, directory tree."""
from __future__ import annotations

from live_memory import pricing
from live_memory.context_window import ContextWindow
from live_memory.directory_tree import generate_directory_tree
from live_memory.models import ChatMessage, FileContext, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil(5/4)


def test_pricing_basic_and_cache_multipliers():
    c = pricing.estimate_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
    assert abs(c.usd - 1.00) < 1e-9
    c2 = pricing.estimate_cost("deepseek-chat", input_tokens=0, output_tokens=1_000_000)
    assert abs(c2.usd - 1.10) < 1e-9
    # cache read is 10% of input rate
    c3 = pricing.estimate_cost("claude-haiku-4-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    assert abs(c3.usd - 0.10) < 1e-9


def test_pricing_unknown_model_fallback():
    c = pricing.estimate_cost("some-unknown-model", input_tokens=1_000_000, output_tokens=0)
    assert abs(c.usd - 1.00) < 1e-9  # fallback (1.00, 5.00)


def test_pricing_env_override_wins_over_table(monkeypatch):
    # deepseek-v4-flash matches the "deepseek" table entry (0.27/1.10), but an
    # explicit env override must take precedence (the server runs one model).
    monkeypatch.setenv("LIVE_MEMORY_PRICE_INPUT", "0.10")
    monkeypatch.setenv("LIVE_MEMORY_PRICE_OUTPUT", "0.40")
    assert abs(pricing.estimate_cost("deepseek-v4-flash", input_tokens=1_000_000, output_tokens=0).usd - 0.10) < 1e-9
    assert abs(pricing.estimate_cost("deepseek-v4-flash", input_tokens=0, output_tokens=1_000_000).usd - 0.40) < 1e-9


def test_pricing_cache_mult_override(monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_PRICE_INPUT", "1.00")
    monkeypatch.setenv("LIVE_MEMORY_PRICE_OUTPUT", "1.00")
    monkeypatch.setenv("LIVE_MEMORY_PRICE_CACHE_READ_MULT", "0.5")
    c = pricing.estimate_cost("anything", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    assert abs(c.usd - 0.50) < 1e-9  # 1.00 input rate × 0.5 read multiplier


def test_context_window_evicts_file_contexts_lru_first():
    cw = ContextWindow(max_context_tokens=100)
    cw.upsert_file_context(FileContext("a.py", "h1", token_estimate=80, last_referenced_at=1))
    cw.upsert_file_context(FileContext("b.py", "h2", token_estimate=80, last_referenced_at=2))
    still_over = cw.enforce_limit()
    assert [fc.path for fc in cw.file_contexts] == ["b.py"]  # evicted the older (a.py)
    assert not still_over
    assert cw.consume_evicted_tokens() == 80


def test_context_window_signals_message_compaction_needed():
    cw = ContextWindow(max_context_tokens=10)
    for i in range(3):
        cw.append_message(ChatMessage("user", "x" * 80))
        cw.append_message(ChatMessage("assistant", "y" * 80))
    # no file contexts to evict, messages exceed budget → still over
    assert cw.enforce_limit() is True


def test_context_window_pop_oldest_pair_keeps_tail():
    cw = ContextWindow(1000)
    for i in range(3):
        cw.append_message(ChatMessage("user", f"q{i}"))
        cw.append_message(ChatMessage("assistant", f"a{i}"))
    assert [m.content for m in cw.pop_oldest_pair()] == ["q0", "a0"]
    # down to 2 → refuses to pop the minimal tail
    cw.pop_oldest_pair()
    assert cw.pop_oldest_pair() == []
    assert cw.message_count() == 2


def test_context_window_stale_and_usage():
    cw = ContextWindow(1000)
    cw.upsert_file_context(FileContext("a.py", "h", token_estimate=5))
    assert cw.has_file("a.py")
    assert cw.invalidate_file_context("a.py") is True
    assert cw.invalidate_file_context("missing.py") is False
    u = cw.get_usage()
    assert u.stale_file_contexts == 1 and u.file_contexts == 1


def test_context_window_mark_deleted():
    cw = ContextWindow(1000)
    cw.upsert_file_context(FileContext("a.py", "h", token_estimate=5))
    assert cw.mark_file_deleted("a.py") is True
    fc = cw.file_contexts[0]
    assert fc.deleted is True and fc.content_hash == ""   # gone, distinct from a plain stale
    assert cw.mark_file_deleted("missing.py") is False


def test_filecontext_deleted_roundtrips_dict():
    d = FileContext("x.py", "", 5, deleted=True).to_dict()
    assert FileContext.from_dict(d).deleted is True
    assert FileContext.from_dict({"path": "y.py"}).deleted is False  # default


def test_context_window_counts_ledger():
    cw = ContextWindow(1000)
    base = cw.estimated_token_count()
    cw.knowledge_ledger = "x" * 40
    assert cw.estimated_token_count() == base + 10


def test_context_window_clone_is_independent():
    cw = ContextWindow(1000)
    cw.append_message(ChatMessage("user", "q0"))
    cw.upsert_file_context(FileContext("a.py", "h", token_estimate=5))
    cw.knowledge_ledger = "L"
    c = cw.clone()
    # mutate the clone — original must be untouched
    c.append_message(ChatMessage("assistant", "a0"))
    c.upsert_file_context(FileContext("b.py", "h2", token_estimate=9))
    c.knowledge_ledger = "L2"
    assert [m.content for m in cw.messages] == ["q0"]
    assert cw.file_context_paths == ["a.py"] and cw.knowledge_ledger == "L"
    assert [m.content for m in c.messages] == ["q0", "a0"]
    assert c.file_context_paths == ["a.py", "b.py"]


def test_exploration_score_files_dominate_then_tokens():
    a = ContextWindow(10_000)  # read 2 files
    a.upsert_file_context(FileContext("x.py", "h", token_estimate=10))
    a.upsert_file_context(FileContext("y.py", "h", token_estimate=10))
    b = ContextWindow(10_000)  # read 1 file but a very long answer
    b.upsert_file_context(FileContext("x.py", "h", token_estimate=10))
    b.append_message(ChatMessage("assistant", "z" * 4000))
    assert a.exploration_score() > b.exploration_score()  # files dominate token length
    # stale (re-read-needed) file contexts don't count toward exploration
    c = ContextWindow(10_000)
    c.upsert_file_context(FileContext("x.py", "", token_estimate=10))  # empty hash → stale
    assert c.exploration_score()[0] == 0


def test_directory_tree_lists_and_caps(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "node_modules").mkdir()  # skipped
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    tree = generate_directory_tree(str(tmp_path), 200_000)
    assert "src/" in tree and "a.py" in tree
    assert "node_modules" not in tree  # SKIP_PARTS
    # tiny cap → truncated
    capped = generate_directory_tree(str(tmp_path), 1)
    assert "truncated" in capped
