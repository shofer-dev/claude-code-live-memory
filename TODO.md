# TODO — Live Memory

Knowingly-deferred work and accepted trade-offs. Keep current-state only.

## Answer-length budget: truncation is disclosed but not *detected*

The answer-length cap (`max_answer_tokens`, `LIVE_MEMORY_MAX_ANSWER_TOKENS`) is
now both enforced (a hard `chat(max_tokens=…)`) and disclosed to the model via
the per-question hints, so it self-regulates rather than overrunning. But if the
model *does* hit the cap, the answer is still cut off with no explicit signal:
the provider `stop_reason`/`finish_reason` (`"max_tokens"` / `"length"`) is not
plumbed through `ChatResult`, so the metadata trailer can't flag "answer was
truncated — re-ask with a larger `max_answer_tokens`." Threading that flag
through would let the caller distinguish a complete terse answer from a clipped
one and react automatically.
