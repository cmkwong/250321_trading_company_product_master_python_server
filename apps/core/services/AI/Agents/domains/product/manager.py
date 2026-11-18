"""Product-domain manager (``ProductMasterAgent``) and its tool lists.

The conversation memory lives in :mod:`...core.memory`, the turn engine / plan
schema / guardrail in :mod:`...core.engine`, and the command parsing / general
supervisor in :mod:`...core.router`.  This module only supplies the product-domain
prompts, tools and the ``ProductMasterAgent`` subclass.
"""

from __future__ import annotations

from apps.core.services.AI.Agents.core.engine import DomainManagerAgent

from apps.core.services.AI.Agents.domains.product.prompts import (
    SYSTEM_PROMPT,
    SUBAGENT_ROUTING_PROMPT,
    ACT_SYSTEM_PROMPT,
)
from apps.core.services.AI.Agents.domains.product.tools import (
    PLAN_TOOLS as PRODUCT_PLAN_TOOLS,
    ACT_TOOLS as PRODUCT_ACT_TOOLS,
    SUBAGENT_TOOLS as PRODUCT_SUBAGENT_TOOLS,
)

# Re-exported at module level so tests and downstream code can read them.
PLAN_TOOLS = PRODUCT_PLAN_TOOLS
ACT_TOOLS = PRODUCT_ACT_TOOLS
SUBAGENT_TOOLS = PRODUCT_SUBAGENT_TOOLS


class ProductMasterAgent(DomainManagerAgent):
    """Product-domain manager: product-master tools and prompts."""

    DOMAIN = "product"

    PLAN_TOOLS = PRODUCT_PLAN_TOOLS
    ACT_TOOLS = PRODUCT_ACT_TOOLS
    PLAN_PROMPT = SYSTEM_PROMPT + SUBAGENT_ROUTING_PROMPT
    ACT_PROMPT = ACT_SYSTEM_PROMPT
