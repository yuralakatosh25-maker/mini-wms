"""Převod objednávek z formátů e-shopů na jednotnou (normalizovanou) podobu.

Podporované vstupy:

* **Shoptet JSON** – ``{"data": {"orders": [...]}}``, ``{"orders": [...]}``,
  seznam nebo jedna objednávka; pozná se podle klíčů ``code`` + ``items``.
* **WooCommerce JSON** – seznam objednávek z REST API ``/wc/v3/orders``
  nebo jedna objednávka; pozná se podle klíče ``line_items``.
* **XML** – vlastní jednoduchý feed ``<orders><order>…</order></orders>``.

Výstupem je seznam slovníků se shodnými klíči, které dále validuje
``ImportOrderSerializer``. Parsování XML používá ``defusedxml`` (ochrana proti
XXE a „billion laughs“).
"""

import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

SOURCE_SHOPTET = "shoptet"
SOURCE_WOOCOMMERCE = "woocommerce"
SOURCE_XML = "xml"
JSON_SOURCES = (SOURCE_SHOPTET, SOURCE_WOOCOMMERCE)


class ImportFormatError(ValueError):
    """Vstup nelze vůbec přečíst (špatná struktura, nevalidní XML…)."""


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def map_shipping_service(raw: Any) -> str | None:
    """Namapuje název dopravy z e-shopu na ``zasilkovna`` / ``ppl``."""
    if not isinstance(raw, str):
        return None
    text = _strip_accents(raw).lower()
    if "zasilkovna" in text or "packeta" in text:
        return "zasilkovna"
    if "ppl" in text:
        return "ppl"
    return None


def _quantity(value: Any) -> Any:
    """Shoptet posílá množství jako řetězec ``"2.000"`` – převedeme na int.

    Neceločíselné nebo nečíselné hodnoty vracíme beze změny, ať je odmítne
    validace s rozumnou chybovou hláškou.
    """
    if isinstance(value, bool):
        return value
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return value
    if number == number.to_integral_value():
        return int(number)
    return value


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _join(*parts: Any) -> str:
    return " ".join(p for p in (_text(x) for x in parts) if p)


def _shoptet(order: dict) -> dict:
    address = order.get("deliveryAddress") or order.get("billingAddress") or {}
    shipping = order.get("shipping") or {}
    items = [
        item
        for item in order.get("items") or []
        if isinstance(item, dict) and item.get("itemType", "product") == "product"
    ]
    return {
        "external_id": order.get("code"),
        "source": SOURCE_SHOPTET,
        "created_at": order.get("creationTime"),
        "customer_name": address.get("fullName") or _join(order.get("fullName")),
        "email": _text(order.get("email")),
        "phone": _text(order.get("phone")),
        "street": _join(address.get("street"), address.get("houseNumber")),
        "city": address.get("city"),
        "zip_code": address.get("zip"),
        "country": _text(address.get("countryCode")) or "CZ",
        "shipping_service": shipping.get("name"),
        "items": [
            {
                "sku": item.get("code"),
                "quantity": _quantity(item.get("amount")),
                "unit_price": item.get("itemPriceWithVat") or item.get("unitPriceWithVat") or "0",
            }
            for item in items
        ],
    }


def _woo_created_at(order: dict) -> Any:
    """WooCommerce posílá ``date_created_gmt`` bez zóny – doplníme UTC."""
    gmt = order.get("date_created_gmt")
    if isinstance(gmt, str) and gmt:
        return gmt if gmt.endswith("Z") or "+" in gmt else f"{gmt}Z"
    return order.get("date_created")


def _woocommerce(order: dict) -> dict:
    shipping = order.get("shipping") or {}
    billing = order.get("billing") or {}
    address = shipping if shipping.get("address_1") else billing
    lines = order.get("shipping_lines") or [{}]
    first_line = lines[0] if isinstance(lines[0], dict) else {}
    external_id = order.get("number") or order.get("id")
    return {
        "external_id": None if external_id is None else str(external_id),
        "source": SOURCE_WOOCOMMERCE,
        "created_at": _woo_created_at(order),
        "customer_name": _join(address.get("first_name"), address.get("last_name"))
        or _join(address.get("company")),
        "email": _text(billing.get("email")),
        "phone": _text(shipping.get("phone") or billing.get("phone")),
        "street": _join(address.get("address_1"), address.get("address_2")),
        "city": address.get("city"),
        "zip_code": address.get("postcode"),
        "country": _text(address.get("country")) or "CZ",
        "shipping_service": first_line.get("method_title") or first_line.get("method_id"),
        "items": [
            {
                "sku": item.get("sku"),
                "quantity": _quantity(item.get("quantity")),
                "unit_price": item.get("price") if item.get("price") is not None else "0",
            }
            for item in order.get("line_items") or []
            if isinstance(item, dict)
        ],
    }


def _detect_source(order: dict) -> str | None:
    if "line_items" in order:
        return SOURCE_WOOCOMMERCE
    if "code" in order and "items" in order:
        return SOURCE_SHOPTET
    return None


def _unknown(order: Any, message: str) -> dict:
    external_id = None
    if isinstance(order, dict):
        external_id = order.get("code") or order.get("number") or order.get("id")
    return {"external_id": external_id, "_error": message}


def normalize_json(data: Any, source_hint: str | None = None) -> list[dict]:
    """Převede JSON (už rozparsovaný) na seznam normalizovaných objednávek."""
    if source_hint and source_hint not in JSON_SOURCES:
        raise ImportFormatError(
            f"Neznámý zdroj '{source_hint}'. Povolené hodnoty: {', '.join(JSON_SOURCES)}."
        )

    if isinstance(data, dict):
        if isinstance(data.get("data"), dict) and "orders" in data["data"]:
            orders = data["data"]["orders"]  # Shoptet API obálka
        elif "orders" in data:
            orders = data["orders"]
        else:
            orders = [data]
    elif isinstance(data, list):
        orders = data
    else:
        raise ImportFormatError("Očekáván JSON objekt nebo seznam objednávek.")

    if not isinstance(orders, list):
        raise ImportFormatError("Klíč 'orders' musí obsahovat seznam.")

    result = []
    for order in orders:
        if not isinstance(order, dict):
            result.append(_unknown(order, "Objednávka musí být JSON objekt."))
            continue
        source = source_hint or _detect_source(order)
        if source == SOURCE_SHOPTET:
            result.append(_shoptet(order))
        elif source == SOURCE_WOOCOMMERCE:
            result.append(_woocommerce(order))
        else:
            result.append(
                _unknown(order, "Nerozpoznaný formát objednávky (očekáván Shoptet/WooCommerce).")
            )
    return result


def _find(element, path: str) -> str:
    return _text(element.findtext(path))


def parse_xml(body: bytes) -> list[dict]:
    """Rozparsuje XML feed objednávek.

    Formát::

        <orders>
          <order>
            <external_id>X-1001</external_id>
            <created_at>2026-11-10T09:15:00+01:00</created_at>
            <customer>
              <name>…</name><email>…</email><phone>…</phone>
              <street>…</street><city>…</city><zip>…</zip><country>CZ</country>
            </customer>
            <shipping>Zásilkovna</shipping>
            <items>
              <item><sku>…</sku><quantity>2</quantity><price>199.00</price></item>
            </items>
          </order>
        </orders>
    """
    try:
        root = SafeET.fromstring(body, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise ImportFormatError("XML obsahuje zakázané konstrukce (DTD/entity).") from exc
    except SafeET.ParseError as exc:
        raise ImportFormatError(f"Nevalidní XML: {exc}") from exc

    if root.tag == "order":
        order_elements = [root]
    elif root.tag == "orders":
        order_elements = root.findall("order")
    else:
        raise ImportFormatError("Kořenový element musí být <orders> nebo <order>.")

    result = []
    for el in order_elements:
        result.append(
            {
                "external_id": _find(el, "external_id") or None,
                "source": SOURCE_XML,
                "created_at": _find(el, "created_at") or None,
                "customer_name": _find(el, "customer/name"),
                "email": _find(el, "customer/email"),
                "phone": _find(el, "customer/phone"),
                "street": _find(el, "customer/street"),
                "city": _find(el, "customer/city"),
                "zip_code": _find(el, "customer/zip"),
                "country": _find(el, "customer/country") or "CZ",
                "shipping_service": _find(el, "shipping"),
                "items": [
                    {
                        "sku": _find(item, "sku"),
                        "quantity": _quantity(_find(item, "quantity")),
                        "unit_price": _find(item, "price") or "0",
                    }
                    for item in el.findall("items/item")
                ],
            }
        )
    return result
