"""Central AI settings: the SQL table allowlists and builder context size.

Only ``apps.core.config`` may be imported here.  ``Tools/utils.py`` imports this
module lazily (``Agents -> manager_product -> Tools/utils``), so nothing in this
file may import the tools or the agents.
"""

from apps.core import config

# Tables whose full row set may be fetched wholesale (``fetch_master_rows``).
# Kept deliberately small: these are enumerated into the model's context.
ALLOWED_READ_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
    "master_product_name_types",
    "master_currencies",
)

# Tables a generated SELECT may reference (JOIN / aggregate / filter only).
PRODUCT_READ_TABLES = (
    "products",
    "product_names",
    "product_categories",
    "product_costs",
    "product_sale_prices_by_qty",
    "product_keywords",
    "product_varient_sizes",
    "product_varient_capacities",
    "product_varient_colors",
    "product_attribute_values",
    "product_customizations",
    "product_packings",
    "product_certificates",
    "product_delivery_dates",
    "product_alibaba_ids",
    "product_links",
)

SALES_READ_TABLES = (
    "sales_quotations",
    "sales_docs",
    "sales_product_details",
    "sales_service_details",
    "sales_packing_items",
    "sales_packing_dimensions",
    "sales_shipping_details",
    "sales_shipping_dimensions",
    "sales_shipping_prices",
)

# Master lookup tables commonly joined from product / sales questions.
MASTER_LOOKUP_TABLES = (
    "master_product_status",
    "master_size_types",
    "master_capacity_types",
    "master_color_types",
    "master_selling_unit_types",
    "master_keywords",
    "master_packing_types",
    "master_certificate_types",
    "master_incoterms",
    "master_shipping_method",
    "master_countries",
    "master_exchange_rate_hkd",
)

DEFAULT_QUERY_TABLES = (
    ALLOWED_READ_TABLES
    + PRODUCT_READ_TABLES
    + SALES_READ_TABLES
    + MASTER_LOOKUP_TABLES
)

# Tables an INSERT/UPDATE/DELETE may target.
DEFAULT_WRITE_TABLES = (
    "master_categories",
    "master_product_attributes",
    "master_product_attribute_dropdown",
    "master_product_category_attribute_assign",
)


def _env_tuple(key: str, default: tuple) -> tuple:
    """Read a comma-separated allowlist from the environment, else the default."""
    raw = str(config.get(key, ",".join(default)))
    return tuple(t.strip() for t in raw.split(",") if t.strip())


QUERY_ALLOWED_TABLES = _env_tuple("AI_QUERY_TABLES", DEFAULT_QUERY_TABLES)
WRITE_ALLOWED_TABLES = _env_tuple("AI_WRITE_TABLES", DEFAULT_WRITE_TABLES)

# The SQL-builder worker injects a compact schema index; give it more headroom
# than the global OLLAMA_NUM_CTX.
SQL_BUILDER_NUM_CTX = int(config.get("AI_SQL_BUILDER_NUM_CTX", "16384"))

