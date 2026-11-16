"""Shared background-thread streaming loop for the agent graphs.

Workers and the turn engine consume their LangGraph agent the same way: on a
background thread, with a ``status`` heartbeat while the model prefills or a
tool executes (both otherwise produce no events and read as a hang), yielding
``status`` / ``token`` events and a terminal ``_done`` / ``error``.

The canonical ``HEARTBEAT_SECONDS`` lives here.  ``core.engine`` re-exports it
and passes it to :func:`stream_agent` explicitly, so tests can patch
``engine.HEARTBEAT_SECONDS`` and still drive the manager's heartbeat.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Optional

from .llm import chunk_text

# How long stream_agent() waits for the next agent event before emitting a
# "waiting for model..." heartbeat, so a slow prefill / tool call doesn't read
# as a hang.  Tests override this on the manager module.
HEARTBEAT_SECONDS = 5.0


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

