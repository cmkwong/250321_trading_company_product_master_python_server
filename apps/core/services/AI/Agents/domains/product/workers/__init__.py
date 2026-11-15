"""Product-domain specialist workers (pure logic, no ``@tool`` machinery)."""

from apps.core.services.AI.Agents.domains.product.workers.name import ProductNameWorker  # noqa: F401
from apps.core.services.AI.Agents.domains.product.workers.web_research import WebResearchWorker  # noqa: F401
from apps.core.services.AI.Agents.domains.product.workers.price import ProductPriceWorker  # noqa: F401
from apps.core.services.AI.Agents.domains.product.workers.attributes import ProductAttributesWorker  # noqa: F401
from apps.core.services.AI.Agents.domains.product.workers.sql_builder import ProductSqlBuilderWorker  # noqa: F401

__all__ = [
    "ProductNameWorker",
    "WebResearchWorker",
    "ProductPriceWorker",
    "ProductAttributesWorker",
    "ProductSqlBuilderWorker",
]
