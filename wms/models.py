from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class Product(models.Model):
    sku = models.CharField("SKU", max_length=64, unique=True)
    name = models.CharField("název", max_length=255)
    stock = models.PositiveIntegerField("skladem (ks)", default=0)
    weight_kg = models.DecimalField(
        "hmotnost (kg)",
        max_digits=8,
        decimal_places=3,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    updated_at = models.DateTimeField("upraveno", auto_now=True)

    class Meta:
        ordering = ["sku"]
        verbose_name = "produkt"
        verbose_name_plural = "produkty"

    def __str__(self) -> str:
        return f"{self.sku} – {self.name}"


class Order(models.Model):
    class Source(models.TextChoices):
        SHOPTET = "shoptet", "Shoptet"
        WOOCOMMERCE = "woocommerce", "WooCommerce"
        XML = "xml", "XML feed"

    class Status(models.TextChoices):
        NEW = "new", "Nová"
        CONFIRMED = "confirmed", "Potvrzená"
        SHIPPED = "shipped", "Odeslaná"
        CANCELLED = "cancelled", "Stornovaná"

    class ShippingService(models.TextChoices):
        ZASILKOVNA = "zasilkovna", "Zásilkovna"
        PPL = "ppl", "PPL"

    external_id = models.CharField("externí ID", max_length=64, unique=True)
    source = models.CharField("zdroj", max_length=16, choices=Source.choices)
    status = models.CharField(
        "stav", max_length=16, choices=Status.choices, default=Status.NEW, db_index=True
    )
    created_at = models.DateTimeField("vytvořeno v e-shopu", default=timezone.now, db_index=True)
    imported_at = models.DateTimeField("importováno", auto_now_add=True)
    confirmed_at = models.DateTimeField("potvrzeno", null=True, blank=True)

    customer_name = models.CharField("jméno", max_length=255)
    email = models.EmailField("e-mail", blank=True)
    phone = models.CharField("telefon", max_length=32, blank=True)
    street = models.CharField("ulice a č. p.", max_length=255)
    city = models.CharField("město", max_length=128)
    zip_code = models.CharField("PSČ", max_length=16)
    country = models.CharField("země", max_length=2, default="CZ")
    shipping_service = models.CharField("dopravce", max_length=16, choices=ShippingService.choices)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "objednávka"
        verbose_name_plural = "objednávky"

    def __str__(self) -> str:
        return f"{self.external_id} ({self.get_status_display()})"

    @property
    def total_weight_kg(self) -> Decimal:
        """Celková hmotnost zásilky podle hmotností produktů."""
        return sum(
            (item.quantity * item.product.weight_kg for item in self.items.all()),
            Decimal("0"),
        )


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items", verbose_name="objednávka"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="order_items", verbose_name="produkt"
    )
    quantity = models.PositiveIntegerField("množství", validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        "cena za kus", max_digits=10, decimal_places=2, default=Decimal("0")
    )

    class Meta:
        verbose_name = "položka objednávky"
        verbose_name_plural = "položky objednávky"
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="unique_product_per_order"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.product.sku}"
