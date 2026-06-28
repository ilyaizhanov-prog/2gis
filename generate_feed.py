"""
Генерация статического YML-фида для 2ГИС из МойСклад.
Запускается GitHub Actions по расписанию, результат → public/feed.yml
Фото скачиваются в public/images/ и публикуются вместе с фидом на GitHub Pages,
поэтому в фиде стоят публичные ссылки на картинки (а не закрытые URL МойСклад).
"""

import os
import sys
import requests
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from datetime import datetime


def load_dotenv(path: str = ".env") -> None:
    """Простой загрузчик .env для локального запуска (KEY=VALUE → os.environ)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# Windows-консоль по умолчанию cp1251 — форсируем UTF-8, иначе падает на кириллице
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

MOYSKLAD_TOKEN   = os.environ.get("MOYSKLAD_TOKEN", "")
SHOP_NAME        = "Мебель — столы и стулья"
COMPANY_NAME     = "Ваша компания"
SHOP_URL         = "https://example.com"          # замени на свой сайт
CURRENCY         = "KZT"                           # тенге (бизнес в Казахстане)
DEFAULT_ARTICLES = ["g1007"]

# Публичный адрес GitHub Pages, куда публикуется содержимое папки public/
PAGES_BASE = "https://ilyaizhanov-prog.github.io/2gis"

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"

if not MOYSKLAD_TOKEN:
    sys.exit("❌ Не задан MOYSKLAD_TOKEN (переменная окружения / .env / GitHub Secrets).")


def ms_get(path, params=None):
    resp = requests.get(
        f"{MS_BASE}{path}",
        headers={"Authorization": f"Bearer {MOYSKLAD_TOKEN}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_products(articles: list[str]) -> list[dict]:
    results = []
    for article in articles:
        rows = ms_get("/entity/product", {
            "filter": f"article={article}",
            "expand": "productFolder",
            "limit": 5,
        }).get("rows", [])
        results.extend(rows)
    return results


def get_variants(product_id: str) -> list[dict]:
    """Все модификации товара."""
    try:
        return ms_get("/entity/variant", {"filter": f"productid={product_id}", "limit": 100}).get("rows", [])
    except Exception:
        return []


def get_sale_price(product: dict) -> float | None:
    for price in product.get("salePrices", []):
        val = price.get("value", 0)
        if val:
            return val / 100
    return None


def download_images(obj_id: str, name: str, out_dir: str, entity: str = "product") -> list[str]:
    """Скачивает фото товара/модификации в {out_dir}/images/ и возвращает публичные URL."""
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    try:
        rows = ms_get(f"/entity/{entity}/{obj_id}/images", {"limit": 3}).get("rows", [])
    except Exception:
        return []

    urls = []
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-")[:40].strip()
    for i, row in enumerate(rows):
        href = row.get("meta", {}).get("downloadHref", "")
        if not href:
            continue
        try:
            img = requests.get(href, headers={"Authorization": f"Bearer {MOYSKLAD_TOKEN}"}, timeout=30)
            img.raise_for_status()
        except Exception:
            continue
        ct = img.headers.get("Content-Type", "image/jpeg")
        ext = "jpg" if "jpeg" in ct else ct.split("/")[-1].split(";")[0]
        filename = f"{safe_name}_{i+1}.{ext}"
        with open(os.path.join(images_dir, filename), "wb") as f:
            f.write(img.content)
        urls.append(f"{PAGES_BASE}/images/{quote(filename)}")
        print(f"    📷 {filename}")
    return urls


def build_yml(base_products: list[dict], variants: list[dict], out_dir: str) -> str:
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
    for p in base_products:
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

    base_by_id = {p["id"]: p for p in base_products}
    rows = variants if variants else base_products

    offers = SubElement(shop, "offers")
    for obj in rows:
        if variants:
            parent_id = obj.get("product", {}).get("meta", {}).get("href", "").split("/")[-1]
            base = base_by_id.get(parent_id, {})
        else:
            base = obj
            parent_id = obj["id"]

        folder = base.get("productFolder")
        cat_id = folder["meta"]["href"].split("/")[-1] if folder else (list(cat_ids)[0] if cat_ids else "1")

        offer = SubElement(offers, "offer")
        offer.set("id", obj["id"])
        offer.set("available", "true")

        SubElement(offer, "name").text       = obj.get("name", "—")
        SubElement(offer, "url").text        = f"{SHOP_URL}/product/{parent_id}"
        SubElement(offer, "currencyId").text = CURRENCY
        SubElement(offer, "categoryId").text = cat_id

        price = get_sale_price(obj) or get_sale_price(base)
        if price:
            SubElement(offer, "price").text = str(int(price))
        if base.get("article"):
            SubElement(offer, "vendorCode").text = base["article"]
        if base.get("description"):
            SubElement(offer, "description").text = base["description"]

        # Фото: сначала из модификации, иначе из базового товара
        images = download_images(obj["id"], obj.get("name", ""), out_dir, "variant") if variants else []
        if not images:
            images = download_images(parent_id, base.get("name", ""), out_dir, "product")
        for img_url in images:
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
    base_products = get_products(articles)
    print(f"Найдено базовых товаров: {len(base_products)}")

    if not base_products:
        raise SystemExit("Товары не найдены — прерываю")

    variants = []
    for base in base_products:
        variants.extend(get_variants(base["id"]))
    print(f"Найдено модификаций: {len(variants)}")

    out = os.getenv("OUTPUT_FILE", "feed.yml")
    out_dir = os.path.dirname(out) or "."
    os.makedirs(out_dir, exist_ok=True)

    yml = build_yml(base_products, variants, out_dir)
    with open(out, "w", encoding="utf-8") as f:
        f.write(yml)

    print(f"Фид сохранён: {out}")
