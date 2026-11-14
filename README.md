# mini-wms

Jednoduchý skladový systém (WMS) pro menší e-shop:

* import objednávek z e-shopu (**Shoptet JSON**, **WooCommerce JSON**, **XML**) s validací a ochranou proti duplicitám,
* skladové zásoby – potvrzení objednávky odečte sklad, při nedostatku vrací **409 Conflict**,
* export zásilek pro dopravce (**Zásilkovna**, **PPL**) do CSV,
* Django admin pro produkty a objednávky, report **„Nejprodávanější produkty“** (Bootstrap 5).

**Stack:** Python 3.12 · Django 5.2 · Django REST Framework · MariaDB 11 · Docker Compose · pytest · ruff · GitHub Actions

---

## Rychlý start (Docker Compose)

```bash
cp .env.example .env          # upravte hesla a DJANGO_SECRET_KEY
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_demo      # volitelně: demo produkty
```

* Admin: <http://localhost:8000/admin/>
* Report: <http://localhost:8000/reports/top-products/>
* API: <http://localhost:8000/api/>

Migrace se spouští automaticky při startu kontejneru `web`. Data MariaDB jsou ve volume `db_data`.

Náhodný `DJANGO_SECRET_KEY` vygenerujete např.:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

## Lokální vývoj (bez Dockeru)

Bez proměnné `DB_HOST` se použije SQLite, takže stačí:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo
DJANGO_DEBUG=1 python manage.py runserver
```

Pro vývoj proti MariaDB nastavte `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (viz `.env.example`).

---

## Datový model

| Model | Pole |
|---|---|
| `Product` | `sku` (unikátní), `name`, `stock`, `weight_kg` |
| `Order` | `external_id` (unikátní), `source` (`shoptet` / `woocommerce` / `xml`), `status` (`new` → `confirmed` → `shipped`, případně `cancelled`), `created_at`, doručovací údaje, `shipping_service` (`zasilkovna` / `ppl`) |
| `OrderItem` | `order`, `product`, `quantity`, `unit_price` |

Hmotnost zásilky se počítá z `Product.weight_kg × množství`.

## Autentizace API

Všechny endpointy `/api/` vyžadují přihlášení. Pro e-shop/integraci vytvořte uživatele a token:

```bash
docker compose exec web python manage.py createsuperuser --username eshop
docker compose exec web python manage.py drf_create_token eshop
```

a posílejte hlavičku `Authorization: Token <token>`. Funguje i HTTP Basic a session (prohlížeč po přihlášení do adminu).

---

## API

### `POST /api/import/orders/` – import objednávek

Podporované vstupy (rozhoduje `Content-Type`):

| Formát | Content-Type | Rozpoznání |
|---|---|---|
| Shoptet | `application/json` | objekt s `code` + `items`; obálka `{"data": {"orders": [...]}}`, `{"orders": [...]}`, seznam nebo jedna objednávka |
| WooCommerce | `application/json` | objekt s `line_items` (formát `/wp-json/wc/v3/orders`) |
| XML | `application/xml` nebo `text/xml` | `<orders><order>…</order></orders>` |

Formát lze vynutit parametrem `?source=shoptet` nebo `?source=woocommerce`. Ukázky jsou ve složce [`samples/`](samples/).

```bash
curl -X POST http://localhost:8000/api/import/orders/ \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  --data-binary @samples/shoptet_orders.json

curl -X POST http://localhost:8000/api/import/orders/ \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/xml" \
  --data-binary @samples/orders.xml
```

XML formát:

```xml
<orders>
  <order>
    <external_id>XML-9001</external_id>
    <created_at>2026-11-13T08:00:00+01:00</created_at>
    <customer>
      <name>Lucie Černá</name><email>lucie@example.cz</email><phone>+420 602 000 111</phone>
      <street>Husova 3</street><city>Plzeň</city><zip>301 00</zip><country>CZ</country>
    </customer>
    <shipping>Zásilkovna</shipping>
    <items>
      <item><sku>HRNEK-BILY</sku><quantity>4</quantity><price>199.00</price></item>
    </items>
  </order>
</orders>
```

**Pravidla importu**

* `external_id` je unikátní napříč všemi zdroji – už existující (nebo opakovaná v dávce) objednávka se **přeskočí** a vrátí v `duplicates`.
* Validuje se: povinné jméno/adresa/město/PSČ, PSČ pro CZ/SK = 5 číslic, e-mail, telefon, kód země, dopravce (název musí obsahovat *Zásilkovna*/*Packeta* nebo *PPL*), aspoň jedna položka, množství celé číslo ≥ 1, SKU musí existovat v produktech.
* Řádky stejného SKU se sloučí; u Shoptetu se ignorují položky typu doprava/platba (`itemType != "product"`).
* Každá objednávka se ukládá ve vlastní transakci – nevalidní se neuloží, validní z téže dávky ano.
* Import **nemění sklad** – ten se mění až potvrzením.
* XML se parsuje přes `defusedxml` (DTD a entity jsou zakázány – ochrana proti XXE).
* Limit: `WMS_IMPORT_MAX_ORDERS` objednávek (výchozí 500) a 5 MB na požadavek.

**Odpověď**

```json
{
  "created":    [{"id": 12, "external_id": "2026000101"}],
  "duplicates": ["2026000099"],
  "errors":     [{"external_id": "2026000103", "errors": {"items": ["Neznámé SKU: XYZ"]}}]
}
```

| Kód | Kdy |
|---|---|
| `201` | vznikla aspoň jedna objednávka |
| `200` | nic nového (např. samé duplicity) |
| `400` | nevznikla žádná objednávka a byly chyby, nebo nečitelný JSON/XML |
| `415` | nepodporovaný `Content-Type` |

### Objednávky a sklad

| Metoda | URL | Popis |
|---|---|---|
| `GET` | `/api/orders/?status=new` | seznam objednávek (stránkovaný) |
| `GET` | `/api/orders/{id}/` | detail |
| `POST` | `/api/orders/{id}/confirm/` | potvrzení → odečte sklad |
| `POST` | `/api/orders/{id}/ship/` | označí potvrzenou jako odeslanou |
| `POST` | `/api/orders/{id}/cancel/` | storno; u potvrzené vrátí zboží na sklad |
| `GET` | `/api/products/` | produkty a stav skladu |

Potvrzení probíhá v jedné transakci se zámky řádků (`SELECT … FOR UPDATE`): buď se odečtou **všechny** položky, nebo žádná. Když zboží nestačí:

```http
HTTP/1.1 409 Conflict

{"detail": "Nedostatek zboží na skladě.",
 "shortages": [{"sku": "PONOZKY-3PACK", "requested": 4, "available": 3}]}
```

`409` vrací i pokus o nepovolený přechod stavu (např. potvrdit podruhé, odeslat nepotvrzenou).

### `GET /api/export/shipments.csv` – export pro dopravce

Standardně obsahuje **potvrzené** objednávky (připravené k odeslání). Filtry: `?service=zasilkovna|ppl`, `?status=confirmed|shipped|new|cancelled`.

```bash
curl -H "Authorization: Token $TOKEN" -o zasilky.csv \
  "http://localhost:8000/api/export/shipments.csv?service=zasilkovna"
```

CSV je v UTF-8 s BOM, oddělovač `;` (otevře se rovnou v českém Excelu):

```
cislo_objednavky;jmeno;adresa;mesto;psc;zeme;telefon;email;vaha_kg;sluzba
2026000101;Jan Novák;Dlouhá 12;Praha;11000;CZ;+420 777 123 456;jan.novak@example.cz;0.750;Zásilkovna
```

Buňky začínající `=`, `+`, `-`, `@` se prefixují apostrofem (ochrana proti CSV injection).

---

## Admin a report

* **Produkty** – hledání podle SKU/názvu, filtr stavu zásoby (vyprodáno / málo / dostatek), rychlá úprava skladu přímo v seznamu.
* **Objednávky** – filtry podle stavu, zdroje, dopravce a data, položky inline (upravitelné jen u nové objednávky), hromadné akce *Potvrdit*, *Označit jako odeslané*, *Stornovat*. Při nedostatku zboží akce vypíše, která SKU chybí.
* **Report** `/reports/top-products/` (jen pro `is_staff`) – top 20 produktů podle prodaných kusů z potvrzených a odeslaných objednávek za 7/30/90/365 dní nebo celé období, s počtem objednávek, tržbou a aktuálním stavem skladu. Odkaz „Zobrazit web“ v adminu vede sem.

---

## Testy a kvalita kódu

```bash
pytest                    # SQLite (lokálně)
ruff check .
ruff format --check .
```

Testy (`tests/`) pokrývají import (všechny tři formáty, duplicity, validace, XXE, limity, autentizaci), sklad (potvrzení, 409, atomicita, storno, admin akce), export CSV a report.

**GitHub Actions** (`.github/workflows/ci.yml`) spouští při každém push/PR:

1. `ruff check` a `ruff format --check`,
2. kontrolu chybějících migrací a `pytest` proti **MariaDB 11.4** (service container).

## Struktura projektu

```
config/             nastavení Django, URL
wms/
  models.py         Product, Order, OrderItem
  importers.py      převod Shoptet/WooCommerce/XML → jednotný formát
  serializers.py    validace importu, API serializery
  services.py       potvrzení / odeslání / storno (práce se skladem)
  views.py          import, objednávky, CSV export, report
  admin.py          Django admin
  templates/wms/    Bootstrap 5 šablony
samples/            ukázkové objednávky
tests/              pytest
```

## Konfigurace (proměnné prostředí)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `DJANGO_SECRET_KEY` | – | povinné při běhu s MariaDB |
| `DJANGO_DEBUG` | `0` | `1` pro vývoj |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | čárkou oddělené |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | – | např. `https://wms.example.cz` |
| `DJANGO_SECURE_COOKIES` | `0` | `1` za HTTPS |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | – / `3306` / `mini_wms` / `mini_wms` / – | bez `DB_HOST` se použije SQLite |
| `DB_ROOT_PASSWORD` | – | jen pro kontejner MariaDB |
| `WMS_IMPORT_MAX_ORDERS` | `500` | max. objednávek v jednom importu |
| `WEB_PORT` | `8000` | port na hostiteli |
