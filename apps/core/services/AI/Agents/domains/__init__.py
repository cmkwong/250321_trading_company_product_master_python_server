"""Domain managers (product / sales) and their prompts, tools and sub-agents.

This package is a plain namespace: import concrete modules directly, e.g.
``apps.core.services.AI.Agents.domains.product.sql.manager``.  The managers
import sub-agents here, so the package ``__init__`` intentionally does no eager
imports (it would re-enter a half-initialised manager and raise ``ImportError``).
"""

