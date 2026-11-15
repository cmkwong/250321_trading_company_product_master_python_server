"""Product-master domain: manager, prompts, tools and specialist workers.

Import concrete modules directly (``domains.product.manager``,
``domains.product.workers.sql_builder``, ...); no eager imports here so that
``domains.product.manager`` may import workers without a cycle.
"""

