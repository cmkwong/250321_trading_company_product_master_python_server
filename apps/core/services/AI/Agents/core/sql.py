"""String-level SQL statement guards and builders (mirror the Node guarded endpoints).

Every statement that reaches the master API is normalised and checked here --
single statement, no comments, no DDL, write target in ``WRITE_ALLOWED_TABLES``
and every ``FROM/JOIN`` table in ``QUERY_ALLOWED_TABLES`` -- before it is sent.
The AST-based policy checks (plan/draft/act verbs, LIMIT caps) live in
:mod:`core.guardrail`; this module is the text-level mirror of the Node
controller plus the small SQL builders used by the tools and workers.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from . import settings
from .contracts import MasterApiError

SELECT_QUERY_MAX_LENGTH = 100_000

# Mirrors the Node controller's SELECT_BLOCKED_KEYWORDS / WRITE_BLOCKED_KEYWORDS.
SELECT_BLOCKED_KEYWORDS = (
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "truncate", "rename", "grant", "revoke", "call", "execute", "prepare",
    "deallocate", "set", "use", "lock", "unlock", "handler", "load", "outfile",
    "dumpfile", "into", "sleep", "benchmark", "get_lock", "release_lock",
)
WRITE_BLOCKED_KEYWORDS = (
    "drop", "alter", "create", "truncate", "rename", "grant", "revoke", "call",
    "execute", "prepare", "deallocate", "use", "lock", "unlock", "handler",
    "load", "outfile", "dumpfile", "sleep", "benchmark", "get_lock", "release_lock",
)

_SELECT_BLOCKED_RE = re.compile(
    r"\b(?:" + "|".join(SELECT_BLOCKED_KEYWORDS) + r")\b", re.I
)
_WRITE_BLOCKED_RE = re.compile(
    r"\b(?:" + "|".join(WRITE_BLOCKED_KEYWORDS) + r")\b", re.I
)
_COMMENT_RE = re.compile(r"--|#|/\*|\*/")
_SEMICOLON_RE = re.compile(r";")
_TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z0-9_]+)`?", re.I)
_WRITE_TARGET_RE = re.compile(
    r"^\s*(?:insert\s+into|update|delete\s+from)\s+`?([A-Za-z0-9_]+)`?",
    re.I,
)

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
ATTRIBUTE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _mask_quoted(sql: str) -> str:
    """Replace quoted strings/identifiers so keywords inside them are ignored."""
    return re.sub(
        r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`",
        " ",
        sql,
    )


def first_verb(statement: str) -> str:
    """Return the leading SQL keyword of a statement, uppercased ('' if none)."""
    match = re.match(r"\s*(--[^\n]*\n)?\s*([A-Za-z]+)", statement or "")
    return match.group(2).upper() if match else ""


def write_target(normalized: str) -> Optional[str]:
    """Return the target table of INSERT INTO / UPDATE / DELETE FROM ('' if none)."""
    match = _WRITE_TARGET_RE.match(normalized)
    return match.group(1).lower() if match else None


def _normalise_statement(query: str, label: str) -> Tuple[str, str]:
    """Validate one statement's shape; return (normalized, masked)."""
    if not isinstance(query, str):
        raise MasterApiError(
            f"query must be a string holding a single {label} statement.",
            hint="Pass the SQL as a plain string.",
        )
    trimmed = query.strip()
    if not trimmed:
        raise MasterApiError(
            "Query cannot be empty.", hint=f"Provide a {label} statement."
        )
    if len(trimmed) > SELECT_QUERY_MAX_LENGTH:
        raise MasterApiError(
            f"Query exceeds the maximum allowed length of {SELECT_QUERY_MAX_LENGTH} characters.",
            hint="Shorten the query.",
        )
    normalized = trimmed[:-1].rstrip() if trimmed.endswith(";") else trimmed
    masked = _mask_quoted(normalized)
    if _COMMENT_RE.search(masked):
        raise MasterApiError("SQL comments are not allowed.", hint="Remove comments.")
    if _SEMICOLON_RE.search(masked):
        raise MasterApiError(
            "Only a single SQL statement is allowed.", hint="Send one statement."
        )
    return normalized, masked


def assert_select_only(query: str) -> str:
    """Validate a read-only SELECT and return the normalised statement.

    Mirrors ``assertSelectOnlyQuery`` in the Node server so bad SQL is rejected
    locally with an actionable hint before it ever reaches the API.
    """
    normalized, masked = _normalise_statement(query, "SELECT")
    if not re.match(r"^select\b", masked.strip(), re.I):
        raise MasterApiError(
            "Only SELECT queries are allowed.", hint="Start the query with SELECT."
        )
    if _SELECT_BLOCKED_RE.search(masked):
        raise MasterApiError(
            "Query contains forbidden keywords; only read-only SELECT is allowed.",
            hint="Remove write/DDL keywords.",
        )
    return normalized


def _assert_write_target(normalized: str) -> str:
    target = write_target(normalized)
    allowed = settings.WRITE_ALLOWED_TABLES
    if target is None:
        raise MasterApiError(
            "Could not determine the target table.",
            hint="Start with INSERT INTO / UPDATE / DELETE FROM <table>.",
        )
    if target not in allowed:
        raise MasterApiError(
            f"Table not allowed for writing: {target}",
            hint=f"Allowed tables: {', '.join(allowed)}",
        )
    return target


def assert_write_only(query: str, verbs: Tuple[str, ...] = ("INSERT", "UPDATE")) -> str:
    """Validate a single INSERT/UPDATE (or DELETE) and return the normalised statement.

    Mirrors the Node ``assertInsertOrUpdateOnlyQuery`` / ``assertDeleteOnlyQuery``
    guards, plus a local table allowlist (WRITE_ALLOWED_TABLES).
    """
    normalized, masked = _normalise_statement(query, " or ".join(verbs))
    pattern = r"^(?:" + "|".join(v.lower() for v in verbs) + r")\b"
    if not re.match(pattern, masked.strip(), re.I):
        raise MasterApiError(
            f"Only {'/'.join(verbs)} queries are allowed.",
            hint=f"Start the query with {' or '.join(verbs)}.",
        )
    if _WRITE_BLOCKED_RE.search(masked):
        raise MasterApiError(
            "Query contains forbidden keywords.",
            hint=f"Only {'/'.join(verbs)} statements are allowed.",
        )
    _assert_write_target(normalized)
    return normalized


def assert_delete_only(query: str) -> str:
    """Validate a single DELETE and return the normalised statement."""
    return assert_write_only(query, verbs=("DELETE",))


def _assert_allowed_tables(masked: str) -> None:
    """Every FROM/JOIN identifier must be in QUERY_ALLOWED_TABLES."""
    allowed = settings.QUERY_ALLOWED_TABLES
    for table in _TABLE_RE.findall(masked):
        if table not in allowed:
            raise MasterApiError(
                f"Table not allowed for querying: {table}",
                hint=f"Allowed tables: {', '.join(allowed)}",
            )


def ensure_limit(sql: str, cap: int) -> str:
    if not re.search(r"\blimit\b", _mask_quoted(sql), re.I):
        sql = sql.rstrip().rstrip(";").rstrip() + f"\nLIMIT {cap}"
    return sql


def has_where_clause(sql: str) -> bool:
    """True when the quote-masked statement contains a WHERE keyword."""
    return bool(re.search(r"\bwhere\b", _mask_quoted(sql), re.I))


def _sql_literal(value: Any) -> str:
    """Escape a value for a single-quoted MySQL string literal."""
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"

