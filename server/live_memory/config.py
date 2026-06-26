"""Configuration for the Live Memory server.

Layered: **env vars > `${data_dir}/config.json` > built-in defaults**.

The Live Memory is an independent service — its model/provider is its own config,
unrelated to which model a Claude Code session uses. It runs on any provider:
  - `anthropic` — Anthropic Messages API (Bedrock/Vertex/gateways). Explicit
                  `cache_control` prompt caching. May authenticate with an API key
                  OR (zero-config default) the Claude subscription OAuth token.
  - `openai`    — any OpenAI-compatible `/chat/completions` endpoint (OpenAI,
                  **DeepSeek**, local models, gateways). Implicit prefix caching.

ZERO-CONFIG DEFAULT: with no API key and no config, if a Claude subscription is
present we use OAuth + Haiku — so the plugin runs with no key. Switch models via
the `/live-memory-config` slash command (writes `config.json`) or env vars.
"""
from __future__ import annotations
from typing import Any

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .oauth import subscription_present

_PROVIDER_DEFAULTS = {
    "anthropic": ("https://api.anthropic.com", "claude-haiku-4-5-20251001"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}

# Provider knowledge: how often (s) to ping the prefix to keep its KV/prompt cache
# warm — chosen below the provider's cache TTL. Anthropic's explicit cache_control
# is ~5 min ephemeral; OpenAI prompt caching evicts after a few minutes idle.
_KEEP_WARM_DEFAULTS = {"anthropic": 240.0, "openai": 240.0}
# DeepSeek caches on disk for HOURS/DAYS — far longer than a session's active
# window — so warming is unnecessary. A very long interval makes the keep-warm
# loop a no-op for it (it can never fire before keep_warm_max_idle abandons the
# workspace). Detected by endpoint; still overridable per deployment.
_DEEPSEEK_KEEP_WARM = 21600.0  # ~6h ≫ default max_idle (30 min) → effectively off


def _keep_warm_default(provider: str, base_url: str) -> float:
    if "deepseek" in base_url.lower():
        return _DEEPSEEK_KEEP_WARM
    return _KEEP_WARM_DEFAULTS.get(provider, 240.0)


def _truthy(v: object) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes") if v is not None else False


def _data_dir() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("LIVE_MEMORY_DATA_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude" / "plugins" / "data" / "live-memory"


def workspace_hash(cwd: str) -> str:
    return hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:16]


def is_absolute_cwd(cwd: str) -> bool:
    """True only for a path the shared server can trust: absolute after `~`
    expansion. A relative cwd cannot be resolved correctly — the server runs as a
    separate process and does NOT share the caller's working directory, so
    resolving it would anchor to the *server's* dir (almost always wrong)."""
    return Path(cwd).expanduser().is_absolute()


def _repo_root(p: Path, outermost: bool = False) -> Path | None:
    """Ancestor (including `p`) holding a `.git` entry — a directory (normal repo)
    or a file (git worktree/submodule). Default returns the **nearest** one,
    matching `git rev-parse --show-toplevel` (a submodule is its own repo). With
    `outermost=True`, keep walking and return the **outermost** such ancestor
    instead — folding a submodule into its superproject. None if not in a repo."""
    found: Path | None = None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            found = d
            if not outermost:
                return d
    return found


def canonical_workspace(cwd: str, to_repo_root: bool = True, outermost: bool = False) -> str:
    """Normalize a workspace path to a STABLE partition key: expand `~`, make it
    absolute + canonical (symlinks, `..`, trailing slash), then (default) snap to
    the enclosing git repo root so a subdirectory and the repo root collapse to
    ONE workspace — no accidental per-subdir memory fragmentation. `outermost`
    selects the superproject root over a nested submodule's root. Non-repo dirs
    fall back to the resolved path as-is."""
    p = Path(cwd).expanduser().resolve()
    if to_repo_root:
        root = _repo_root(p, outermost)
        if root is not None:
            return str(root)
    return str(p)


def _load_config_file(data_dir: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads((data_dir / "config.json").read_text())
        return data
    except Exception:  # noqa: BLE001
        return {}


def _api_key_from_env(provider: str) -> str | None:
    return (
        os.environ.get("LIVE_MEMORY_API_KEY")
        or (os.environ.get("ANTHROPIC_API_KEY") if provider == "anthropic" else None)
        or (os.environ.get("OPENAI_API_KEY") if provider == "openai" else None)
        or (os.environ.get("DEEPSEEK_API_KEY") if provider == "openai" else None)
    )


@dataclass
class Config:
    host: str = field(default_factory=lambda: os.environ.get("LIVE_MEMORY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("LIVE_MEMORY_PORT", "7711")))

    # resolved in __post_init__ from env > config.json > defaults
    provider: str = ""
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    use_oauth: bool = False
    metered: bool = True  # whether cost is $-metered (API key) vs subscription (rate-limited)

    max_context_tokens: int = field(default_factory=lambda: int(os.environ.get("LIVE_MEMORY_MAX_CONTEXT_TOKENS", "128000")))
    compaction_threshold: float = field(default_factory=lambda: float(os.environ.get("LIVE_MEMORY_COMPACTION_THRESHOLD", "0.85")))
    directory_tree_fraction: float = field(default_factory=lambda: float(os.environ.get("LIVE_MEMORY_DIRTREE_FRACTION", "0.10")))
    max_iterations: int = field(default_factory=lambda: int(os.environ.get("LIVE_MEMORY_MAX_ITERATIONS", "25")))
    max_queue_size: int = field(default_factory=lambda: int(os.environ.get("LIVE_MEMORY_MAX_QUEUE_SIZE", "100")))
    default_timeout_s: float = field(default_factory=lambda: float(os.environ.get("LIVE_MEMORY_DEFAULT_TIMEOUT_S", "90")))
    # Same-workspace concurrency: "parallel" (default — fork the window per
    # question, run up to max_parallel_queries at once, commit back the fork that
    # explored the most, no queue delay) or "serial" (one question at a time, the
    # shared window grows in place). Anything but "serial" → parallel.
    concurrency: str = field(default_factory=lambda: (os.environ.get("LIVE_MEMORY_CONCURRENCY") or "parallel").strip().lower())
    max_parallel_queries: int = field(default_factory=lambda: int(os.environ.get("LIVE_MEMORY_MAX_PARALLEL_QUERIES", "4")))
    # Opt-in: also expose ask_live_memory_submit / ask_live_memory_result (a
    # server-side submit/poll pattern, since MCP has no native async tool calls).
    async_tools: bool = field(default_factory=lambda: _truthy(os.environ.get("LIVE_MEMORY_ASYNC_TOOLS", "false")))
    # KV/prompt-cache keep-warm: periodically ping each recently-active workspace's
    # prefix so the provider cache doesn't go cold (cold = next query re-reads the
    # whole prefix at full rate). Interval defaults from provider knowledge; stop
    # warming a workspace idle longer than max_idle (don't keep abandoned ones hot).
    keep_warm: bool = field(default_factory=lambda: _truthy(os.environ.get("LIVE_MEMORY_KEEP_WARM", "false")))
    keep_warm_interval_s: float = 0.0  # resolved in __post_init__ (provider default or override)
    keep_warm_max_idle_s: float = field(default_factory=lambda: float(os.environ.get("LIVE_MEMORY_KEEP_WARM_MAX_IDLE_S", "1800")))
    # Snap each workspace key to its enclosing git repo root (default on), so a
    # subdir and the repo root share one memory. Set false for per-subdir memory.
    canonicalize_workspace: bool = field(default_factory=lambda: _truthy(os.environ.get("LIVE_MEMORY_CANONICALIZE_WORKSPACE", "true")))
    # Which repo root to snap to when inside a submodule/worktree: "nearest"
    # (the submodule itself, git's default) or "outermost" (the superproject).
    repo_root_mode: str = field(default_factory=lambda: (os.environ.get("LIVE_MEMORY_REPO_ROOT_MODE") or "nearest").strip().lower())
    data_dir: Path = field(default_factory=_data_dir)

    def snapshot_path(self, cwd: str) -> Path:
        return self.data_dir / f"{workspace_hash(cwd)}.json"

    @property
    def oauth_state_path(self) -> Path:
        return self.data_dir / "oauth_state.json"

    @property
    def is_parallel(self) -> bool:
        return self.concurrency != "serial"  # parallel is the default; only "serial" opts out

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fc = _load_config_file(self.data_dir)

        prov = (os.environ.get("LIVE_MEMORY_PROVIDER") or fc.get("provider") or "anthropic").lower()
        self.provider = prov if prov in _PROVIDER_DEFAULTS else "anthropic"
        d_url, d_model = _PROVIDER_DEFAULTS[self.provider]
        self.base_url = os.environ.get("LIVE_MEMORY_BASE_URL") or fc.get("base_url") or d_url
        self.model = os.environ.get("LIVE_MEMORY_MODEL") or fc.get("model") or d_model
        self.api_key = _api_key_from_env(self.provider) or fc.get("api_key")

        # Zero-config OAuth: anthropic + no key + (explicitly enabled OR a subscription is present)
        explicit = _truthy(os.environ.get("LIVE_MEMORY_USE_CLAUDE_CODE_OAUTH")) or bool(fc.get("use_oauth"))
        if self.provider == "anthropic" and not self.api_key and (explicit or subscription_present()):
            self.use_oauth = True
        self.metered = not self.use_oauth

        # keep-warm interval: explicit override > endpoint-aware provider knowledge
        self.keep_warm_interval_s = float(
            os.environ.get("LIVE_MEMORY_KEEP_WARM_INTERVAL_S")
            or _keep_warm_default(self.provider, self.base_url)
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model, "base_url": self.base_url,
            "auth": "oauth-subscription" if self.use_oauth else ("api-key" if self.api_key else "none"),
            "metered": self.metered,
        }
