"""Worker that creates and assigns product attributes (categories/attributes).

This is the attribute half of the product-master agent, extracted into its own
tool-calling agent so the manager can delegate attribute work to it.  It reads
the master schema via the Node API, builds idempotent SQL through the dedicated
builder tools, and never writes anything (the only SQL it may execute is a
read-only SELECT via ``execute_select_query``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain.agents import create_agent
from langchain_core.tools import tool

from apps.core.services.AI.Agents.core.contracts import (
    as_sql_list,
    as_str_list,
    dump_worker_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.guardrail import assert_draft_sql
from apps.core.services.AI.Agents.core.llm import DEFAULT_MODEL, build_llm
from apps.core.services.AI.Agents.core.streaming import stream_agent
from apps.core.services.AI.Agents.core.plan_tools import (
    build_attribute_dropdown_sql,
    build_category_attribute_assign_sql,
    build_create_attribute_sql,
    fetch_attribute_assignments,
    fetch_attribute_catalog,
    fetch_category_tree,
    get_master_table_schema,
    list_master_tables,
    read_master_schema_file,
    read_table_definition,
    execute_select_query,
)

ATTRIBUTE_TOOLS = [
    list_master_tables,
    get_master_table_schema,
    fetch_category_tree,
    fetch_attribute_catalog,
    fetch_attribute_assignments,
    read_master_schema_file,
    read_table_definition,
    build_category_attribute_assign_sql,
    build_create_attribute_sql,
    build_attribute_dropdown_sql,
    execute_select_query,
]

ALLOWED_ATTRIBUTE_TABLES = (
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
)

ATTRIBUTE_SYSTEM_PROMPT = """You are a product-attribute analyst for a trading company.
You create and assign product attributes against the product-master database
(via the provided tools) and return a PLAN plus SQL text.  You NEVER write data.
The only SQL you may run is a read-only SELECT via `execute_select_query` (to
verify ids / rows); every write SQL you output is text for a human to review.

## Data model (MySQL)
- master_categories: id, name, description, description_trad_chinese, parent_id.
- master_product_attributes: id, name (snake_case), multiple_selection, label,
  description, dropdown_reference, catalog_default_display.
- master_product_attribute_dropdown: id, attribute_id, value.
- master_product_category_attribute_assign: id, category_id, attribute_id.
  UNIQUE(category_id, attribute_id).

## Working rules
1. Start by calling get_master_table_schema for the tables you plan to touch.
2. Discover categories with fetch_category_tree and attributes with
   fetch_attribute_catalog.  Attribute `name` is snake_case (e.g. suitable_pet);
   match against `name` or `label` -- NEVER guess spelling.
3. Check existing assignments with fetch_attribute_assignments before proposing
   new ones, so you do not propose duplicates.
4. To create a new attribute, call build_create_attribute_sql and copy its
   `sql`, `verification_before` and `verification_after` verbatim.  Attribute
   names must be snake_case.
5. To add dropdown values, call build_attribute_dropdown_sql.
6. To assign attributes to a category, call build_category_attribute_assign_sql
   and copy its `sql`, `verification_before` and `verification_after` verbatim.
7. Never invent ids.  If you cannot resolve a name, put it in missing_data.

## Output contract
Return exactly ONE JSON object (no prose, no markdown fences) with:
- summary: one short sentence.
- plan: ordered list of concrete steps.
- sql: list of {purpose, statement, verification} objects.
- missing_data: list of things you could not find.
- risks: list of warnings or assumptions.

SQL style: MySQL, use UUID() for new ids, name-joined idempotent INSERTs only,
one statement per category/attribute.  No UPDATE/DELETE/DDL of any kind.
"""


def _format_attributes(result: Dict[str, Any]) -> str:
    lines = [f"[product_attributes] {result.get('summary', '')}"]
    for step in result.get("plan", []):
        lines.append(f"  - {step}")
    for step in result.get("sql", []):
        if step.get("purpose"):
            lines.append(f"  -- {step['purpose']}")
        if step.get("statement"):
            lines.append("  " + step["statement"].replace("\n", "\n  "))
    for item in result.get("missing_data", []):
        lines.append(f"  (missing) {item}")
    return "\n".join(lines)


class ProductAttributesWorker:
    """Tool-calling agent for attribute create/assign work."""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 25,
        llm: Any = None,
    ):
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.llm = llm or build_llm(self.model, temperature=temperature)
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            self._agent = create_agent(
                model=self.llm,
                tools=ATTRIBUTE_TOOLS,
                system_prompt=ATTRIBUTE_SYSTEM_PROMPT,
            )
        return self._agent

    def _build_human_message(self, task: str) -> str:
        return (
            "Task:\n"
            + (task or "")
            + "\n\nProduce the plan and SQL only. Do not write data; the only SQL you may execute is a read-only SELECT via `execute_select_query`."
        )

    @staticmethod
    def _normalise(plan: Any) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            plan = {}
        return {
            "summary": str(plan.get("summary", "")),
            "plan": as_str_list(plan.get("plan")),
            "sql": as_sql_list(plan.get("sql")),
            "missing_data": as_str_list(plan.get("missing_data")),
            "risks": as_str_list(plan.get("risks")),
        }

    def _parse_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        structured = result.get("structured_response")
        if structured is None:
            messages = result.get("messages", [])
            text = ""
            for message in reversed(messages):
                if getattr(message, "type", None) == "ai":
                    content = getattr(message, "content", "") or ""
                    if isinstance(content, list):
                        content = "".join(
                            str(p) if isinstance(p, str) else str(p.get("text", ""))
                            for p in content
                        )
                    text = str(content)
                    if text.strip():
                        break
            structured = extract_json_block(text)

        norm = self._normalise(structured)
        safe, risks = assert_draft_sql(norm["sql"], ALLOWED_ATTRIBUTE_TABLES)
        norm["sql"] = safe
        norm["risks"] = list(norm["risks"]) + risks
        norm["worker"] = "product_attributes"
        norm["ok"] = True
        return norm

    def plan(self, task: str) -> Dict[str, Any]:
        agent = self._get_agent()
        result = agent.invoke(
            {"messages": [("human", self._build_human_message(task))]},
            config={"recursion_limit": self.recursion_limit},
        )
        return self._parse_result(result)

    def stream(self, task: str):
        agent = self._get_agent()
        inp = {"messages": [("human", self._build_human_message(task))]}
        last_state: Dict[str, Any] = {}
        for event in stream_agent(agent, inp, recursion_limit=self.recursion_limit):
            if event["type"] == "_done":
                last_state = event["state"]
                break
            yield event
        result = self._parse_result(last_state)
        yield {"type": "result", "result": result, "text": _format_attributes(result)}


@tool
def manage_product_attributes(task: str) -> str:
    """Create and/or assign product attributes and categories. Returns a JSON envelope with a plan and idempotent SQL (never executed)."""
    try:
        result = ProductAttributesWorker().plan(task)
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "product_attributes", "ok": False, "error": str(exc)}
    return dump_worker_result(result)
