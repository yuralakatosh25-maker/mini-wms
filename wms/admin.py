from django.contrib import admin, messages
from django.db.models import Count

from . import services
from .models import Order, OrderItem, Product

admin.site.site_header = "mini-wms"
admin.site.site_title = "mini-wms"
admin.site.index_title = "Správa skladu"
# Odkaz „Zobrazit web“ v adminu vede na report nejprodávanějších produktů.
admin.site.site_url = "/reports/top-products/"


class LowStockFilter(admin.SimpleListFilter):
    title = "stav zásoby"
    parameter_name = "zasoba"

    def lookups(self, request, model_admin):
        return [("0", "Vyprodáno"), ("low", "Málo (1–5 ks)"), ("ok", "Dostatek (> 5 ks)")]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "0":
            return queryset.filter(stock=0)
        if value == "low":
            return queryset.filter(stock__gte=1, stock__lte=5)
        if value == "ok":
            return queryset.filter(stock__gt=5)
        return queryset


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["sku", "name", "stock", "weight_kg", "updated_at"]
    list_editable = ["stock"]
    list_filter = [LowStockFilter]
    search_fields = ["sku", "name"]
    ordering = ["sku"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    autocomplete_fields = ["product"]

    def has_change_permission(self, request, obj=None):
        # Položky lze měnit jen u nové (zatím nepotvrzené) objednávky,
        # jinak by nesouhlasil odečet ze skladu.
        return obj is None or obj.status == Order.Status.NEW

    has_add_permission = has_change_permission
    has_delete_permission = has_change_permission


def _run_bulk(modeladmin, request, queryset, func, success_msg):
    ok = 0
    for order in queryset.order_by("pk"):
        try:
            func(order.pk)
            ok += 1
        except services.InsufficientStock as exc:
            detail = ", ".join(
                f"{s.sku} (chce {s.requested}, skladem {s.available})" for s in exc.shortages
            )
            modeladmin.message_user(
                request, f"{order.external_id}: nedostatek zboží – {detail}", messages.ERROR
            )
        except services.StockError as exc:
            modeladmin.message_user(request, f"{order.external_id}: {exc}", messages.WARNING)
    if ok:
        modeladmin.message_user(request, f"{success_msg}: {ok}", messages.SUCCESS)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        "external_id",
        "source",
        "status",
        "customer_name",
        "city",
        "shipping_service",
        "item_count",
        "created_at",
    ]
    list_filter = ["status", "source", "shipping_service", "created_at"]
    search_fields = ["external_id", "customer_name", "email", "city", "zip_code"]
    date_hierarchy = "created_at"
    readonly_fields = ["status", "imported_at", "confirmed_at"]
    inlines = [OrderItemInline]
    actions = ["confirm_selected", "ship_selected", "cancel_selected"]
    fieldsets = [
        (None, {"fields": ["external_id", "source", "status", "created_at"]}),
        (
            "Doručení",
            {
                "fields": [
                    "customer_name",
                    ("email", "phone"),
                    "street",
                    ("city", "zip_code", "country"),
                    "shipping_service",
                ]
            },
        ),
        ("Časy", {"fields": ["imported_at", "confirmed_at"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_item_count=Count("items"))

    @admin.display(description="položek", ordering="_item_count")
    def item_count(self, obj):
        return obj._item_count

    @admin.action(description="Potvrdit vybrané objednávky (odečíst sklad)")
    def confirm_selected(self, request, queryset):
        _run_bulk(self, request, queryset, services.confirm_order, "Potvrzeno")

    @admin.action(description="Označit vybrané jako odeslané")
    def ship_selected(self, request, queryset):
        _run_bulk(self, request, queryset, services.mark_shipped, "Odesláno")

    @admin.action(description="Stornovat vybrané (vrátit zboží na sklad)")
    def cancel_selected(self, request, queryset):
        _run_bulk(self, request, queryset, services.cancel_order, "Stornováno")
