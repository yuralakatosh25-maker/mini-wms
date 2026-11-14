import csv
import io

from wms.models import Order
from wms.views import SHIPMENT_CSV_HEADER

URL = "/api/export/shipments.csv"


def read_csv(response) -> list[list[str]]:
    content = b"".join(response.streaming_content) if response.streaming else response.content
    text = content.decode("utf-8")
    assert text.startswith("﻿"), "CSV má začínat BOM kvůli Excelu"
    return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def test_export_contains_confirmed_orders_with_weight(api, make_order):
    make_order(
        {"TRIKO-M-CERNE": 2, "HRNEK-BILY": 1},  # 2×0,2 + 0,35 = 0,75 kg
        external_id="E-1",
        status=Order.Status.CONFIRMED,
        customer_name="Jan Novák",
        street="Dlouhá 12",
        city="Praha",
        zip_code="11000",
    )
    make_order(
        {"MIKINA-L-SEDA": 1},
        external_id="E-2",
        status=Order.Status.CONFIRMED,
        shipping_service=Order.ShippingService.PPL,
    )

    response = api.get(URL)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    rows = read_csv(response)
    assert rows[0] == SHIPMENT_CSV_HEADER
    by_id = {row[0]: dict(zip(rows[0], row, strict=True)) for row in rows[1:]}
    assert by_id["E-1"]["jmeno"] == "Jan Novák"
    assert by_id["E-1"]["adresa"] == "Dlouhá 12"
    assert by_id["E-1"]["psc"] == "11000"
    assert by_id["E-1"]["vaha_kg"] == "0.750"
    assert by_id["E-1"]["sluzba"] == "Zásilkovna"
    assert by_id["E-2"]["vaha_kg"] == "0.600"
    assert by_id["E-2"]["sluzba"] == "PPL"


def test_export_skips_unconfirmed_orders_by_default(api, make_order):
    make_order({"HRNEK-BILY": 1}, external_id="NEW-1")
    make_order({"HRNEK-BILY": 1}, external_id="CAN-1", status=Order.Status.CANCELLED)
    make_order({"HRNEK-BILY": 1}, external_id="OK-1", status=Order.Status.CONFIRMED)

    rows = read_csv(api.get(URL))

    assert [r[0] for r in rows[1:]] == ["OK-1"]


def test_export_filter_by_service_and_status(api, make_order):
    make_order({"HRNEK-BILY": 1}, external_id="Z-1", status=Order.Status.CONFIRMED)
    make_order(
        {"HRNEK-BILY": 1},
        external_id="P-1",
        status=Order.Status.CONFIRMED,
        shipping_service=Order.ShippingService.PPL,
    )
    make_order(
        {"HRNEK-BILY": 1},
        external_id="P-2",
        status=Order.Status.SHIPPED,
        shipping_service=Order.ShippingService.PPL,
    )

    assert [r[0] for r in read_csv(api.get(URL + "?service=ppl"))[1:]] == ["P-1"]
    assert [r[0] for r in read_csv(api.get(URL + "?status=shipped"))[1:]] == ["P-2"]
    assert api.get(URL + "?service=dhl").status_code == 400
    assert api.get(URL + "?status=foo").status_code == 400


def test_export_empty_has_only_header(api, db):
    rows = read_csv(api.get(URL))
    assert rows == [SHIPMENT_CSV_HEADER]


def test_export_accepts_text_csv_header(api, db):
    assert api.get(URL, HTTP_ACCEPT="text/csv").status_code == 200


def test_export_neutralizes_formulas(api, make_order):
    make_order(
        {"HRNEK-BILY": 1},
        external_id="F-1",
        status=Order.Status.CONFIRMED,
        customer_name='=HYPERLINK("http://evil")',
    )
    rows = read_csv(api.get(URL))
    assert rows[1][1].startswith("'=")


def test_export_requires_authentication(anon_api, db):
    assert anon_api.get(URL).status_code in (401, 403)


def test_imported_and_confirmed_order_appears_in_export(api, products, samples_dir):
    api.post(
        "/api/import/orders/",
        data=(samples_dir / "woocommerce_orders.json").read_bytes(),
        content_type="application/json",
    )
    order = Order.objects.get(external_id="WC-5521")
    api.post(f"/api/orders/{order.pk}/confirm/")

    rows = read_csv(api.get(URL))

    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["cislo_objednavky"] == "WC-5521"
    assert row["mesto"] == "Ostrava"
    assert row["vaha_kg"] == "0.650"  # 3×0,15 + 0,2
    assert row["sluzba"] == "PPL"
