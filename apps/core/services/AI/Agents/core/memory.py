"""Conversation memory shared by every manager agent.

``ConversationMemory`` holds the transcript as ``("human", text)`` /
``("ai", text)`` pairs: each turn is recorded as the user's request and a JSON
summary of the rendered plan, not as raw graph messages, so the transcript is
domain-agnostic, cheap to replay and safe to hand to a different domain manager.
Compaction replaces the whole history with a single summarised pair.

The small plan-shape coercion helpers (``as_str_list`` / ``as_sql_list`` /
``as_dict_list``) live in ``..contracts`` alongside the plan/sub-agent result
shapes, so
:mod:`core.engine` can import them without creating an import cycle back
through this module.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from apps.core import config

from .contracts import as_dict_list, as_sql_list, as_str_list
from .llm import build_llm

if TYPE_CHECKING:  # type annotations only; avoids an engine <-> memory cycle
    from .engine import DomainPlan


def _clip(text: Any, limit: int) -> str:
    """Collapse to a length-capped string for the history block."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _merged(*values: Any) -> List[str]:
    """De-duplicate a union of list-valued fields, preserving order."""
    out: List[str] = []
    for value in values:
        for item in as_str_list(value):
            if item not in out:
                out.append(item)
    return out


def render_turn(task: str, plan: "DomainPlan") -> str:
    """Render a finished turn as a readable block for the next turn's history.

    Concatenates the human request with the plan's ``answer``, ``summary``,
    ``plan`` steps, ``sql``, ``missing_data`` and ``risks``, plus the
    ``agent_results`` (per sub-agent status / summary / error / tables / queries),
    ``drafts`` and ``skipped_sql`` the top-level fields normally omit.  The
    top-level value wins; when it is empty the successful sub-agents fill the gap
    (the ``_parse_result`` fallback path leaves ``answer``/``summary``/``plan``/
    ``sql`` empty even though a sub-agent fetched the rows).  Raw
    ``queries[].rows`` payloads are deliberately excluded to keep history cheap,
    and the whole block is capped at ``AI_TURN_RENDER_MAX_CHARS``.  The human
    message and system prompt still instruct the model to answer with a single
    JSON object, and :meth:`core.engine.DomainManagerAgent._parse_result` falls
    back to the fetched rows if the model drifts into prose.
    """
    # ``agent_results`` is current; ``worker_results`` is accepted for one release
    # so plans cached before the rename still render into history.
    workers = as_dict_list(
        plan.get("agent_results")
        if "agent_results" in plan
        else plan.get("worker_results")
    )
    ok_workers = [w for w in workers if w.get("ok") and not w.get("error")]
    task_text = str(task or "").strip()
    lines: List[str] = []
    if task_text:
        lines.append(f"Human request: {task_text}")

    answer = str(plan.get("answer", "") or "").strip()
    if not answer:
        answer = "\n".join(
            _clip(w.get("answer"), 1200)
            for w in ok_workers
            if str(w.get("answer", "") or "").strip()
        )
    if answer:
        lines.append(f"Answer: {answer}")

    summary = str(plan.get("summary", "") or "").strip()
    if not summary:
        summary = next(
            (
                str(w.get("summary", "") or "").strip()
                for w in ok_workers
                if str(w.get("summary", "") or "").strip()
            ),
            "",
        )
    if summary:
        lines.append(f"Summary: {_clip(summary, 600)}")

    steps = as_str_list(plan.get("plan"))
    if not steps:
        for w in ok_workers:
            for step in as_str_list(w.get("plan")):
                steps.append(f"[{w.get('agent') or w.get('worker') or '?'}] {step}")
    if steps:
        lines.append("Plan:")
        lines.extend(f"  {i + 1}. {step}" for i, step in enumerate(steps))

    sql_steps = as_sql_list(plan.get("sql"))
    if not sql_steps:
        for w in ok_workers:
            for step in as_sql_list(w.get("sql")):
                if step.get("statement"):
                    sql_steps.append(
                        {
                            "purpose": f"[{w.get('agent') or w.get('worker') or '?'}] "
                            + step.get("purpose", ""),
                            "statement": step.get("statement", ""),
                            "verification": step.get("verification", ""),
                        }
                    )
    for step in sql_steps:
        statement = str(step.get("statement", "") or "").strip()
        if not statement:
            continue
        purpose = str(step.get("purpose", "") or "").strip()
        lines.append(f"SQL ({purpose}):" if purpose else "SQL:")
        lines.append("  " + _clip(statement, 1500).replace("\n", "\n  "))
        verification = str(step.get("verification", "") or "").strip()
        if verification:
            lines.append("  verify: " + _clip(verification, 300).replace("\n", "\n  "))
    drafts = as_sql_list(plan.get("drafts"))
    if drafts:
        lines.append("Drafts (not executed):")
        for step in drafts:
            statement = str(step.get("statement", "") or "").strip()
            if not statement:
                continue
            label = str(step.get("agent") or step.get("worker") or "").strip()
            purpose = _clip(step.get("purpose", ""), 300)
            if label:
                lines.append(f"  - [{label}] {purpose}")
            else:
                lines.append(f"  - {purpose}")
            lines.append("    " + _clip(statement, 600).replace("\n", "\n    "))

    if workers:
        lines.append("Sub-agents:")
        for w in workers:
            name = str(w.get("agent") or w.get("worker") or "?").strip()
            flags = ["FAILED"] if (not w.get("ok") or w.get("error")) else ["ok"]
            if w.get("executed"):
                flags.append("executed")
            row_counts = [
                int(q.get("rowCount", 0) or 0) for q in as_dict_list(w.get("queries"))
            ]
            if row_counts:
                flags.append(f"{sum(row_counts)} row(s)")
            head = f"  - [{name}] {', '.join(flags)}"
            worker_summary = _clip(w.get("summary"), 300)
            lines.append(f"{head}: {worker_summary}" if worker_summary else head)
            if w.get("error"):
                lines.append(f"    error: {_clip(w['error'], 300)}")

    tables: List[str] = []
    for w in workers:
        for table in as_str_list(w.get("tables_used")):
            if table not in tables:
                tables.append(table)
    if tables:
        suffix = " …" if len(tables) > 20 else ""
        lines.append("Tables: " + ", ".join(tables[:20]) + suffix)

    query_lines: List[str] = []
    for w in workers:
        for q in as_dict_list(w.get("queries")):
            statement = " ".join(str(q.get("query", "") or "").split())
            if not statement:
                continue
            count = int(q.get("rowCount", 0) or 0)
            query_lines.append(f"  - {count} row(s): {_clip(statement, 160)}")
    if query_lines:
        lines.append("Queries:")
        lines.extend(query_lines)

    missing = _merged(
        plan.get("missing_data"), *[w.get("missing_data") for w in workers]
    )
    if missing:
        lines.append("Missing data:")
        lines.extend(f"  - {item}" for item in missing)

    risks = _merged(plan.get("risks"), *[w.get("risks") for w in workers])
    if risks:
        lines.append("Risks:")
        lines.extend(f"  - {item}" for item in risks)

    skipped = as_dict_list(plan.get("skipped_sql"))
    if skipped:
        lines.append("Skipped statements:")
        for entry in skipped:
            statement = " ".join(str(entry.get("statement", "") or "").split())
            if not statement:
                continue
            lines.append(f"  - {entry.get('verb', '?')}: {_clip(statement, 200)}")

    text = "\n".join(lines)
    limit = int(config.get("AI_TURN_RENDER_MAX_CHARS", "4000"))
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "\n… (turn truncated)"
    return text


class ConversationMemory:
    """Conversation state shared by every manager agent.

    ``messages`` holds ``("human", text)`` / ``("ai", text)`` pairs: each turn is
    recorded as the user's request and the rendered plan (``format_plan``), not as
    raw graph messages, so the transcript is domain-agnostic, cheap to replay and
    safe to hand to a different domain manager.
    """

    def __init__(self, conversation_id: Optional[str] = None) -> None:
        self.messages: List[Any] = []
        self.compactions: int = 0
        # One id per running conversation; shared with the domain managers so
        # every turn traces into the same LangSmith thread.  ``/new`` rotates it.
        self.conversation_id: str = conversation_id or str(uuid.uuid4())

    def reset(self) -> None:
        """Forget the conversation history (the agents and prompts stay loaded).

        Also rotates ``conversation_id`` so the next turn starts a fresh
        LangSmith thread instead of appending to the cleared one.
        """
        self.messages = []
        self.conversation_id = str(uuid.uuid4())

    def history(self) -> List[Any]:
        """Return a copy of the conversation to replay before the next turn."""
        return list(self.messages)

    def record(self, task: str, plan: DomainPlan) -> None:
        """Append a finished turn to the history as ``(human, ai)`` pairs.

        The assistant turn is stored as the readable :func:`render_turn` block
        (human request + answer + summary + plan + missing data/risks), so the
        next turn replays the real context rather than a bare JSON blob.  The
        human message and system prompt still demand a single JSON object, and
        ``_parse_result`` falls back to the fetched rows if the model drifts
        into prose.
        """
        self.messages.append(("human", str(task)))
        self.messages.append(("ai", render_turn(task, plan)))

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
