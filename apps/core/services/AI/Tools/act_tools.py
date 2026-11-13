"""Execution tools for the product-master agent (``/act`` mode).

These tools run *approved* write statements through the Node server's guarded
endpoints and verify with read-only SELECTs.  They are only wired into the
agent when it is constructed in ``act`` mode, so a normal ``/plan`` request can
never write data.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.tools import tool

from . import utils
from .plan_tools import execute_select_query


@tool
def execute_update_query(query: str, confirm: str = "") -> str:
    """Execute ONE approved INSERT/UPDATE via /trade-business/query/update.

    The target table must be in WRITE_ALLOWED_TABLES. An UPDATE without a WHERE
    clause requires confirm='UPDATE_ALL_ROWS'. Returns affected/changed rows and
    insertId from the Node server.
    """
    try:
        sql = utils.assert_write_only(query, verbs=("INSERT", "UPDATE"))
        payload = {"query": sql}
        if confirm:
            payload["confirm"] = confirm
        data = utils._api_post(utils.UPDATE_QUERY_PATH, payload, label="INSERT/UPDATE")
        result = data.get("data") or {}
        return json.dumps(
            {
                "ok": True,
                "verb": utils.first_verb(sql),
                "affectedRows": result.get("affectedRows"),
                "changedRows": result.get("changedRows"),
                "insertId": result.get("insertId"),
                "warningStatus": result.get("warningStatus"),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return utils._tool_error(exc)


@tool
def execute_delete_query(query: str, confirm: str = "") -> str:
    """Execute ONE approved DELETE via /trade-business/query/delete.

    The target table must be in WRITE_ALLOWED_TABLES. A DELETE without a WHERE
    clause requires confirm='DELETE_ALL_ROWS'.
    """
    try:
        sql = utils.assert_delete_only(query)
        payload = {"query": sql}
        if confirm:
            payload["confirm"] = confirm
        data = utils._api_post(utils.DELETE_QUERY_PATH, payload, label="DELETE")
        result = data.get("data") or {}
        return json.dumps(
            {"ok": True, "verb": "DELETE",
             "affectedRows": result.get("affectedRows"), "info": result.get("info")},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return utils._tool_error(exc)


# Tools the manager may call directly in /act mode.
ACT_TOOLS = [execute_select_query, execute_update_query, execute_delete_query]

_ROUTE = {"SELECT": "select", "INSERT": "update", "UPDATE": "update", "DELETE": "delete"}


def execute_plan(plan: Dict[str, Any], *, dry_run: bool = False, confirm: bool = False) -> List[Dict[str, Any]]:
    """Deterministically run the approved statements in ``plan['sql']``.

    Routing by verb:
      SELECT        -> /query/select (read-only)
      INSERT/UPDATE -> /query/update
      DELETE        -> /query/delete
      anything else -> skipped

    Statements are validated locally (verb, single statement, table allowlist,
    forbidden keywords) before any HTTP call; the Node server re-checks them.
    A no-WHERE UPDATE/DELETE is reported with ``needs_confirmation`` and is not
    executed unless ``confirm=True``.  ``plan['drafts']`` (worker output) is
    never touched.  Execution stops at the first failure.
    """
    statements = plan.get("sql", []) if isinstance(plan, dict) else []
    if not isinstance(statements, list):
        return []

    results: List[Dict[str, Any]] = []
    for step in statements[: utils.ACT_MAX_STATEMENTS]:
        if isinstance(step, dict):
            statement = str(step.get("statement", "")).strip()
            purpose = str(step.get("purpose", ""))
        elif isinstance(step, str):
            statement = step.strip()
            purpose = ""
        else:
            continue
        if not statement:
            continue

        verb = utils.first_verb(statement)
        entry: Dict[str, Any] = {"purpose": purpose, "statement": statement, "verb": verb}

        if verb not in _ROUTE:
            entry.update({"ok": False, "skipped": True,
                          "error": f"verb {verb or '?'} is not executable"})
            results.append(entry)
            continue

        # Local validation (mirrors the Node guards); keep the normalised SQL.
        try:
            if verb == "SELECT":
                sql = utils.assert_select_only(statement)
            elif verb == "DELETE":
                sql = utils.assert_delete_only(statement)
            else:
                sql = utils.assert_write_only(statement)
        except utils.MasterApiError as exc:
            entry.update({"ok": False, "error": str(exc), "hint": exc.hint})
            results.append(entry)
            continue

        needs_confirm = verb in ("UPDATE", "DELETE") and not utils.has_where_clause(sql)

        if dry_run:
            entry.update({"ok": True, "dry_run": True,
                          "needs_confirmation": needs_confirm, "route": _ROUTE[verb]})
            results.append(entry)
            continue

        if needs_confirm and not confirm:
            entry.update({
                "ok": False, "needs_confirmation": True,
                "error": "write has no WHERE clause; pass confirm=True (or the matching confirm token)",
            })
            results.append(entry)
            continue

        payload: Dict[str, Any] = {"query": sql}
        if needs_confirm:
            payload["confirm"] = "DELETE_ALL_ROWS" if verb == "DELETE" else "UPDATE_ALL_ROWS"

        try:
            if verb == "SELECT":
                sql = utils.ensure_limit(sql, 100)
                resp = utils._api_post(utils.SELECT_QUERY_PATH, {"query": sql}, label="SELECT")
                data = resp.get("data") or {}
                entry.update({"ok": True, "rows": data.get("rows", []),
                              "rowCount": data.get("rowCount", 0)})
            elif verb == "DELETE":
                resp = utils._api_post(utils.DELETE_QUERY_PATH, payload, label="DELETE")
                entry.update({"ok": True, **(resp.get("data") or {})})
            else:
                resp = utils._api_post(utils.UPDATE_QUERY_PATH, payload, label="INSERT/UPDATE")
                entry.update({"ok": True, **(resp.get("data") or {})})
        except utils.MasterApiError as exc:
            entry.update({"ok": False, "error": str(exc), "hint": exc.hint})
            results.append(entry)
            break

        results.append(entry)

    return results


