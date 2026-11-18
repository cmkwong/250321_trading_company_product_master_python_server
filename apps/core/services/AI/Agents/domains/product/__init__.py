"""Product-master domain: manager plus specialist sub-agents.

Import concrete modules directly (``domains.product.manager``,
``domains.product.sql.manager``, ...); no eager imports here so that
``domains.product.manager`` may import sub-agents without a cycle.
"""

