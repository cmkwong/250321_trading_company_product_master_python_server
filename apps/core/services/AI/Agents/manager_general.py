"""General manager agent: shared conversation memory, the turn engine and routing.

This module is the single home for the cross-domain manager logic that used to
live inside ``manager_product``:

- the conversation memory (``ConversationMemory``): ``messages``, ``reset()``,
  history replay, and ``compact()`` summarisation;
- the turn engine (``DomainManagerAgent``): ``plan()`` / ``stream()``,
  message building, result parsing and worker-result collection -- parameterised
  by each domain's tools and prompts;
- the plan schema and guardrail (``SqlStep`` / ``ProductPlan``,
  ``assert_plan_sql``, ``format_plan``) and the ``/plan`` / ``/act`` command
  parsing (``parse_mode``, ``parse_command``);
- the general supervisor (``GeneralManagerAgent``) that owns a running
  conversation, routes each request to the ``product`` or ``sales`` domain
  manager, executes approved plans (``/act``) and compacts the history
  (``/compact``).

Domain managers live in ``manager_product`` and ``manager_sales``; they are
imported lazily inside :meth:`GeneralManagerAgent._manager` so this module has
no import cycle with them.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from langchain.agents import create_agent

from apps.core import config
from . import worker_base as _base
from apps.core.services.AI.Tools import utils as _tool_utils
from apps.core.services.AI.Tools.act_tools import execute_plan


class SqlStep(TypedDict, total=False):
    purpose: str
    statement: str
    verification: str


class ProductPlan(TypedDict, total=False):
    answer: str
    summary: str
    plan: List[str]
    sql: List[SqlStep]
    missing_data: List[str]
    risks: List[str]
    worker_results: List[Dict[str, Any]]
    drafts: List[SqlStep]


DEFAULT_MODEL = config.get("OLLAMA_MODEL", "qwen3.5")
NUM_CTX = int(config.get("OLLAMA_NUM_CTX", "32768"))

# How long stream() waits for the next agent event before emitting a
# "waiting for model..." heartbeat, so a slow prefill / tool call doesn't
# read as a hang. Tests override this module constant to a tiny value.
HEARTBEAT_SECONDS = 5.0


# Statements the agent may never emit in any mode (hard guardrail, applied after
# the model responds and independent of whatever the model was told).  UPDATE and
# DELETE are NOT listed here: they are gated per-mode by assert_plan_sql below.
_FORBIDDEN_SQL_VERBS = (
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "RENAME",
    "REPLACE",
)

# Verbs allowed per mode. Plan mode drafts INSERT/UPDATE for review; act mode
# additionally allows DELETE (and executes everything).
_MODE_WRITE_VERBS = {
    "plan": ("SELECT", "INSERT", "UPDATE"),
    "act": ("SELECT", "INSERT", "UPDATE", "DELETE"),
}

# Leading /plan or /act command token (only honoured at the start of the text,
# so a URL or path containing "/act" is not mistaken for a command).
_MODE_RE = re.compile(r"^\s*/(plan|act)\b[\s:,\-]*", re.IGNORECASE)


def parse_mode(text: Any, default: str = "plan") -> tuple:
    """Split an optional leading ``/plan`` or ``/act`` command from the request text.

    Returns ``(mode, task)``. ``mode`` is ``"plan"`` or ``"act"``; ``task`` is the
    remainder of the text with the command token stripped.  Text without a token
    defaults to ``plan`` (read-only), so nothing executes by accident.
    """
    text = str(text or "")
    match = _MODE_RE.match(text)
    if match:
        return match.group(1).lower(), _MODE_RE.sub("", text, count=1).strip()
    default = str(default).lower()
    if default not in ("plan", "act"):
        default = "plan"
    return default, text.strip()


def _normalise_plan(plan: Any) -> ProductPlan:
    """Coerce whatever the model returned into the ProductPlan shape."""
    if not isinstance(plan, dict):
        plan = {}
    return {
        "answer": str(plan.get("answer", "")),
        "summary": str(plan.get("summary", "")),
        "plan": _as_str_list(plan.get("plan")),
        "sql": _as_sql_list(plan.get("sql")),
        "missing_data": _as_str_list(plan.get("missing_data")),
        "risks": _as_str_list(plan.get("risks")),
        "worker_results": _as_dict_list(plan.get("worker_results")),
        "drafts": _as_sql_list(plan.get("drafts")),
    }


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _as_sql_list(value: Any) -> List[SqlStep]:
    if not isinstance(value, list):
        return []
    steps = []
    for item in value:
        if isinstance(item, str):
            steps.append({"purpose": "", "statement": item, "verification": ""})
        elif isinstance(item, dict):
            steps.append(
                {
                    "purpose": str(item.get("purpose", "")),
                    "statement": str(item.get("statement", "")),
                    "verification": str(item.get("verification", "")),
                }
            )
    return steps


def _first_verb(statement: str) -> str:
    match = re.match(r"\s*(--[^\n]*\n)?\s*([A-Za-z]+)", statement or "")
    return match.group(2).upper() if match else ""


def assert_plan_sql(plan: ProductPlan, *, mode: str = "plan") -> ProductPlan:
    """Enforce the SQL guardrail for a mode.

    - ``plan`` mode: SELECT / INSERT / UPDATE are kept (writes are shown for the
      human to review before switching to ``/act``).
    - ``act`` mode: SELECT / INSERT / UPDATE / DELETE are kept.
    - both modes: DDL is always removed, and every write must target a table in
      ``WRITE_ALLOWED_TABLES``.

    Removed statements are reported in ``risks`` and mirrored in a new
    ``skipped_sql`` list so the response can explain exactly what was dropped.
    """
    mode = "act" if str(mode).lower() == "act" else "plan"
    allowed_verbs = _MODE_WRITE_VERBS[mode]

    safe: List[SqlStep] = []
    skipped: List[Dict[str, Any]] = []
    risks = list(plan.get("risks", []))

    for step in plan.get("sql", []):
        statement = step.get("statement", "")
        verb = _first_verb(statement)

        if verb in _FORBIDDEN_SQL_VERBS:
            risks.append(f"Removed unsafe SQL ({verb}): {statement[:120]}")
            skipped.append({"verb": verb, "statement": statement})
            continue

        if verb in ("INSERT", "UPDATE", "DELETE"):
            if verb not in allowed_verbs:
                risks.append(
                    f"Removed {verb} (not allowed in {mode} mode): {statement[:120]}"
                )
                skipped.append({"verb": verb, "statement": statement})
                continue
            target = _tool_utils.write_target(statement)
            if target is None or target not in _tool_utils.ai_settings().WRITE_ALLOWED_TABLES:
                risks.append(f"Removed write outside allowed tables: {statement[:120]}")
                skipped.append({"verb": verb, "statement": statement})
                continue

        if statement.strip() or step.get("purpose") or step.get("verification"):
            safe.append(step)

    plan["sql"] = safe
    plan["skipped_sql"] = skipped
    plan["risks"] = risks
    return plan


def assert_plan_read_only(plan: ProductPlan) -> ProductPlan:
    """Backward-compatible alias for the plan-mode guardrail."""
    return assert_plan_sql(plan, mode="plan")


def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first {...} JSON object out of free-form model text."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def _row_label(row: Any) -> str:
    """Best-effort human label for one result row."""
    if not isinstance(row, dict):
        return str(row)
    for key in ("name", "label", "code", "value"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    for key, val in row.items():
        if str(key).lower() not in ("id",) and val not in (None, ""):
            return str(val)
    return str(row)


def _rows_answer(row_count: int, rows: List[Dict[str, Any]]) -> str:
    """Deterministic fallback answer derived from actual fetched rows."""
    if not rows:
        return "none"
    labels = [_row_label(row) for row in rows[:200]]
    return f"{row_count} row(s): " + ", ".join(labels)


def _chunk_text(message: Any) -> str:
    """Return the plain text of a streamed message chunk (str or block list)."""
    content = getattr(message, "content", "") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def format_plan(
    plan: ProductPlan,
    executed: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render a plan as human-readable text.

    When ``executed`` (the result of ``act_tools.execute_plan``) is supplied, an
    ``Execution:`` block is appended.  In plan mode (no ``executed``), a footer
    reminds the user that write statements are only run via ``/act``.
    """
    lines = []
    if plan.get("answer"):
        lines.append(f"Answer: {plan['answer']}")
    lines.append(f"Summary: {plan.get('summary', '')}")
    if plan.get("plan"):
        lines.append("\nPlan:")
        lines.extend(f"  {i + 1}. {step}" for i, step in enumerate(plan["plan"]))
    if plan.get("sql"):
        lines.append("\nSQL:")
        for step in plan["sql"]:
            if step.get("purpose"):
                lines.append(f"  -- {step['purpose']}")
            if step.get("statement"):
                lines.append("  " + step["statement"].replace("\n", "\n  "))
            if step.get("verification"):
                lines.append("  -- verify:")
                lines.append("  " + step["verification"].replace("\n", "\n  "))
    if plan.get("missing_data"):
        lines.append("\nMissing data:")
        lines.extend(f"  - {item}" for item in plan["missing_data"])
    if plan.get("worker_results"):
        for wr in plan["worker_results"]:
            status = "executed" if wr.get("executed") else "NOT executed"
            lines.append(f"\n[{wr.get('worker', '?')}] {wr.get('summary', '')}")
            lines.append(f"  (status: {status})")
            if wr.get("answer"):
                lines.append(f"  Answer: {wr['answer']}")
            if wr.get("error"):
                lines.append(f"  error: {wr['error']}")
            for query in wr.get("queries", []):
                lines.append(f"  Executed: {query.get('rowCount', 0)} row(s)")
            data = wr.get("data") or wr.get("fields") or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if value not in (None, ""):
                        lines.append(f"  {key}: {value}")
            for step in wr.get("sql", []):
                if step.get("statement"):
                    lines.append("  " + step["statement"].replace("\n", "\n  "))
    if plan.get("drafts"):
        lines.append("\nDrafts (not executed):")
        for step in plan["drafts"]:
            if step.get("worker"):
                lines.append(f"  -- [{step['worker']}] {step.get('purpose', '')}")
            if step.get("statement"):
                lines.append("  " + step["statement"].replace("\n", "\n  "))
            if step.get("verification"):
                lines.append("  -- verify:")
                lines.append("  " + step["verification"].replace("\n", "\n  "))
    if executed:
        lines.append("\nExecution:")
        for entry in executed:
            verb = entry.get("verb", "?")
            if entry.get("skipped"):
                status = "SKIPPED"
            elif entry.get("ok"):
                status = "OK"
            else:
                status = "FAILED"
            line = f"  - {verb} {status}"
            if entry.get("affectedRows") is not None:
                line += f" (affected {entry['affectedRows']})"
            if entry.get("needs_confirmation"):
                line += " [needs confirmation]"
            lines.append(line)
            if entry.get("error"):
                lines.append(f"    error: {entry['error']}")
    elif plan.get("sql"):
        write_count = sum(
            1
            for step in plan["sql"]
            if _first_verb(step.get("statement", "")) != "SELECT"
        )
        if write_count:
            lines.append(
                f"\nRun with /act to execute ({write_count} write statement(s) above)."
            )
    if plan.get("risks"):
        lines.append("\nRisks:")
        lines.extend(f"  - {item}" for item in plan["risks"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Conversation memory
# --------------------------------------------------------------------------- #
def _plan_summary(plan: ProductPlan) -> Dict[str, Any]:
    """Trim a plan to the fields worth keeping in the conversation history."""
    return {
        "answer": str(plan.get("answer", "")),
        "summary": str(plan.get("summary", "")),
        "plan": _as_str_list(plan.get("plan")),
        "sql": _as_sql_list(plan.get("sql")),
        "missing_data": _as_str_list(plan.get("missing_data")),
        "risks": _as_str_list(plan.get("risks")),
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

    def record(self, task: str, plan: ProductPlan) -> None:
        """Append a finished turn to the history as (human, JSON summary) pairs.

        The assistant turn is stored as a JSON object (not the prose
        ``format_plan``) so the replayed assistant message stays consistent with
        the agents' "return exactly one JSON object" output contract.  Storing
        prose would make the model imitate the "Answer:/Summary:" layout on the
        next turn instead of emitting parseable JSON.
        """
        self.messages.append(("human", str(task)))
        self.messages.append(("ai", json.dumps(_plan_summary(plan), ensure_ascii=False)))

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

    def compact(self, force: bool = False, max_words: Optional[int] = None) -> Dict[str, Any]:
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
        llm = getattr(self, "llm", None) or _base.build_llm()
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


# --------------------------------------------------------------------------- #
# Domain turn engine
# --------------------------------------------------------------------------- #
class DomainManagerAgent(ConversationMemory):
    """Shared turn engine for a single-domain manager (product / sales).

    Subclasses supply ``PLAN_TOOLS``, ``ACT_TOOLS``, ``PLAN_PROMPT`` and
    ``ACT_PROMPT``.  The agent graph is a LangChain tool-calling agent backed by
    the local Ollama model.
    """

    PLAN_TOOLS = ()
    ACT_TOOLS = ()
    PLAN_PROMPT = ""
    ACT_PROMPT = ""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
        mode: str = "plan",
    ) -> None:
        super().__init__()
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.mode = "act" if str(mode).lower() == "act" else "plan"
        self.llm = _base.build_llm(
            self.model,
            temperature=self.temperature,
            num_ctx=NUM_CTX,
        )
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            if self.mode == "act":
                tools, prompt = self.ACT_TOOLS, self.ACT_PROMPT
            else:
                tools, prompt = self.PLAN_TOOLS, self.PLAN_PROMPT
            self._agent = create_agent(
                model=self.llm,
                tools=tools,
                system_prompt=prompt,
            )
        return self._agent

    def _build_human_message(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        *,
        plan_text: Optional[str] = None,
    ) -> str:
        """Build the human-readable message to send to the agent."""
        lines = ["Task:", task or ""]
        if links:
            lines.append("\nProduct links (1688):")
            lines.extend(f"  {i + 1}. {link}" for i, link in enumerate(links))
        if images:
            lines.append("\nProduct images (paths or data URLs):")
            lines.extend(f"  {i + 1}. {img}" for i, img in enumerate(images))
        if context:
            lines.append("\nAdditional context:")
            for key, value in context.items():
                lines.append(f"  {key}: {value}")
        if plan_text:
            lines.append("\nApproved plan (execute verbatim):")
            lines.append(plan_text)
        if self.mode == "act":
            lines.append(
                "\nYou are in ACT mode. Execute the approved plan above using the "
                "execute_* tools and report the actual affected rows. Do not invent "
                "new statements. If the approved plan contains no SQL, report its "
                "result and do not invent statements."
            )
        else:
            lines.append(
                "\nUse the tool or worker that fits the request. Answer informational "
                "requests from data; draft SQL only for change requests. Do not "
                "execute anything; write SQL for review."
            )
        return "\n".join(lines)


    @staticmethod
    def _last_select_rows(result: Dict[str, Any]) -> tuple:
        """Return (rowCount, rows) of the last successful execute_select_query tool message."""
        rows: List[Dict[str, Any]] = []
        row_count = 0
        for message in result.get("messages", []):
            if getattr(message, "type", None) != "tool":
                continue
            content = getattr(message, "content", "") or ""
            if isinstance(content, list):
                content = "".join(
                    str(part) if isinstance(part, str) else str(part.get("text", ""))
                    for part in content
                )
            data = _extract_json_block(str(content))
            if not isinstance(data, dict) or "worker" in data:
                continue
            if data.get("ok") and "query" in data and isinstance(data.get("rows"), list):
                rows = data["rows"]
                row_count = int(data.get("rowCount", len(rows)))
        return row_count, rows

    def _parse_result(self, result: Dict[str, Any]) -> ProductPlan:
        """Normalise an agent result dict into a guarded ProductPlan."""
        structured = result.get("structured_response")
        if structured is None:
            # Fallback: parse the last AI message as JSON.
            messages = result.get("messages", [])
            text = ""
            for message in reversed(messages):
                if getattr(message, "type", None) == "ai":
                    content = getattr(message, "content", "") or ""
                    if isinstance(content, list):
                        content = "".join(
                            (
                                str(part)
                                if isinstance(part, str)
                                else str(part.get("text", ""))
                            )
                            for part in content
                        )
                    text = str(content)
                    if text.strip():
                        break
            structured = _extract_json_block(text)

        if structured is None:
            # The model ended its turn without a JSON reply (a known qwen failure
            # mode right after tool calls). Rebuild the answer from any rows it
            # actually fetched instead of silently returning a blank plan.
            row_count, rows = self._last_select_rows(result)
            plan = _normalise_plan(None)
            if rows:
                plan["answer"] = _rows_answer(row_count, rows)
                plan["summary"] = (
                    f"{row_count} row(s) returned by execute_select_query."
                )
                plan["risks"] = [
                    "The model produced no structured reply; the answer was rebuilt "
                    "from the rows of the executed SELECT."
                ]
            else:
                plan["risks"] = [
                    "The model produced no structured reply and no executed SELECT "
                    "rows were available."
                ]
        else:
            plan = _normalise_plan(structured)

        plan["worker_results"], plan["drafts"] = self._collect_worker_results(result)
        for wr in plan["worker_results"]:
            if not wr.get("ok") and wr.get("error"):
                plan["missing_data"].append(f"[{wr.get('worker', '?')}] {wr.get('error')}")
        return assert_plan_sql(plan, mode=self.mode)

    @staticmethod
    def _collect_worker_results(result: Dict[str, Any]):
        """Aggregate worker tool envelopes from the agent message history."""
        worker_results: List[Dict[str, Any]] = []
        drafts: List[SqlStep] = []
        for message in result.get("messages", []):
            if getattr(message, "type", None) != "tool":
                continue
            content = getattr(message, "content", "") or ""
            if isinstance(content, list):
                content = "".join(
                    str(part) if isinstance(part, str) else str(part.get("text", ""))
                    for part in content
                )
            data = _extract_json_block(str(content))
            if not isinstance(data, dict) or not data.get("worker"):
                continue
            worker_results.append(data)
            for step in _as_sql_list(data.get("sql")):
                # An answer SELECT has already been executed by the worker; only
                # write statements belong in the pending /act drafts.
                if _first_verb(step.get("statement", "")) == "SELECT":
                    continue
                drafts.append({"worker": data.get("worker"), **step})
        return worker_results, drafts

    @staticmethod
    def _turn_messages(result: Dict[str, Any], start: int) -> Dict[str, Any]:
        """Copy ``result`` with ``messages`` limited to the current turn.

        The graph replays the whole conversation, so dropping the ``start``
        messages that were already there keeps ``_collect_worker_results`` from
        re-collecting drafts produced by earlier turns.
        """
        turn = dict(result)
        turn["messages"] = list(result.get("messages", []) or [])[start:]
        return turn

    def plan(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Any]] = None,
    ) -> ProductPlan:
        """Run one agent turn and return a normalised, read-only-guarded plan.

        ``history`` (or, by default, the conversation kept in ``self.messages``)
        is replayed before the new human message so follow-up requests keep their
        context.  The plan is built from this turn's messages only.
        """
        agent = self._get_agent()
        prior = list(self.messages if history is None else history)
        result = agent.invoke(
            {
                "messages": [
                    *prior,
                    ("human", self._build_human_message(task, links, images, context)),
                ]
            },
            config={"recursion_limit": self.recursion_limit},
        )
        self.messages = list(result.get("messages", []) or [])
        return self._parse_result(self._turn_messages(result, len(prior)))


    def stream(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Any]] = None,
    ):
        """Run one agent turn, yielding live events, then the guarded plan.

        Yields dicts: ``{"type": "status"|"token"|"plan", ...}``.  The final
        ``plan`` event carries the guardrail-checked plan and its formatted text.

        ``history`` (or, by default, the conversation kept in ``self.messages``)
        is replayed before the new human message, so a caller can drive a
        multi-turn conversation on one agent.  When the turn finishes
        ``self.messages`` holds the whole conversation for the next turn.

        The underlying graph is consumed on a background thread so we can emit a
        ``status`` heartbeat while the model is prefilling or a tool is executing
        (both otherwise produce no events and read as a hang).
        """
        agent = self._get_agent()
        prior = list(self.messages if history is None else history)
        inp = {
            "messages": [
                *prior,
                ("human", self._build_human_message(task, links, images, context)),
            ]
        }
        last_state: Dict[str, Any] = {}
        announced = set()
        q = queue.Queue()

        def _produce() -> None:
            try:
                for item in agent.stream(
                    inp,
                    config={"recursion_limit": self.recursion_limit},
                    stream_mode=["messages", "values"],
                ):
                    q.put(("item", item))
                q.put(("done", None))
            except Exception as exc:  # noqa: BLE001 - surface as a terminal event
                q.put(("error", exc))

        threading.Thread(target=_produce, daemon=True).start()

        idle_since = time.monotonic()
        while True:
            try:
                kind, payload = q.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                idle = int(time.monotonic() - idle_since)
                yield {"type": "status", "text": f"waiting for model… ({idle}s)"}
                continue

            idle_since = time.monotonic()

            if kind == "done":
                break
            if kind == "error":
                yield {"type": "error", "error": str(payload)}
                return

            mode, data = payload
            if mode == "messages":
                chunk, metadata = data
                if metadata.get("langgraph_node") != "model":
                    continue
                for tc in getattr(chunk, "tool_call_chunks", None) or []:
                    tc = tc or {}
                    name = tc.get("name") if isinstance(tc, dict) else None
                    idx = tc.get("index") if isinstance(tc, dict) else None
                    key = idx if idx is not None else name
                    if name and key not in announced:
                        announced.add(key)
                        yield {"type": "status", "text": f"calling tool: {name}"}
                text = _chunk_text(chunk)
                if text:
                    yield {"type": "token", "text": text}
            else:  # "values": full state snapshot
                last_state = data

        final_messages = list(last_state.get("messages", []) or [])
        if final_messages:
            self.messages = final_messages
        plan = self._parse_result(self._turn_messages(last_state, len(prior)))
        yield {"type": "plan", "plan": plan, "text": format_plan(plan)}


# --------------------------------------------------------------------------- #
# Slash-command parsing
# --------------------------------------------------------------------------- #
@dataclass
class SlashCommand:
    """A parsed interactive command line (see :func:`parse_command`)."""

    name: str = "empty"  # empty|quit|reset|compact|help|dry|live|message
    mode: str = "plan"  # plan|act
    domain: Optional[str] = None  # product|sales override, or None to route
    text: str = ""  # the request text (mode/domain tokens stripped)


_QUIT_TOKENS = {"/q", "q", "/quit", "quit", "/exit", "exit"}
_RESET_TOKENS = {"/new", "/reset", "/clear"}
_DRY_TOKENS = {"/dry", "/dryrun", "/dry-run"}
_HELP_TOKENS = {"/help", "?"}
# Leading domain override: /product, /sales, @product, @sales.
_DOMAIN_RE = re.compile(r"^\s*(?:/(product|sales)|@(product|sales))\b[\s:,\-]*", re.IGNORECASE)


def parse_command(raw: str) -> SlashCommand:
    """Parse an interactive REPL line into a :class:`SlashCommand`.

    Handles ``/q``, ``/new``, ``/compact``, ``/dry``, ``/live``, ``/help``,
    a leading ``/plan`` or ``/act`` mode, and a ``/product`` | ``/sales`` |
    ``@product`` | ``@sales`` domain override.  Anything else is a plain message
    (plan mode by default).
    """
    text = (raw or "").strip()
    low = text.lower()
    if not text:
        return SlashCommand(name="empty")
    if low in _QUIT_TOKENS:
        return SlashCommand(name="quit")
    if low in _RESET_TOKENS:
        return SlashCommand(name="reset")
    if low in _DRY_TOKENS:
        return SlashCommand(name="dry")
    if low == "/live":
        return SlashCommand(name="live")
    if low == "/compact":
        return SlashCommand(name="compact")
    if low in _HELP_TOKENS:
        return SlashCommand(name="help")

    domain: Optional[str] = None
    domain_match = _DOMAIN_RE.match(text)
    if domain_match:
        domain = (domain_match.group(1) or domain_match.group(2)).lower()
        text = _DOMAIN_RE.sub("", text, count=1).strip()

    mode, task = parse_mode(text, default="plan")
    return SlashCommand(name="message", mode=mode, domain=domain, text=task)


# --------------------------------------------------------------------------- #
# General supervisor
# --------------------------------------------------------------------------- #
_ROUTER_SYSTEM = """Classify the user's request into exactly one domain.
- product: product master data (categories, attributes, dropdown values, name \
types, products, prices, costs, images, 1688 links, keywords).
- sales: quotations, sales documents, packing, shipping, customers, invoicing, \
incoterms, freight.
If the request is a follow-up to the recent conversation, keep the same domain \
unless the topic clearly changes.
Reply with JSON only: {"domain": "product"|"sales", "reason": "<short>"}"""

_SALES_KEYWORDS = (
    "quotation", "invoice", "packing", "shipping", "shipment", "customer",
    "order", "sales", "incoterm", "freight", "cbm", "carton",
)
_PRODUCT_KEYWORDS = (
    "category", "attribute", "product", "sku", "price", "cost", "image",
    "1688", "name", "material", "color", "size", "capacity", "dropdown",
    "keyword", "certificate",
)


class GeneralManagerAgent(ConversationMemory):
    """Supervisor agent that owns the conversation and routes to a domain manager.

    ``pinned="product"`` skips the router entirely (the ``-mp`` CLI); otherwise
    each request is classified and dispatched to ``product`` or ``sales``.
    """

    DOMAINS = ("product", "sales")

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
        pinned: Optional[str] = None,
        router: Optional[str] = None,
        llm: Any = None,
    ) -> None:
        super().__init__()
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.pinned = pinned
        self.router = router or str(config.get("AI_GENERAL_ROUTER", "llm")).strip().lower()
        self.llm = llm or _base.build_llm(
            self.model, temperature=self.temperature, num_ctx=NUM_CTX
        )
        self._managers: Dict[str, Any] = {}
        self.last_domain: str = pinned or "product"
        self.last_plan: Optional[ProductPlan] = None

    def reset(self) -> None:
        """Forget the conversation and the last plan (agents stay loaded)."""
        super().reset()
        self.last_plan = None
        self.last_domain = self.pinned or "product"

    # -- domain managers ----------------------------------------------------
    def _manager(self, domain: str):
        domain = domain or self.last_domain or "product"
        if domain not in self._managers:
            self._managers[domain] = self._build_manager(domain)
        return self._managers[domain]

    def _build_manager(self, domain: str):
        if domain == "product":
            from apps.core.services.AI.Agents.manager_product import ProductMasterAgent
            return ProductMasterAgent(
                model=self.model,
                temperature=self.temperature,
                recursion_limit=self.recursion_limit,
            )
        if domain == "sales":
            from apps.core.services.AI.Agents.manager_sales import SalesManagerAgent
            return SalesManagerAgent(
                model=self.model,
                temperature=self.temperature,
                recursion_limit=self.recursion_limit,
            )
        raise ValueError(f"Unknown domain: {domain!r}")

    # -- routing ------------------------------------------------------------
    def classify(self, task: str) -> str:
        """Pick the domain for a request (explicit overrides skip this)."""
        if self.pinned:
            return self.pinned
        if self.router == "keyword":
            return self._keyword_classify(task) or self.last_domain

        recent = self.transcript(limit=3000)
        try:
            response = self.llm.invoke(
                [
                    ("system", _ROUTER_SYSTEM),
                    ("human", f"Recent conversation:\n{recent}\n\nNew request: {task}"),
                ]
            )
            data = _extract_json_block(str(getattr(response, "content", "") or ""))
            domain = str((data or {}).get("domain", "")).strip().lower()
            if domain in self.DOMAINS:
                self.last_domain = domain
                return domain
        except Exception:  # noqa: BLE001 - fall back to keywords
            pass
        return self._keyword_classify(task) or self.last_domain

    @staticmethod
    def _keyword_classify(task: str) -> Optional[str]:
        text = str(task).lower()
        sales = any(k in text for k in _SALES_KEYWORDS)
        product = any(k in text for k in _PRODUCT_KEYWORDS)
        if sales and not product:
            return "sales"
        if product and not sales:
            return "product"
        if sales:
            return "sales"
        if product:
            return "product"
        return None


    # -- turns --------------------------------------------------------------
    def _maybe_auto_compact(self) -> Optional[Dict[str, Any]]:
        if str(config.get("AI_AUTO_COMPACT", "1")).strip().lower() not in ("1", "true", "yes"):
            return None
        limit = int(config.get("AI_HISTORY_MAX_CHARS", "48000"))
        if len(self.transcript()) > limit:
            return self.compact(force=True)
        return None

    def stream(self, task: str, domain: Optional[str] = None):
        """Run one turn through the routed domain manager, yielding events.

        Emits the same ``status``/``token``/``plan``/``error`` events as the
        domain managers, prefixed by a ``status`` naming the chosen domain.  The
        finished turn is recorded into the shared conversation memory.
        """
        if not str(task).strip():
            yield {"type": "status", "text": "empty request"}
            return
        self._maybe_auto_compact()
        domain = domain or self.classify(task)
        self.last_domain = domain
        sub = self._manager(domain)
        yield {"type": "status", "text": f"domain: {domain}"}
        plan: Optional[ProductPlan] = None
        for event in sub.stream(task, history=self.history()):
            if event["type"] == "plan":
                plan = event["plan"]
            yield event
        if plan is not None:
            self.record(task, plan)

    def plan(self, task: str, domain: Optional[str] = None) -> ProductPlan:
        """Run one routed turn and return the guarded plan (no token streaming)."""
        plan: Optional[ProductPlan] = None
        for event in self.stream(task, domain=domain):
            if event["type"] == "plan":
                plan = event["plan"]
        return plan or {}

    # -- act ----------------------------------------------------------------
    def act(
        self,
        plan: ProductPlan,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ):
        """Guard and execute an approved plan; return ``(plan, executed)``."""
        plan = assert_plan_sql(plan, mode="act")
        executed = execute_plan(plan, dry_run=dry_run, confirm=confirm)
        return plan, executed


__all__ = [
    "SqlStep",
    "ProductPlan",
    "DEFAULT_MODEL",
    "NUM_CTX",
    "HEARTBEAT_SECONDS",
    "parse_mode",
    "parse_command",
    "SlashCommand",
    "assert_plan_sql",
    "assert_plan_read_only",
    "format_plan",
    "ConversationMemory",
    "DomainManagerAgent",
    "GeneralManagerAgent",
]









