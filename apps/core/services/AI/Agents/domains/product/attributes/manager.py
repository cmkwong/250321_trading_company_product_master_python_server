"""Product-attribute sub-agent manager.

Owns the ``ProductAttributesAgent`` tool-calling loop (``model -> tools ->
model``) for attribute create/assign work, plus the ``manage_product_attributes``
``@tool`` the product manager delegates to.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from langchain_core.tools import tool

from apps.core.services.AI.Agents.core.agent import ToolLoopAgent
from apps.core.services.AI.Agents.core.contracts import (
    SqlDraft,
    as_sql_list,
    as_str_list,
    dump_agent_result,
    extract_json_block,
)
from apps.core.services.AI.Agents.core.guardrail import assert_draft_sql
from apps.core.services.AI.Agents.core.streaming import stream_agent

from apps.core.services.AI.Agents.domains.product.attributes.prompts import (
    ATTRIBUTE_SYSTEM_PROMPT,
)
from apps.core.services.AI.Agents.domains.product.attributes.tools import (
    ALLOWED_ATTRIBUTE_TABLES,
    ATTRIBUTE_TOOLS,
)


# Helper to format the agent's structured result into a human-readable string.
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


class AttributePlan(BaseModel):
    summary: str = Field(default="", description="One short sentence")
    plan: List[str] = Field(default_factory=list, description="Ordered concrete steps")
    sql: List[SqlDraft] = Field(default_factory=list)
    missing_data: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)


class ProductAttributesAgent(ToolLoopAgent):
    """Tool-calling sub-agent for attribute create/assign work.

    Inherits the shared reason-act loop (:class:`core.agent.ToolLoopAgent`) and
    supplies the attribute tools and prompt.  It never writes data; every write
    SQL it returns is text for human review.
    """

    TOOLS = ATTRIBUTE_TOOLS
    SYSTEM_PROMPT = ATTRIBUTE_SYSTEM_PROMPT
    RESPONSE_MODEL = AttributePlan

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
        structured = self._structured(result)
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
        norm["agent"] = "product_attributes"
        norm["ok"] = True
        return norm

    def plan(self, task: str) -> Dict[str, Any]:
        result = self._invoke([("human", self._build_human_message(task))])
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
        result = ProductAttributesAgent().plan(task)
    except Exception as exc:  # noqa: BLE001
        result = {"agent": "product_attributes", "ok": False, "error": str(exc)}
    return dump_agent_result(result)
