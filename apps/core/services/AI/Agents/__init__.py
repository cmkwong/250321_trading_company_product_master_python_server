"""LangChain agent definitions: a manager agent plus specialist workers."""

from apps.core.services.AI.Agents.core.router import GeneralManagerAgent, parse_command
from apps.core.services.AI.Agents.core.memory import ConversationMemory
from apps.core.services.AI.Agents.core.engine import DomainManagerAgent, format_plan
from apps.core.services.AI.Agents.domains.product.manager import ProductMasterAgent
from apps.core.services.AI.Agents.domains.sales.manager import SalesManagerAgent
from apps.core.services.AI.Agents.domains.product.workers.name import ProductNameWorker
from apps.core.services.AI.Agents.domains.product.workers.web_research import WebResearchWorker
from apps.core.services.AI.Agents.domains.product.workers.price import ProductPriceWorker
from apps.core.services.AI.Agents.domains.product.workers.attributes import ProductAttributesWorker
from apps.core.services.AI.Agents.domains.product.workers.sql_builder import ProductSqlBuilderWorker
from apps.core.services.AI.Agents.core import registry

# Registry mapping a worker name to its class, used by the HTTP API dispatcher
# and the manager tool list.
registry.register_worker("product_name")(ProductNameWorker)
registry.register_worker("web_research")(WebResearchWorker)
registry.register_worker("product_price")(ProductPriceWorker)
registry.register_worker("product_attributes")(ProductAttributesWorker)
registry.register_worker("product_sql_builder")(ProductSqlBuilderWorker)

WORKERS = registry.WORKERS

# Domain registry: a name -> manager factory, used by the general router.
DOMAINS = {
    "product": ProductMasterAgent,
    "sales": SalesManagerAgent,
}
registry.DOMAINS.update(DOMAINS)

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
    "DOMAINS",
]


