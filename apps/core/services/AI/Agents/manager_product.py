"""Product-master maintenance agent (LangChain + Ollama qwen3.5).

Given a task (optionally with 1688 product links), the agent reads the product
master database via the Node server's REST API and produces a maintenance plan
plus SQL queries.  It never executes anything -- the returned SQL is text for a
human (or a later, explicitly-approved executor) to run.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional, TypedDict

from langchain.agents import create_agent

from apps.core import config
from . import worker_base as _base
from apps.core.services.AI.Agents.worker_product_name import rename_product_with_seo
from apps.core.services.AI.Agents.worker_web_research import research_product_web
from apps.core.services.AI.Agents.worker_product_price import define_product_price
from apps.core.services.AI.Agents.worker_product_attributes import manage_product_attributes
from apps.core.services.AI.Tools.product_master_data_retrieve import (
    list_master_tables,
    get_master_table_schema,
    fetch_category_tree,
    fetch_attribute_catalog,
)


class SqlStep(TypedDict, total=False):
    purpose: str
    statement: str
    verification: str


class ProductPlan(TypedDict, total=False):
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
# "waiting for model…" heartbeat, so a slow prefill / tool call doesn't
# read as a hang. Tests override this module constant to a tiny value.
HEARTBEAT_SECONDS = 5.0


# Statements that the agent may never emit (hard guardrail, applied after the
# model responds and independent of whatever the model was told).
_FORBIDDEN_SQL_VERBS = (
    "UPDATE",
    "DELETE",
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "RENAME",
    "REPLACE",
)

# The only table an INSERT may target.
_INSERT_ALLOWED_TABLE = "master_product_category_attribute_assign"


SYSTEM_PROMPT = """You are a product-maintenance analyst for a trading company.

Your job is to read the product-master database (via the provided tools) and
produce a maintenance PLAN plus SQL queries. You NEVER run SQL and you NEVER
write data; everything you output is text for a human to review.

## Data model (MySQL)
- master_categories: id, name, description, description_trad_chinese, parent_id (self FK).
- master_product_attributes: id, name (snake_case), multiple_selection, label,
  description, dropdown_reference, catalog_default_display.
- master_product_category_attribute_assign: id, category_id -> master_categories.id,
  attribute_id -> master_product_attributes.id. UNIQUE(category_id, attribute_id).

## Working rules
1. Start by calling get_master_table_schema for the tables you plan to touch.
2. Discover category names with fetch_category_tree and attribute names with
   fetch_attribute_catalog. Attribute `name` is snake_case (e.g. suitable_pet);
   match against `name` or `label` -- NEVER guess spelling.
3. Check what is already assigned with fetch_attribute_assignments before
   proposing new assignments, so you do not propose duplicates.
4. You MUST call build_category_attribute_assign_sql for every assignment and
   copy its `sql`, `verification_before` and `verification_after` into your
   answer verbatim. Never hand-write an INSERT and never re-derive ids or SQL
   yourself: the tool resolves ids, quotes literals correctly and makes the
   INSERT idempotent. If the tool reports an unknown attribute, put it in
   missing_data and do not invent a statement for it.
5. Never invent ids. If you cannot resolve a name, report it in missing_data.
6. If the Node API is unreachable, put the reason in missing_data and stop
   rather than fabricating results.

## Output contract
Return a JSON object (or fill the structured response) with exactly:
- summary: one short sentence describing the outcome.
- plan: ordered list of concrete steps you performed or recommend.
- sql: list of {purpose, statement, verification} objects. statement is one
  runnable MySQL statement; verification is a SELECT to run before/after and
  must be a plain SQL string (never a JSON object or a Python dict).
- missing_data: list of things you could not find or that need human input.
- risks: list of warnings or assumptions.

SQL style: MySQL, use UUID() for new ids, name-joined idempotent INSERTs only,
one statement per category. No UPDATE/DELETE/DDL of any kind.

At the end of your work, respond with exactly ONE JSON object matching the
contract above (no surrounding prose and no markdown fences). If you call tools,
make all necessary tool calls first, then answer with the JSON object.
"""


WORKER_ROUTING_PROMPT = """
## Delegation to specialist workers
You have four specialist workers you MUST delegate to instead of doing the work
yourself:

- rename_product_with_seo(chinese_name, images, seo_keywords, name_type,
  product_id): use when the user gives a Chinese product name (and possibly
  product photos) and wants an SEO English name.
- research_product_web(urls, target_fields, question): use when the user gives
  a web link and wants a summary against target fields.
- define_product_price(cost, qty_tiers, profit_max, profit_min, currency,
  exchange_rate, selling_by_mode, currency_id, product_id, research_summary):
  use to turn a cost + research summary into a price and SQL.
- manage_product_attributes(task): use for any attribute / category create or
  assign request.

Call the right worker with the information the user supplied. Copy each worker
result into your `worker_results` list and put every worker SQL statement into
`drafts`. Never hand-write SQL that a worker should own. Workers never execute
SQL; their output is text for human review only.
"""


# Tools the manager agent may call directly.
WORKER_TOOLS = [
    rename_product_with_seo,
    research_product_web,
    define_product_price,
    manage_product_attributes,
    list_master_tables,
    get_master_table_schema,
    fetch_category_tree,
    fetch_attribute_catalog,
]


def _normalise_plan(plan: Any) -> ProductPlan:
    """Coerce whatever the model returned into the ProductPlan shape."""
    if not isinstance(plan, dict):
        plan = {}
    return {
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


def assert_plan_read_only(plan: ProductPlan) -> ProductPlan:
    """Enforce the read-only guardrail.

    SELECT is allowed anywhere; INSERT is allowed only into
    master_product_category_attribute_assign.  Any other write/DDL statement is
    removed from ``sql`` and reported in ``risks``.
    """
    safe: List[SqlStep] = []
    risks = list(plan.get("risks", []))
    for step in plan.get("sql", []):
        statement = step.get("statement", "")
        verb = _first_verb(statement)

        if verb in _FORBIDDEN_SQL_VERBS:
            risks.append(f"Removed unsafe SQL ({verb}): {statement[:120]}")
            continue

        if verb == "INSERT" and _INSERT_ALLOWED_TABLE not in statement:
            risks.append(f"Removed INSERT outside {_INSERT_ALLOWED_TABLE}: {statement[:120]}")
            continue

        if statement.strip() or step.get("purpose") or step.get("verification"):
            safe.append(step)

    plan["sql"] = safe
    plan["risks"] = risks
    return plan


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


def format_plan(plan: ProductPlan) -> str:
    """Render a plan as a human-readable block of text."""
    lines = [f"Summary: {plan.get('summary', '')}"]
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
            lines.append(f"\n[{wr.get('worker', '?')}] {wr.get('summary', '')}")
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
    if plan.get("risks"):
        lines.append("\nRisks:")
        lines.extend(f"  - {item}" for item in plan["risks"])
    return "\n".join(lines)


class ProductMasterAgent:
    """LangChain tool-calling agent backed by a local Ollama qwen3.5 model."""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.llm = _base.build_llm(
            self.model,
            temperature=self.temperature,
            num_ctx=NUM_CTX,
        )
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            self._agent = create_agent(
                model=self.llm,
                tools=WORKER_TOOLS,
                system_prompt=SYSTEM_PROMPT + WORKER_ROUTING_PROMPT,
            )
        return self._agent

    def _build_human_message(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
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
        lines.append(
            "\nDelegate to the appropriate worker and produce the plan. Do not execute anything."
        )
        return "\n".join(lines)

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
                            str(part) if isinstance(part, str) else str(part.get("text", ""))
                            for part in content
                        )
                    text = str(content)
                    if text.strip():
                        break
            structured = _extract_json_block(text)

        plan = _normalise_plan(structured)
        plan["worker_results"], plan["drafts"] = self._collect_worker_results(result)
        return assert_plan_read_only(plan)

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
                drafts.append({"worker": data.get("worker"), **step})
        return worker_results, drafts

    def plan(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ProductPlan:
        """Run the agent and return a normalised, read-only-guarded plan."""
        agent = self._get_agent()
        result = agent.invoke(
            {"messages": [("human", self._build_human_message(task, links, images, context))]},
            config={"recursion_limit": self.recursion_limit},
        )
        return self._parse_result(result)

    def stream(
        self,
        task: str,
        links: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Run the agent, yielding live events, then the guarded plan.

        Yields dicts: ``{"type": "status"|"token"|"plan", ...}``.  The final
        ``plan`` event carries the guardrail-checked plan and its formatted text.

        The underlying graph is consumed on a background thread so we can emit a
        ``status`` heartbeat while the model is prefilling or a tool is executing
        (both otherwise produce no events and read as a hang).
        """
        agent = self._get_agent()
        inp = {"messages": [("human", self._build_human_message(task, links, images, context))]}
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

        plan = self._parse_result(last_state)
        yield {"type": "plan", "plan": plan, "text": format_plan(plan)}


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    no_stream = "--no-stream" in args
    args = [a for a in args if a != "--no-stream"]

    task = " ".join(args) or (
        "Assign the Material, Color and Size attributes to the 'Pet Playpens' category."
    )
    agent = ProductMasterAgent()

    if no_stream:
        print(format_plan(agent.plan(task)))
    else:
        for event in agent.stream(task):
            if event["type"] == "token":
                print(event["text"], end="", flush=True)
            elif event["type"] == "status":
                print(f"\n[{event['text']}]", flush=True)
            elif event["type"] == "error":
                print(f"\n[ERROR] {event['error']}", flush=True)
            elif event["type"] == "plan":
                print("\n\n" + event["text"])



