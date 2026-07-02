"""Central registry of tunable magic numbers and defaults.

Every internal knob and every *default* for a user-configurable setting lives here, so
the system's behaviour is reviewable and editable in one place — no magic numbers
scattered across modules. User configuration itself still flows through env vars /
`config.json` (see `config.py`) and the `ask_live_memory` tool args; this module holds
only the values those fall back to, plus the internal constants that are not
user-configurable.

Deliberately NOT here (not "magic numbers"): model $ rates + cache multipliers (a
reference-data table in `pricing.py`, itself env-overridable), string identifiers/URLs
(provider base URLs, default model ids, the OAuth client id/endpoint), and the on-disk
snapshot format `VERSION` (a format tag, not a tunable).
"""
from __future__ import annotations

# ── tokens ──
CHARS_PER_TOKEN = 4                        # chars→tokens heuristic (see DESIGN Appendix A)

# ── server / network (defaults for LIVE_MEMORY_HOST / _PORT) ──
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7711

# ── context window + compaction ──
DEFAULT_MAX_CONTEXT_TOKENS = 128_000       # LIVE_MEMORY_MAX_CONTEXT_TOKENS
DEFAULT_COMPACTION_THRESHOLD = 0.85        # high watermark: compaction TRIGGER (LIVE_MEMORY_COMPACTION_THRESHOLD)
DEFAULT_COMPACTION_FLOOR = 0.6             # low watermark: compact DOWN to this (LIVE_MEMORY_COMPACTION_FLOOR)
DEFAULT_DIRECTORY_TREE_FRACTION = 0.10     # dir-tree cap as a fraction of the window (LIVE_MEMORY_DIRTREE_FRACTION)
COLD_LEDGER_MAX_CHARS = 160                # is_cold(): a ledger shorter than this counts as "no grounding"

# ── summarization / distillation ──
MAX_TRANSCRIPT_CHARS = 60_000              # cap on the transcript fed to one neutral-summary call
DEFAULT_DISTILL_MIN_INTERVAL_S = 60.0      # min seconds between observation-distillations per workspace; within
                                           # it, shed raw bytes for free instead (LIVE_MEMORY_DISTILL_MIN_INTERVAL_S)

# ── agent loop ──
DEFAULT_MAX_ITERATIONS = 25                # LIVE_MEMORY_MAX_ITERATIONS
DEFAULT_TIMEOUT_S = 90.0                   # soft per-question budget (LIVE_MEMORY_DEFAULT_TIMEOUT_S)
MIN_QUESTION_TIMEOUT_S = 5.0               # clamp floor for the tool's `timeout` arg
MAX_QUESTION_TIMEOUT_S = 1800.0            # clamp ceiling for the tool's `timeout` arg

# ── queue / concurrency ──
DEFAULT_MAX_QUEUE_SIZE = 100               # LIVE_MEMORY_MAX_QUEUE_SIZE
DEFAULT_MAX_PARALLEL_QUERIES = 4           # LIVE_MEMORY_MAX_PARALLEL_QUERIES
HARD_BACKSTOP_MARGIN_S = 15.0              # queue hard wait_for margin above the soft deadline

# ── passive ingestion ──
DEFAULT_PASSIVE_MAX_FILE_BYTES = 262_144   # per-file cap on teed content (LIVE_MEMORY_PASSIVE_MAX_FILE_BYTES)
OBSERVE_INVALIDATE_GRACE_MS = 5_000        # ignore a FileChanged within this window of our own tee

# ── tools ──
MAX_TOOL_OUTPUT_BYTES = 200_000            # truncation cap applied to any tool result

# ── keep-warm ──
DEFAULT_KEEP_WARM_INTERVAL_S = 240.0       # provider default (anthropic / openai)
DEEPSEEK_KEEP_WARM_INTERVAL_S = 21_600.0   # ~6h ≫ max_idle ⇒ effectively off for DeepSeek's long disk cache
DEFAULT_KEEP_WARM_MAX_IDLE_S = 1_800.0     # stop warming a workspace idle longer than this (LIVE_MEMORY_KEEP_WARM_MAX_IDLE_S)
KEEP_WARM_PING_MAX_TOKENS = 1              # tokens requested by a keep-warm ping (output discarded)

# ── http client ──
HTTP_TIMEOUT_S = 600.0
HTTP_CONNECT_TIMEOUT_S = 15.0

# ── oauth ──
OAUTH_REFRESH_MARGIN_MS = 120_000          # refresh the subscription token if within this of expiry

# ── workspace keying ──
WORKSPACE_HASH_LEN = 16                    # hex chars of the cwd sha256 used as the per-workspace snapshot key
