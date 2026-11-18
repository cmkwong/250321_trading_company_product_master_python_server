"""Product SQL-builder sub-agent manager.

Turns a product-master business question into ONE read-only SELECT, runs it
(via ``execute_select_query``) when every referenced table is queryable, and
returns the real rows.  Owns the ``build_product_sql`` ``@tool`` the product and
sales managers delegate to.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from apps.core.services.AI.Agents.core import contracts
from apps.core.services.AI.Agents.core import settings
from apps.core.services.AI.Agents.core import sql
from apps.core.services.AI.Agents.core.agent import ToolLoopAgent
from apps.core.services.AI.Agents.core.graph import build_tool_graph
from apps.core.services.AI.Agents.core.guardrail import assert_draft_sql
from apps.core.services.AI.Agents.core.llm import build_llm
from apps.core.services.AI.Agents.core.plan_tools import execute_select_query
from apps.core.services.AI.Agents.core.schemas import BuildSqlInput
from apps.core.services.AI.Agents.core.streaming import stream_agent

from apps.core.services.AI.Agents.domains.product.sql.prompts import _system_prompt
from apps.core.services.AI.Agents.domains.product.sql.tools import (
    SQL_BUILDER_TOOLS,
    _normalise_execute,
    _normalise_tables,
)


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


def _tables_in_sql(statement: str) -> List[str]:
    masked = sql._mask_quoted(statement or "")
    return sorted(set(sql._TABLE_RE.findall(masked)))


class ProductSqlAgent(ToolLoopAgent):
    """Tool-calling sub-agent (``model -> tools -> model``) that builds and
    (optionally) runs a read-only SELECT."""

    DEFAULT_RECURSION_LIMIT = 12

    def _build_llm(self):
        return build_llm(
            self.model, temperature=self.temperature, num_ctx=settings.SQL_BUILDER_NUM_CTX
        )

    def _get_agent(self):
        if self._agent is None:
            self._agent = build_tool_graph(self.llm, SQL_BUILDER_TOOLS, _system_prompt())
        return self._agent

    def _build_human_message(
        self, question: str, tables: Optional[List[str]] = None, execute: bool = True
    ) -> str:
        lines = ["Question:", str(question or "")]
        if tables:
            names = [str(t).strip() for t in tables if str(t).strip()]
            if names:
                lines.append("\nSuggested tables: " + ", ".join(names))
        if execute:
            lines.append(
                "\nProduce ONE read-only SELECT that answers the question. You MUST "
                "call `execute_select_query` with it before writing `answer`; an "
                "answer with no tool result is invalid. Put the real fetched rows in "
                "`answer`."
            )
        else:
            lines.append(
                "\nProduce ONE read-only SELECT that answers the question. Do NOT "
                "execute anything: return the SQL only and note in missing_data that "
                "it was not run."
            )
        return "\n".join(lines)

    @staticmethod
    def _normalise(plan: Any) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            plan = {}
        return {
            "answer": str(plan.get("answer", "")),
            "summary": str(plan.get("summary", "")),
            "plan": contracts.as_str_list(plan.get("plan")),
            "sql": contracts.as_sql_list(plan.get("sql")),
            "tables_used": contracts.as_str_list(plan.get("tables_used")),
            "missing_data": contracts.as_str_list(plan.get("missing_data")),
            "risks": contracts.as_str_list(plan.get("risks")),
        }

    @staticmethod
    def _collect_queries(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Pull the rows the agent actually fetched via ``execute_select_query``."""
        queries: List[Dict[str, Any]] = []
        for message in result.get("messages", []):
            if getattr(message, "type", None) != "tool":
                continue
            content = getattr(message, "content", "") or ""
            if isinstance(content, list):
                content = "".join(
                    str(p) if isinstance(p, str) else str(p.get("text", ""))
                    for p in content
                )
            data = contracts.extract_json_block(str(content))
            if (
                isinstance(data, dict)
                and data.get("ok")
                and "query" in data
                and "rows" in data
            ):
                queries.append(
                    {
                        "query": data.get("query"),
                        "rowCount": data.get("rowCount"),
                        "rows": data.get("rows"),
                    }
                )
        return queries

    @staticmethod
    def _auto_execute(norm: Dict[str, Any]) -> Dict[str, Any]:
        """Run the drafted SELECT ourselves when the model never called the tool.

        This is the hard guarantee that a non-executed draft can never be published
        as a confident answer: either we fetch real rows here, or we mark the
        envelope ``ok: false`` with the verbatim tool error.
        """
        statement = ""
        for step in norm.get("sql", []):
            candidate = (step.get("statement") or "").strip()
            if candidate:
                statement = candidate
                break
        if not statement:
            return norm

        raw = execute_select_query.invoke({"query": statement})
        data = contracts.extract_json_block(str(raw))
        if not isinstance(data, dict):
            data = {}

        if data.get("ok") and isinstance(data.get("rows"), list):
            rows = data["rows"]
            row_count = int(data.get("rowCount", len(rows)))
            norm["queries"] = [
                {
                    "query": data.get("query"),
                    "rowCount": row_count,
                    "rows": rows,
                }
            ]
            norm["executed"] = True
            norm["answer"] = _rows_answer(row_count, rows)
            norm["summary"] = (
                f"Executed the drafted SELECT; {row_count} row(s) returned."
            )
        else:
            error = data.get("error") or "execute_select_query failed"
            hint = data.get("hint") or ""
            norm["ok"] = False
            norm["answer"] = ""
            norm["summary"] = "DRAFT ONLY - the SQL was not executed."
            norm["error"] = str(error) + (f" ({hint})" if hint else "")
            norm["missing_data"] = list(norm["missing_data"]) + [
                f"Auto-execution failed: {error}"
            ]
        return norm

    def _parse_result(
        self, result: Dict[str, Any], execute: bool = True
    ) -> Dict[str, Any]:
        execute = _normalise_execute(execute)
        structured = result.get("structured_response")
        if structured is None:
            text = ""
            for message in reversed(result.get("messages", [])):
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
            structured = contracts.extract_json_block(text)

        if structured is None:
            return {
                "agent": "product_sql_builder",
                "ok": False,
                "error": "model did not return a JSON object",
                "raw": (text or "")[:500],
                "answer": "",
                "summary": "",
                "plan": [],
                "sql": [],
                "tables_used": [],
                "missing_data": [],
                "risks": [],
                "queries": [],
                "executed": False,
                "execute_requested": execute,
            }

        norm = self._normalise(structured)
        safe, risks = assert_draft_sql(norm["sql"], ())
        norm["sql"] = safe
        norm["risks"] = list(norm["risks"]) + risks

        allowed = set(settings.QUERY_ALLOWED_TABLES)
        tables_used = list(norm["tables_used"])
        seen = set(tables_used)
        for step in safe:
            for table in _tables_in_sql(step.get("statement", "")):
                if table not in seen:
                    seen.add(table)
                    tables_used.append(table)
        norm["tables_used"] = tables_used

        for table in tables_used:
            if table not in allowed:
                norm["missing_data"].append(
                    f"`{table}` is outside AI_QUERY_TABLES; the SQL was drafted but "
                    "not executed. Add it to Agents/core/settings.py (or AI_QUERY_TABLES) "
                    "to run it."
                )

        norm["queries"] = self._collect_queries(result)
        norm["executed"] = bool(norm["queries"])
        norm["agent"] = "product_sql_builder"
        norm["ok"] = True
        norm["execute_requested"] = execute
        return norm

    def _ensure_executed(self, norm: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-run a drafted SELECT when the model produced one but never executed it.

        Kept out of ``_parse_result`` (a pure parse) so unit tests that call it with
        synthetic results do not trigger a real database call.  Live ``plan``/``stream``
        pass their parsed envelope through here instead.
        """
        if norm.get("execute_requested") and not norm.get("executed"):
            return self._auto_execute(norm)
        return norm

    def plan(
        self,
        question: str,
        tables: Optional[List[str]] = None,
        execute: bool = True,
    ) -> Dict[str, Any]:
        tables = _normalise_tables(tables)
        result = self._invoke(
            [("human", self._build_human_message(question, tables, execute))]
        )
        return self._ensure_executed(self._parse_result(result, execute=execute))

    def stream(
        self,
        question: str,
        tables: Optional[List[str]] = None,
        execute: bool = True,
    ):
        tables = _normalise_tables(tables)
        agent = self._get_agent()
        inp = {
            "messages": [
                ("human", self._build_human_message(question, tables, execute))
            ]
        }
        last_state: Dict[str, Any] = {}
        for event in stream_agent(agent, inp, recursion_limit=self.recursion_limit):
            if event["type"] == "_done":
                last_state = event["state"]
                break
            yield event
        result = self._ensure_executed(self._parse_result(last_state, execute=execute))
        yield {"type": "result", "result": result, "text": _format_sql(result)}


def _format_sql(result: Dict[str, Any]) -> str:
    lines = [f"[product_sql_builder] {result.get('summary', '')}"]
    if result.get("answer"):
        lines.append("Answer: " + str(result["answer"]))
    for step in result.get("sql", []):
        if step.get("statement"):
            lines.append("SQL:\n  " + str(step["statement"]).replace("\n", "\n  "))
    for query in result.get("queries", []):
        lines.append(f"Executed: {query.get('rowCount', 0)} row(s)")
    for item in result.get("missing_data", []):
        lines.append("Missing: " + str(item))
    return "\n".join(lines)


@tool(args_schema=BuildSqlInput)
def build_product_sql(
    question: str,
    tables=None,
    execute=True,
) -> str:
    """Turn a product-master business question into ONE read-only SELECT, run it when
    every table is queryable, and return the rows + SQL. Use for questions that need
    joins, counts, grouping or comparisons across tables.

    ``tables`` is an optional hint: a list of table names or a single
    comma-separated string (e.g. "master_categories,products").
    """
    try:
        result = ProductSqlAgent().plan(
            question, tables=_normalise_tables(tables), execute=execute
        )
    except Exception as exc:  # noqa: BLE001
        result = {"agent": "product_sql_builder", "ok": False, "error": str(exc)}
    return contracts.dump_agent_result(result)


