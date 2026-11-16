"""Product-domain manager (``ProductMasterAgent``) and its tool lists.

The conversation memory lives in :mod:`...core.memory`, the turn engine / plan
schema / guardrail in :mod:`...core.engine`, and the command parsing / general
supervisor in :mod:`...core.router`.  This module only supplies the product-domain
prompts, tools and the ``ProductMasterAgent`` subclass; the shared names are
re-exported here for backward compatibility with the old ``manager_product``
import surface.
"""

from __future__ import annotations

from apps.core.services.AI.Agents.core.engine import (  # noqa: F401  (re-exports)
    SqlStep,
    ProductPlan,
    DEFAULT_MODEL,
    NUM_CTX,
    HEARTBEAT_SECONDS,
    assert_plan_sql,
    assert_plan_read_only,
    format_plan,
    DomainManagerAgent,
)
from apps.core.services.AI.Agents.core.memory import ConversationMemory  # noqa: F401
from apps.core.services.AI.Agents.core.router import (  # noqa: F401  (re-exports)
    parse_mode,
    parse_command,
    SlashCommand,
)

from apps.core.services.AI.Agents.domains.product.prompts import (
    SYSTEM_PROMPT,
    WORKER_ROUTING_PROMPT,
    ACT_SYSTEM_PROMPT,
)
from apps.core.services.AI.Agents.domains.product.tools import (
    _MANAGER_WORKER_TOOLS,
    WORKER_TOOLS,
    ACT_TOOLS,
)


class ProductMasterAgent(DomainManagerAgent):
    """Product-domain manager: product-master tools and prompts."""

    PLAN_TOOLS = WORKER_TOOLS
    ACT_TOOLS = ACT_TOOLS
    PLAN_PROMPT = SYSTEM_PROMPT + WORKER_ROUTING_PROMPT
    ACT_PROMPT = ACT_SYSTEM_PROMPT

