from django.urls import path

from .views import top_products

app_name = "wms"

urlpatterns = [
    path("top-products/", top_products, name="top-products"),
]
