from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ImportOrdersView, OrderViewSet, ProductViewSet, ShipmentsExportView

router = DefaultRouter()
router.register("products", ProductViewSet, basename="product")
router.register("orders", OrderViewSet, basename="order")

urlpatterns = [
    path("import/orders/", ImportOrdersView.as_view(), name="import-orders"),
    path("export/shipments.csv", ShipmentsExportView.as_view(), name="export-shipments"),
    *router.urls,
]
