"""Shared reason-act loop for sub-agent managers.

Each sub-agent (``domains.product.attributes``, ``domains.product.naming``,
``domains.product.web_research``, ``domains.product.sql``) supplies its own
``TOOLS`` and ``SYSTEM_PROMPT`` and inherits the same compiled
``model -> tools -> model`` LangGraph loop from here.  The domain managers
(:class:`core.engine.DomainManagerAgent`) keep their own richer plan/act loop,
conversation memory and guardrails.
"""

from __future__ import annotations

from typing import Any, List, Optional, Type
from pydantic import BaseModel

from .graph import build_tool_graph
from .llm import DEFAULT_MODEL, build_llm


class ToolLoopAgent:
    """Owns a cached tool-calling loop built via :func:`core.graph.build_tool_graph`.

    Subclasses set ``TOOLS`` and ``SYSTEM_PROMPT`` and may override
    ``DEFAULT_MODEL_NAME`` (e.g. a vision model) and ``_build_llm`` (e.g. to pass
    ``num_ctx`` or ``vision=True``).
    """

    TOOLS = ()
    SYSTEM_PROMPT = ""
    RESPONSE_MODEL: Optional[Type[BaseModel]] = None
    DEFAULT_MODEL_NAME = DEFAULT_MODEL
    DEFAULT_RECURSION_LIMIT = 25

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        recursion_limit: Optional[int] = None,
        llm: Any = None,
    ) -> None:
        self.model = model or self.DEFAULT_MODEL_NAME
        self.temperature = temperature
        self.recursion_limit = recursion_limit or self.DEFAULT_RECURSION_LIMIT
        self.llm = llm or self._build_llm()
        self._agent = None

    def _build_llm(self):
        """Build the model; subclasses override for vision / wider context."""
        return build_llm(self.model, temperature=self.temperature)

    def _get_agent(self):
        """Compile (once) the ``model -> tools -> model`` loop."""
        if self._agent is None:
            self._agent = build_tool_graph(
                self.llm,
                self.TOOLS,
                self.SYSTEM_PROMPT,
                response_model=self.RESPONSE_MODEL,
            )
        return self._agent

    def _structured(self, state: dict) -> Optional[dict]:
        """
        Return the validated result as a dict, or None if unavailable.

        The state dict is expected to have a key "structured_response" containing
        the validated output from the model.
        """
        out = state.get("structured_response")
        if isinstance(out, BaseModel):
            return out.model_dump()
        if isinstance(out, dict):
            return out
        return None

    def _invoke(self, messages: List[Any]) -> Any:
        """Run the loop with this agent's recursion limit."""
        return self._get_agent().invoke(
            {"messages": messages},
            config={"recursion_limit": self.recursion_limit},
        )
