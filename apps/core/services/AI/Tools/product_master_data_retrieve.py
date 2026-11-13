"""Read-only tools for the product-master maintenance agent.

These tools talk to the Node product-master server (default
``http://127.0.0.1:3001/api/v1``) or read the server's schema modules on disk.
Most tools never write data; SQL is *produced as text* for the caller to review
/ run manually.  The one exception is ``execute_select_query``, which runs a
read-only SELECT through the Node server's guarded endpoint using a service JWT.
All HTTP access goes through a ``requests.Session`` with ``trust_env = False`` so
a localhost request can never be hijacked by a mis-configured system proxy.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from langchain_core.tools import tool

from apps.core import config

DEFAULT_API_BASE = "http://127.0.0.1:3001/api/v1"

MASTER_API_BASE = str(config.get("MASTER_API_BASE_URL", DEFAULT_API_BASE)).rstrip("/")

NODE_SERVER_ROOT = Path(
    config.get(
        "MASTER_SERVER_ROOT",
        str(Path(config.BASE_DIR).parent / "241127_trading_company_product_master_server"),
    )
)

SCHEMA_DIR = NODE_SERVER_ROOT / "MVC" / "models" / "table_schema"

REQUEST_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))
AI_MAX_ROWS = int(config.get("AI_MAX_ROWS", "200"))
MAX_FILE_CHARS = int(config.get("AI_MAX_FILE_CHARS", "20000"))

ALLOWED_READ_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
    "master_product_name_types",
    "master_currencies",
)

# --- Guarded SELECT execution (see routes/general/index.js -> query/select) ---
SELECT_QUERY_PATH = "/general/trade-business/query/select"
SELECT_QUERY_MAX_LENGTH = 100_000

# Tables a generated SELECT may reference. Override with AI_QUERY_TABLES (.env).
QUERY_ALLOWED_TABLES = tuple(
    t.strip()
    for t in str(config.get("AI_QUERY_TABLES", ",".join(ALLOWED_READ_TABLES))).split(",")
    if t.strip()
)

# Mirrors SELECT_BLOCKED_KEYWORDS in
# 241127_trading_company_product_master_server/MVC/controller/general/trade_business_controller.js
SELECT_BLOCKED_KEYWORDS = (
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "truncate", "rename", "grant", "revoke", "call", "execute", "prepare",
    "deallocate", "set", "use", "lock", "unlock", "handler", "load",
    "outfile", "dumpfile", "into", "sleep", "benchmark",
    "get_lock", "release_lock",
)
_SELECT_BLOCKED_RE = re.compile(
    r"\b(?:" + "|".join(SELECT_BLOCKED_KEYWORDS) + r")\b", re.I
)
_SELECT_COMMENT_RE = re.compile(r"--|#|/\*|\*/")
_SELECT_SEMICOLON_RE = re.compile(r";")
_SELECT_TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z0-9_]+)`?", re.I)

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


class MasterApiError(Exception):
    """Raised when the Node product-master API cannot be reached or errors."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


_session: Optional[requests.Session] = None


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


def _api_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
            hint="The server-side SELECT guard rejected the query; simplify it and retry.",
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
    data = _api_get("/trade_business/panel/master/rows", params={"tableName": table_name})
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


def _compact(rows: List[Dict[str, Any]], *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
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


def _resolve(rows: List[Dict[str, Any]], key_field: str, name_field: str, target: str) -> tuple:
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


@tool
def list_master_tables() -> str:
    """List the master table names available via the Node API (names only, no schema)."""
    try:
        data = _api_get("/trade_business/panel/master/schema")
    except MasterApiError as exc:
        return _tool_error(exc)
    schemas = data.get("schemas", {})
    names = sorted(schemas.keys()) if isinstance(schemas, dict) else []
    allowed = [n for n in names if n in ALLOWED_READ_TABLES]
    return json.dumps(
        {"ok": True, "readable_tables": allowed},
        ensure_ascii=False,
    )


@tool
def get_master_table_schema(table_name: str) -> str:
    """Return the field schema (type, notNull, references) for one master table."""
    try:
        table_name = _check_table(table_name)
        data = _api_get(f"/trade_business/panel/master/schema/{table_name}")
    except MasterApiError as exc:
        return _tool_error(exc)

    schema = data.get("schema") or data.get("schemas", {}).get(table_name, {})
    fields = schema.get("fields", {})
    compact = {}
    for field, definition in fields.items():
        compact[field] = {
            k: v
            for k, v in definition.items()
            if k in ("type", "notNull", "primaryKey", "references", "description")
        }
    return json.dumps(
        {"ok": True, "table": schema.get("name", table_name), "fields": compact},
        ensure_ascii=False,
    )


@tool
def fetch_master_rows(table_name: str, limit: int = 100) -> str:
    """Fetch up to `limit` rows from one master table (timestamps omitted)."""
    try:
        rows = _rows(table_name)
        cleaned = _compact(rows, limit=limit)
        for row in cleaned:
            _normalise_bool(table_name, row)
    except MasterApiError as exc:
        return _tool_error(exc)
    return json.dumps(
        {"ok": True, "table": table_name, "rows": cleaned, "total": len(rows)},
        ensure_ascii=False,
    )


@tool
def fetch_category_tree() -> str:
    """Fetch every master_categories row and render it as an indented tree with ids."""
    try:
        rows = _rows("master_categories")
    except MasterApiError as exc:
        return _tool_error(exc)

    by_id: Dict[str, Dict[str, Any]] = {str(r.get("id")): r for r in rows}
    depth_cache: Dict[str, int] = {}

    def depth(node: Dict[str, Any]) -> int:
        nid = str(node.get("id"))
        if nid in depth_cache:
            return depth_cache[nid]
        d = 0
        seen = {nid}
        cur = node
        while cur.get("parent_id"):
            pid = str(cur.get("parent_id"))
            if pid not in by_id or pid in seen:
                break
            seen.add(pid)
            cur = by_id[pid]
            d += 1
        depth_cache[nid] = d
        return d

    lines = []
    for row in sorted(rows, key=lambda r: str(r.get("name", "")).lower()):
        indent = "  " * depth(row)
        lines.append(f"{indent}{row.get('name')}")
    return json.dumps(
        {"ok": True, "count": len(rows), "tree": lines}, ensure_ascii=False
    )


@tool
def fetch_attribute_catalog() -> str:
    """Fetch every master_product_attributes row (name, label, multiple_selection, id)."""
    try:
        rows = _rows("master_product_attributes")
    except MasterApiError as exc:
        return _tool_error(exc)

    lines = []
    for row in sorted(rows, key=lambda r: str(r.get("name", "")).lower()):
        lines.append(
            f"{row.get('name')} | label={row.get('label') or ''}"
            f" | multiple={bool(row.get('multiple_selection'))}"
        )
    return json.dumps({"ok": True, "count": len(rows), "attributes": lines}, ensure_ascii=False)


@tool
def fetch_attribute_assignments(category: str, summarize: bool = False) -> str:
    """List attributes already assigned to a category (by name or id). Pass summarize=True for a compact list of names."""
    try:
        categories = _rows("master_categories")
        cat, candidates = _resolve(categories, "id", "name", category)
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category not found: {category!r}",
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )

        assigns = _rows("master_product_category_attribute_assign")
        attributes = _rows("master_product_attributes")
        attr_by_id = {str(a.get("id")): a for a in attributes}

        cat_assigns = [
            a for a in assigns if str(a.get("category_id")) == str(cat.get("id"))
        ]

        resolved = []
        for a in cat_assigns:
            attr = attr_by_id.get(str(a.get("attribute_id")), {})
            resolved.append(
                {
                    "attribute_name": attr.get("name", "?"),
                    "attribute_id": a.get("attribute_id"),
                    "assign_id": a.get("id"),
                }
            )
    except MasterApiError as exc:
        return _tool_error(exc)

    if summarize:
        names = sorted({r["attribute_name"] for r in resolved})
        return json.dumps(
            {
                "ok": True,
                "category": cat.get("name"),
                "category_id": cat.get("id"),
                "assigned_attributes": names,
                "count": len(names),
            },
            ensure_ascii=False,
        )

    resolved.sort(key=lambda r: r["attribute_name"])
    return json.dumps(
        {
            "ok": True,
            "category": cat.get("name"),
            "category_id": cat.get("id"),
            "assignments": resolved,
            "count": len(resolved),
        },
        ensure_ascii=False,
    )


@tool
def read_master_schema_file(file_name: str) -> str:
    """Read a schema module (e.g. master.js, products.js) from the Node server's table_schema folder."""
    try:
        name = (file_name or "").strip()
        if name.endswith(".js"):
            name = name[:-3]
        if not IDENTIFIER_RE.match(name):
            raise MasterApiError(
                f"Invalid schema file name: {file_name!r}",
                hint="Provide a bare filename like 'master' or 'products'.",
            )

        path = (SCHEMA_DIR / f"{name}.js").resolve()
        if SCHEMA_DIR.resolve() not in path.parents:
            raise MasterApiError(
                f"Path escapes the schema directory: {name}",
                hint="Provide a bare filename only.",
            )
        if not path.is_file():
            raise MasterApiError(
                f"Schema file not found: {path}",
                hint=f"Available under: {SCHEMA_DIR}",
            )

        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n...[truncated]..."
    except (MasterApiError, OSError) as exc:
        return _tool_error(exc)

    return json.dumps(
        {"ok": True, "file": path.name, "content": content},
        ensure_ascii=False,
    )


@tool
def build_category_attribute_assign_sql(category: str, attributes: List[str]) -> str:
    """Build an idempotent MySQL INSERT..SELECT assigning attribute names to a category (name or id). Resolves ids, skips already-assigned pairs, and returns SQL plus verification statements. Read-only: nothing is executed."""
    try:
        categories = _rows("master_categories")
        all_attrs = _rows("master_product_attributes")

        cat, cat_candidates = _resolve(categories, "id", "name", category)
        if cat is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"category not found: {category!r}",
                    "candidates": cat_candidates,
                },
                ensure_ascii=False,
            )

        matched = []
        unknown = []
        for raw in attributes or []:
            target = str(raw).strip()
            if not target:
                continue
            match, candidates = _resolve(all_attrs, "id", "name", target)
            if match is not None:
                matched.append(match)
            else:
                unknown.append({"attribute": target, "candidates": candidates})

        if not matched:
            return json.dumps(
                {
                    "ok": False,
                    "error": "no attributes resolved",
                    "category": cat.get("name"),
                    "unknown": unknown,
                },
                ensure_ascii=False,
            )

        assigns = _rows("master_product_category_attribute_assign")
        existing_ids = {
            str(a.get("attribute_id"))
            for a in assigns
            if str(a.get("category_id")) == str(cat.get("id"))
        }

        already = [a["name"] for a in matched if str(a.get("id")) in existing_ids]
        to_add = [a for a in matched if str(a.get("id")) not in existing_ids]

        cat_id = _sql_literal(cat.get("id"))
        names_lit = ", ".join(_sql_literal(a["name"]) for a in to_add)
        ids_lit = ", ".join(_sql_literal(a["id"]) for a in to_add)

        if to_add:
            sql = (
                "INSERT INTO master_product_category_attribute_assign "
                "(id, category_id, attribute_id)\n"
                "SELECT UUID(), c.id, a.id\n"
                "FROM master_categories AS c\n"
                "JOIN master_product_attributes AS a ON a.name IN ("
                + names_lit
                + ")\n"
                "WHERE c.id = "
                + cat_id
                + "\n"
                "  AND NOT EXISTS (\n"
                "    SELECT 1 FROM master_product_category_attribute_assign AS x\n"
                "    WHERE x.category_id = c.id AND x.attribute_id = a.id\n"
                "  );"
            )
            verification_before = (
                "SELECT a.name AS attribute_name, a.id AS attribute_id\n"
                "FROM master_categories c\n"
                "JOIN master_product_attributes a ON a.name IN ("
                + names_lit
                + ")\n"
                "WHERE c.id = " + cat_id + ";"
            )
            verification_after = (
                "SELECT COUNT(*) AS assigned FROM master_product_category_attribute_assign\n"
                "WHERE category_id = " + cat_id + " AND attribute_id IN (" + ids_lit + ");"
            )
        else:
            sql = "-- nothing to insert: all requested attributes are already assigned"
            verification_before = "-- nothing to verify (no new rows)"
            verification_after = "-- nothing to verify (no new rows)"

    except MasterApiError as exc:
        return _tool_error(exc)

    return json.dumps(
        {
            "ok": True,
            "category": cat.get("name"),
            "category_id": cat.get("id"),
            "to_add": [a["name"] for a in to_add],
            "already_assigned": already,
            "unknown_attributes": unknown,
            "sql": sql,
            "verification_before": verification_before,
            "verification_after": verification_after,
            "notes": "UUID() generates the 36-char id; NOT EXISTS keeps the insert idempotent.",
        },
        ensure_ascii=False,
    )


ATTRIBUTE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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


@tool
def read_table_definition(table_name: str) -> str:
    """Read a table's field definitions from the Node schema files (products/master). Returns field names and their types. Read-only."""
    fields = extract_table_fields(table_name)
    if not fields:
        return json.dumps(
            {"ok": False, "error": f"table not found in schema files: {table_name}"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "table": table_name, "fields": fields}, ensure_ascii=False
    )


@tool
def build_create_attribute_sql(
    name: str,
    label: str = "",
    description: str = "",
    multiple_selection: bool = False,
    dropdown_reference: str = "",
    catalog_default_display: bool = False,
) -> str:
    """Build an idempotent MySQL INSERT creating one attribute in master_product_attributes. Validates the snake_case name. Read-only: nothing is executed."""
    try:
        name = (name or "").strip()
        label = (label or "").strip() or name
        if not ATTRIBUTE_NAME_RE.match(name):
            return json.dumps(
                {"ok": False, "error": f"invalid attribute name {name!r} (must be snake_case)"},
                ensure_ascii=False,
            )

        existing = _rows("master_product_attributes")
        if any(str(a.get("name", "")) == name for a in existing):
            return json.dumps(
                {
                    "ok": True,
                    "already_exists": True,
                    "name": name,
                    "sql": "-- attribute already exists, nothing to insert",
                    "verification_before": "-- none",
                    "verification_after": (
                        f"SELECT COUNT(*) AS cnt FROM master_product_attributes "
                        f"WHERE name = {_sql_literal(name)};"
                    ),
                },
                ensure_ascii=False,
            )

        cols = ["id", "name", "label"]
        vals = ["UUID()", _sql_literal(name), _sql_literal(label)]
        if description:
            cols.append("description")
            vals.append(_sql_literal(description))
        if dropdown_reference:
            cols.append("dropdown_reference")
            vals.append(_sql_literal(dropdown_reference))
        cols.append("multiple_selection")
        vals.append("1" if multiple_selection else "0")
        cols.append("catalog_default_display")
        vals.append("1" if catalog_default_display else "0")

        sql = (
            f"INSERT INTO master_product_attributes ({', '.join(cols)})\n"
            f"SELECT {', '.join(vals)}\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM master_product_attributes "
            f"WHERE name = {_sql_literal(name)});"
        )
        verification_before = (
            f"SELECT id, name FROM master_product_attributes WHERE name = {_sql_literal(name)};"
        )
        verification_after = (
            f"SELECT COUNT(*) AS created FROM master_product_attributes "
            f"WHERE name = {_sql_literal(name)};"
        )
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "sql": sql,
                "verification_before": verification_before,
                "verification_after": verification_after,
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


@tool
def build_attribute_dropdown_sql(attribute: str, values: List[str]) -> str:
    """Build idempotent MySQL INSERTs adding dropdown values to an attribute. Read-only: nothing is executed."""
    try:
        all_attrs = _rows("master_product_attributes")
        attr, candidates = _resolve(all_attrs, "id", "name", attribute)
        if attr is None:
            return json.dumps(
                {"ok": False, "error": f"attribute not found: {attribute!r}",
                 "candidates": candidates},
                ensure_ascii=False,
            )

        attr_id = str(attr.get("id"))
        attr_name = attr.get("name")
        statements = []
        for raw in values or []:
            value = str(raw).strip()
            if not value:
                continue
            val_lit = _sql_literal(value)
            stmt = (
                "INSERT INTO master_product_attribute_dropdown (id, attribute_id, value)\n"
                f"SELECT UUID(), {_sql_literal(attr_id)}, {val_lit}\n"
                "WHERE NOT EXISTS (SELECT 1 FROM master_product_attribute_dropdown "
                f"WHERE attribute_id = {_sql_literal(attr_id)} AND value = {val_lit});"
            )
            statements.append({"value": value, "sql": stmt})

        verification_before = (
            "SELECT value FROM master_product_attribute_dropdown "
            f"WHERE attribute_id = {_sql_literal(attr_id)};"
        )
        return json.dumps(
            {
                "ok": True,
                "attribute": attr_name,
                "attribute_id": attr_id,
                "statements": statements,
                "verification_before": verification_before,
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


def _mask_quoted(sql: str) -> str:
    """Replace quoted strings/identifiers so keywords inside them are ignored."""
    return re.sub(
        r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`",
        " ",
        sql,
    )


def _assert_select_only(query: str) -> str:
    """Validate a read-only SELECT and return the normalised statement.

    Mirrors ``assertSelectOnlyQuery`` in the Node server so bad SQL is rejected
    locally with an actionable hint before it ever reaches the API.
    """
    if not isinstance(query, str):
        raise MasterApiError(
            'query must be a string holding a single SELECT statement.',
            hint="Pass the SQL as a plain string.",
        )
    trimmed = query.strip()
    if not trimmed:
        raise MasterApiError("Query cannot be empty.", hint="Provide a SELECT statement.")
    if len(trimmed) > SELECT_QUERY_MAX_LENGTH:
        raise MasterApiError(
            f"Query exceeds the maximum allowed length of {SELECT_QUERY_MAX_LENGTH} characters.",
            hint="Shorten the query.",
        )

    normalized = trimmed[:-1].rstrip() if trimmed.endswith(";") else trimmed
    masked = _mask_quoted(normalized)

    if _SELECT_COMMENT_RE.search(masked):
        raise MasterApiError("SQL comments are not allowed.", hint="Remove comments.")
    if _SELECT_SEMICOLON_RE.search(masked):
        raise MasterApiError(
            "Only a single SELECT statement is allowed.", hint="Send one statement."
        )
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


def _assert_allowed_tables(masked: str) -> None:
    """Every FROM/JOIN identifier must be in QUERY_ALLOWED_TABLES."""
    for table in _SELECT_TABLE_RE.findall(masked):
        if table not in QUERY_ALLOWED_TABLES:
            raise MasterApiError(
                f"Table not allowed for querying: {table}",
                hint=f"Allowed tables: {', '.join(QUERY_ALLOWED_TABLES)}",
            )


def _ensure_limit(sql: str, cap: int) -> str:
    if not re.search(r"\blimit\b", _mask_quoted(sql), re.I):
        sql = sql.rstrip().rstrip(";").rstrip() + f"\nLIMIT {cap}"
    return sql


@tool
def execute_select_query(query: str, limit: int = 100) -> str:
    """Run ONE read-only SELECT against the product-master database and return its rows.

    Only tables in QUERY_ALLOWED_TABLES may be referenced. Writes, DDL, comments and
    multiple statements are rejected locally and again by the Node server. A LIMIT is
    appended when absent and returned rows are capped at AI_MAX_ROWS. Use it to verify
    existing rows / ids before proposing changes; never to write data.
    """
    try:
        sql = _assert_select_only(query)
        _assert_allowed_tables(_mask_quoted(sql))

        cap = max(1, min(int(limit or 100), AI_MAX_ROWS))
        sql = _ensure_limit(sql, cap)

        data = _api_post(SELECT_QUERY_PATH, {"query": sql})
        payload = data.get("data") or {}
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise MasterApiError(
                "Unexpected 'data.rows' payload.",
                hint="The Node API did not return a list of rows.",
            )

        total = int(payload.get("rowCount", len(rows)))
        returned = rows[:cap]
        return json.dumps(
            {
                "ok": True,
                "query": sql,
                "rowCount": total,
                "returned": len(returned),
                "truncated": len(rows) > len(returned) or total > len(returned),
                "rows": returned,
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 - render any failure as a stable JSON string
        return _tool_error(exc)


# Tools exposed to the agent.
TOOLS = [
    list_master_tables,
    get_master_table_schema,
    fetch_master_rows,
    fetch_category_tree,
    fetch_attribute_catalog,
    fetch_attribute_assignments,
    read_master_schema_file,
    read_table_definition,
    build_category_attribute_assign_sql,
    build_create_attribute_sql,
    build_attribute_dropdown_sql,
    execute_select_query,
]


if __name__ == "__main__":
    # Offline self-test: exercises the API client and SQL builder without the LLM.
    print("MASTER_API_BASE:", MASTER_API_BASE)
    print("SCHEMA_DIR:", SCHEMA_DIR)

    print("\n=== list_master_tables ===")
    print(list_master_tables.invoke({}))

    print("\n=== get_master_table_schema ===")
    print(get_master_table_schema.invoke(
        {"table_name": "master_product_category_attribute_assign"}
    ))

    print("\n=== fetch_attribute_catalog ===")
    print(fetch_attribute_catalog.invoke({}))

    print("\n=== fetch_category_tree (first 1200 chars) ===")
    print(fetch_category_tree.invoke({})[:1200])

    print("\n=== build_category_attribute_assign_sql ===")
    print(build_category_attribute_assign_sql.invoke(
        {"category": "Pet Playpens", "attributes": ["material", "color"]}
    ))

    print("\n=== execute_select_query (allowed) ===")
    print(execute_select_query.invoke(
        {"query": "SELECT id, name FROM master_categories LIMIT 3"}
    ))

    print("\n=== execute_select_query (rejected: DROP) ===")
    print(execute_select_query.invoke({"query": "DROP TABLE master_categories"}))


