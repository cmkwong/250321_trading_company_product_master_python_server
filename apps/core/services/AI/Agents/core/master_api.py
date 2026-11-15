"""HTTP client and row-access helpers for the Node product-master API.

All HTTP access goes through a ``requests.Session`` with ``trust_env = False``
so a localhost request can never be hijacked by a mis-configured system proxy.
Row shaping (``_rows`` / ``_compact`` / ``_normalise_bool`` / ``_resolve``) also
lives here because it reinterprets the master API's ``results`` payload.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

import requests

from apps.core import config

from . import settings
from .contracts import MasterApiError
from .schema_catalog import NODE_SERVER_ROOT
from .sql import IDENTIFIER_RE

DEFAULT_API_BASE = "http://127.0.0.1:3001/api/v1"

MASTER_API_BASE = str(config.get("MASTER_API_BASE_URL", DEFAULT_API_BASE)).rstrip("/")

REQUEST_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))
AI_MAX_ROWS = int(config.get("AI_MAX_ROWS", "200"))

# Guarded master endpoints (see routes/general/index.js).
SELECT_QUERY_PATH = "/general/trade-business/query/select"
UPDATE_QUERY_PATH = "/general/trade-business/query/update"
DELETE_QUERY_PATH = "/general/trade-business/query/delete"
BATCH_QUERY_PATH = "/general/trade-business/query/batch"

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


def _check_table(table_name: str) -> str:
    table_name = (table_name or "").strip()
    allowed = settings.ALLOWED_READ_TABLES
    if not IDENTIFIER_RE.match(table_name):
        raise MasterApiError(
            f"Invalid table name: {table_name!r}",
            hint=f"Allowed tables: {', '.join(allowed)}",
        )
    if table_name not in allowed:
        raise MasterApiError(
            f"Table not allowed for reading: {table_name}",
            hint=f"Allowed tables: {', '.join(allowed)}",
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
