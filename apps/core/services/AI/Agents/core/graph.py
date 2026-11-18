"""Explicit LangGraph tool-calling loop shared by the agents.

This is the graph ``langchain.agents.create_agent`` builds internally, spelled
out so it can be extended and tested directly::

    START -> model -> (tool_calls?) -> tools -> model -> ... -> END

The model node is named ``"model"`` because :mod:`core.streaming` filters token
chunks on ``langgraph_node == "model"``; the tools node is the classic
``ToolNode`` and the loop back is ``tools_condition``.

Notes:

- The system prompt is prepended to the message list on every model call and is
  never written into ``state["messages"]``, matching ``create_agent``.
- ``ToolNode`` uses its default error handler, which is also what
  ``create_agent`` constructs; the workers already return JSON error envelopes,
  so a failing tool is surfaced as a tool message the model can read.
- Unlike ``create_agent``'s per-call ``Send`` edge, ``tools_condition`` re-routes
  to ``tools`` whenever the last ``AIMessage`` carries any ``tool_calls``; this
  is only observable if a model re-emits an already-answered tool-call id, which
  the local models do not do.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, Type

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel

# Node names are public so streaming and tests reference the same strings.
MODEL_NODE = "model"
TOOLS_NODE = "tools"
FORMAT_NODE = "format"

_FORMAT_INSTRUCTION = (
    "Convert your findings above into the required structured result. "
    "Use ONLY facts from the conversation and tool results. "
    "Do not invent values; use empty lists or empty strings when unknown."
)


# Custom state class to hold structured response when using a response model.
class _State(MessagesState):
    structured_response: Any


def build_tool_graph(
    model: Any,
    tools: Sequence[Any],
    system_prompt: str = "",
    response_model: Optional[Type[BaseModel]] = None,
):
    """Compile a ReAct-style tool-calling graph for one agent.

    ``model`` is a LangChain chat model; ``tools`` its ``@tool`` list (may be
    empty, in which case the graph is a single model node).  ``system_prompt``,
    when non-empty, is prepended to each model call but never stored in state.
    """
    tools = list(tools)
    bound = model.bind_tools(tools) if tools else model

    def call_model(state: MessagesState):
        messages = state["messages"]
        if system_prompt:
            messages = [SystemMessage(content=system_prompt), *messages]
        return {"messages": [bound.invoke(messages)]}

    def format_result(state: _State):
        msgs = [*state["messages"], HumanMessage(content=_FORMAT_INSTRUCTION)]
        if system_prompt:
            msgs = [SystemMessage(content=system_prompt), *msgs]
        try:
            out = model.with_structured_output(
                response_model, method="json_schema"
            ).invoke(msgs)
        except Exception:  # noqa: BLE001 - model lacks structured output
            return {"structured_response": None}  # caller falls back to JSON parsing
        return {"structured_response": out}

    graph = StateGraph(_State if response_model else MessagesState)
    graph.add_node(MODEL_NODE, call_model)
    graph.add_edge(START, MODEL_NODE)
    end_target = END

    if response_model:
        graph.add_node(FORMAT_NODE, format_result)
        graph.add_edge(FORMAT_NODE, END)
        end_target = FORMAT_NODE

    if not tools:
        graph.add_edge(MODEL_NODE, end_target)
        return graph.compile()

    graph.add_node(TOOLS_NODE, ToolNode(tools))
    graph.add_conditional_edges(
        MODEL_NODE, tools_condition, {"tools": TOOLS_NODE, END: end_target}
    )
    graph.add_edge(TOOLS_NODE, MODEL_NODE)
    return graph.compile()
