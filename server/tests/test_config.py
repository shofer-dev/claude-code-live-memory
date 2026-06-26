"""Config layering (env > config.json > defaults) + zero-config OAuth detection."""
from __future__ import annotations

import json
import os

import pytest

from live_memory import config as configmod
from live_memory.config import Config, canonical_workspace, is_absolute_cwd


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("LIVE_MEMORY_") or k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(configmod, "subscription_present", lambda: False)  # deterministic default


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    cfg = Config()
    assert cfg.provider == "anthropic" and "haiku" in cfg.model
    assert cfg.base_url == "https://api.anthropic.com"
    assert cfg.use_oauth is False and cfg.api_key is None


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "provider": "openai", "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat", "api_key": "sk-x"}))
    cfg = Config()
    assert cfg.provider == "openai" and cfg.model == "deepseek-chat"
    assert cfg.api_key == "sk-x" and cfg.metered is True


def test_env_overrides_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"model": "deepseek-chat"}))
    monkeypatch.setenv("LIVE_MEMORY_MODEL", "claude-haiku-4-5-20251001")
    assert Config().model == "claude-haiku-4-5-20251001"


def test_oauth_autodetect_when_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(configmod, "subscription_present", lambda: True)
    cfg = Config()
    assert cfg.use_oauth is True and cfg.metered is False and "haiku" in cfg.model


def test_api_key_disables_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(configmod, "subscription_present", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = Config()
    assert cfg.use_oauth is False and cfg.metered is True and cfg.api_key == "sk-ant-x"


def test_keep_warm_interval_from_provider_knowledge(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    # off by default (opt-in); interval still resolves from provider knowledge
    assert Config().keep_warm is False and Config().keep_warm_interval_s == 240.0
    # DeepSeek (openai-compat, long disk cache) → very long, so keep-warm self-disables
    monkeypatch.setenv("LIVE_MEMORY_PROVIDER", "openai")
    monkeypatch.setenv("LIVE_MEMORY_BASE_URL", "https://api.deepseek.com")
    assert Config().keep_warm_interval_s == 21600.0
    # explicit override wins over provider knowledge
    monkeypatch.setenv("LIVE_MEMORY_KEEP_WARM_INTERVAL_S", "90")
    assert Config().keep_warm_interval_s == 90.0
    # opt-in turns it on
    monkeypatch.setenv("LIVE_MEMORY_KEEP_WARM", "true")
    assert Config().keep_warm is True


def test_concurrency_defaults_to_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_MEMORY_DATA_DIR", str(tmp_path))
    assert Config().is_parallel is True                      # parallel is the default
    monkeypatch.setenv("LIVE_MEMORY_CONCURRENCY", "serial")
    assert Config().is_parallel is False                     # only "serial" opts out
    monkeypatch.setenv("LIVE_MEMORY_CONCURRENCY", "parallel")
    assert Config().is_parallel is True


# ── workspace canonicalization (repo-root snap) + absolute-cwd guard ──
def test_is_absolute_cwd():
    assert is_absolute_cwd("/abs/path") is True
    assert is_absolute_cwd("~/under_home") is True          # ~ expands to an absolute path
    assert is_absolute_cwd("relative/dir") is False
    assert is_absolute_cwd("./here") is False


def test_canonical_snaps_subdir_to_repo_root(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)                     # normal repo marker
    sub = repo / "server" / "live_memory"
    sub.mkdir(parents=True)
    assert canonical_workspace(str(sub)) == str(repo)        # subdir → repo root
    assert canonical_workspace(str(repo)) == str(repo)       # root → itself
    # opt out → resolved path as-given (no snap)
    assert canonical_workspace(str(sub), to_repo_root=False) == str(sub.resolve())


def test_canonical_submodule_nearest_vs_outermost(tmp_path):
    # superproject with a nested submodule (submodule uses a `.git` *file*)
    sup = tmp_path / "super"
    (sup / ".git").mkdir(parents=True)
    sub = sup / "vendor" / "lib"
    (sub / "src").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib")
    inside = sub / "src"
    assert canonical_workspace(str(inside)) == str(sub)                      # nearest (default): submodule root
    assert canonical_workspace(str(inside), outermost=True) == str(sup)      # outermost: superproject root
    # a path in the superproject but outside the submodule → superproject either way
    plain = sup / "app"
    plain.mkdir()
    assert canonical_workspace(str(plain)) == str(sup)
    assert canonical_workspace(str(plain), outermost=True) == str(sup)


def test_canonical_handles_worktree_dotgit_file_and_non_repo(tmp_path):
    # git worktrees/submodules use a `.git` *file*, not a dir — still a repo root
    wt = tmp_path / "wt"
    (wt / "deep" / "nested").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt")
    assert canonical_workspace(str(wt / "deep" / "nested")) == str(wt)  # walks up to the .git file
    # a directory with no .git ancestor falls back to the resolved path
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert canonical_workspace(str(plain)) == str(plain.resolve())
