"""Shared data contracts for the AI agents.

This is the single home for the plan and sub-agent result shapes.  Sub-agents
return ``AgentResult`` (validated to a JSON string at the tool boundary); the
turn engine parses tool messages through the same model so a failed sub-agent
is never silently dropped, and the ``/plan``-``/act`` flow signs a plan with a
content hash so ``/act`` can only run what a human already reviewed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SqlDraft(BaseModel):
    purpose: str = Field(default="", description="What this statement does")
    statement: str = Field(
        default="",
        description="One MySQL statement copied verbatim from a build_* tool",
    )
    verification: str = Field(
        default="", description="Verification query from the build_* tool"
    )


class SqlStep(SqlDraft):
    """One SQL statement plus its intent and verification."""

    model_config = ConfigDict(extra="allow")
    agent: Optional[str] = None


class AgentResult(BaseModel):
    """The one result contract every sub-agent tool returns.

    ``extra="allow"`` preserves sub-agent-specific payload fields (``data``,
    ``answer``, ``tables_used``, ``queries``, ``executed``, ``needs_confirmation``,
    ``route``, ...) so existing callers and tests keep working while the core
    fields stay uniform.
    """

    model_config = ConfigDict(extra="allow")

    agent: str
    ok: bool = True
    summary: str = ""
    answer: str = ""
    sql: List[SqlStep] = Field(default_factory=list)
    missing_data: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    hint: Optional[str] = None

    @classmethod
    def success(cls, agent: str, **fields: Any) -> "AgentResult":
        return cls(agent=agent, ok=True, **fields)

    @classmethod
    def failure(cls, agent: str, error: str, **fields: Any) -> "AgentResult":
        return cls(agent=agent, ok=False, error=error, **fields)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)

    # Dict-style access kept for backward compatibility with existing callers
    # and tests that subscript agent results (``result["ok"]``).
    def __getitem__(self, key: str) -> Any:
        return self.model_dump(exclude_none=True)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(exclude_none=True).get(key, default)

    @classmethod
    def from_tool_content(cls, content: Any) -> Optional["AgentResult"]:
        """Parse a tool message's content into an AgentResult, or None.

        Accepts a dict, a JSON string, or free-form text with an embedded JSON
        object (the models sometimes wrap the envelope in prose).  Accepts the
        legacy ``worker`` key for one release while callers migrate to ``agent``.
        """
        data: Any = None
        if isinstance(content, dict):
            data = content
        elif isinstance(content, str):
            data = _extract_json_block(content)
        elif isinstance(content, list):
            text = "".join(
                str(p) if isinstance(p, str) else str(getattr(p, "text", p) or "")
                for p in content
            )
            data = _extract_json_block(text)
        if not isinstance(data, dict):
            return None
        if not data.get("agent") and not data.get("worker"):
            return None
        if "agent" not in data and "worker" in data:
            data = {**data, "agent": data["worker"]}
        try:
            return cls.model_validate(data)
        except Exception:  # noqa: BLE001 - tolerate a malformed envelope
            return None


class Plan(BaseModel):
    """The structured plan produced by ``/plan`` and executed by ``/act``."""

    model_config = ConfigDict(extra="allow")

    plan_id: Optional[str] = None
    plan_hash: Optional[str] = None
    mode: str = "plan"
    domain: Optional[str] = None
    answer: str = ""
    summary: str = ""
    plan: List[str] = Field(default_factory=list)
    sql: List[SqlStep] = Field(default_factory=list)
    drafts: List[SqlStep] = Field(default_factory=list)
    missing_data: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    agent_results: List[Dict[str, Any]] = Field(default_factory=list)
    skipped_sql: List[Dict[str, Any]] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)


# JSON extraction utility for free-form model text.
def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first ``{...}`` JSON object out of free-form model text."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


# ``extract_json_block`` is the public name; ``_extract_json_block`` is kept as
# a backward-compatible alias for ``AgentResult.from_tool_content``.
extract_json_block = _extract_json_block


# Convert raw values into a list of dicts, filtering out non-dict items.
def as_dict_list(value: Any) -> List[Dict[str, Any]]:
    """Keep only dict items from a raw list (agent-results / plan fields)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# Convert raw values into a list of strings, filtering out None and empty strings.
def as_str_list(value: Any) -> List[str]:
    """Coerce a raw list into a clean list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


# Convert raw SQL steps into a structured list of dicts with purpose, statement, and verification.
def as_sql_list(value: Any) -> List[Dict[str, str]]:
    """Coerce raw plan/sub-agent SQL into a list of {purpose, statement, verification}."""
    if not isinstance(value, list):
        return []
    steps: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, SqlStep):
            step = item
        elif isinstance(item, str):
            step = SqlStep(statement=item)
        elif isinstance(item, dict):
            step = SqlStep(
                purpose=str(item.get("purpose", "") or ""),
                statement=str(item.get("statement", "") or ""),
                verification=str(item.get("verification", "") or ""),
            )
        else:
            continue
        steps.append(step.model_dump(include={"purpose", "statement", "verification"}))
    return steps


def dump_agent_result(payload: Any) -> str:
    """Validate a sub-agent payload against ``AgentResult`` and dump it to JSON.

    Every sub-agent ``@tool`` returns this string, so the manager has one code
    path for collecting results, surfacing failures and building drafts.
    """
    if isinstance(payload, AgentResult):
        return payload.to_json()
    return AgentResult.model_validate(payload).to_json()


# Compute a canonical digest for a plan, ignoring cosmetic changes.
def canonical_digest(plan: Any, *, mode: Optional[str] = None) -> str:
    """Content hash over the executable part of a plan.

    Only ``sql`` and ``drafts`` are hashed (in a stable key order), so cosmetic
    edits to ``answer``/``summary``/``risks`` do not invalidate a review but any
    change to the statements does.  The execution ``mode`` is intentionally NOT
    hashed: the hash is computed on the reviewed (plan-mode) statements and must
    stay identical when ``/act`` re-checks the plan.
    """
    if isinstance(plan, BaseModel):
        payload = plan.model_dump(exclude_none=True)
    elif isinstance(plan, dict):
        payload = plan
    else:
        payload = {}

    canonical = {
        "sql": as_sql_list(payload.get("sql")),
        "drafts": as_sql_list(payload.get("drafts")),
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MasterApiError(Exception):
    """Raised when the Node product-master API cannot be reached or errors."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _tool_error(exc: BaseException) -> str:
    """Render an exception as a stable JSON string the agent can act on."""
    hint = getattr(exc, "hint", "") or "Inspect the error and retry with valid inputs."
    return json.dumps(
        {"ok": False, "error": str(exc), "hint": hint},
        ensure_ascii=False,
    )
