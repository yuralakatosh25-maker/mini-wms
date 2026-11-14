import csv
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .importers import ImportFormatError, normalize_json
from .models import Order, OrderItem, Product
from .parsers import CSVRenderer, TextXMLOrdersParser, XMLOrders, XMLOrdersParser
from .serializers import ImportOrderSerializer, OrderSerializer, ProductSerializer

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- import


class ImportOrdersView(APIView):
    """``POST /api/import/orders/`` – import objednávek z e-shopu (JSON nebo XML).

    * Objednávky s již existujícím ``external_id`` (nebo opakované v dávce)
      se přeskočí a vrátí v ``duplicates``.
    * Nevalidní objednávky se neuloží a vrátí v ``errors``; validní z téže dávky
      se uloží (každá objednávka ve vlastní transakci).
    * Odpověď: 201 pokud vznikla aspoň jedna objednávka, 400 pokud nevznikla
      žádná a byly chyby, jinak 200 (např. samé duplicity).
    """

    parser_classes = [JSONParser, XMLOrdersParser, TextXMLOrdersParser]

    def post(self, request):
        data = request.data
        if isinstance(data, XMLOrders):
            raw_orders = list(data)
        else:
            if not data:
                raise ParseError("Prázdné tělo požadavku.")
            try:
                raw_orders = normalize_json(data, request.query_params.get("source"))
            except ImportFormatError as exc:
                raise ParseError(str(exc)) from exc

        if not raw_orders:
            raise ValidationError({"detail": "Požadavek neobsahuje žádné objednávky."})
        limit = settings.WMS_IMPORT_MAX_ORDERS
        if len(raw_orders) > limit:
            raise ValidationError({"detail": f"Maximálně {limit} objednávek v jednom importu."})

        created, duplicates, errors = [], [], []
        seen: set[str] = set()

        for raw in raw_orders:
            external_id = raw.get("external_id")
            external_id = str(external_id).strip() if external_id is not None else None

            if raw.get("_error"):
                errors.append({"external_id": external_id, "errors": {"detail": [raw["_error"]]}})
                continue

            if external_id and (
                external_id in seen or Order.objects.filter(external_id=external_id).exists()
            ):
                duplicates.append(external_id)
                continue

            serializer = ImportOrderSerializer(data=raw)
            if not serializer.is_valid():
                errors.append({"external_id": external_id, "errors": serializer.errors})
                continue

            try:
                with transaction.atomic():
                    order = serializer.save()
            except IntegrityError:
                # Souběžný import stejné objednávky – unikátní index to zachytil.
                duplicates.append(external_id)
                continue

            seen.add(order.external_id)
            created.append({"id": order.pk, "external_id": order.external_id})

        logger.info(
            "Import objednávek: %d nových, %d duplicit, %d chyb",
            len(created),
            len(duplicates),
            len(errors),
        )

        if created:
            code = status.HTTP_201_CREATED
        elif errors:
            code = status.HTTP_400_BAD_REQUEST
        else:
            code = status.HTTP_200_OK
        return Response(
            {"created": created, "duplicates": duplicates, "errors": errors}, status=code
        )


# --------------------------------------------------------------------------- objednávky


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer

    def get_queryset(self):
        qs = Order.objects.prefetch_related("items__product")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def _transition(self, func):
        order = self.get_object()
        try:
            order = func(order.pk)
        except services.InsufficientStock as exc:
            return Response(
                {
                    "detail": "Nedostatek zboží na skladě.",
                    "shortages": [s.as_dict() for s in exc.shortages],
                },
                status=status.HTTP_409_CONFLICT,
            )
        except services.InvalidOrderState as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        order = self.get_queryset().get(pk=order.pk)
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Potvrdí objednávku a sníží sklad; při nedostatku vrátí 409."""
        return self._transition(services.confirm_order)

    @action(detail=True, methods=["post"])
    def ship(self, request, pk=None):
        return self._transition(services.mark_shipped)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._transition(services.cancel_order)


# --------------------------------------------------------------------------- export

SHIPMENT_CSV_HEADER = [
    "cislo_objednavky",
    "jmeno",
    "adresa",
    "mesto",
    "psc",
    "zeme",
    "telefon",
    "email",
    "vaha_kg",
    "sluzba",
]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Ochrana proti CSV/formula injection při otevření v Excelu."""
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


class ShipmentsExportView(APIView):
    """``GET /api/export/shipments.csv`` – zásilky pro dopravce.

    Standardně exportuje potvrzené objednávky (připravené k odeslání).
    Parametry: ``service=zasilkovna|ppl``, ``status=confirmed|shipped|new``.
    CSV je v UTF-8 s BOM a oddělovačem ``;`` (otevře se rovnou v Excelu).
    """

    # JSON kvůli chybovým odpovědím, CSV aby prošlo „Accept: text/csv“.
    renderer_classes = [JSONRenderer, CSVRenderer]

    def get(self, request):
        status_filter = request.query_params.get("status", Order.Status.CONFIRMED)
        if status_filter not in Order.Status.values:
            raise ValidationError({"status": f"Neplatný stav '{status_filter}'."})
        service = request.query_params.get("service")
        if service and service not in Order.ShippingService.values:
            raise ValidationError({"service": f"Neplatný dopravce '{service}'."})

        orders = (
            Order.objects.filter(status=status_filter)
            .prefetch_related("items__product")
            .order_by("created_at", "pk")
        )
        if service:
            orders = orders.filter(shipping_service=service)

        filename = f"shipments-{timezone.localdate():%Y%m%d}.csv"
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.write("﻿")
        writer = csv.writer(response, delimiter=";", lineterminator="\r\n")
        writer.writerow(SHIPMENT_CSV_HEADER)
        for order in orders:
            writer.writerow(
                [
                    _csv_safe(order.external_id),
                    _csv_safe(order.customer_name),
                    _csv_safe(order.street),
                    _csv_safe(order.city),
                    order.zip_code,
                    order.country,
                    order.phone,
                    _csv_safe(order.email),
                    f"{order.total_weight_kg:.3f}",
                    order.get_shipping_service_display(),
                ]
            )
        return response


# --------------------------------------------------------------------------- report

REPORT_PERIODS = {"7": 7, "30": 30, "90": 90, "365": 365, "all": None}


@staff_member_required
def top_products(request):
    """Report „nejprodávanější produkty“ (potvrzené a odeslané objednávky)."""
    period = request.GET.get("days", "30")
    if period not in REPORT_PERIODS:
        period = "30"

    items = OrderItem.objects.filter(
        order__status__in=[Order.Status.CONFIRMED, Order.Status.SHIPPED]
    )
    days = REPORT_PERIODS[period]
    if days is not None:
        items = items.filter(order__created_at__gte=timezone.now() - timedelta(days=days))

    revenue = ExpressionWrapper(
        F("quantity") * F("unit_price"), output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    rows = list(
        items.values("product__sku", "product__name", "product__stock")
        .annotate(
            total_qty=Sum("quantity"),
            order_count=Count("order", distinct=True),
            revenue=Sum(revenue),
        )
        .order_by("-total_qty", "product__sku")[:20]
    )
    top_qty = rows[0]["total_qty"] if rows else 0
    for row in rows:
        row["share_pct"] = round(row["total_qty"] * 100 / top_qty) if top_qty else 0

    context = {
        "rows": rows,
        "period": period,
        "periods": [
            ("7", "7 dní"),
            ("30", "30 dní"),
            ("90", "90 dní"),
            ("365", "rok"),
            ("all", "vše"),
        ],
        "total_qty": sum(r["total_qty"] for r in rows),
        "total_revenue": sum((r["revenue"] or 0) for r in rows),
    }
    return render(request, "wms/top_products.html", context)
