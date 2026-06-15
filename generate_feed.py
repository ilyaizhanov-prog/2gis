"""
Генерация статического YML-фида для 2ГИС из МойСклад.
Запускается GitHub Actions по расписанию, результат → feed.yml
"""

import os
import requests
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from datetime import datetime

MOYSKLAD_TOKEN   = os.environ["MOYSKLAD_TOKEN"]   # задаётся в GitHub Secrets
SHOP_NAME        = "Мебель — столы и стулья"
COMPANY_NAME     = "Ваша компания"
SHOP_URL         = "https://example.com"          # замени на свой сайт
CURRENCY         = "KZT"
DEFAULT_ARTICLES = ["00031", "00032", "40-543"]

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"

MS_HEADERS = {
    "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
    "Accept":        "application/json;charset=utf-8",
    "Content-Type":  "application/json",
}


def ms_get(path, params=None):
    resp = requests.get(
        f"{MS_BASE}{path}",
        headers=MS_HEADERS,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_products(codes: list[str]) -> list[dict]:
    results = []
    for code in codes:
        data = ms_get("/entity/product", {
            "filter": f"code={code}",
            "expand": "productFolder",
            "limit": 5,
        })
        rows = data.get("rows", [])
        print(f"  Код '{code}': найдено {len(rows)} шт.")
        results.extend(rows)
    return results


def get_sale_price(product: dict) -> float | None:
    for price in product.get("salePrices", []):
        val = price.get("value", 0)
        if val:
            return val / 100
    return None


def get_images(product_id: str) -> list[str]:
    try:
        rows = ms_get(f"/entity/product/{product_id}/images", {"limit": 3}).get("rows", [])
        return [r["meta"].get("downloadHref", "") for r in rows if r.get("meta")]
    except Exception:
        return []


def build_yml(products: list[dict]) -> str:
    catalog = Element("yml_catalog")
    catalog.set("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
    shop = SubElement(catalog, "shop")

    SubElement(shop, "name").text    = SHOP_NAME
    SubElement(shop, "company").text = COMPANY_NAME
    SubElement(shop, "url").text     = SHOP_URL

    currencies = SubElement(shop, "currencies")
    cur = SubElement(currencies, "currency")
    cur.set("id", CURRENCY)
    cur.set("rate", "1")

    categories = SubElement(shop, "categories")
    cat_ids: dict[str, str] = {}
    for p in products:
        folder = p.get("productFolder")
        if folder:
            fid  = folder["meta"]["href"].split("/")[-1]
            name = folder.get("name", "Без категории")
            if fid not in cat_ids:
                cat_ids[fid] = name
                el = SubElement(categories, "category")
                el.set("id", fid)
                el.text = name
    if not cat_ids:
        el = SubElement(categories, "category")
        el.set("id", "1")
        el.text = "Мебель"

    offers = SubElement(shop, "offers")
    for p in products:
        pid   = p["id"]
        folder = p.get("productFolder")
        cat_id = folder["meta"]["href"].split("/")[-1] if folder else (list(cat_ids)[0] if cat_ids else "1")

        offer = SubElement(offers, "offer")
        offer.set("id", pid)
        offer.set("available", "true")

        SubElement(offer, "name").text       = p.get("name", "—")
        SubElement(offer, "url").text        = f"{SHOP_URL}/product/{pid}"
        SubElement(offer, "currencyId").text = CURRENCY
        SubElement(offer, "categoryId").text = cat_id

        price = get_sale_price(p)
        if price:
            SubElement(offer, "price").text = str(int(price))
        if p.get("article"):
            SubElement(offer, "vendorCode").text = p["article"]
        if p.get("description"):
            SubElement(offer, "description").text = p["description"]

        for img_url in get_images(pid):
            if img_url:
                SubElement(offer, "picture").text = img_url

    raw = tostring(catalog, encoding="unicode")
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(
        dom.toprettyxml(indent="  ").splitlines()[1:]
    )


if __name__ == "__main__":
    articles = os.getenv("ARTICLES", ",".join(DEFAULT_ARTICLES)).split(",")
    articles = [a.strip() for a in articles if a.strip()]

    print(f"Запрашиваю товары: {articles}")
    products = get_products(articles)
    print(f"Найдено товаров: {len(products)}")

    if not products:
        raise SystemExit("Товары не найдены — прерываю")

    yml = build_yml(products)

    out = os.getenv("OUTPUT_FILE", "feed.yml")
    with open(out, "w", encoding="utf-8") as f:
        f.write(yml)

    print(f"Фид сохранён: {out}")
