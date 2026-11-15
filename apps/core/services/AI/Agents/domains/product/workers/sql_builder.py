"""Worker that turns a product-master business question into executable SQL.

Unlike the other workers (which are narrow specialists), this one is the
"analyst": it knows every table / column / foreign key from the Node schema
modules on disk, drafts ONE read-only SELECT with the local Ollama model, runs
it through ``execute_select_query`` when every referenced table is in
``AI_QUERY_TABLES``, and returns the real rows in an envelope the manager can
quote verbatim.

It is deliberately read-only: write SQL stays with ``manage_product_attributes``
and the ``build_*_sql`` tools.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from langchain.agents import create_agent
from langchain_core.tools import tool

from apps.core.services.AI.Agents.core.guardrail import assert_draft_sql
from apps.core.services.AI.Agents.core.llm import DEFAULT_MODEL, build_llm
from apps.core.services.AI.Agents.core.streaming import stream_agent
from apps.core.services.AI.Agents.core import settings
from apps.core.services.AI.Agents.core import contracts
from apps.core.services.AI.Agents.core import schema_catalog, sql
from apps.core.services.AI.Agents.core.plan_tools import execute_select_query
from apps.core.services.AI.Agents.core.schemas import (
    BuildSqlInput,
    _bool_like,
    _csv_to_list,
)

# Tables whose compact schema is injected into the prompt (the ones nearly every
# product question joins through).  Everything else is one `describe_tables` call
# away.
CORE_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
    "master_product_name_types",
    "master_currencies",
    "products",
    "product_names",
    "product_costs",
    "product_sale_prices_by_qty",
    "master_product_status",
)

MAX_DESCRIBE_TABLES = 12


# ``tables`` / ``execute`` argument coercion now lives in Agents/schemas.py
# (``_csv_to_list`` / ``_bool_like``), declared once on the tool's args_schema.
# These thin aliases keep the internal ``plan()`` / ``stream()`` call sites and
# the ``_e2e_check.py`` dev script working against the single implementation.
def _normalise_tables(tables: Any) -> Optional[List[str]]:
    return _csv_to_list(tables)


def _normalise_execute(execute: Any) -> bool:
    return _bool_like(execute)


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


@tool
def describe_tables(tables: str) -> str:
    """Describe one or more schema tables: columns, types, PK and foreign keys.

    ``tables`` is a comma-separated list, e.g. "products,product_names,
    master_categories". Reads the Node schema files on disk -- no HTTP call and
    no data rows. Returns compact ``- table(col TYPE -> fk.table.fk)`` lines.
    """
    try:
        names = [t.strip() for t in str(tables or "").split(",") if t.strip()]
        names = names[:MAX_DESCRIBE_TABLES]
        if not names:
            return json.dumps(
                {
                    "ok": False,
                    "error": "no tables given",
                    "hint": "Pass a comma-separated list of table names.",
                },
                ensure_ascii=False,
            )
        known = schema_catalog.load_schema_catalog()
        unknown = [n for n in names if n not in known]
        text = schema_catalog.render_schema_catalog(names)
        return json.dumps(
            {
                "ok": True,
                "schema": text,
                "unknown": unknown,
                "hint": "Unknown names are not in the schema; correct the spelling.",
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return contracts._tool_error(exc)


SQL_BUILDER_TOOLS = [describe_tables, execute_select_query]


SQL_BUILDER_SYSTEM_PROMPT = """
You are a senior MySQL analyst for a trading company's product-master database. 
Your job is to turn a business question into correct SQL query with joining different tables.

## Executable tables (the only tables a SELECT may reference and run)
{table_index}

Other tables exist in the schema but cannot be executed here. If a question needs
one, still write the SQL and say so in missing_data.

## Core tables (columns already known)
{core_schemas}

## How to work
1. Extract the entities, filters, grouping and expected output columns from the
   question.
2. Pick tables from the executable index. Call `describe_tables("t1,t2,...")`
   ONCE for any table not described above; it returns columns and foreign keys.
3. Write SQL queries:
   - explicit columns with table aliases; never `SELECT *`;
   - JOIN on the `-> table.field` references shown in the schema;
   - "has no X" => LEFT JOIN + WHERE x.id IS NULL;
   - counts => COUNT(*) / COUNT(x.id) with every non-aggregated column in
     GROUP BY;
   - "which / list" answers => ORDER BY the natural order; LIMIT 200 unless the
     question asks for everything.
4. Call `execute_select_query` with the exact SQL. This is MANDATORY: you must
   call it and read its rows BEFORE writing `answer`; a reply without a tool
   result is invalid. If it errors, read the hint, fix the SQL and retry ONCE.
   If the retry also fails, put the failure in missing_data and do NOT claim any
   rows.
5. Put the REAL result in `answer`, in the user's language: state the count and
   list the values. If zero rows, say "none". If you did NOT run
   `execute_select_query`, leave `answer` empty. Never claim rows you did not
   fetch.
6. If a column is missing from the schema, put it in missing_data. Never invent a
   column.

## SQL rules
- MySQL 8, uppercase keywords, ONE statement, no SQL comments (the guard rejects
  `--`, `#`, `/* */`).
- Single-quote literals; escape inner quotes as ''.
- Read-only: never draft INSERT/UPDATE/DELETE/DDL.
- Do not use the read-guard's blocked words even as aliases: insert, update,
  delete, replace, drop, alter, create, truncate, rename, grant, revoke, call,
  execute, prepare, deallocate, set, use, lock, unlock, handler, load, outfile,
  dumpfile, into, sleep, benchmark, get_lock, release_lock.

## Output contract ??exactly ONE JSON object (no prose, no fences)
- answer: the direct reply (count + values, or "none"), in the user's language.
- summary: one short sentence describing the query.
- sql: list of {{purpose, statement, verification}} (usually one SELECT).
- tables_used: list of table names referenced.
- plan: ordered steps you performed.
- missing_data: unresolved columns/tables or failures.
- risks: assumptions or warnings.
"""


def _system_prompt() -> str:
    allow = settings.QUERY_ALLOWED_TABLES
    return SQL_BUILDER_SYSTEM_PROMPT.format(
        table_index="\n".join(f"- {t}" for t in allow),
        core_schemas=schema_catalog.render_schema_catalog(CORE_TABLES),
    )


def _tables_in_sql(statement: str) -> List[str]:
    masked = sql._mask_quoted(statement or "")
    return sorted(set(sql._TABLE_RE.findall(masked)))


class ProductSqlBuilderWorker:
    """Tool-calling agent that builds and (optionally) runs a read-only SELECT."""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: int = 12,
        llm: Any = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.recursion_limit = recursion_limit
        self.llm = llm or build_llm(
            self.model, temperature=temperature, num_ctx=settings.SQL_BUILDER_NUM_CTX
        )
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            self._agent = create_agent(
                model=self.llm,
                tools=SQL_BUILDER_TOOLS,
                system_prompt=_system_prompt(),
            )
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
                "worker": "product_sql_builder",
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
        norm["worker"] = "product_sql_builder"
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
        agent = self._get_agent()
        result = agent.invoke(
            {
                "messages": [
                    ("human", self._build_human_message(question, tables, execute))
                ]
            },
            config={"recursion_limit": self.recursion_limit},
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
        result = ProductSqlBuilderWorker().plan(
            question, tables=_normalise_tables(tables), execute=execute
        )
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "product_sql_builder", "ok": False, "error": str(exc)}
    return contracts.dump_worker_result(result)
