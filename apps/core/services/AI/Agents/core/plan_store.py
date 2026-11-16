"""Plan storage with an id + content hash for the ``/plan``-``/act`` flow.

A plan is signed with a SHA-256 digest of its executable content so ``/act`` can
only run what a human already reviewed.  Storage is the Django cache (with a
lazy import so the CLI / standalone tools do not need Django at import time).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .contracts import canonical_digest


class PlanNotFound(Exception):
    pass


class PlanHashMismatch(Exception):
    pass


def _cache():
    from django.core.cache import cache

    return cache


def _key(plan_id: str) -> str:
    return f"ai_plan:{plan_id}"


def put(
    plan: Dict[str, Any],
    *,
    domain: Optional[str] = None,
    mode: str = "plan",
    ttl: Optional[int] = None,
) -> Tuple[str, str]:
    """Store a plan and return ``(plan_id, plan_hash)``."""
    from apps.core import config

    ttl = ttl if ttl is not None else int(config.get("AI_PLAN_CACHE_TTL", "3600"))
    plan_id = str(uuid.uuid4())
    plan_hash = canonical_digest(plan, mode=mode)
    envelope = {
        "plan": plan,
        "plan_hash": plan_hash,
        "domain": domain,
        "mode": mode,
        "created_at": time.time(),
    }
    _cache().set(_key(plan_id), envelope, ttl)
    return plan_id, plan_hash


def get(plan_id: Optional[str], *, plan_hash: Optional[str] = None) -> Dict[str, Any]:
    """Fetch a stored plan, verifying the content hash when supplied."""
    if not plan_id:
        raise PlanNotFound("no plan_id")
    envelope = _cache().get(_key(plan_id))
    if not isinstance(envelope, dict):
        raise PlanNotFound(f"unknown or expired plan: {plan_id}")
    if plan_hash and envelope.get("plan_hash") != plan_hash:
        raise PlanHashMismatch("plan changed since review; re-run /plan")
    return envelope


def consume(plan_id: Optional[str]) -> None:
    """Delete a plan after a successful non-dry-run act (one-shot)."""
    if plan_id:
        _cache().delete(_key(plan_id))

