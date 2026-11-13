from django.contrib import admin

from .models import CommandJob, NodeServiceAccount, PriceQuote, Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("folder_name", "created_at")
    search_fields = ("folder_name",)


@admin.register(PriceQuote)
class PriceQuoteAdmin(admin.ModelAdmin):
    list_display = ("qty_string", "cost", "profit_max", "profit_min", "created_at")


@admin.register(CommandJob)
class CommandJobAdmin(admin.ModelAdmin):
    list_display = ("command", "kind", "status", "created_at", "finished_at")
    list_filter = ("kind", "status")


@admin.register(NodeServiceAccount)
class NodeServiceAccountAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "email",
        "role",
        "is_active",
        "token_expires_at",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "email")
    readonly_fields = ("role", "token", "token_expires_at", "created_at", "updated_at")
