"""Shared helpers for the product-master AI tools.

No ``@tool`` decorators live here: this module holds the configuration, the
HTTP client, the SQL guardrails and the small data helpers used by
:mod:`apps.core.services.AI.Tools.plan_tools` and
:mod:`apps.core.services.AI.Tools.act_tools`.

All HTTP access goes through a ``requests.Session`` with ``trust_env = False``
so a localhost request can never be hijacked by a mis-configured system proxy.
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from apps.core import config

DEFAULT_API_BASE = "http://127.0.0.1:3001/api/v1"

MASTER_API_BASE = str(config.get("MASTER_API_BASE_URL", DEFAULT_API_BASE)).rstrip("/")

NODE_SERVER_ROOT = Path(
    config.get(
        "MASTER_SERVER_ROOT",
        str(
            Path(config.BASE_DIR).parent
            / "241127_trading_company_product_master_server"
        ),
    )
)

SCHEMA_DIR = NODE_SERVER_ROOT / "MVC" / "models" / "table_schema"

REQUEST_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))
AI_MAX_ROWS = int(config.get("AI_MAX_ROWS", "200"))
MAX_FILE_CHARS = int(config.get("AI_MAX_FILE_CHARS", "20000"))
ACT_MAX_STATEMENTS = int(config.get("AI_ACT_MAX_STATEMENTS", "25"))
WEB_FETCH_MAX_CHARS = int(config.get("AI_WEB_FETCH_MAX_CHARS", "8000"))

ALLOWED_READ_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
    "master_product_name_types",
    "master_currencies",
)

# Tables a generated SELECT may reference. Override with AI_QUERY_TABLES (.env).
QUERY_ALLOWED_TABLES = tuple(
    t.strip()
    for t in str(config.get("AI_QUERY_TABLES", ",".join(ALLOWED_READ_TABLES))).split(
        ","
    )
    if t.strip()
)

DEFAULT_WRITE_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
)

# Tables an INSERT/UPDATE/DELETE may target. Override with AI_WRITE_TABLES (.env).
WRITE_ALLOWED_TABLES = tuple(
    t.strip()
    for t in str(config.get("AI_WRITE_TABLES", ",".join(DEFAULT_WRITE_TABLES))).split(
        ","
    )
    if t.strip()
)

# Guarded master endpoints (see routes/general/index.js).
SELECT_QUERY_PATH = "/general/trade-business/query/select"
UPDATE_QUERY_PATH = "/general/trade-business/query/update"
DELETE_QUERY_PATH = "/general/trade-business/query/delete"

SELECT_QUERY_MAX_LENGTH = 100_000

# Mirrors the Node controller's SELECT_BLOCKED_KEYWORDS / WRITE_BLOCKED_KEYWORDS.
SELECT_BLOCKED_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "replace",
    "drop",
    "alter",
    "create",
    "truncate",
    "rename",
    "grant",
    "revoke",
    "call",
    "execute",
    "prepare",
    "deallocate",
    "set",
    "use",
    "lock",
    "unlock",
    "handler",
    "load",
    "outfile",
    "dumpfile",
    "into",
    "sleep",
    "benchmark",
    "get_lock",
    "release_lock",
)
WRITE_BLOCKED_KEYWORDS = (
    "drop",
    "alter",
    "create",
    "truncate",
    "rename",
    "grant",
    "revoke",
    "call",
    "execute",
    "prepare",
    "deallocate",
    "use",
    "lock",
    "unlock",
    "handler",
    "load",
    "outfile",
    "dumpfile",
    "sleep",
    "benchmark",
    "get_lock",
    "release_lock",
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


class MasterApiError(Exception):
    """Raised when the Node product-master API cannot be reached or errors."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


_session: Optional[requests.Session] = None


# Get or create a cached requests session for API calls.
def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
        _session.headers.update({"Accept": "application/json"})
    return _session


def _api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET a JSON payload from the master API, normalising failures."""
    url = f"{MASTER_API_BASE}{path}"
    try:
        response = _get_session().get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:  # connection refused / timeout
        raise MasterApiError(
            f"MASTER_API_UNREACHABLE: {type(exc).__name__}: {exc}",
            hint=f"Start the Node server (npm start) in {NODE_SERVER_ROOT} and confirm {url} answers.",
        ) from exc

    if response.status_code >= 400:
        body = response.text[:300]
        raise MasterApiError(
            f"MASTER_API_HTTP_{response.status_code}: {body}",
            hint="Check the tableName / endpoint and retry.",
        )

    try:
        return response.json()
    except ValueError as exc:  # non-JSON body
        raise MasterApiError(
            f"MASTER_API_BAD_JSON: {response.text[:200]}",
            hint="Unexpected response format from the Node API.",
        ) from exc


def _service_headers() -> Dict[str, str]:
    """Authorization header using the cached Node service JWT (lazy import)."""
    from apps.core.services import node_auth

    return {"Authorization": f"Bearer {node_auth.get_service_token()}"}


def _api_post(
    path: str, payload: Dict[str, Any], *, label: str = "query"
) -> Dict[str, Any]:
    """POST a JSON payload to a guarded master endpoint, normalising failures.

    On a 401/403 the cached service token is dropped and the call retried once,
    covering both a token that expired (168h TTL) and one invalidated by a
    password change (token_version bump) on the Node side.
    """
    url = f"{MASTER_API_BASE}{path}"

    def _post() -> requests.Response:
        return _get_session().post(
            url,
            json=payload,
            headers=_service_headers(),
            timeout=REQUEST_TIMEOUT,
        )

    try:
        response = _post()
    except requests.RequestException as exc:
        raise MasterApiError(
            f"MASTER_API_UNREACHABLE: {type(exc).__name__}: {exc}",
            hint=f"Start the Node server (npm start) in {NODE_SERVER_ROOT} and confirm {url} answers.",
        ) from exc

    if response.status_code in (401, 403):
        from apps.core.services import node_auth

        node_auth.invalidate_service_token()
        try:
            response = _post()
        except requests.RequestException as exc:
            raise MasterApiError(
                f"MASTER_API_UNREACHABLE: {type(exc).__name__}: {exc}",
                hint=f"Start the Node server (npm start) in {NODE_SERVER_ROOT} and confirm {url} answers.",
            ) from exc

    if response.status_code in (401, 403):
        raise MasterApiError(
            f"MASTER_API_AUTH_{response.status_code}: {response.text[:300]}",
            hint=(
                "The service account token was rejected. Check the NodeServiceAccount "
                "row (or MASTER_API_EMAIL / MASTER_API_PASSWORD) belongs to a Node user "
                "with an admin / system-admin / developer role."
            ),
        )
    if response.status_code >= 400:
        raise MasterApiError(
            f"MASTER_API_HTTP_{response.status_code}: {response.text[:300]}",
            hint=f"The server-side {label} guard rejected the request; simplify it and retry.",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise MasterApiError(
            f"MASTER_API_BAD_JSON: {response.text[:200]}",
            hint="Unexpected response format from the Node API.",
        ) from exc


def _tool_error(exc: BaseException) -> str:
    """Render an exception as a stable JSON string the agent can act on."""
    hint = getattr(exc, "hint", "") or "Inspect the error and retry with valid inputs."
    return json.dumps(
        {"ok": False, "error": str(exc), "hint": hint},
        ensure_ascii=False,
    )


def _check_table(table_name: str) -> str:
    table_name = (table_name or "").strip()
    if not IDENTIFIER_RE.match(table_name):
        raise MasterApiError(
            f"Invalid table name: {table_name!r}",
            hint=f"Allowed tables: {', '.join(ALLOWED_READ_TABLES)}",
        )
    if table_name not in ALLOWED_READ_TABLES:
        raise MasterApiError(
            f"Table not allowed for reading: {table_name}",
            hint=f"Allowed tables: {', '.join(ALLOWED_READ_TABLES)}",
        )
    return table_name


def _rows(table_name: str) -> List[Dict[str, Any]]:
    """Fetch all rows for an allowed master table (raw list of dicts)."""
    table_name = _check_table(table_name)
    data = _api_get(
        "/trade_business/panel/master/rows", params={"tableName": table_name}
    )
    rows = data.get("results", [])
    if not isinstance(rows, list):
        raise MasterApiError(
            f"Unexpected 'results' payload for {table_name}",
            hint="The Node API did not return a list of rows.",
        )
    return rows


def _sql_literal(value: Any) -> str:
    """Escape a value for a single-quoted MySQL string literal."""
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


_BOOL_FIELDS = {
    "master_product_attributes": {"multiple_selection", "catalog_default_display"},
}


def _compact(
    rows: List[Dict[str, Any]], *, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Drop timestamps and cap the row count."""
    limit = min(limit if limit is not None else AI_MAX_ROWS, AI_MAX_ROWS)
    return [
        {k: v for k, v in row.items() if k not in ("created_at", "updated_at")}
        for row in rows[:limit]
    ]


def _normalise_bool(table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
    for field in _BOOL_FIELDS.get(table_name, ()):
        if field in row and row[field] in (0, 1):
            row[field] = bool(row[field])
    return row


def _resolve(
    rows: List[Dict[str, Any]], key_field: str, name_field: str, target: str
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Resolve a target (id or name) against rows. Returns (match, candidates)."""
    target = (target or "").strip()
    if not target:
        return None, []

    for row in rows:
        if str(row.get(key_field, "")) == target:
            return row, []

    low = target.lower()
    exact = [row for row in rows if str(row.get(name_field, "")).strip().lower() == low]
    if exact:
        return exact[0], []

    names = [str(row.get(name_field, "")) for row in rows]
    candidates = difflib.get_close_matches(target, names, n=5, cutoff=0.4)
    return None, candidates


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
    if target is None:
        raise MasterApiError(
            "Could not determine the target table.",
            hint="Start with INSERT INTO / UPDATE / DELETE FROM <table>.",
        )
    if target not in WRITE_ALLOWED_TABLES:
        raise MasterApiError(
            f"Table not allowed for writing: {target}",
            hint=f"Allowed tables: {', '.join(WRITE_ALLOWED_TABLES)}",
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
    for table in _TABLE_RE.findall(masked):
        if table not in QUERY_ALLOWED_TABLES:
            raise MasterApiError(
                f"Table not allowed for querying: {table}",
                hint=f"Allowed tables: {', '.join(QUERY_ALLOWED_TABLES)}",
            )


def ensure_limit(sql: str, cap: int) -> str:
    if not re.search(r"\blimit\b", _mask_quoted(sql), re.I):
        sql = sql.rstrip().rstrip(";").rstrip() + f"\nLIMIT {cap}"
    return sql


def has_where_clause(sql: str) -> bool:
    """True when the quote-masked statement contains a WHERE keyword."""
    return bool(re.search(r"\bwhere\b", _mask_quoted(sql), re.I))


def extract_table_fields(table_name: str) -> Optional[Dict[str, Any]]:
    """Extract the ``fields`` block for a table from the Node schema files on disk."""
    target = (table_name or "").strip()
    if not target:
        return None
    for base in ("master", "products"):
        path = SCHEMA_DIR / f"{base}.js"
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        fields = _extract_fields_from_js(content, target)
        if fields:
            return fields
    return None


def _extract_fields_from_js(content: str, table_name: str) -> Optional[Dict[str, Any]]:
    marker = f"name: '{table_name}',"
    idx = content.find(marker)
    if idx == -1:
        return None
    start = content.find("fields: {", idx)
    if start == -1:
        return None

    fields: Dict[str, Any] = {}
    current: Optional[Dict[str, Any]] = None
    for line in content[start:].splitlines()[1:]:
        if line.startswith("    }") or line.startswith("    },"):
            break  # end of the fields object
        if line.startswith("      ") and line.rstrip().endswith(": {"):
            raw = line.strip()[:-1].strip()  # "id: {"
            name = raw.rstrip(":").strip()
            current = {}
            fields[name] = current
            continue
        if current is not None and line.startswith("        "):
            kv = line.strip().rstrip(",").split(":", 1)
            if len(kv) == 2:
                current[kv[0].strip()] = kv[1].strip().strip("'")
    return fields or None


# ---- Web fetch helpers (SSRF-guarded) --------------------------------------


def validate_url(url: str) -> str:
    """Reject non-http(s) and private/loopback hosts; return the URL unchanged."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    allow_private = str(config.get("AI_WEB_ALLOW_PRIVATE_HOSTS", "0")).lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_private:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"blocked private/loopback host: {host}")
    return url


def fetch_text(url: str, max_chars: Optional[int] = None) -> str:
    """Fetch a page and return clean, whitespace-collapsed text."""
    from bs4 import BeautifulSoup

    max_chars = max_chars or WEB_FETCH_MAX_CHARS
    validate_url(url)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


def fetch_html(url: str, max_chars: Optional[int] = None) -> str:
    """Fetch a page and return the raw HTML markup (truncated)."""
    max_chars = max_chars or WEB_FETCH_MAX_CHARS
    validate_url(url)
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    return resp.text[:max_chars]
