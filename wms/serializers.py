import re
from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

from .importers import map_shipping_service
from .models import Order, OrderItem, Product

ZIP_5_DIGITS = re.compile(r"^\d{5}$")
PHONE_RE = re.compile(r"^\+?[\d\s()/-]{6,32}$")
ZIP_5_COUNTRIES = {"CZ", "SK"}


class ImportItemSerializer(serializers.Serializer):
    sku = serializers.CharField(max_length=64)
    quantity = serializers.IntegerField(min_value=1, max_value=100_000)
    unit_price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0"),
        rounding=ROUND_HALF_UP,
        required=False,
        default=Decimal("0"),
    )


class ImportOrderSerializer(serializers.Serializer):
    """Validuje jednu normalizovanou objednávku a uloží ji i s položkami."""

    external_id = serializers.CharField(max_length=64)
    source = serializers.ChoiceField(choices=Order.Source.choices)
    created_at = serializers.DateTimeField(required=False, allow_null=True)
    customer_name = serializers.CharField(max_length=255)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    street = serializers.CharField(max_length=255)
    city = serializers.CharField(max_length=128)
    zip_code = serializers.CharField(max_length=16)
    country = serializers.CharField(max_length=2, required=False, default="CZ")
    shipping_service = serializers.CharField(max_length=128)
    items = ImportItemSerializer(many=True, allow_empty=False)

    def validate_country(self, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise serializers.ValidationError("Země musí být dvoupísmenný kód ISO (např. CZ).")
        return value

    def validate_phone(self, value: str) -> str:
        if value and not PHONE_RE.match(value):
            raise serializers.ValidationError("Neplatné telefonní číslo.")
        return value

    def validate_shipping_service(self, value: str) -> str:
        service = map_shipping_service(value)
        if service is None:
            raise serializers.ValidationError(
                f"Nepodporovaný dopravce „{value}“. Povoleno: Zásilkovna, PPL."
            )
        return service

    def validate(self, attrs: dict) -> dict:
        zip_code = attrs["zip_code"].replace(" ", "")
        if attrs["country"] in ZIP_5_COUNTRIES and not ZIP_5_DIGITS.match(zip_code):
            raise serializers.ValidationError({"zip_code": ["PSČ musí mít 5 číslic."]})
        attrs["zip_code"] = zip_code

        # Sloučení řádků se stejným SKU (e-shopy občas posílají varianty zvlášť).
        merged: dict[str, dict] = {}
        for item in attrs["items"]:
            sku = item["sku"].strip()
            if sku in merged:
                merged[sku]["quantity"] += item["quantity"]
            else:
                merged[sku] = {"quantity": item["quantity"], "unit_price": item["unit_price"]}

        products = Product.objects.in_bulk(list(merged), field_name="sku")
        missing = sorted(set(merged) - set(products))
        if missing:
            raise serializers.ValidationError({"items": [f"Neznámé SKU: {', '.join(missing)}"]})

        attrs["items"] = [{"product": products[sku], **data} for sku, data in merged.items()]
        return attrs

    def create(self, validated_data: dict) -> Order:
        items = validated_data.pop("items")
        if validated_data.get("created_at") is None:
            validated_data.pop("created_at", None)
        order = Order.objects.create(**validated_data)
        OrderItem.objects.bulk_create(
            OrderItem(
                order=order,
                product=item["product"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )
            for item in items
        )
        return order


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "sku", "name", "stock", "weight_kg"]


class OrderItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="product.sku", read_only=True)
    name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = OrderItem
        fields = ["sku", "name", "quantity", "unit_price"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    total_weight_kg = serializers.DecimalField(max_digits=10, decimal_places=3, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "external_id",
            "source",
            "status",
            "created_at",
            "confirmed_at",
            "customer_name",
            "email",
            "phone",
            "street",
            "city",
            "zip_code",
            "country",
            "shipping_service",
            "total_weight_kg",
            "items",
        ]
