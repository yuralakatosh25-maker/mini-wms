"""Skladová logika: potvrzení, odeslání a storno objednávky.

Všechny operace běží v transakci a zamykají dotčené řádky
(``SELECT ... FOR UPDATE``), aby dvě souběžná potvrzení nemohla prodat
stejný kus dvakrát. Produkty se zamykají vždy ve stejném pořadí (podle id),
čímž se předchází deadlockům.
"""

from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Order, Product


@dataclass(frozen=True)
class Shortage:
    sku: str
    requested: int
    available: int

    def as_dict(self) -> dict:
        return {"sku": self.sku, "requested": self.requested, "available": self.available}


class StockError(Exception):
    """Základní výjimka skladových operací."""


class InsufficientStock(StockError):
    def __init__(self, shortages: list[Shortage]):
        self.shortages = shortages
        skus = ", ".join(s.sku for s in shortages)
        super().__init__(f"Nedostatek zboží na skladě: {skus}")


class InvalidOrderState(StockError):
    pass


def _lock_order(order_id: int) -> Order:
    return Order.objects.select_for_update().get(pk=order_id)


def _required_quantities(order: Order) -> dict[int, int]:
    required: dict[int, int] = defaultdict(int)
    for item in order.items.all():
        required[item.product_id] += item.quantity
    return required


@transaction.atomic
def confirm_order(order_id: int) -> Order:
    """Potvrdí objednávku a odečte zboží ze skladu.

    Buď se odečtou všechny položky, nebo žádná. Při nedostatku vyhodí
    :class:`InsufficientStock` se seznamem chybějících SKU.
    """
    order = _lock_order(order_id)
    if order.status != Order.Status.NEW:
        raise InvalidOrderState(
            f"Objednávku ve stavu „{order.get_status_display()}“ nelze potvrdit."
        )

    required = _required_quantities(order)
    if not required:
        raise InvalidOrderState("Objednávka nemá žádné položky.")

    products = {
        p.pk: p for p in Product.objects.select_for_update().filter(pk__in=required).order_by("pk")
    }
    shortages = [
        Shortage(sku=products[pid].sku, requested=qty, available=products[pid].stock)
        for pid, qty in sorted(required.items())
        if products[pid].stock < qty
    ]
    if shortages:
        raise InsufficientStock(shortages)

    for pid, qty in required.items():
        Product.objects.filter(pk=pid).update(stock=F("stock") - qty)

    order.status = Order.Status.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])
    return order


@transaction.atomic
def mark_shipped(order_id: int) -> Order:
    order = _lock_order(order_id)
    if order.status != Order.Status.CONFIRMED:
        raise InvalidOrderState("Odeslat lze jen potvrzenou objednávku.")
    order.status = Order.Status.SHIPPED
    order.save(update_fields=["status"])
    return order


@transaction.atomic
def cancel_order(order_id: int) -> Order:
    """Stornuje objednávku; u potvrzené vrátí zboží zpět na sklad."""
    order = _lock_order(order_id)
    if order.status not in (Order.Status.NEW, Order.Status.CONFIRMED):
        raise InvalidOrderState(
            f"Objednávku ve stavu „{order.get_status_display()}“ nelze stornovat."
        )
    if order.status == Order.Status.CONFIRMED:
        for pid, qty in sorted(_required_quantities(order).items()):
            Product.objects.filter(pk=pid).update(stock=F("stock") + qty)
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status"])
    return order
