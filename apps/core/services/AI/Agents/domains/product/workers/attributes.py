"""Worker that creates and assigns product attributes (categories/attributes).

This is the attribute half of the product-master agent, extracted into its own
tool-calling agent so the manager can delegate attribute work to it.  It reads
the master schema via the Node API, builds idempotent SQL through the dedicated
builder tools, and never writes anything (the only SQL it may execute is a
read-only SELECT via ``execute_select_query``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import json
import re
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from apps.core.services.AI.Agents.core.llm import message_text
from apps.core.services.AI.Agents.core.master_api import _rows
from apps.core.services.AI.Agents.core.contracts import (
    MasterApiError,
    _tool_error,
    as_sql_list,
    as_str_list,
    dump_worker_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.graph import build_tool_graph
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


class _Reuse(BaseModel):
    name: str
    reason: str = ""


class _New(BaseModel):
    name: str = Field(description="snake_case, e.g. suitable_pet")
    label: str = Field(description="short Title Case label, max 3 words")
    description: str = ""
    multiple_selection: bool = False
    values: List[str] = Field(default_factory=list, description="2-12 dropdown values")
    reason: str = ""

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(v).strip().lower()).strip("_")


class _Proposal(BaseModel):
    reuse: List[_Reuse] = Field(default_factory=list)
    create: List[_New] = Field(default_factory=list)
    missing_data: List[str] = Field(default_factory=list)


_PROPOSE_PROMPT = """You are a product-data architect for a trading company.
Decide which attributes buyers need to filter and compare products in ONE category.

Rules:
1. PREFER reusing an existing attribute. Create a new one only if no existing
   attribute covers the meaning (check names AND labels, including synonyms).
2. Choose attributes that distinguish products inside THIS category
   (material, size, capacity, power type...). Skip generic ones it does not need.
3. name: snake_case ASCII. label: short Title Case, max 3 words.
4. Dropdown values: 2-12 concise, mutually exclusive, real-world values.
   If values are open-ended, leave values empty.
5. multiple_selection = true only when a product can have several values at once.
6. Do not repeat attributes already assigned to the category.
7. At most MAX_NEW new attributes. Put doubts in missing_data.
Return ONLY a JSON object with keys: reuse, create, missing_data."""


def _propose(category: dict, catalog: list, assigned: list, max_new: int) -> _Proposal:
    llm = build_llm(DEFAULT_MODEL, temperature=0.0)
    messages = [
        SystemMessage(content=_PROPOSE_PROMPT.replace("MAX_NEW", str(max_new))),
        HumanMessage(
            content=json.dumps(
                {
                    "category": {
                        "name": category.get("name"),
                        "description": category.get("description"),
                    },
                    "existing_attributes": [
                        {"name": a.get("name"), "label": a.get("label")}
                        for a in catalog
                    ],
                    "already_assigned": assigned,
                },
                ensure_ascii=False,
            )
        ),
    ]
    try:
        out = llm.with_structured_output(_Proposal, method="json_schema").invoke(
            messages
        )
        if isinstance(out, _Proposal):
            return out
        if isinstance(out, dict):
            return _Proposal.model_validate(out)
    except Exception:  # noqa: BLE001 - model/package without structured output
        pass
    raw = message_text(llm.invoke(messages))
    return _Proposal.model_validate(extract_json_block(raw) or {})


@tool
def suggest_category_attributes(category: str, max_new: int = 5) -> str:
    """Decide which attributes (label + dropdown values) a product category should have.

    Use when the user asks what attributes a category needs, or to "set up" a
    category without naming exact attributes. Returns validated `steps`
    ("assign" an existing attribute, or "create" a new one with label and values).
    It builds NO SQL and writes nothing: call the build_* tools for each step.
    Do NOT use when the user already gave exact attribute names and values.
    """
    try:
        max_new = max(1, min(int(max_new or 5), 8))
        cats = _rows("master_categories")
        key = str(category or "").strip().lower()
        cat = next(
            (
                c
                for c in cats
                if key in (str(c.get("name", "")).lower(), str(c.get("id", "")).lower())
            ),
            None,
        )
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category {category!r} not found",
                    "hint": "Use fetch_category_tree to find the exact name.",
                },
                ensure_ascii=False,
            )

        catalog = _rows("master_product_attributes")
        by_name = {str(a.get("name")): a for a in catalog}
        assign = _rows("master_product_category_attribute_assign")
        assigned_ids = {
            r.get("attribute_id")
            for r in assign
            if r.get("category_id") == cat.get("id")
        }
        assigned = [str(a.get("name")) for a in catalog if a.get("id") in assigned_ids]

        proposal = _propose(cat, catalog, assigned, max_new)

        steps: List[Dict[str, Any]] = []
        risks: List[str] = []
        for r in proposal.reuse:
            if r.name not in by_name:
                risks.append(
                    f"model proposed unknown existing attribute {r.name!r}; ignored"
                )
            elif r.name not in assigned:
                steps.append(
                    {"kind": "assign", "attribute": r.name, "reason": r.reason}
                )
        for n in proposal.create[:max_new]:
            if not n.name:
                continue
            if n.name in by_name:  # model missed that it already exists
                if n.name not in assigned:
                    steps.append(
                        {
                            "kind": "assign",
                            "attribute": n.name,
                            "reason": "already exists; reused",
                        }
                    )
                continue
            values = list(
                dict.fromkeys(v.strip() for v in n.values if v and v.strip())
            )[:12]
            steps.append(
                {
                    "kind": "create",
                    "attribute": n.name,
                    "label": n.label or n.name,
                    "description": n.description,
                    "multiple_selection": n.multiple_selection,
                    "values": values,
                    "reason": n.reason,
                }
            )
        return json.dumps(
            {
                "ok": True,
                "category": {"id": cat.get("id"), "name": cat.get("name")},
                "steps": steps,
                "missing_data": proposal.missing_data,
                "risks": risks,
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)
    except Exception as exc:  # noqa: BLE001 - always return an envelope
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


ATTRIBUTE_TOOLS = [
    suggest_category_attributes,
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
1. If the user did NOT name exact attributes, call
   suggest_category_attributes(category) first, then follow its `steps`:
   - kind "create": call build_create_attribute_sql (name, label, description,
     multiple_selection), then build_attribute_dropdown_sql for its `values`
     if any, then build_category_attribute_assign_sql for the category.
   - kind "assign": call build_category_attribute_assign_sql only.
   Drop a step that looks wrong and say why in `risks`.
   If the user DID name exact attributes, skip it. Call get_master_table_schema
   only when a column is unknown.
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
7. Copy suggested names and values exactly; do not rename them.

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
    """Tool-calling agent (``model`` -> ``tools`` -> ``model``) for attribute
    create/assign work, built via :func:`core.graph.build_tool_graph`."""

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
            self._agent = build_tool_graph(
                self.llm, ATTRIBUTE_TOOLS, ATTRIBUTE_SYSTEM_PROMPT
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
