from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from wms.models import Order, OrderItem, Product

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    """V testech neexistuje manifest z collectstatic – použijeme prosté úložiště."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES_DIR


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="eshop", password="x-test-pass-123")


@pytest.fixture
def api(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def anon_api() -> APIClient:
    return APIClient()


@pytest.fixture
def products(db) -> dict[str, Product]:
    data = [
        ("TRIKO-M-CERNE", "Tričko černé M", 25, Decimal("0.200")),
        ("HRNEK-BILY", "Hrnek bílý", 40, Decimal("0.350")),
        ("MIKINA-L-SEDA", "Mikina šedá L", 8, Decimal("0.600")),
        ("PONOZKY-3PACK", "Ponožky 3-pack", 3, Decimal("0.150")),
    ]
    return {
        sku: Product.objects.create(sku=sku, name=name, stock=stock, weight_kg=weight)
        for sku, name, stock, weight in data
    }


@pytest.fixture
def make_order(products):
    """Továrna na objednávky: ``make_order({"HRNEK-BILY": 2}, status=..., ...)``."""

    counter = {"n": 0}

    def factory(items: dict[str, int], **kwargs) -> Order:
        counter["n"] += 1
        defaults = {
            "external_id": f"T-{counter['n']}",
            "source": Order.Source.SHOPTET,
            "customer_name": "Jan Novák",
            "street": "Dlouhá 12",
            "city": "Praha",
            "zip_code": "11000",
            "country": "CZ",
            "phone": "+420 777 123 456",
            "email": "jan@example.cz",
            "shipping_service": Order.ShippingService.ZASILKOVNA,
        }
        defaults.update(kwargs)
        order = Order.objects.create(**defaults)
        for sku, qty in items.items():
            OrderItem.objects.create(
                order=order, product=products[sku], quantity=qty, unit_price=Decimal("100.00")
            )
        return order

    return factory
