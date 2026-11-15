"""LangChain agent definitions: a manager agent plus specialist workers."""

from apps.core.services.AI.Agents.manager_general import (
    GeneralManagerAgent,
    ConversationMemory,
    DomainManagerAgent,
    parse_command,
)
from apps.core.services.AI.Agents.manager_product import ProductMasterAgent, format_plan
from apps.core.services.AI.Agents.manager_sales import SalesManagerAgent
from apps.core.services.AI.Agents.worker_product_name import ProductNameWorker
from apps.core.services.AI.Agents.worker_web_research import WebResearchWorker
from apps.core.services.AI.Agents.worker_product_price import ProductPriceWorker
from apps.core.services.AI.Agents.worker_product_attributes import ProductAttributesWorker
from apps.core.services.AI.Agents.worker_product_sql_builder import ProductSqlBuilderWorker

# Registry mapping a worker name to its class, used by the HTTP API dispatcher.
WORKERS = {
    "product_name": ProductNameWorker,
    "web_research": WebResearchWorker,
    "product_price": ProductPriceWorker,
    "product_attributes": ProductAttributesWorker,
    "product_sql_builder": ProductSqlBuilderWorker,
}

__all__ = [
    "ProductMasterAgent",
    "SalesManagerAgent",
    "GeneralManagerAgent",
    "ConversationMemory",
    "DomainManagerAgent",
    "format_plan",
    "parse_command",
    "ProductNameWorker",
    "WebResearchWorker",
    "ProductPriceWorker",
    "ProductAttributesWorker",
    "ProductSqlBuilderWorker",
    "WORKERS",
]

