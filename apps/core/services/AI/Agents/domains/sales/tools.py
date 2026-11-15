"""Sales-domain tools.

``build_sales_sql`` is a thin sales-flavoured wrapper over the shared SQL-builder
worker, plus the plan tools the manager may call directly.
"""

from __future__ import annotations

from langchain.tools import tool

from apps.core.services.AI.Agents.core.contracts import dump_worker_result
from apps.core.services.AI.Agents.domains.product.workers.sql_builder import (
    ProductSqlBuilderWorker,
)
from apps.core.services.AI.Agents.core.schemas import BuildSqlInput
from apps.core.services.AI.Agents.core.plan_tools import (
    build_insert_sql,
    build_update_sql,
    execute_select_query,
    fetch_master_rows,
    fetch_web_content,
    get_master_table_schema,
    list_master_tables,
    read_table_definition,
    search_web,
)


@tool(args_schema=BuildSqlInput)
def build_sales_sql(
    question: str,
    tables=None,
    execute=True,
) -> str:
    """Turn a sales business question into ONE read-only SELECT, run it when every
    table is queryable, and return the rows + SQL. Use for questions that need
    joins, counts, grouping or comparisons across sales/master tables.

    ``tables`` is an optional hint: a list of table names or a single
    comma-separated string (e.g. "sales_quotations,master_currencies")."""
    try:
        result = ProductSqlBuilderWorker().plan(
            question, tables=tables, execute=execute
        )
    except Exception as exc:  # noqa: BLE001
        result = {"worker": "sales_sql_builder", "ok": False, "error": str(exc)}
    return dump_worker_result(result)


SALES_TOOLS = [
    list_master_tables,
    get_master_table_schema,
    read_table_definition,
    fetch_master_rows,
    execute_select_query,
    build_insert_sql,
    build_update_sql,
    build_sales_sql,
    fetch_web_content,
    search_web,
]

__all__ = ["build_sales_sql", "SALES_TOOLS"]
