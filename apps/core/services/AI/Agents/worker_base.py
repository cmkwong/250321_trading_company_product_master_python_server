"""Shared plumbing for the product-master AI workers.

Each worker is a focused, single-purpose helper exposed to the
:mod:`manager_product` agent as a LangChain ``@tool``.  This module holds the
bits every worker repeats so the four worker modules stay small:

- the local Ollama LLM factory and model / context constants,
- a shared streaming loop (workers that are tool-calling agents stream events
  the same way the manager does),
- JSON and SQL-guardrail helpers (SQL is always produced as *text*, never run).
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from langchain_ollama import ChatOllama

from apps.core import config

DEFAULT_MODEL = config.get("OLLAMA_MODEL", "qwen3.5")
VISION_MODEL = config.get("OLLAMA_VISION_MODEL", "gemma3:4b")
NUM_CTX = int(config.get("OLLAMA_NUM_CTX", "32768"))

# How long stream() waits for the next agent event before emitting a
# "waiting for model…" heartbeat.  Tests override this on the manager module.
HEARTBEAT_SECONDS = 5.0

# Statements a worker may never emit.  SELECT is always allowed; INSERT is
# allowed only into a caller-supplied allowlist of tables.
FORBIDDEN_SQL_VERBS = (
    "UPDATE",
    "DELETE",
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "RENAME",
    "REPLACE",
)


def build_llm(
    model: Optional[str] = None,
    *,
    temperature: float = 0.0,
    num_ctx: Optional[int] = None,
    vision: bool = False,
) -> ChatOllama:
    """Build a local Ollama chat model (vision model when ``vision=True``)."""
    name = model or (VISION_MODEL if vision else DEFAULT_MODEL)
    return ChatOllama(
        model=name,
        temperature=temperature,
        num_ctx=num_ctx or NUM_CTX,
        reasoning=False,
    )


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
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


def as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def as_sql_list(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    steps: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            steps.append({"purpose": "", "statement": item, "verification": ""})
        elif isinstance(item, dict):
            steps.append(
                {
                    "purpose": str(item.get("purpose", "")),
                    "statement": str(item.get("statement", "")),
                    "verification": str(item.get("verification", "")),
                }
            )
    return steps


def first_verb(statement: str) -> str:
    match = re.match(r"\s*(--[^\n]*\n)?\s*([A-Za-z]+)", statement or "")
    return match.group(2).upper() if match else ""


def chunk_text(message: Any) -> str:
    """Return the plain text of a message / chunk (str or block list)."""
    content = getattr(message, "content", "") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def message_text(message: Any) -> str:
    """Return the plain text of an LLM response message."""
    return chunk_text(message)


def dumps(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# SQL guardrail (worker side)
# --------------------------------------------------------------------------- #
def assert_draft_sql(steps: Any, allowed_tables: Iterable[str]) -> Any:
    """Drop any non-SELECT statement whose target table isn't allowlisted.

    Returns ``(safe, risks)`` where ``safe`` is a list of ``{purpose, statement,
    verification}`` dicts and ``risks`` lists what was removed and why.
    """
    allowed = {str(t).lower() for t in allowed_tables}
    safe: List[Dict[str, str]] = []
    risks: List[str] = []
    for step in as_sql_list(steps):
        statement = step["statement"]
        verb = first_verb(statement)
        if verb in FORBIDDEN_SQL_VERBS:
            risks.append(f"Removed unsafe SQL ({verb}): {statement[:120]}")
            continue
        if verb == "INSERT" and not any(t in statement.lower() for t in allowed):
            risks.append(f"Removed INSERT outside allowed tables: {statement[:120]}")
            continue
        if statement.strip() or step["purpose"] or step["verification"]:
            safe.append(step)
    return safe, risks


# --------------------------------------------------------------------------- #
# Shared streaming loop
# --------------------------------------------------------------------------- #
def stream_agent(
    agent: Any,
    inp: Dict[str, Any],
    *,
    recursion_limit: int = 25,
    heartbeat_seconds: Optional[float] = None,
):
    """Consume an agent graph on a background thread, yielding event dicts.

    Yields ``{"type": "status"|"token", ...}``, then a terminal
    ``{"type": "_done", "state": {...}}`` or ``{"type": "error", "error": ...}``.
    """
    heartbeat = HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    announced = set()
    last_state: Dict[str, Any] = {}
    q: "queue.Queue[Any]" = queue.Queue()

    def _produce() -> None:
        try:
            for item in agent.stream(
                inp,
                config={"recursion_limit": recursion_limit},
                stream_mode=["messages", "values"],
            ):
                q.put(("item", item))
            q.put(("done", None))
        except Exception as exc:  # noqa: BLE001 - surface as a terminal event
            q.put(("error", exc))

    threading.Thread(target=_produce, daemon=True).start()

    idle_since = time.monotonic()
    while True:
        try:
            kind, payload = q.get(timeout=heartbeat)
        except queue.Empty:
            idle = int(time.monotonic() - idle_since)
            yield {"type": "status", "text": f"waiting for model… ({idle}s)"}
            continue

        idle_since = time.monotonic()
        if kind == "done":
            yield {"type": "_done", "state": last_state}
            return
        if kind == "error":
            yield {"type": "error", "error": str(payload)}
            return

        mode, data = payload
        if mode == "messages":
            chunk, metadata = data
            if metadata.get("langgraph_node") != "model":
                continue
            for tc in getattr(chunk, "tool_call_chunks", None) or []:
                tc = tc or {}
                name = tc.get("name") if isinstance(tc, dict) else None
                idx = tc.get("index") if isinstance(tc, dict) else None
                key = idx if idx is not None else name
                if name and key not in announced:
                    announced.add(key)
                    yield {"type": "status", "text": f"calling tool: {name}"}
            text = chunk_text(chunk)
            if text:
                yield {"type": "token", "text": text}
        else:  # "values": full state snapshot
            last_state = data
