from django.urls import path

from . import views

urlpatterns = [
    path("commands/", views.list_commands, name="command-list"),
    path("commands/<str:name>/", views.run_command, name="command-run"),
    path("agents/manager_product/", views.manager_product, name="product-agents"),
    # path("agents/product_name/", views.product_name_worker, name="agent-product-name"),
    # path("agents/web_research/", views.web_research_worker, name="agent-web-research"),
    # path("agents/product_price/", views.product_price_worker, name="agent-product-price"),
    # path("agents/product_attributes/", views.product_attributes_worker, name="agent-product-attributes"),
    # path("agents/product_sql_builder/", views.product_sql_builder_worker, name="agent-product-sql-builder"),
]
