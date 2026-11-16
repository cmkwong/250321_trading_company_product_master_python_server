"""Conversation memory shared by every manager agent.

``ConversationMemory`` holds the transcript as ``("human", text)`` /
``("ai", text)`` pairs: each turn is recorded as the user's request and a JSON
summary of the rendered plan, not as raw graph messages, so the transcript is
domain-agnostic, cheap to replay and safe to hand to a different domain manager.
Compaction replaces the whole history with a single summarised pair.

The small plan-shape coercion helpers (``as_str_list`` / ``as_sql_list`` /
``as_dict_list``) live in ``..contracts`` alongside the plan/worker shapes, so
:mod:`core.engine` can import them without creating an import cycle back
through this module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from apps.core import config

from .contracts import as_sql_list, as_str_list
from .llm import build_llm

if TYPE_CHECKING:  # type annotations only; avoids an engine <-> memory cycle
    from .engine import ProductPlan


def _plan_summary(plan: "ProductPlan") -> Dict[str, Any]:
    """Trim a plan to the fields worth keeping in the conversation history."""
    return {
        "answer": str(plan.get("answer", "")),
        "summary": str(plan.get("summary", "")),
        "plan": as_str_list(plan.get("plan")),
        "sql": as_sql_list(plan.get("sql")),
        "missing_data": as_str_list(plan.get("missing_data")),
        "risks": as_str_list(plan.get("risks")),
    }


class ConversationMemory:
    """Conversation state shared by every manager agent.

    ``messages`` holds ``("human", text)`` / ``("ai", text)`` pairs: each turn is
    recorded as the user's request and the rendered plan (``format_plan``), not as
    raw graph messages, so the transcript is domain-agnostic, cheap to replay and
    safe to hand to a different domain manager.
    """

    def __init__(self) -> None:
        self.messages: List[Any] = []
        self.compactions: int = 0

    def reset(self) -> None:
        """Forget the conversation history (the agents and prompts stay loaded)."""
        self.messages = []

    def history(self) -> List[Any]:
        """Return a copy of the conversation to replay before the next turn."""
        return list(self.messages)

    def record(self, task: str, plan: "ProductPlan") -> None:
        """Append a finished turn to the history as (human, JSON summary) pairs.

        The assistant turn is stored as a JSON object (not the prose
        ``format_plan``) so the replayed assistant message stays consistent with
        the agents' "return exactly one JSON object" output contract.  Storing
        prose would make the model imitate the "Answer:/Summary:" layout on the
        next turn instead of emitting parseable JSON.
        """
        self.messages.append(("human", str(task)))
        self.messages.append(
            ("ai", json.dumps(_plan_summary(plan), ensure_ascii=False))
        )

    def transcript(self, limit: Optional[int] = None) -> str:
        """Render the whole conversation as plain text (for compaction/routing)."""
        parts = []
        for message in self.messages:
            if isinstance(message, (tuple, list)) and len(message) == 2:
                role, content = message
            else:
                role, content = "?", str(message)
            parts.append(f"{role}: {content}")
        text = "\n".join(parts)
        if limit and len(text) > limit:
            text = text[:limit] + "…"
        return text

    def compact(
        self, force: bool = False, max_words: Optional[int] = None
    ) -> Dict[str, Any]:
        """Replace the history with a single summarised pair.

        ``force=False`` is a no-op once the history is already a summary (<= 2
        messages).  Returns ``{"compacted", "before", "after", "words", "summary"}``.
        """
        max_words = max_words or int(config.get("AI_COMPACT_MAX_WORDS", "250"))
        if not force and len(self.messages) <= 2:
            return {
                "compacted": False,
                "before": len(self.messages),
                "after": len(self.messages),
                "words": 0,
                "summary": "",
            }
        summary = self._summarize(self.transcript(), max_words)
        before = len(self.messages)
        self.messages = [
            ("human", "Summary of the conversation so far:\n" + summary),
            ("ai", "Understood. I will continue using that summary as context."),
        ]
        self.compactions += 1
        return {
            "compacted": True,
            "before": before,
            "after": len(self.messages),
            "words": len(summary.split()),
            "summary": summary,
        }

    def _summarize(self, transcript: str, max_words: int) -> str:
        llm = getattr(self, "llm", None) or build_llm()
        prompt = _COMPACT_SYSTEM.format(max_words=max_words)
        try:
            response = llm.invoke([("system", prompt), ("human", transcript)])
            return str(getattr(response, "content", "") or "").strip()
        except Exception:  # noqa: BLE001 - fall back to a raw truncation
            return transcript[-4000:]


_COMPACT_SYSTEM = """You compress a working conversation into a compact summary that a \
follow-up assistant can continue from. Preserve precisely: table/row identifiers, \
product/category/sales document names and ids, decisions taken, any SQL already \
drafted (verbatim), and open questions. Drop pleasantries and already-resolved \
dead ends. Reply with a single prose summary of at most {max_words} words, no \
markdown headers. Keep any pending SQL inline, verbatim."""

