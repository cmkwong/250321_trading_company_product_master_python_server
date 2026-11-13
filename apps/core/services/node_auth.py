"""Obtain and cache a Node API JWT for machine-to-machine calls.

The Node product-master server owns authentication.  This module stores the
credentials of a single Node user (role ``admin`` / ``system-admin`` /
``developer``) and exchanges them for a short-lived JWT via
``POST /trade_business/panel/auth/getToken``, caching the token until just before
it expires.  The Python server never mints tokens itself: ``JWT_SECRET`` stays on
the Node side.

The account comes from the ``NodeServiceAccount`` Django model (or, for the
standalone CLI, from the ``MASTER_API_EMAIL`` / ``MASTER_API_PASSWORD`` env vars).
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from apps.core import config

DEFAULT_API_BASE = "http://127.0.0.1:3001/api/v1"

MASTER_API_BASE = str(config.get("MASTER_API_BASE_URL", DEFAULT_API_BASE)).rstrip("/")
TOKEN_PATH = "/trade_business/panel/auth/getToken"
REQUEST_TIMEOUT = float(config.get("MASTER_API_TIMEOUT", "20"))

TOKEN_EXPIRY_SKEW = timedelta(minutes=5)
FALLBACK_TTL = timedelta(hours=1)


class NodeAuthError(Exception):
    """Raised when a service token cannot be obtained or refreshed."""

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


_cache: Dict[str, Any] = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode_expiry(token: str) -> Optional[datetime]:
    """Read the JWT ``exp`` claim (unsigned) purely to size the cache window."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        exp = claims.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        return None


def _load_account() -> Optional[Dict[str, str]]:
    """Return {email, password, name} from the DB row, or the env fallback."""
    try:
        from apps.core.models import NodeServiceAccount

        row = NodeServiceAccount.objects.filter(is_active=True).order_by("id").first()
        if row is not None:
            return {"email": row.email, "password": row.password, "name": row.name}
    except Exception:  # noqa: BLE001 - missing table / no Django app / not configured
        pass

    email = str(config.get("MASTER_API_EMAIL", "") or "").strip()
    password = str(config.get("MASTER_API_PASSWORD", "") or "")
    if email and password:
        return {"email": email, "password": password, "name": "env"}

    return None


def _persist_token(
    email: str, role: str, token: str, expires_at: Optional[datetime]
) -> None:
    try:
        from apps.core.models import NodeServiceAccount

        NodeServiceAccount.objects.filter(email=email, is_active=True).update(
            role=role or "",
            token=token,
            token_expires_at=expires_at,
        )
    except Exception:  # noqa: BLE001 - best-effort persistence
        pass


def invalidate_service_token() -> None:
    """Drop the cached token so the next call re-authenticates against Node."""
    with _lock:
        _cache.clear()


def get_service_token(force_refresh: bool = False) -> str:
    """Return a valid JWT for the active service account, logging in when needed."""
    with _lock:
        cached = _cache.get("token")
        cached_exp = _cache.get("expires_at")
        if (
            not force_refresh
            and cached
            and cached_exp is not None
            and cached_exp - TOKEN_EXPIRY_SKEW > _now()
        ):
            return cached

        account = _load_account()
        if account is None:
            raise NodeAuthError(
                "NODE_AUTH_NOT_CONFIGURED",
                hint=(
                    "Add a NodeServiceAccount row in Django admin "
                    "(or set MASTER_API_EMAIL / MASTER_API_PASSWORD in .env) holding "
                    "the credentials of a Node user with an admin / system-admin / "
                    "developer role."
                ),
            )

        url = f"{MASTER_API_BASE}{TOKEN_PATH}"
        try:
            response = _get_session().post(
                url,
                json={"email": account["email"], "password": account["password"]},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise NodeAuthError(
                f"NODE_AUTH_UNREACHABLE: {type(exc).__name__}: {exc}",
                hint=f"Start the Node server (npm start) and confirm {url} answers.",
            ) from exc

        if response.status_code in (400, 404):
            raise NodeAuthError(
                f"NODE_AUTH_REJECTED: {response.text[:300]}",
                hint=(
                    "The Node server rejected the stored email/password. Update the "
                    "NodeServiceAccount row (or MASTER_API_EMAIL / MASTER_API_PASSWORD)."
                ),
            )
        if response.status_code >= 500:
            raise NodeAuthError(
                f"NODE_AUTH_HTTP_{response.status_code}: {response.text[:300]}",
                hint="The Node auth endpoint errored; check the Node server logs.",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise NodeAuthError(
                f"NODE_AUTH_BAD_JSON: {response.text[:200]}",
                hint="Unexpected response format from the Node auth endpoint.",
            ) from exc

        token = body.get("token") if isinstance(body, dict) else None
        if not token:
            raise NodeAuthError(
                f"NODE_AUTH_BAD_RESPONSE: {json.dumps(body)[:200]}",
                hint="The Node auth response did not contain a 'token' field.",
            )

        expires_at = _decode_expiry(token) or (_now() + FALLBACK_TTL)
        _cache["token"] = token
        _cache["expires_at"] = expires_at
        _persist_token(account["email"], str(body.get("role") or ""), token, expires_at)
        return token

