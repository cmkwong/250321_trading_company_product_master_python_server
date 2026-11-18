"""Product-domain tool assembly.

Assembles the product manager's plan tools from the sub-agent ``@tool`` wrappers
plus the generic shared tools in :mod:`core.plan_tools`.  Attribute/SQL write
builders live with their sub-agents and are NOT exposed directly to the manager:
the manager reaches them only through the sub-agent tools
(``manage_product_attributes`` / ``build_product_sql``).
"""

from __future__ import annotations

from apps.core.services.AI.Agents.core.act_tools import ACT_TOOLS as _ACT_TOOLS
from apps.core.services.AI.Agents.core.plan_tools import PLAN_TOOLS as SHARED_PLAN_TOOLS

from apps.core.services.AI.Agents.domains.product.attributes.manager import (
    manage_product_attributes,
)
from apps.core.services.AI.Agents.domains.product.naming.manager import (
    rename_product_with_seo,
)
from apps.core.services.AI.Agents.domains.product.sql.manager import build_product_sql
from apps.core.services.AI.Agents.domains.product.web_research.manager import (
    research_product_web,
)

# Public alias so downstream code (``manager.ACT_TOOLS``) keeps working.
ACT_TOOLS = _ACT_TOOLS

# Sub-agents the manager delegates to in /plan mode.
SUBAGENT_TOOLS = [
    rename_product_with_seo,
    research_product_web,
    manage_product_attributes,
    build_product_sql,
]

# Tools the manager agent may call directly in /plan mode (sub-agents + shared).
PLAN_TOOLS = SUBAGENT_TOOLS + SHARED_PLAN_TOOLS
