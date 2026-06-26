"""USD cost estimation from token usage.

Per-million-token rates by model (input / output), with prompt-caching
multipliers: a cache *write* costs 1.25x input, a cache *read* costs 0.1x input.
Rates are best-effort defaults; the server runs ONE model, so env vars express/
override *its* price and WIN over the built-in table:

  LIVE_MEMORY_PRICE_INPUT             USD per 1M input tokens
  LIVE_MEMORY_PRICE_OUTPUT           USD per 1M output tokens   (both required to override)
  LIVE_MEMORY_PRICE_CACHE_READ_MULT  cache-read rate as a fraction of input (default 0.10)
  LIVE_MEMORY_PRICE_CACHE_WRITE_MULT cache-write rate as a fraction of input (default 1.25)

Unknown models with no override fall back to a generic estimate.
"""
from __future__ import annotations

import os

from .models import CostSnapshot

# model substring → (input_per_mtok, output_per_mtok) in USD
_RATES: dict[str, tuple[float, float]] = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek": (0.27, 1.10),
    "gemini-flash": (0.075, 0.30),
    "gemini-3": (1.25, 5.00),
}
_FALLBACK = (1.00, 5.00)

_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is not None:
        try:
            return float(v)
        except ValueError:
            pass
    return default


def _rates_for(model: str) -> tuple[float, float]:
    # Explicit env override wins over the built-in table — the server runs ONE
    # model, so this expresses *its* price. Then the substring table, then fallback.
    in_env = os.environ.get("LIVE_MEMORY_PRICE_INPUT")
    out_env = os.environ.get("LIVE_MEMORY_PRICE_OUTPUT")
    if in_env is not None and out_env is not None:
        try:
            return float(in_env), float(out_env)
        except ValueError:
            pass
    m = model.lower()
    for key, rate in _RATES.items():
        if key in m:
            return rate
    return _FALLBACK


def _cache_mults() -> tuple[float, float]:
    return (
        _env_float("LIVE_MEMORY_PRICE_CACHE_READ_MULT", _CACHE_READ_MULT),
        _env_float("LIVE_MEMORY_PRICE_CACHE_WRITE_MULT", _CACHE_WRITE_MULT),
    )


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> CostSnapshot:
    in_rate, out_rate = _rates_for(model)
    read_mult, write_mult = _cache_mults()
    # "input_tokens" here is the uncached input (Anthropic reports cache_read and
    # cache_write separately); price each bucket at its multiplier.
    usd = (
        (input_tokens / 1_000_000) * in_rate
        + (output_tokens / 1_000_000) * out_rate
        + (cache_write_tokens / 1_000_000) * in_rate * write_mult
        + (cache_read_tokens / 1_000_000) * in_rate * read_mult
    )
    return CostSnapshot(
        usd=usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
