"""LangChain agent definitions: a manager agent plus specialist workers."""

from apps.core.services.AI.Agents.manager_product import ProductMasterAgent, format_plan
from apps.core.services.AI.Agents.worker_product_name import ProductNameWorker
from apps.core.services.AI.Agents.worker_web_research import WebResearchWorker
from apps.core.services.AI.Agents.worker_product_price import ProductPriceWorker
from apps.core.services.AI.Agents.worker_product_attributes import ProductAttributesWorker

# Registry mapping a worker name to its class, used by the HTTP API dispatcher.
WORKERS = {
    "product_name": ProductNameWorker,
    "web_research": WebResearchWorker,
    "product_price": ProductPriceWorker,
    "product_attributes": ProductAttributesWorker,
}

__all__ = [
    "ProductMasterAgent",
    "format_plan",
    "ProductNameWorker",
    "WebResearchWorker",
    "ProductPriceWorker",
    "ProductAttributesWorker",
    "WORKERS",
]

