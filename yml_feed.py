"""
YML-фид для 2ГИС из МойСклад
==============================
Запуск:
    pip install flask requests
    python yml_feed.py

Фид будет доступен по адресу:
    http://localhost:5000/feed.yml          — все активные товары
    http://localhost:5000/feed.yml?articles=00031,00032,40-543  — конкретные артикулы

Эту ссылку вставляешь в Личный кабинет 2ГИС → Товары и услуги → Указать URL.
Если нужен публичный URL — разверни на сервере или используй ngrok.
"""

import os
import requests
from flask import Flask, Response, request
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from datetime import datetime

# ──────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "91f6afceabefb1ff36a498b3f7b7efbeff0cfabb")
SHOP_NAME      = "Мебель — столы и стулья"
COMPANY_NAME   = "Ваша компания"
SHOP_URL       = "https://example.com"   # замени на свой сайт
CURRENCY       = "KZT"
DEFAULT_ARTICLES = ["00031", "00032", "40-543"]  # артикулы по умолчанию

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"

app = Flask(__name__)


# ──────────────────────────────────────────
# МойСклад API
# ──────────────────────────────────────────
def ms_headers():
    return {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Accept-Encoding": "gzip",
    }


def get_products_by_articles(articles: list[str]) -> list[dict]:
    """Получить товары из МойСклад по списку артикулов."""
    results = []
    for article in articles:
        resp = requests.get(
            f"{MS_BASE}/entity/product",
            headers=ms_headers(),
            params={"filter": f"article={article}", "limit": 5, "expand": "productFolder"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        results.extend(rows)
    return results


def get_product_images(product_id: str) -> list[str]:
    """Получить URL-изображений товара."""
    resp = requests.get(
        f"{MS_BASE}/entity/product/{product_id}/images",
        headers=ms_headers(),
        params={"limit": 3},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    urls = []
    for img in resp.json().get("rows", []):
        # Берём miniature или thumbnail если есть
        meta = img.get("meta", {})
        href = meta.get("downloadHref") or meta.get("href", "")
        if href:
            urls.append(href)
    return urls


def get_sale_price(product: dict) -> float | None:
    """Извлечь цену продажи (первая цена типа 'Цена продажи')."""
    for price in product.get("salePrices", []):
        ptype = price.get("priceType", {}).get("name", "")
        if "продаж" in ptype.lower() or ptype == "":
            val = price.get("value", 0)
            return val / 100 if val else None
    return None


# ──────────────────────────────────────────
# Генерация YML
# ──────────────────────────────────────────
def build_yml(products: list[dict]) -> str:
    # Корень
    catalog = Element("yml_catalog")
    catalog.set("date", datetime.now().strftime("%Y-%m-%d %H:%M"))

    shop = SubElement(catalog, "shop")

    SubElement(shop, "name").text    = SHOP_NAME
    SubElement(shop, "company").text = COMPANY_NAME
    SubElement(shop, "url").text     = SHOP_URL

    # Валюты
    currencies = SubElement(shop, "currencies")
    cur = SubElement(currencies, "currency")
    cur.set("id", CURRENCY)
    cur.set("rate", "1")

    # Категории — собираем из папок товаров
    categories = SubElement(shop, "categories")
    cat_ids: dict[str, str] = {}
    for p in products:
        folder = p.get("productFolder")
        if folder:
            fid  = folder["meta"]["href"].split("/")[-1]
            name = folder.get("name", "Без категории")
            if fid not in cat_ids:
                cat_ids[fid] = name
                cat_el = SubElement(categories, "category")
                cat_el.set("id", fid)
                cat_el.text = name

    if not cat_ids:
        # Дефолтная категория если папок нет
        cat_el = SubElement(categories, "category")
        cat_el.set("id", "1")
        cat_el.text = "Мебель"

    # Офферы
    offers = SubElement(shop, "offers")
    for p in products:
        pid     = p["id"]
        name    = p.get("name", "—")
        article = p.get("article", "")
        desc    = p.get("description", "")
        price   = get_sale_price(p)

        folder  = p.get("productFolder")
        if folder:
            cat_id = folder["meta"]["href"].split("/")[-1]
        else:
            cat_id = list(cat_ids.keys())[0] if cat_ids else "1"

        offer = SubElement(offers, "offer")
        offer.set("id", pid)
        offer.set("available", "true" if p.get("saleable", True) else "false")

        SubElement(offer, "name").text       = name
        SubElement(offer, "url").text        = f"{SHOP_URL}/product/{pid}"
        SubElement(offer, "currencyId").text = CURRENCY
        SubElement(offer, "categoryId").text = cat_id

        if price is not None:
            SubElement(offer, "price").text = str(int(price))
        if article:
            SubElement(offer, "vendorCode").text = article
        if desc:
            SubElement(offer, "description").text = desc

        # Изображения (отдельный запрос на каждый товар)
        try:
            for img_url in get_product_images(pid)[:3]:
                SubElement(offer, "picture").text = img_url
        except Exception:
            pass

    # Красивый XML
    raw = tostring(catalog, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    return dom.toprettyxml(indent="  ", encoding=None).replace('<?xml version="1.0" ?>', '')


# ──────────────────────────────────────────
# Flask-маршруты
# ──────────────────────────────────────────
@app.route("/feed.yml")
def feed():
    # ?articles=00031,00032,40-543 или дефолт
    raw = request.args.get("articles", "")
    articles = [a.strip() for a in raw.split(",") if a.strip()] or DEFAULT_ARTICLES

    try:
        products = get_products_by_articles(articles)
    except requests.HTTPError as e:
        return Response(f"Ошибка МойСклад: {e}", status=502, mimetype="text/plain")

    if not products:
        return Response("Товары не найдены", status=404, mimetype="text/plain")

    yml = build_yml(products)
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>\n{yml}',
        mimetype="application/xml; charset=utf-8",
    )


@app.route("/")
def index():
    articles = ",".join(DEFAULT_ARTICLES)
    return (
        "<h2>YML-фид для 2ГИС</h2>"
        f'<p><a href="/feed.yml">/feed.yml</a> — все товары по умолчанию ({articles})</p>'
        f'<p><a href="/feed.yml?articles={articles}">/feed.yml?articles={articles}</a> — явный список артикулов</p>'
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"\n🚀  Фид запущен: http://localhost:{port}/feed.yml")
    print(f"    Артикулы по умолчанию: {DEFAULT_ARTICLES}\n")
    app.run(host="0.0.0.0", port=port, debug=True)
