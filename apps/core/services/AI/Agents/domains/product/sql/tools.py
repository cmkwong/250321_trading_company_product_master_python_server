"""Tools for the product SQL-builder sub-agent.

``describe_tables`` reads the Node schema files on disk; ``execute_select_query``
(shared from :mod:`core.plan_tools`) runs one read-only SELECT.  The SQL builder
itself is deliberately read-only: write SQL stays with the attribute sub-agent
and the ``build_*_sql`` tools.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from langchain_core.tools import tool

from apps.core.services.AI.Agents.core import contracts
from apps.core.services.AI.Agents.core import schema_catalog
from apps.core.services.AI.Agents.core.plan_tools import execute_select_query
from apps.core.services.AI.Agents.core.schemas import _bool_like, _csv_to_list

MAX_DESCRIBE_TABLES = 12


# ``tables`` / ``execute`` argument coercion lives in Agents/schemas.py
# (``_csv_to_list`` / ``_bool_like``), declared once on the tool's args_schema.
# These thin aliases keep the internal ``plan()`` / ``stream()`` call sites and
# the ``_e2e_check.py`` dev script working against the single implementation.
def _normalise_tables(tables: Any) -> Optional[List[str]]:
    return _csv_to_list(tables)


def _normalise_execute(execute: Any) -> bool:
    return _bool_like(execute)


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
