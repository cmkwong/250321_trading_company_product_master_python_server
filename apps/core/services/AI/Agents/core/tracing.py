"""LangSmith tracing configuration for the agent engine.

LangChain/LangGraph traces automatically once ``LANGSMITH_TRACING=true`` and a
``LANGSMITH_API_KEY`` are set, so no ``@traceable`` decorators or callback
plumbing are needed here.  This module only:

- normalises the ``LANGSMITH_*`` environment so the local Ollama agents are
  addressed to an *agent environment* (``LANGSMITH_AGENT_ID`` +
  ``LANGSMITH_AGENT_ENVIRONMENT``) rather than a project, which is how the
  ``langsmith-trace`` skill expects agent traces to be routed;
- builds the ``RunnableConfig`` (``run_name`` / ``tags`` / ``metadata``) each
  agent turn passes to the graph, so traces are filterable per domain and mode.

It imports no ``langsmith`` symbols at module scope, so importing it (and, in
turn, the agents) never constructs a LangSmith client or performs network I/O.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from apps.core import config  # noqa: F401  (loads .env before any SDK env read)

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.smith.langchain.com"
DEFAULT_AGENT_ID = "ollama-agents-qwen3-5"
DEFAULT_AGENT_ENVIRONMENT = "local"

# Mirrors langsmith.address.AGENT_ID_PATTERN: a DNS label of 1-63 lowercase
# ASCII letters, digits or hyphens, starting with a letter and ending with a
# letter or digit.
AGENT_ID_PATTERN = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")

# Mirrors langsmith.address._ENVIRONMENTS.
ENVIRONMENTS = ("local", "development", "staging", "production")

# Set once so configuring is idempotent across every DomainManagerAgent.
_configured = False


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value if value else None


def _setdefault(name: str, value: str) -> None:
    """Set ``name`` only when it is unset (empty counts as unset)."""
    if not os.getenv(name):
        os.environ[name] = value


def agent_id() -> str:
    return _env("LANGSMITH_AGENT_ID") or DEFAULT_AGENT_ID


def agent_environment() -> str:
    return _env("LANGSMITH_AGENT_ENVIRONMENT") or DEFAULT_AGENT_ENVIRONMENT


def is_enabled() -> bool:
    return str(os.getenv("LANGSMITH_TRACING", "")).strip().lower() == "true"


def destination() -> str:
    """Human-readable trace destination, for logs and diagnostics."""
    if not is_enabled():
        return "disabled"
    if _env("LANGSMITH_PROJECT"):
        return f"project {_env('LANGSMITH_PROJECT')}"
    if _env("LANGSMITH_AGENT_ID"):
        return f"agent {agent_id()}/{agent_environment()}"
    return "project default"


def configure_tracing() -> bool:
    """Populate/validate ``LANGSMITH_*`` defaults; return whether tracing is on.

    Idempotent and safe to call before every agent is built.  Never overwrites a
    non-empty environment value (so ``.env``, the shell, and test overrides stay
    authoritative), and never raises: bad addressing disables tracing with a log
    line rather than crashing a request.
    """
    global _configured
    if _configured:
        return is_enabled()
    _configured = True

    _setdefault("LANGSMITH_ENDPOINT", DEFAULT_ENDPOINT)

    if _env("LANGSMITH_PROJECT"):
        # The SDK leaves root runs untraced when a project and an agent address
        # are both set in the environment; keep the project authoritative.
        logger.warning(
            "LangSmith tracing: LANGSMITH_PROJECT is set, so LANGSMITH_AGENT_ID/"
            "LANGSMITH_AGENT_ENVIRONMENT are ignored (runs address the project)."
        )
    else:
        _setdefault("LANGSMITH_AGENT_ID", DEFAULT_AGENT_ID)
        _setdefault("LANGSMITH_AGENT_ENVIRONMENT", DEFAULT_AGENT_ENVIRONMENT)

    aid = _env("LANGSMITH_AGENT_ID")
    if aid and not AGENT_ID_PATTERN.fullmatch(aid):
        logger.warning(
            "LangSmith tracing disabled: LANGSMITH_AGENT_ID=%r is not a valid "
            "agent id (1-63 lowercase letters/digits/hyphens).",
            aid,
        )
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    aenv = _env("LANGSMITH_AGENT_ENVIRONMENT")
    if aenv and aenv.lower() not in ENVIRONMENTS:
        logger.warning(
            "LangSmith tracing disabled: LANGSMITH_AGENT_ENVIRONMENT=%r must be "
            "one of %s.",
            aenv,
            ", ".join(ENVIRONMENTS),
        )
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    api_key = _env("LANGSMITH_API_KEY")
    tracing = _env("LANGSMITH_TRACING")
    if tracing is not None:
        # Normalise case only; the SDK compares against the literal "true".
        os.environ["LANGSMITH_TRACING"] = tracing.strip().lower()
    elif api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
    else:
        os.environ["LANGSMITH_TRACING"] = "false"

    # A tracing flag without a key cannot upload; keep the app fully local and
    # avoid LangSmith "missing API key" noise until a key is supplied.
    if not api_key and is_enabled():
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.info("LangSmith tracing disabled (no LANGSMITH_API_KEY).")

    return is_enabled()


def run_config(
    *,
    run_name: str,
    domain: str,
    mode: str,
    model: str,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``RunnableConfig`` a turn passes to ``invoke``/``stream``.

    ``run_name`` names the root graph run; the tags/metadata make traces
    filterable by agent id, environment, domain and mode in LangSmith.
    """
    return {
        "run_name": run_name,
        "tags": [
            "langsmith-trace",
            agent_id(),
            agent_environment(),
            domain,
            mode,
            *(tags or []),
        ],
        "metadata": {
            "agent_id": agent_id(),
            "agent_environment": agent_environment(),
            "domain": domain,
            "mode": mode,
            "model": model,
            **(metadata or {}),
        },
    }


@contextmanager
def turn_span(
    run_name: str,
    *,
    inputs: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """Open the root LangSmith run for one agent turn.

    Yields the ``langsmith.RunTree`` (or ``None`` when tracing is disabled or no
    ``run_name`` is given, so callers need no special-casing).  The caller ends
    it with :func:`finish_turn` once the rendered plan is available, which is
    what puts the human-readable summary into the trace's outputs.

    ``langsmith`` is imported lazily so a disabled turn never constructs a
    client or touches the network.
    """
    if not run_name or not is_enabled():
        yield None
        return

    from langsmith import trace as ls_trace  # noqa: PLC0415 - lazy

    with ls_trace(
        run_name,
        run_type="chain",
        inputs=inputs or {},
        tags=list(tags or []),
        metadata=dict(metadata or {}),
    ) as run:
        yield run


def finish_turn(
    run: Any,
    plan: Optional[Dict[str, Any]],
    text: str,
) -> None:
    """Close a turn run with the structured plan and its rendered summary."""
    if run is None:
        return
    run.end(outputs={"plan": plan or {}, "summary": text})
