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


def get_variant_as_product(variant: dict) -> dict:
    """Склеивает данные модификации с родительским товаром."""
    # Получаем родительский товар
    product_href = variant.get("product", {}).get("meta", {}).get("href", "")
    parent = {}
    if product_href:
        try:
            parent = requests.get(
                product_href,
                headers=MS_HEADERS,
                params={"expand": "productFolder"},
                timeout=15,
            ).json()
        except Exception:
            pass

    # Собираем итоговый объект: берём данные модификации, дополняем родителем
    result = {**parent, **{k: v for k, v in variant.items() if v}}
    # Название: "Товар / Характеристики"
    characteristics = ", ".join(
        c.get("value", "") for c in variant.get("characteristics", [])
    )
    base_name = parent.get("name") or variant.get("name", "—")
    result["name"] = f"{base_name} / {characteristics}" if characteristics else base_name
    result["id"]   = variant["id"]
    return result


def get_products(codes: list[str]) -> list[dict]:
    results = []
    seen_ids = set()

    for code in codes:
        found = False

        # 1. Ищем среди модификаций (variant) по коду
        rows = ms_get("/entity/variant", {
            "filter": f"code={code}",
            "expand": "product",
            "limit": 5,
        }).get("rows", [])
        if rows:
            print(f"  Код '{code}': найдено {len(rows)} модификаций")
            for v in rows:
                if v["id"] not in seen_ids:
                    seen_ids.add(v["id"])
                    results.append(get_variant_as_product(v))
            found = True

        # 2. Если не нашли — ищем среди обычных товаров по артикулу/коду
        if not found:
            for params in [
                {"filter": f"article={code}", "expand": "productFolder", "limit": 5},
                {"filter": f"code={code}",    "expand": "productFolder", "limit": 5},
            ]:
                rows = ms_get("/entity/product", params).get("rows", [])
                if rows:
                    print(f"  Код '{code}': найдено {len(rows)} товаров")
                    for r in rows:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            results.append(r)
                    found = True
                    break

        if not found:
            print(f"  Код '{code}': не найден")

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
