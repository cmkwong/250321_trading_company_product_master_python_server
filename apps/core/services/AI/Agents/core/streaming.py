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

import contextvars
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import tracing
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
    config: Optional[Dict[str, Any]] = None,
    run_name: Optional[str] = None,
    trace_inputs: Optional[Dict[str, Any]] = None,
    tags: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None,
    finalize: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Consume an agent graph on a background thread, yielding event dicts.

    Yields ``{"type": "status"|"token", ...}``, then a terminal
    ``{"type": "_done", "state": {...}, "final": {...}}`` or
    ``{"type": "error", "error": ...}``.

    ``config`` (a LangChain ``RunnableConfig``) is merged into the config passed
    to ``agent.stream``; ``recursion_limit`` fills in when ``config`` omits it.

    When ``run_name`` is given, the whole stream is wrapped in a LangSmith turn
    span (:func:`tracing.turn_span`) opened inside the producer thread, and
    ``finalize`` (run in the same thread, before the span closes) returns the
    plan/text that become the run's outputs.  The caller's ``contextvars``
    context is copied into the thread so the graph nests under the span.
    """
    heartbeat = HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    announced = set()
    q: "queue.Queue[Any]" = queue.Queue()

    def _produce() -> None:
        try:
            cfg = {"recursion_limit": recursion_limit, **(config or {})}
            state: Dict[str, Any] = {}
            with tracing.turn_span(
                run_name,
                inputs=trace_inputs,
                tags=tags,
                metadata=metadata,
            ) as run:
                for mode, data in agent.stream(
                    inp,
                    config=cfg,
                    stream_mode=["messages", "values"],
                ):
                    if mode == "values":
                        state = data
                    q.put(("item", (mode, data)))
                extra = (finalize(state) if finalize is not None else {}) or {}
                tracing.finish_turn(run, extra.get("plan"), extra.get("text", ""))
            q.put(("done", {"state": state, "final": extra}))
        except Exception as exc:  # noqa: BLE001 - surface as a terminal event
            q.put(("error", exc))

    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(_produce,), daemon=True).start()

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
            payload = payload or {}
            yield {
                "type": "_done",
                "state": payload.get("state", {}),
                "final": payload.get("final", {}),
            }
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
        else:  # "values": full state snapshot (already captured in _produce)
            continue

