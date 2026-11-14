from decimal import Decimal

from django.core.management.base import BaseCommand

from wms.models import Product

DEMO_PRODUCTS = [
    ("TRIKO-M-CERNE", "Tričko černé M", 25, Decimal("0.200")),
    ("HRNEK-BILY", "Hrnek bílý", 40, Decimal("0.350")),
    ("MIKINA-L-SEDA", "Mikina šedá L", 8, Decimal("0.600")),
    ("PONOZKY-3PACK", "Ponožky 3-pack", 3, Decimal("0.150")),
]


class Command(BaseCommand):
    help = "Založí demo produkty odpovídající ukázkovým objednávkám ve složce samples/."

    def handle(self, *args, **options):
        for sku, name, stock, weight in DEMO_PRODUCTS:
            _, created = Product.objects.update_or_create(
                sku=sku, defaults={"name": name, "stock": stock, "weight_kg": weight}
            )
            self.stdout.write(f"{'+' if created else '~'} {sku}")
        self.stdout.write(self.style.SUCCESS("Demo produkty připraveny."))
