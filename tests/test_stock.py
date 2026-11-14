import pytest

from wms import services
from wms.models import Order


def confirm_url(order):
    return f"/api/orders/{order.pk}/confirm/"


def stock(product):
    product.refresh_from_db()
    return product.stock


def test_confirm_decreases_stock(api, products, make_order):
    order = make_order({"TRIKO-M-CERNE": 2, "HRNEK-BILY": 5})

    response = api.post(confirm_url(order))

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "confirmed"
    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.confirmed_at is not None
    assert stock(products["TRIKO-M-CERNE"]) == 23
    assert stock(products["HRNEK-BILY"]) == 35


def test_confirm_exact_stock_goes_to_zero(api, products, make_order):
    order = make_order({"PONOZKY-3PACK": 3})
    assert api.post(confirm_url(order)).status_code == 200
    assert stock(products["PONOZKY-3PACK"]) == 0


def test_insufficient_stock_returns_409_and_changes_nothing(api, products, make_order):
    # Triko by stačilo, ponožky ne -> nesmí se odečíst ani triko.
    order = make_order({"TRIKO-M-CERNE": 1, "PONOZKY-3PACK": 4})

    response = api.post(confirm_url(order))

    assert response.status_code == 409
    body = response.json()
    assert body["shortages"] == [{"sku": "PONOZKY-3PACK", "requested": 4, "available": 3}]
    order.refresh_from_db()
    assert order.status == Order.Status.NEW
    assert stock(products["TRIKO-M-CERNE"]) == 25
    assert stock(products["PONOZKY-3PACK"]) == 3


def test_second_order_fails_when_first_used_the_stock(api, products, make_order):
    first = make_order({"MIKINA-L-SEDA": 5})
    second = make_order({"MIKINA-L-SEDA": 5})

    assert api.post(confirm_url(first)).status_code == 200
    response = api.post(confirm_url(second))

    assert response.status_code == 409
    assert response.json()["shortages"][0]["available"] == 3
    assert stock(products["MIKINA-L-SEDA"]) == 3


def test_confirm_twice_returns_409(api, products, make_order):
    order = make_order({"HRNEK-BILY": 1})
    assert api.post(confirm_url(order)).status_code == 200

    response = api.post(confirm_url(order))

    assert response.status_code == 409
    assert stock(products["HRNEK-BILY"]) == 39


def test_confirm_unknown_order_returns_404(api, products):
    assert api.post("/api/orders/999999/confirm/").status_code == 404


def test_confirm_requires_authentication(anon_api, make_order):
    order = make_order({"HRNEK-BILY": 1})
    assert anon_api.post(confirm_url(order)).status_code in (401, 403)


def test_cancel_confirmed_order_returns_stock(api, products, make_order):
    order = make_order({"HRNEK-BILY": 10})
    api.post(confirm_url(order))
    assert stock(products["HRNEK-BILY"]) == 30

    response = api.post(f"/api/orders/{order.pk}/cancel/")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert stock(products["HRNEK-BILY"]) == 40


def test_cancel_new_order_keeps_stock(products, make_order):
    order = make_order({"HRNEK-BILY": 10})
    services.cancel_order(order.pk)
    assert stock(products["HRNEK-BILY"]) == 40


def test_ship_flow(api, products, make_order):
    order = make_order({"HRNEK-BILY": 1})
    assert api.post(f"/api/orders/{order.pk}/ship/").status_code == 409  # ještě nepotvrzená
    api.post(confirm_url(order))
    assert api.post(f"/api/orders/{order.pk}/ship/").json()["status"] == "shipped"
    # Odeslanou už nelze stornovat.
    assert api.post(f"/api/orders/{order.pk}/cancel/").status_code == 409


def test_service_raises_insufficient_stock(products, make_order):
    order = make_order({"MIKINA-L-SEDA": 9})
    with pytest.raises(services.InsufficientStock) as exc_info:
        services.confirm_order(order.pk)
    assert exc_info.value.shortages[0].sku == "MIKINA-L-SEDA"


def test_import_then_confirm_end_to_end(api, products, samples_dir):
    api.post(
        "/api/import/orders/",
        data=(samples_dir / "orders.xml").read_bytes(),
        content_type="application/xml",
    )
    order = Order.objects.get(external_id="XML-9001")

    assert api.post(confirm_url(order)).status_code == 200
    assert stock(products["HRNEK-BILY"]) == 36
    assert stock(products["PONOZKY-3PACK"]) == 2


def test_admin_confirm_action(client, products, make_order, django_user_model):
    admin = django_user_model.objects.create_superuser("admin", "a@example.cz", "x-pass-123")
    client.force_login(admin)
    ok = make_order({"HRNEK-BILY": 1})
    too_big = make_order({"PONOZKY-3PACK": 99})

    response = client.post(
        "/admin/wms/order/",
        {"action": "confirm_selected", "_selected_action": [ok.pk, too_big.pk]},
        follow=True,
    )

    assert response.status_code == 200
    ok.refresh_from_db()
    too_big.refresh_from_db()
    assert ok.status == Order.Status.CONFIRMED
    assert too_big.status == Order.Status.NEW
    messages = [str(m) for m in response.context["messages"]]
    assert any("nedostatek zboží" in m for m in messages)
