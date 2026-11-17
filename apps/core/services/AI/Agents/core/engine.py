"""Domain turn engine: the plan schema, guardrails and ``DomainManagerAgent``.

This is the real home of the cross-domain turn engine that used to live inside
``manager_general``:

- the plan schema (``SqlStep`` / ``ProductPlan``) and the ``/plan``-``/act`` SQL
  guardrail wrappers (``assert_plan_sql`` / ``assert_plan_read_only``, which
  delegate to the single :mod:`core.guardrail` implementation);
- the human-readable plan renderer (``format_plan``);
- the turn engine (``DomainManagerAgent``): ``plan()`` / ``stream()``, message
  building, result parsing and worker-result collection -- parameterised by each
  domain's tools and prompts.

Domain managers live in ``domains.product`` and ``domains.sales`` and subclass
``DomainManagerAgent``.  The conversation memory lives in :mod:`core.memory`;
command parsing and the general supervisor live in :mod:`core.router`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from . import guardrail as _guardrail
from .graph import build_tool_graph
from .guardrail import (
    assert_plan_read_only,
    assert_plan_sql,
)  # noqa: F401  (re-exports)
from . import tracing
from .llm import DEFAULT_MODEL, NUM_CTX, build_llm
from .memory import ConversationMemory
from .streaming import HEARTBEAT_SECONDS, stream_agent
from .contracts import (
    WorkerResult,
    as_dict_list,
    as_sql_list,
    as_str_list,
    extract_json_block,
)


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


def _normalise_plan(plan: Any) -> ProductPlan:
    """Coerce whatever the model returned into the ProductPlan shape."""
    if not isinstance(plan, dict):
        plan = {}
    return {
        "answer": str(plan.get("answer", "")),
        "summary": str(plan.get("summary", "")),
        "plan": as_str_list(plan.get("plan")),
        "sql": as_sql_list(plan.get("sql")),
        "missing_data": as_str_list(plan.get("missing_data")),
        "risks": as_str_list(plan.get("risks")),
        "worker_results": as_dict_list(plan.get("worker_results")),
        "drafts": as_sql_list(plan.get("drafts")),
    }


def _first_verb(statement: str) -> str:
    # Single guardrail: classify from the AST in core/guardrail.py.
    return _guardrail.first_verb(statement)


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


class DomainManagerAgent(ConversationMemory):
    """Shared turn engine for a single-domain manager (product / sales).

    Subclasses supply ``PLAN_TOOLS``, ``ACT_TOOLS``, ``PLAN_PROMPT`` and
    ``ACT_PROMPT``.  The agent graph is an explicit LangGraph tool-calling loop
    (``model`` -> ``tools`` -> ``model``, see :func:`core.graph.build_tool_graph`)
    backed by the local Ollama model.
    """

    PLAN_TOOLS = ()
    ACT_TOOLS = ()
    PLAN_PROMPT = ""
    ACT_PROMPT = ""

    # Tracing label for this domain; subclasses override it ("product"/"sales").
    DOMAIN = "agent"

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
        mode: str = "plan",
    ) -> None:
        super().__init__()
        tracing.configure_tracing()
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.mode = "act" if str(mode).lower() == "act" else "plan"
        self.llm = build_llm(
            self.model,
            temperature=self.temperature,
            num_ctx=NUM_CTX,
        )
        self._agent = None
        self._agent_mode: Optional[str] = None

    def _get_agent(self):
        if self._agent is None or self._agent_mode != self.mode:
            if self.mode == "act":
                tools, prompt = self.ACT_TOOLS, self.ACT_PROMPT
            else:
                tools, prompt = self.PLAN_TOOLS, self.PLAN_PROMPT
            self._agent = build_tool_graph(self.llm, tools, prompt)
            self._agent_mode = self.mode
        return self._agent

    def _run_config(self, action: str) -> Dict[str, Any]:
        """LangChain config for one turn: recursion limit + tracing identity."""
        cfg = tracing.run_config(
            run_name=f"{type(self).__name__}.{action}",
            domain=self.DOMAIN,
            mode=self.mode,
            model=self.model,
        )
        cfg["recursion_limit"] = self.recursion_limit
        return cfg

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
                "requests from data; draft SQL only for change requests. You could build "
                "SQL SELECT statements for review if appropriate."
            )
        return "\n".join(lines)

    @staticmethod
    # Return the row count and rows of the last successful SELECT query executed by the agent.
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
            data = extract_json_block(str(content))
            if not isinstance(data, dict) or "worker" in data:
                continue
            if (
                data.get("ok")
                and "query" in data
                and isinstance(data.get("rows"), list)
            ):
                rows = data["rows"]
                row_count = int(data.get("rowCount", len(rows)))
        return row_count, rows

    # Parse the agent result into a structured ProductPlan.
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
            structured = extract_json_block(text)

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
                plan["missing_data"].append(
                    f"[{wr.get('worker', '?')}] {wr.get('error')}"
                )
        return assert_plan_sql(plan, mode=self.mode)

    @staticmethod
    def _collect_worker_results(result: Dict[str, Any]):
        """Aggregate worker tool envelopes from the agent message history.

        Every tool message is parsed through the single ``WorkerResult`` contract,
        so a worker that fails (or returns a bare error envelope without a
        ``worker`` key) is never dropped silently: it lands in ``worker_results``
        and, via ``_parse_result``, in ``missing_data``.
        """
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
            envelope = WorkerResult.from_tool_content(content)
            if envelope is None:
                # A tool produced something that is neither a worker envelope nor
                # a plain SELECT result. Surface it instead of dropping it.
                data = extract_json_block(str(content))
                if isinstance(data, dict) and (
                    data.get("error") or data.get("ok") is False
                ):
                    envelope = WorkerResult(
                        worker=str(data.get("worker") or "unknown"),
                        ok=False,
                        error=str(data.get("error") or "worker returned no result"),
                        summary=str(data.get("summary", "")),
                    )
            if envelope is None:
                continue
            worker_results.append(envelope.to_dict())
            for step in envelope.sql:
                if _first_verb(getattr(step, "statement", "") or "") == "SELECT":
                    continue
                drafts.append(
                    {
                        "worker": envelope.worker,
                        "purpose": getattr(step, "purpose", "") or "",
                        "statement": getattr(step, "statement", "") or "",
                        "verification": getattr(step, "verification", "") or "",
                    }
                )
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

    # Public API for running agent turns: plan and stream.
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
            config=self._run_config("plan"),
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
        for event in stream_agent(
            agent,
            inp,
            config=self._run_config("stream"),
            heartbeat_seconds=HEARTBEAT_SECONDS,
        ):
            if event["type"] == "error":
                yield event
                return
            if event["type"] == "_done":
                last_state = event["state"]
                break
            yield event

        final_messages = list(last_state.get("messages", []) or [])
        if final_messages:
            self.messages = final_messages
        plan = self._parse_result(self._turn_messages(last_state, len(prior)))
        yield {"type": "plan", "plan": plan, "text": format_plan(plan)}
