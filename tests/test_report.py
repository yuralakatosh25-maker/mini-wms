from wms.models import Order

URL = "/reports/top-products/"


def test_report_requires_staff(client, user):
    client.force_login(user)  # běžný uživatel bez is_staff
    response = client.get(URL)
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_report_orders_by_sold_quantity(admin_client, make_order):
    make_order({"HRNEK-BILY": 5, "TRIKO-M-CERNE": 1}, status=Order.Status.CONFIRMED)
    make_order({"TRIKO-M-CERNE": 2}, status=Order.Status.SHIPPED)
    make_order({"MIKINA-L-SEDA": 50})  # nová – nepočítá se
    make_order({"PONOZKY-3PACK": 50}, status=Order.Status.CANCELLED)  # storno – nepočítá se

    response = admin_client.get(URL + "?days=all")

    assert response.status_code == 200
    rows = response.context["rows"]
    assert [(r["product__sku"], r["total_qty"], r["order_count"]) for r in rows] == [
        ("HRNEK-BILY", 5, 1),
        ("TRIKO-M-CERNE", 3, 2),
    ]
    assert rows[0]["share_pct"] == 100
    html = response.content.decode()
    assert "Nejprodávanější produkty" in html
    assert "bootstrap@5" in html


def test_report_invalid_period_falls_back(admin_client, db):
    response = admin_client.get(URL + "?days=abc")
    assert response.status_code == 200
    assert response.context["period"] == "30"
