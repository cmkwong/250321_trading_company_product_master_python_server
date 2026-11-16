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
from apps.core.services.AI.Agents.core.plan_tools import (
    PLAN_TOOLS,
    build_attribute_dropdown_sql,
    build_category_attribute_assign_sql,
    build_create_attribute_sql,
)
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

# Attribute/category writes are owned by ``manage_product_attributes``.  Keep the
# builders out of the manager's toolset so there is only ONE route to them.
_ATTRIBUTE_BUILDERS = {
    build_category_attribute_assign_sql.name,
    build_create_attribute_sql.name,
    build_attribute_dropdown_sql.name,
}

MANAGER_PLAN_TOOLS = [t for t in PLAN_TOOLS if t.name not in _ATTRIBUTE_BUILDERS]

# Tools the manager agent may call directly in /plan mode (read + draft + web).
WORKER_TOOLS = _MANAGER_WORKER_TOOLS + MANAGER_PLAN_TOOLS

__all__ = [
    "rename_product_with_seo",
    "research_product_web",
    "define_product_price",
    "manage_product_attributes",
    "build_product_sql",
    "_MANAGER_WORKER_TOOLS",
    "MANAGER_PLAN_TOOLS",
    "WORKER_TOOLS",
    "ACT_TOOLS",
]
