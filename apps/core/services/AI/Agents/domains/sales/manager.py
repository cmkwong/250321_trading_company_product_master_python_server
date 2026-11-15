"""Sales-domain manager (``SalesManagerAgent``) and its tool list.

Reuses the shared turn engine, conversation memory and guardrails from
:mod:`apps.core.services.AI.Agents.core.engine` / ``core.memory`` and supplies
its own prompts and tools.
"""

from __future__ import annotations

from apps.core.services.AI.Agents.core.engine import DomainManagerAgent
from apps.core.services.AI.Agents.core.act_tools import ACT_TOOLS as _ACT_TOOLS

from apps.core.services.AI.Agents.domains.sales.prompts import (
    SALES_SYSTEM_PROMPT,
    SALES_ACT_SYSTEM_PROMPT,
)
from apps.core.services.AI.Agents.domains.sales.tools import (
    SALES_TOOLS,
    build_sales_sql,
)


class SalesManagerAgent(DomainManagerAgent):
    """Sales-domain manager: sales tools and prompts."""

    PLAN_TOOLS = SALES_TOOLS
    ACT_TOOLS = _ACT_TOOLS
    PLAN_PROMPT = SALES_SYSTEM_PROMPT
    ACT_PROMPT = SALES_ACT_SYSTEM_PROMPT


__all__ = [
    "SALES_SYSTEM_PROMPT",
    "SALES_ACT_SYSTEM_PROMPT",
    "SALES_TOOLS",
    "build_sales_sql",
    "SalesManagerAgent",
]
