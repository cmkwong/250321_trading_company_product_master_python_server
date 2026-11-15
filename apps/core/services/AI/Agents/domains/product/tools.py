"""Product-domain tool assembly.

Thin ``@tool`` wrappers for the specialist workers (the worker logic lives in
``domains.product.workers``, pure and model-free) plus the plan/act tool lists
used by :class:`domains.product.manager.ProductMasterAgent`.
"""

from __future__ import annotations

from apps.core.services.AI.Agents.domains.product.workers.name import rename_product_with_seo
from apps.core.services.AI.Agents.domains.product.workers.web_research import research_product_web
from apps.core.services.AI.Agents.domains.product.workers.price import define_product_price
from apps.core.services.AI.Agents.domains.product.workers.attributes import manage_product_attributes
from apps.core.services.AI.Agents.domains.product.workers.sql_builder import build_product_sql
from apps.core.services.AI.Agents.core.plan_tools import PLAN_TOOLS
from apps.core.services.AI.Agents.core.act_tools import ACT_TOOLS as _ACT_TOOLS

# Public alias so downstream code (``manager.ACT_TOOLS``) keeps working.
ACT_TOOLS = _ACT_TOOLS

# Specialist workers the manager delegates to in /plan mode.
_MANAGER_WORKER_TOOLS = [
    rename_product_with_seo,
    research_product_web,
    define_product_price,
    manage_product_attributes,
    build_product_sql,
]

# Tools the manager agent may call directly in /plan mode (read + draft + web).
WORKER_TOOLS = _MANAGER_WORKER_TOOLS + list(PLAN_TOOLS)

__all__ = [
    "rename_product_with_seo",
    "research_product_web",
    "define_product_price",
    "manage_product_attributes",
    "build_product_sql",
    "_MANAGER_WORKER_TOOLS",
    "WORKER_TOOLS",
    "ACT_TOOLS",
]
