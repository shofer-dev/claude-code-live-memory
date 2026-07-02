"""Neutral knowledge-ledger summarizer (DESIGN.md §Compaction).

Folds a batch of about-to-be-dropped Q&A history into the durable, query-agnostic
`knowledge_ledger`. Run with the recent/current question OUT of scope (only the
dropped transcript is passed) so the summary cannot bias toward recent queries.
"""
from __future__ import annotations

from .constants import MAX_TRANSCRIPT_CHARS
from .llm_client import LlmClient
from .models import ChatMessage, CostSnapshot
from .prompts import NEUTRAL_SUMMARY_SYSTEM_PROMPT, neutral_summary_user_prompt


def render_transcript(messages: list[ChatMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        if m.role == "user" and m.tool_results:
            for tr in m.tool_results:
                lines.append(f"[tool result]\n{tr.content}")
        elif m.role == "assistant" and m.tool_calls:
            if m.content:
                lines.append(f"[assistant] {m.content}")
            for tc in m.tool_calls:
                lines.append(f"[assistant calls {tc.name}({tc.arguments})]")
        else:
            lines.append(f"[{m.role}] {m.content}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[-MAX_TRANSCRIPT_CHARS:]  # keep the most recent of the dropped batch
    return text


class Summarizer:
    def __init__(self, llm: LlmClient):
        self.llm = llm

    async def summarize(self, existing_ledger: str, dropped: list[ChatMessage]) -> tuple[str, CostSnapshot]:
        transcript = render_transcript(dropped)
        if not transcript.strip():
            return existing_ledger, CostSnapshot()
        user = neutral_summary_user_prompt(existing_ledger, transcript)
        try:
            new_ledger, cost = await self.llm.complete(NEUTRAL_SUMMARY_SYSTEM_PROMPT, user, max_tokens=2048)
        except Exception:
            # If summarization fails, fall back to keeping the existing ledger
            # (we still drop the batch — correctness over completeness).
            return existing_ledger, CostSnapshot()
        return (new_ledger.strip() or existing_ledger), cost
