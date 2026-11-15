"""Execution tools for the product-master agent (``/act`` mode).

These tools run *approved* write statements through the Node server's guarded
endpoints and verify with read-only SELECTs.  They are only wired into the
agent when it is constructed in ``act`` mode, so a normal ``/plan`` request can
never write data.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from . import settings as _settings
from .contracts import MasterApiError, _tool_error
from . import guardrail as _guardrail
from . import master_api, sql as sqlguard
from .plan_tools import execute_select_query


@tool
def execute_update_query(query: str, confirm: str = "") -> str:
    """Execute ONE approved INSERT/UPDATE via /trade-business/query/update.

    The target table must be in WRITE_ALLOWED_TABLES. An UPDATE without a WHERE
    clause requires confirm='UPDATE_ALL_ROWS'. Returns affected/changed rows and
    insertId from the Node server.
    """
    try:
        sql = sqlguard.assert_write_only(query, verbs=("INSERT", "UPDATE"))
        payload = {"query": sql}
        if confirm:
            payload["confirm"] = confirm
        data = master_api._api_post(
            master_api.UPDATE_QUERY_PATH, payload, label="INSERT/UPDATE"
        )
        result = data.get("data") or {}
        return json.dumps(
            {
                "ok": True,
                "verb": sqlguard.first_verb(sql),
                "affectedRows": result.get("affectedRows"),
                "changedRows": result.get("changedRows"),
                "insertId": result.get("insertId"),
                "warningStatus": result.get("warningStatus"),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc)


@tool
def execute_delete_query(query: str, confirm: str = "") -> str:
    """Execute ONE approved DELETE via /trade-business/query/delete.

    The target table must be in WRITE_ALLOWED_TABLES. A DELETE without a WHERE
    clause requires confirm='DELETE_ALL_ROWS'.
    """
    try:
        sql = sqlguard.assert_delete_only(query)
        payload = {"query": sql}
        if confirm:
            payload["confirm"] = confirm
        data = master_api._api_post(
            master_api.DELETE_QUERY_PATH, payload, label="DELETE"
        )
        result = data.get("data") or {}
        return json.dumps(
            {
                "ok": True,
                "verb": "DELETE",
                "affectedRows": result.get("affectedRows"),
                "info": result.get("info"),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc)


# Tools the manager may call directly in /act mode.
ACT_TOOLS = [execute_select_query, execute_update_query, execute_delete_query]

_ROUTE = {
    "SELECT": "select",
    "INSERT": "update",
    "UPDATE": "update",
    "DELETE": "delete",
}


def execute_plan(
    plan: Dict[str, Any],
    *,
    plan_id: Optional[str] = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> List[Dict[str, Any]]:
    """Deterministically run the approved statements in ``plan['sql']``.

    The plan is re-checked against the single guardrail (act policy) immediately
    before execution, then either posted as ONE transactional batch to the Node
    server (``AI_ACT_BATCH``, the default) or executed sequentially per statement.
    ``plan['drafts']`` is never executed.
    """
    if not isinstance(plan, dict):
        return []
    plan = _guardrail.assert_plan_sql(plan, mode="act")
    steps = plan.get("sql", [])[: _settings.AI_ACT_MAX_STATEMENTS]
    if not isinstance(steps, list) or not steps:
        return []

    entries: List[Dict[str, Any]] = [
        {
            "purpose": str(s.get("purpose", "")),
            "statement": str(s.get("statement", "")),
            "verb": _guardrail.verb_of(str(s.get("statement", ""))),
        }
        for s in steps
        if isinstance(s, dict) and str(s.get("statement", "")).strip()
    ]
    if not entries:
        return []

    if _settings.AI_ACT_BATCH:
        return _execute_batch(
            entries, steps, plan_id=plan_id, dry_run=dry_run, confirm=confirm
        )
    return _execute_sequential(entries, dry_run=dry_run, confirm=confirm)


def _single_confirm_token(entries: List[Dict[str, Any]]) -> Optional[str]:
    """Return one confirm token when every no-WHERE write shares a single verb."""
    tokens = set()
    for e in entries:
        if e["verb"] == "UPDATE" and not sqlguard.has_where_clause(e["statement"]):
            tokens.add("UPDATE_ALL_ROWS")
        elif e["verb"] == "DELETE" and not sqlguard.has_where_clause(e["statement"]):
            tokens.add("DELETE_ALL_ROWS")
    return tokens.pop() if len(tokens) == 1 else None


def _execute_batch(
    entries: List[Dict[str, Any]],
    steps: List[Dict[str, Any]],
    *,
    plan_id: Optional[str],
    dry_run: bool,
    confirm: bool,
) -> List[Dict[str, Any]]:
    """POST the whole plan to the transactional /query/batch endpoint."""
    # Preserve the full-table-write safety in batch mode: a no-WHERE UPDATE (or
    # DELETE, though the guardrail strips those) needs an explicit confirm.
    unconfirmed = [
        e
        for e in entries
        if e["verb"] in ("UPDATE", "DELETE")
        and not sqlguard.has_where_clause(e["statement"])
    ]
    if unconfirmed and not confirm:
        results = []
        for e in entries:
            out = dict(e)
            if e["verb"] in ("UPDATE", "DELETE") and not sqlguard.has_where_clause(
                e["statement"]
            ):
                out.update(
                    {
                        "ok": False,
                        "needs_confirmation": True,
                        "error": "write has no WHERE clause; pass confirm=True (or the matching confirm token)",
                    }
                )
            else:
                out.update({"ok": True})
            results.append(out)
        return results

    payload: Dict[str, Any] = {
        "planId": plan_id,
        "dryRun": dry_run,
        "statements": [
            {"statement": s.get("statement", ""), "maxRows": s.get("max_rows")}
            for s in steps
        ],
    }
    if confirm:
        token = _single_confirm_token(entries)
        if token:
            payload["confirm"] = token

    resp = master_api._api_post(master_api.BATCH_QUERY_PATH, payload, label="batch")
    data = resp.get("data") or {}
    by_index = {r.get("index"): r for r in data.get("results", [])}

    results = []
    for i, entry in enumerate(entries):
        row = by_index.get(i) or {}
        out = dict(entry)
        if out["verb"] == "SELECT":
            out.update(
                {
                    "ok": True,
                    "rows": row.get("rows", []),
                    "rowCount": row.get("rowCount", 0),
                }
            )
        else:
            out.update(
                {
                    "ok": True,
                    "affectedRows": row.get("affectedRows"),
                    "changedRows": row.get("changedRows"),
                    "insertId": row.get("insertId"),
                    "info": row.get("info"),
                }
            )
        if dry_run:
            out.update({"dry_run": True, "committed": False})
        results.append(out)
    return results


def _execute_sequential(
    entries: List[Dict[str, Any]],
    *,
    dry_run: bool,
    confirm: bool,
) -> List[Dict[str, Any]]:
    """Fallback: execute each statement individually, stopping at first failure."""
    results: List[Dict[str, Any]] = []
    for entry in entries:
        statement = entry["statement"]
        verb = entry["verb"]
        out: Dict[str, Any] = {
            "purpose": entry["purpose"],
            "statement": statement,
            "verb": verb,
        }

        if verb not in _ROUTE:
            out.update(
                {
                    "ok": False,
                    "skipped": True,
                    "error": f"verb {verb or '?'} is not executable",
                }
            )
            results.append(out)
            continue

        try:
            if verb == "SELECT":
                sql = sqlguard.assert_select_only(statement)
            elif verb == "DELETE":
                sql = sqlguard.assert_delete_only(statement)
            else:
                sql = sqlguard.assert_write_only(statement)
        except MasterApiError as exc:
            out.update({"ok": False, "error": str(exc), "hint": exc.hint})
            results.append(out)
            continue

        needs_confirm = verb in ("UPDATE", "DELETE") and not sqlguard.has_where_clause(
            sql
        )

        if dry_run:
            out.update(
                {
                    "ok": True,
                    "dry_run": True,
                    "needs_confirmation": needs_confirm,
                    "route": _ROUTE[verb],
                }
            )
            results.append(out)
            continue

        if needs_confirm and not confirm:
            out.update(
                {
                    "ok": False,
                    "needs_confirmation": True,
                    "error": "write has no WHERE clause; pass confirm=True (or the matching confirm token)",
                }
            )
            results.append(out)
            continue

        payload: Dict[str, Any] = {"query": sql}
        if needs_confirm:
            payload["confirm"] = (
                "DELETE_ALL_ROWS" if verb == "DELETE" else "UPDATE_ALL_ROWS"
            )

        try:
            if verb == "SELECT":
                sql = sqlguard.ensure_limit(sql, 100)
                resp = master_api._api_post(
                    master_api.SELECT_QUERY_PATH, {"query": sql}, label="SELECT"
                )
                data = resp.get("data") or {}
                out.update(
                    {
                        "ok": True,
                        "rows": data.get("rows", []),
                        "rowCount": data.get("rowCount", 0),
                    }
                )
            elif verb == "DELETE":
                resp = master_api._api_post(
                    master_api.DELETE_QUERY_PATH, payload, label="DELETE"
                )
                out.update({"ok": True, **(resp.get("data") or {})})
            else:
                resp = master_api._api_post(
                    master_api.UPDATE_QUERY_PATH, payload, label="INSERT/UPDATE"
                )
                out.update({"ok": True, **(resp.get("data") or {})})
        except MasterApiError as exc:
            out.update({"ok": False, "error": str(exc), "hint": exc.hint})
            results.append(out)
            break

        results.append(out)

    return results
