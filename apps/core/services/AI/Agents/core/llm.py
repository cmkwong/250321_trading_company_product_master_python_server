"""Local Ollama model factory, model constants and text extraction.

This is the single home for the bits every agent and worker shares around the
local LLM:

- the model / context constants and ``build_llm`` (an Ollama ``ChatOllama``);
- plain-text extraction from a message / chunk (``chunk_text`` / ``message_text``).

Workers and managers both import this module; there is no second copy.  The SQL
guardrail lives in :mod:`core.guardrail`, the shared streaming loop in
:mod:`core.streaming`, and the plan/worker data shapes in ``..contracts``.
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_ollama import ChatOllama

from apps.core import config

DEFAULT_MODEL = config.get("OLLAMA_MODEL", "qwen3.5")
VISION_MODEL = config.get("OLLAMA_VISION_MODEL", "gemma3:4b")
NUM_CTX = int(config.get("OLLAMA_NUM_CTX", "32768"))


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

