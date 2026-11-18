"""Planning / read-only tools shared across domain managers (``/plan`` mode).

These generic tools read the master/product-master database (via the Node
server) or the server's schema modules on disk, draft write SQL as *text* for
human review, run read-only SELECT queries, and fetch/search the web.  Nothing
here writes data -- every INSERT/UPDATE the tools draft is returned as text to
be executed later via ``/act`` mode (:mod:`Agents.core.act_tools`).

Domain-specific tools (attribute readers/builders, the SQL ``describe_tables``
helper, etc.) live with their sub-agent packages under
:mod:`Agents.domains.product`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool

from apps.core import config
from . import settings
from .contracts import MasterApiError, _tool_error
from . import master_api, schema_catalog, sql as sqlguard, web


@tool
def list_master_tables() -> str:
    """List the master table names available via the Node API (names only, no schema)."""
    try:
        data = master_api._api_get("/trade_business/panel/master/schema")
    except MasterApiError as exc:
        return _tool_error(exc)
    schemas = data.get("schemas", {})
    names = sorted(schemas.keys()) if isinstance(schemas, dict) else []
    allowed = [n for n in names if n in settings.QUERY_ALLOWED_TABLES]
    return json.dumps(
        {"ok": True, "readable_tables": allowed},
        ensure_ascii=False,
    )


@tool
def get_master_table_schema(table_name: str) -> str:
    """Return the field schema (type, notNull, references) for one master table."""
    try:
        table_name = master_api._check_table(table_name)
        data = master_api._api_get(f"/trade_business/panel/master/schema/{table_name}")
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
        rows = master_api._rows(table_name)
        cleaned = master_api._compact(rows, limit=limit)
        for row in cleaned:
            master_api._normalise_bool(table_name, row)
    except MasterApiError as exc:
        return _tool_error(exc)
    return json.dumps(
        {"ok": True, "table": table_name, "rows": cleaned, "total": len(rows)},
        ensure_ascii=False,
    )


@tool
def read_master_schema_file(file_name: str) -> str:
    """Read a schema module (e.g. master.js, products.js) from the Node server's table_schema folder."""
    try:
        name = (file_name or "").strip()
        if name.endswith(".js"):
            name = name[:-3]
        if not sqlguard.IDENTIFIER_RE.match(name):
            raise MasterApiError(
                f"Invalid schema file name: {file_name!r}",
                hint="Provide a bare filename like 'master' or 'products'.",
            )

        path = (schema_catalog.SCHEMA_DIR / f"{name}.js").resolve()
        if schema_catalog.SCHEMA_DIR.resolve() not in path.parents:
            raise MasterApiError(
                f"Path escapes the schema directory: {name}",
                hint="Provide a bare filename only.",
            )
        if not path.is_file():
            raise MasterApiError(
                f"Schema file not found: {path}",
                hint=f"Available under: {schema_catalog.SCHEMA_DIR}",
            )

        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > schema_catalog.MAX_FILE_CHARS:
            content = content[: schema_catalog.MAX_FILE_CHARS] + "\n...[truncated]..."
    except (MasterApiError, OSError) as exc:
        return _tool_error(exc)

    return json.dumps(
        {"ok": True, "file": path.name, "content": content},
        ensure_ascii=False,
    )


@tool
def read_table_definition(table_name: str) -> str:
    """Read a table's field definitions from the Node schema files (products/master). Returns field names and their types. Read-only."""
    fields = schema_catalog.extract_table_fields(table_name)
    if not fields:
        return json.dumps(
            {"ok": False, "error": f"table not found in schema files: {table_name}"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "table": table_name, "fields": fields}, ensure_ascii=False
    )


@tool
def execute_select_query(query: str, limit: int = 100) -> str:
    """Run ONE read-only SELECT against the product-master database and return its rows.

    Only tables in QUERY_ALLOWED_TABLES may be referenced. Writes, DDL, comments and
    multiple statements are rejected locally and again by the Node server. A LIMIT is
    appended when absent and returned rows are capped at AI_MAX_ROWS. Use it to verify
    existing rows / ids before proposing changes; never to write data.
    """
    try:
        sql = sqlguard.assert_select_only(query)
        sqlguard._assert_allowed_tables(sqlguard._mask_quoted(sql))

        cap = max(1, min(int(limit or 100), master_api.AI_MAX_ROWS))
        sql = sqlguard.ensure_limit(sql, cap)

        data = master_api._api_post(master_api.SELECT_QUERY_PATH, {"query": sql})
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
    except (
        Exception
    ) as exc:  # noqa: BLE001 - render any failure as a stable JSON string
        return _tool_error(exc)


def _quote_write_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return sqlguard._sql_literal(value)


@tool
def build_insert_sql(table: str, values: Dict[str, Any]) -> str:
    """Draft ONE INSERT for a write-allowed master table. Returns SQL text for review in /plan mode; nothing is executed. Columns are validated against the Node schema files."""
    try:
        table = (table or "").strip()
        write_tables = settings.WRITE_ALLOWED_TABLES
        if table not in write_tables:
            raise MasterApiError(
                f"Table not allowed for writing: {table}",
                hint=f"Allowed tables: {', '.join(write_tables)}",
            )
        fields = schema_catalog.extract_table_fields(table) or {}
        if not fields:
            raise MasterApiError(
                f"schema not found for {table}", hint="Check the table name."
            )
        values = {
            str(k).strip(): v for k, v in (values or {}).items() if str(k).strip()
        }
        if not values:
            raise MasterApiError(
                "no values provided", hint="Pass at least one column: value pair."
            )
        unknown = [k for k in values if k not in fields]
        if unknown:
            raise MasterApiError(
                f"unknown columns for {table}: {', '.join(unknown)}",
                hint=f"Known fields: {', '.join(fields.keys())}",
            )
        cols = list(values.keys())
        vals = [_quote_write_value(values[c]) for c in cols]
        sql = f"INSERT INTO {table} ({', '.join(cols)})\nVALUES ({', '.join(vals)});"
        sqlguard.assert_write_only(sql, verbs=("INSERT",))
        return json.dumps(
            {
                "ok": True,
                "table": table,
                "sql": sql,
                "verification_before": "",
                "verification_after": f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1;",
                "note": "Review this INSERT before switching to /act mode.",
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


@tool
def build_update_sql(table: str, set_values: Dict[str, Any], where: str = "") -> str:
    """Draft ONE UPDATE for a write-allowed master table. `where` is required (no full-table updates). Returns SQL text for review; nothing is executed."""
    try:
        table = (table or "").strip()
        write_tables = settings.WRITE_ALLOWED_TABLES
        if table not in write_tables:
            raise MasterApiError(
                f"Table not allowed for writing: {table}",
                hint=f"Allowed tables: {', '.join(write_tables)}",
            )
        fields = schema_catalog.extract_table_fields(table) or {}
        if not fields:
            raise MasterApiError(
                f"schema not found for {table}", hint="Check the table name."
            )
        set_values = {
            str(k).strip(): v for k, v in (set_values or {}).items() if str(k).strip()
        }
        if not set_values:
            raise MasterApiError(
                "no set_values provided", hint="Pass columns to update."
            )
        unknown = [k for k in set_values if k not in fields]
        if unknown:
            raise MasterApiError(
                f"unknown columns for {table}: {', '.join(unknown)}",
                hint=f"Known fields: {', '.join(fields.keys())}",
            )
        where = (where or "").strip()
        if not where:
            raise MasterApiError(
                "UPDATE requires a WHERE clause",
                hint="Provide a `where` condition to avoid a full-table update.",
            )
        sets = ", ".join(
            f"{c} = {_quote_write_value(set_values[c])}" for c in set_values
        )
        sql = f"UPDATE {table}\nSET {sets}\nWHERE {where};"
        sqlguard.assert_write_only(sql, verbs=("UPDATE",))
        return json.dumps(
            {
                "ok": True,
                "table": table,
                "sql": sql,
                "verification_before": f"SELECT * FROM {table} WHERE {where};",
                "verification_after": "",
                "note": "Review this UPDATE before switching to /act mode.",
            },
            ensure_ascii=False,
        )
    except MasterApiError as exc:
        return _tool_error(exc)


@tool
def fetch_web_content(url: str, format: str = "text", max_chars: int = 0) -> str:
    """Fetch a URL and return its raw content as 'text' (collapsed whitespace) or 'html' (raw markup). Private/loopback hosts are blocked unless AI_WEB_ALLOW_PRIVATE_HOSTS=1."""
    try:
        fmt = (format or "text").strip().lower()
        if fmt not in ("text", "html"):
            raise MasterApiError(
                f"unsupported format {format!r}", hint="Use 'text' or 'html'."
            )
        cap = int(max_chars) if max_chars else web.WEB_FETCH_MAX_CHARS
        if fmt == "html":
            content = web.fetch_html(url, max_chars=cap)
        else:
            content = web.fetch_text(url, max_chars=cap)
        return json.dumps(
            {
                "ok": True,
                "url": url,
                "format": fmt,
                "chars": len(content),
                "content": content,
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc)


_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"


@tool
def search_web(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return [{title, url, snippet}]. Best-effort scraping with no API key; rate limits return ok:false with a hint."""
    try:
        provider = (
            str(config.get("AI_WEB_SEARCH_PROVIDER", "duckduckgo")).strip().lower()
        )
        if provider in ("none", "off", "0", ""):
            return json.dumps(
                {
                    "ok": False,
                    "error": "web search disabled",
                    "hint": "Set AI_WEB_SEARCH_PROVIDER=duckduckgo to enable.",
                },
                ensure_ascii=False,
            )
        if provider != "duckduckgo":
            return json.dumps(
                {
                    "ok": False,
                    "error": f"unknown provider {provider!r}",
                    "hint": "Supported providers: duckduckgo (default) or none.",
                },
                ensure_ascii=False,
            )

        q = (query or "").strip()
        if not q:
            raise MasterApiError("empty query", hint="Provide a search term.")
        max_results = max(1, min(int(max_results or 5), 10))

        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            _DDG_ENDPOINT,
            params={"q": q},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=master_api.REQUEST_TIMEOUT,
        )
        if resp.status_code in (202, 429, 403):
            return json.dumps(
                {
                    "ok": False,
                    "error": "rate_limited",
                    "hint": "DuckDuckGo rate-limited the request; retry later.",
                },
                ensure_ascii=False,
            )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results: List[Dict[str, str]] = []
        for el in soup.select("div.result"):
            classes = el.get("class") or []
            if "result--ad" in classes:
                continue
            a = el.select_one("a.result__a")
            if a is None:
                continue
            snippet_el = el.select_one("a.result__snippet") or el.select_one(
                ".result__snippet"
            )
            href = a.get("href") or ""
            url = href
            if "duckduckgo.com/l/" in href:
                uddg = parse_qs(urlparse(href).query).get("uddg")
                if uddg:
                    url = uddg[0]
            results.append(
                {
                    "title": " ".join(a.get_text(separator=" ").split()),
                    "url": url,
                    "snippet": (
                        " ".join(snippet_el.get_text(separator=" ").split())
                        if snippet_el
                        else ""
                    ),
                }
            )
        results = results[:max_results]
        if not results:
            return json.dumps(
                {
                    "ok": True,
                    "query": q,
                    "results": [],
                    "count": 0,
                    "hint": "No results found (or DuckDuckGo changed its HTML).",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "query": q, "results": results, "count": len(results)},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc)


PLAN_TOOLS = [
    list_master_tables,
    get_master_table_schema,
    fetch_master_rows,
    read_master_schema_file,
    read_table_definition,
    build_insert_sql,
    build_update_sql,
    execute_select_query,
    fetch_web_content,
    search_web,
]



