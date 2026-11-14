import json
from decimal import Decimal

import pytest

from wms.importers import map_shipping_service, normalize_json
from wms.models import Order

URL = "/api/import/orders/"


def post_json(client, payload, **params):
    query = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    return client.post(URL + query, data=json.dumps(payload), content_type="application/json")


def post_xml(client, body: str | bytes, content_type="application/xml"):
    if isinstance(body, str):
        body = body.encode()
    return client.post(URL, data=body, content_type=content_type)


def shoptet_order(code="S-1", **overrides):
    order = {
        "code": code,
        "creationTime": "2026-11-10T09:15:00+01:00",
        "email": "jan@example.cz",
        "deliveryAddress": {
            "fullName": "Jan Novák",
            "street": "Dlouhá",
            "houseNumber": "12",
            "city": "Praha",
            "zip": "110 00",
            "countryCode": "CZ",
        },
        "shipping": {"name": "Zásilkovna"},
        "items": [{"itemType": "product", "code": "HRNEK-BILY", "amount": "1.000"}],
    }
    order.update(overrides)
    return order


# --------------------------------------------------------------------------- formáty


def test_import_shoptet_sample(api, products, samples_dir):
    payload = json.loads((samples_dir / "shoptet_orders.json").read_text(encoding="utf-8"))

    response = post_json(api, payload)

    assert response.status_code == 201, response.json()
    body = response.json()
    assert [c["external_id"] for c in body["created"]] == ["2026000101", "2026000102"]
    assert body["duplicates"] == [] and body["errors"] == []

    order = Order.objects.get(external_id="2026000101")
    assert order.source == Order.Source.SHOPTET
    assert order.status == Order.Status.NEW
    assert order.customer_name == "Jan Novák"
    assert order.street == "Dlouhá 12"
    assert order.zip_code == "11000"
    assert order.shipping_service == Order.ShippingService.ZASILKOVNA
    # Řádek dopravy (itemType=shipping) se jako položka neimportuje.
    items = {i.product.sku: i for i in order.items.all()}
    assert set(items) == {"TRIKO-M-CERNE", "HRNEK-BILY"}
    assert items["TRIKO-M-CERNE"].quantity == 2
    assert items["TRIKO-M-CERNE"].unit_price == Decimal("349.00")

    # Druhá objednávka nemá doručovací adresu -> použije se fakturační.
    second = Order.objects.get(external_id="2026000102")
    assert second.city == "Brno"
    assert second.shipping_service == Order.ShippingService.PPL


def test_import_does_not_touch_stock(api, products, samples_dir):
    payload = json.loads((samples_dir / "shoptet_orders.json").read_text(encoding="utf-8"))
    post_json(api, payload)
    products["TRIKO-M-CERNE"].refresh_from_db()
    assert products["TRIKO-M-CERNE"].stock == 25


def test_import_woocommerce_sample(api, products, samples_dir):
    payload = json.loads((samples_dir / "woocommerce_orders.json").read_text(encoding="utf-8"))

    response = post_json(api, payload)

    assert response.status_code == 201, response.json()
    order = Order.objects.get(external_id="WC-5521")
    assert order.source == Order.Source.WOOCOMMERCE
    assert order.customer_name == "Karel Dvořák"
    assert order.street == "Nádražní 101"
    assert order.email == "karel.dvorak@example.cz"
    assert order.shipping_service == Order.ShippingService.PPL
    assert order.items.get(product__sku="PONOZKY-3PACK").quantity == 3


def test_import_xml_sample(api, products, samples_dir):
    body = (samples_dir / "orders.xml").read_bytes()

    response = post_xml(api, body)

    assert response.status_code == 201, response.json()
    order = Order.objects.get(external_id="XML-9001")
    assert order.source == Order.Source.XML
    assert order.city == "Plzeň"
    assert order.zip_code == "30100"
    assert order.items.get(product__sku="HRNEK-BILY").quantity == 4


def test_import_xml_text_content_type_and_single_order_root(api, products):
    body = """<order>
        <external_id>XML-1</external_id>
        <customer><name>Eva</name><street>A 1</street><city>Brno</city><zip>60200</zip></customer>
        <shipping>PPL</shipping>
        <items><item><sku>HRNEK-BILY</sku><quantity>1</quantity></item></items>
    </order>"""
    response = post_xml(api, body, content_type="text/xml")
    assert response.status_code == 201, response.json()
    assert Order.objects.filter(external_id="XML-1", country="CZ").exists()


def test_source_hint_forces_format(api, products):
    response = post_json(api, [shoptet_order("S-HINT")], source="shoptet")
    assert response.status_code == 201
    response = post_json(api, [shoptet_order("S-HINT-2")], source="magento")
    assert response.status_code == 400


def test_duplicate_skus_are_merged(api, products):
    order = shoptet_order(
        items=[
            {"code": "HRNEK-BILY", "amount": "1"},
            {"code": "HRNEK-BILY", "amount": "2"},
        ]
    )
    assert post_json(api, order).status_code == 201
    item = Order.objects.get(external_id="S-1").items.get()
    assert item.quantity == 3


# --------------------------------------------------------------------------- duplicity


def test_duplicate_external_id_is_skipped(api, products):
    assert post_json(api, [shoptet_order("S-1")]).status_code == 201

    response = post_json(api, [shoptet_order("S-1")])

    assert response.status_code == 200
    assert response.json() == {"created": [], "duplicates": ["S-1"], "errors": []}
    assert Order.objects.filter(external_id="S-1").count() == 1


def test_duplicate_within_one_batch(api, products):
    response = post_json(api, [shoptet_order("S-1"), shoptet_order("S-1"), shoptet_order("S-2")])

    assert response.status_code == 201
    body = response.json()
    assert [c["external_id"] for c in body["created"]] == ["S-1", "S-2"]
    assert body["duplicates"] == ["S-1"]
    assert Order.objects.count() == 2


def test_duplicate_across_formats(api, products, samples_dir):
    """external_id je unikátní napříč zdroji (JSON i XML)."""
    post_xml(api, (samples_dir / "orders.xml").read_bytes())
    response = post_json(api, shoptet_order("XML-9001"))
    assert response.json()["duplicates"] == ["XML-9001"]


# --------------------------------------------------------------------------- validace


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"items": []}, "items"),
        ({"items": [{"code": "NEEXISTUJE", "amount": "1"}]}, "items"),
        ({"items": [{"code": "HRNEK-BILY", "amount": "0"}]}, "items"),
        ({"items": [{"code": "HRNEK-BILY", "amount": "1.5"}]}, "items"),
        ({"shipping": {"name": "DHL Express"}}, "shipping_service"),
        ({"code": None}, "external_id"),
        ({"email": "neni-email"}, "email"),
        ({"creationTime": "včera"}, "created_at"),
        (
            {
                "deliveryAddress": {
                    "fullName": "Jan",
                    "street": "A 1",
                    "city": "Praha",
                    "zip": "1100",
                    "countryCode": "CZ",
                }
            },
            "zip_code",
        ),
        (
            {"deliveryAddress": {"fullName": "", "street": "A", "city": "B", "zip": "11000"}},
            "customer_name",
        ),
    ],
)
def test_invalid_order_is_rejected(api, products, overrides, field):
    response = post_json(api, [shoptet_order(**overrides)])

    assert response.status_code == 400, response.json()
    errors = response.json()["errors"]
    assert len(errors) == 1
    assert field in errors[0]["errors"]
    assert Order.objects.count() == 0


def test_mixed_batch_imports_valid_orders(api, products):
    bad = shoptet_order("S-BAD", items=[{"code": "NEEXISTUJE", "amount": "1"}])
    response = post_json(api, [shoptet_order("S-OK"), bad])

    assert response.status_code == 201
    body = response.json()
    assert [c["external_id"] for c in body["created"]] == ["S-OK"]
    assert body["errors"][0]["external_id"] == "S-BAD"
    assert "Neznámé SKU: NEEXISTUJE" in body["errors"][0]["errors"]["items"][0]
    assert not Order.objects.filter(external_id="S-BAD").exists()


def test_unknown_json_shape_is_reported(api, products):
    response = post_json(api, [{"foo": "bar"}])
    assert response.status_code == 400
    assert "Nerozpoznaný formát" in response.json()["errors"][0]["errors"]["detail"][0]


@pytest.mark.parametrize("payload", ["[1, 2", "", "42"])
def test_malformed_json_returns_400(api, products, payload):
    response = api.post(URL, data=payload, content_type="application/json")
    assert response.status_code == 400


def test_malformed_xml_returns_400(api, products):
    assert post_xml(api, "<orders><order>").status_code == 400
    assert post_xml(api, "<foo/>").status_code == 400


def test_xml_entities_are_forbidden(api, products):
    """Ochrana proti XXE – DTD a entity se odmítnou."""
    xxe = """<?xml version="1.0"?>
    <!DOCTYPE orders [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <orders><order><external_id>&xxe;</external_id></order></orders>"""
    response = post_xml(api, xxe)
    assert response.status_code == 400
    assert Order.objects.count() == 0


def test_unsupported_content_type(api, products):
    response = api.post(URL, data="a,b", content_type="text/csv")
    assert response.status_code == 415


def test_batch_size_limit(api, products, settings):
    settings.WMS_IMPORT_MAX_ORDERS = 2
    response = post_json(api, [shoptet_order(f"S-{i}") for i in range(3)])
    assert response.status_code == 400
    assert Order.objects.count() == 0


def test_import_requires_authentication(anon_api, products):
    response = post_json(anon_api, [shoptet_order()])
    assert response.status_code in (401, 403)
    assert Order.objects.count() == 0


# --------------------------------------------------------------------------- jednotky


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Zásilkovna", "zasilkovna"),
        ("ZASILKOVNA - Z-BOX", "zasilkovna"),
        ("Packeta Home", "zasilkovna"),
        ("PPL Parcel CZ", "ppl"),
        ("ppl", "ppl"),
        ("Česká pošta", None),
        (None, None),
    ],
)
def test_map_shipping_service(raw, expected):
    assert map_shipping_service(raw) == expected


def test_normalize_accepts_single_object_and_orders_key():
    single = normalize_json(shoptet_order("A"))
    wrapped = normalize_json({"orders": [shoptet_order("B")]})
    assert [o["external_id"] for o in single + wrapped] == ["A", "B"]
