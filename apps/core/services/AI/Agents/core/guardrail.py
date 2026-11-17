"""The single SQL guardrail for the AI agents.

Every statement that could reach the database is checked here, whether it came
from a worker draft, a plan, or an ``/act`` execution.  It is sqlglot-backed
(fail-closed): statements are classified from their AST rather than a leading
token, so ``SELECT 1; DELETE ...``, CTE writes and multi-statement strings are
caught.  Workers and managers both import this module -- there is no second copy.

Modes
-----
- ``plan``: SELECT / INSERT / UPDATE (writes are shown for review).
- ``act``: adds DELETE; DELETE without WHERE is rejected unless
  ``AI_ACT_ALLOW_DELETE_ALL``; UPDATE/DELETE get a LIMIT cap.
- ``draft``: worker output; SELECT / INSERT only, table policy is caller-supplied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .settings import (
    AI_ACT_ALLOW_DELETE_ALL,
    AI_ACT_MAX_AFFECTED_ROWS,
    WRITE_ALLOWED_TABLES,
)
from .contracts import as_sql_list

try:  # sqlglot is a declared dependency; fall back to a regex scanner if absent.
    import sqlglot
    from sqlglot import exp as _exp

    _HAS_SQLGLOT = True
except Exception:  # noqa: BLE001
    sqlglot = None
    _exp = None
    _HAS_SQLGLOT = False

# Statements the agent may never emit in any mode (kept for backward-compatible
# imports; these verbs are also rejected by the AST classifier below).
FORBIDDEN_SQL_VERBS = (
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "RENAME",
    "REPLACE",
)

# Verbs allowed per mode.
_MODE_WRITE_VERBS = {
    "plan": ("SELECT", "INSERT", "UPDATE"),
    "act": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "draft": ("SELECT", "INSERT"),
}

_FIRST_WORD_RE = re.compile(r"^\s*(?:--[^\n]*\n)?\s*([A-Za-z]+)")
_COMMENT_RE = re.compile(r"--|#|/\*|\*/")
_SEMICOLON_RE = re.compile(r";")


@dataclass
class GuardDecision:
    ok: bool
    verb: str = ""
    target: Optional[str] = None
    statement: str = ""  # normalised (possibly with a LIMIT appended)
    reason: str = ""
    max_rows: Optional[int] = None


def _mask_quoted(sql: str) -> str:
    """Blank quoted strings/identifiers so keywords inside them are ignored."""
    return re.sub(
        r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`",
        "",
        sql,
    )


def _single_expression(sql: str) -> Tuple[Optional[Any], str]:
    """Parse ``sql`` into exactly one AST expression, or (None, reason)."""
    if not _HAS_SQLGLOT:
        return None, "sqlglot unavailable"
    try:
        statements = sqlglot.parse(sql, read="mysql")
    except Exception as exc:  # noqa: BLE001
        return None, f"unparseable SQL: {exc}"
    if len(statements) == 0:
        return None, "empty SQL"
    if len(statements) != 1:
        return None, "multiple statements are not allowed"
    return statements[0], ""


def _verb_from_expr(expr: Any) -> str:
    if isinstance(expr, (_exp.Select, _exp.Union, _exp.Subquery)):
        return "SELECT"
    if isinstance(expr, _exp.Insert):
        return "INSERT"
    if isinstance(expr, _exp.Update):
        return "UPDATE"
    if isinstance(expr, _exp.Delete):
        return "DELETE"
    if isinstance(expr, _exp.Drop):
        return "DROP"
    if isinstance(expr, _exp.Create):
        return "CREATE"
    if isinstance(expr, _exp.Alter):
        return "ALTER"
    if isinstance(expr, _exp.TruncateTable):
        return "TRUNCATE"
    key = str(getattr(expr, "key", "") or "").upper()
    return key


def _target_from_expr(expr: Any) -> Optional[str]:
    # For INSERT/UPDATE/DELETE the write target is the primary table.  find_all
    # walks the AST in source order, so the target precedes any SELECT source.
    for node in expr.find_all(_exp.Table):
        name = getattr(node, "name", None)
        if name:
            return str(name)
    return None


def _has_where(sql: str, expr: Optional[Any]) -> bool:
    if expr is not None and _HAS_SQLGLOT:
        try:
            return next(expr.find_all(_exp.Where), None) is not None
        except Exception:  # noqa: BLE001
            pass
    return bool(re.search(r"\bwhere\b", _mask_quoted(sql), re.I))


def _has_limit(sql: str, expr: Optional[Any]) -> bool:
    if expr is not None and _HAS_SQLGLOT:
        try:
            return next(expr.find_all(_exp.Limit), None) is not None
        except Exception:  # noqa: BLE001
            pass
    return bool(re.search(r"\blimit\b", _mask_quoted(sql), re.I))


def first_verb(statement: str) -> str:
    """Return the uppercase leading verb of a statement (AST-based)."""
    sql = str(statement or "").strip()
    if not sql:
        return ""
    if _HAS_SQLGLOT:
        expr, reason = _single_expression(sql)
        if expr is not None:
            return _verb_from_expr(expr)
    match = _FIRST_WORD_RE.match(sql)
    return match.group(1).upper() if match else ""


def verb_of(statement: str) -> str:
    """Alias for :func:`first_verb` used by the act path."""
    return first_verb(statement)


def write_target(statement: str) -> Optional[str]:
    """Return the write target table of a statement, or None."""
    sql = str(statement or "").strip()
    if not sql:
        return None
    if _HAS_SQLGLOT:
        expr, reason = _single_expression(sql)
        if expr is not None:
            return _target_from_expr(expr)
    match = re.search(
        r"\b(?:insert\s+into|update|delete\s+from)\s+([a-zA-Z0-9_`]+)",
        sql,
        re.I,
    )
    return match.group(1).strip("`") if match else None


def validate_statement(
    statement: str,
    *,
    mode: str = "plan",
    table_policy: str = "allowlist",
    allowed_tables: Optional[Iterable[str]] = None,
    append_limit: Optional[int] = None,
    allow_delete_all: Optional[bool] = None,
) -> GuardDecision:
    """Validate a single statement and return a :class:`GuardDecision`.

    ``table_policy`` is ``"allowlist"`` (enforce ``WRITE_ALLOWED_TABLES`` or
    ``allowed_tables``) or ``"advisory"`` (block forbidden verbs only).
    """
    mode = mode if mode in _MODE_WRITE_VERBS else "plan"
    sql = str(statement or "").strip()
    if not sql:
        return GuardDecision(ok=False, verb="", statement=sql, reason="empty statement")

    masked = _mask_quoted(sql)
    if _COMMENT_RE.search(masked):
        return GuardDecision(
            ok=False, verb="", statement=sql, reason="SQL comments are not allowed"
        )

    expr, reason = _single_expression(sql)
    if expr is None:
        return GuardDecision(ok=False, verb="", statement=sql, reason=reason)

    verb = _verb_from_expr(expr)
    if verb in FORBIDDEN_SQL_VERBS:
        return GuardDecision(
            ok=False,
            verb=verb,
            statement=sql,
            reason=f"Removed unsafe SQL ({verb}): {sql[:120]}",
        )

    if verb not in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        return GuardDecision(
            ok=False,
            verb=verb,
            statement=sql,
            reason=f"Removed unsafe SQL ({verb}): {sql[:120]}",
        )

    allowed_verbs = _MODE_WRITE_VERBS[mode]
    if verb in ("INSERT", "UPDATE", "DELETE") and verb not in allowed_verbs:
        return GuardDecision(
            ok=False,
            verb=verb,
            statement=sql,
            reason=f"Removed {verb} (not allowed in {mode} mode): {sql[:120]}",
        )

    target = _target_from_expr(expr)

    if verb in ("INSERT", "UPDATE", "DELETE") and table_policy == "allowlist":
        allow = {str(t).lower() for t in (allowed_tables or WRITE_ALLOWED_TABLES)}
        if target is None or target.lower() not in allow:
            return GuardDecision(
                ok=False,
                verb=verb,
                target=target,
                statement=sql,
                reason=f"Removed write outside allowed tables: {sql[:120]}",
            )

    normalised = sql
    max_rows: Optional[int] = None

    if mode == "act" and verb == "DELETE":
        allow_all = (
            AI_ACT_ALLOW_DELETE_ALL if allow_delete_all is None else allow_delete_all
        )
        if not _has_where(sql, expr) and not allow_all:
            return GuardDecision(
                ok=False,
                verb=verb,
                target=target,
                statement=sql,
                reason="Removed DELETE without a WHERE clause (set "
                "AI_ACT_ALLOW_DELETE_ALL=1 to permit): " + sql[:120],
            )

    if mode == "act" and verb in ("UPDATE", "DELETE"):
        cap = append_limit if append_limit is not None else AI_ACT_MAX_AFFECTED_ROWS
        if cap is not None and not _has_limit(sql, expr):
            normalised = sql.rstrip(";").rstrip() + f" LIMIT {int(cap)}"
        max_rows = int(cap) if cap is not None else None

    return GuardDecision(
        ok=True, verb=verb, target=target, statement=normalised, max_rows=max_rows
    )


def filter_sql(
    steps: Any,
    *,
    mode: str = "plan",
    table_policy: str = "allowlist",
    allowed_tables: Optional[Iterable[str]] = None,
    append_limit: Optional[int] = None,
    allow_delete_all: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Filter a list of SQL steps; return ``(safe, skipped, risks)``."""
    safe: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    risks: List[str] = []
    for step in as_sql_list(steps):
        statement = step["statement"].strip()
        if not statement:
            if step["purpose"] or step["verification"]:
                safe.append(step)
            continue

        decision = validate_statement(
            statement,
            mode=mode,
            table_policy=table_policy,
            allowed_tables=allowed_tables,
            append_limit=append_limit,
            allow_delete_all=allow_delete_all,
        )
        if not decision.ok:
            risks.append(decision.reason)
            skipped.append({"verb": decision.verb, "statement": statement})
            continue

        new_step = dict(step)
        new_step["statement"] = decision.statement
        if decision.max_rows is not None:
            new_step["max_rows"] = decision.max_rows
        safe.append(new_step)
    return safe, skipped, risks


# Plan-mode guardrails for SQL statements.
def assert_plan_sql(
    plan: Dict[str, Any], *, mode: str = "plan", allow_delete_all: Optional[bool] = None
) -> Dict[str, Any]:
    """Enforce the guardrail on a plan's ``sql`` (allowlist) and ``drafts``.

    ``drafts`` are worker proposals and are checked with an advisory table policy
    (a worker may legitimately draft ``product_sale_prices_by_qty``, which
    is not a write-allowed table).  ``sql`` is what ``/act`` executes, so it is
    checked against ``WRITE_ALLOWED_TABLES`` and, in act mode, capped with LIMIT.
    """
    mode = "act" if str(mode).lower() == "act" else "plan"
    append_limit = AI_ACT_MAX_AFFECTED_ROWS if mode == "act" else None

    safe_sql, skipped_sql, risks_sql = filter_sql(
        plan.get("sql"),
        mode=mode,
        table_policy="allowlist",
        append_limit=append_limit,
        allow_delete_all=allow_delete_all,
    )
    safe_drafts, skipped_drafts, risks_drafts = filter_sql(
        plan.get("drafts"),
        mode=mode,
        table_policy="advisory",
        allow_delete_all=allow_delete_all,
    )

    plan["sql"] = safe_sql
    plan["drafts"] = safe_drafts
    plan["skipped_sql"] = skipped_sql + skipped_drafts
    plan["risks"] = list(plan.get("risks", [])) + risks_sql + risks_drafts
    return plan


def assert_plan_read_only(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible alias for the plan-mode guardrail."""
    return assert_plan_sql(plan, mode="plan")


def assert_draft_sql(steps: Any, allowed_tables: Iterable[str]) -> Any:
    """Worker-side guardrail (kept for compatibility).

    Drops any non-SELECT/INSERT statement and any INSERT outside ``allowed_tables``.
    Returns ``(safe, risks)`` with the original statement text preserved.
    """
    safe, _skipped, risks = filter_sql(
        steps,
        mode="draft",
        table_policy="advisory",
    )
    allow = {str(t).lower() for t in allowed_tables}
    filtered: List[Dict[str, str]] = []
    final_risks: List[str] = list(risks)
    for step in safe:
        statement = step.get("statement", "")
        verb = first_verb(statement)
        if verb == "INSERT":
            target = write_target(statement)
            if target is None or target.lower() not in allow:
                final_risks.append(
                    f"Removed INSERT outside allowed tables: {statement[:120]}"
                )
                continue
        if statement.strip() or step.get("purpose") or step.get("verification"):
            filtered.append(
                {k: step.get(k, "") for k in ("purpose", "statement", "verification")}
            )
    return filtered, final_risks
