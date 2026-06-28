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

# Названия доп. полей в МойСклад, по которым работает выгрузка
ATTR_EXPORT_FLAG = "Выгружать 2gis"    # boolean: выгружать товар в 2ГИС
ATTR_DESCRIPTION = "Описание для 2gis"  # text: описание для фида
ATTR_CATEGORY    = "Разделы 2gis"       # справочник: категория (раздел)

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


def get_all_products() -> list[dict]:
    """Все товары из МойСклад (с пагинацией)."""
    products, offset = [], 0
    while True:
        rows = ms_get("/entity/product", {"limit": 1000, "offset": offset}).get("rows", [])
        products.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return products


def get_variants(product_id: str) -> list[dict]:
    """Все модификации товара."""
    try:
        return ms_get("/entity/variant", {"filter": f"productid={product_id}", "limit": 100}).get("rows", [])
    except Exception:
        return []


def get_attr_value(entity: dict, name: str):
    """Значение доп. поля МойСклад по названию (или None)."""
    for a in entity.get("attributes", []):
        if a.get("name") == name:
            return a.get("value")
    return None


def attr_text(entity: dict, name: str) -> str:
    v = get_attr_value(entity, name)
    if isinstance(v, dict):          # справочник (customentity) → {name: ...}
        return v.get("name", "") or ""
    return v if isinstance(v, str) else ""


def attr_flag(entity: dict, name: str) -> bool:
    return get_attr_value(entity, name) is True


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


def add_offer(offers, offer_id, name, parent_id, cat_id, price, article, desc, images):
    offer = SubElement(offers, "offer")
    offer.set("id", offer_id)
    offer.set("available", "true")
    SubElement(offer, "name").text       = name or "—"
    SubElement(offer, "url").text        = f"{SHOP_URL}/product/{parent_id}"
    SubElement(offer, "currencyId").text = CURRENCY
    SubElement(offer, "categoryId").text = cat_id
    if price:
        SubElement(offer, "price").text = str(int(price))
    if article:
        SubElement(offer, "vendorCode").text = article
    if desc:
        SubElement(offer, "description").text = desc
    for url in images:
        SubElement(offer, "picture").text = url


def build_yml(base_products: list[dict], out_dir: str) -> str:
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

    # Категории из поля «Разделы 2gis». YML требует числовой id → маппим имя → 1,2,3...
    categories = SubElement(shop, "categories")
    cat_num: dict[str, int] = {}
    for p in base_products:
        cname = attr_text(p, ATTR_CATEGORY).strip() or "Без категории"
        if cname not in cat_num:
            num = len(cat_num) + 1
            cat_num[cname] = num
            el = SubElement(categories, "category")
            el.set("id", str(num))
            el.text = cname

    offers = SubElement(shop, "offers")
    counter = 0
    skipped_no_photo = 0

    for base in base_products:
        parent_id = base["id"]
        base_name = base.get("name", "")
        article   = base.get("article") or ""
        art       = "".join(c for c in (article or "id") if c.isalnum()) or "id"
        cname     = attr_text(base, ATTR_CATEGORY).strip() or "Без категории"
        cat_id    = str(cat_num.get(cname, 1))
        # описание из поля «Описание для 2gis», без переносов строк (2ГИС их не допускает)
        desc      = " ".join(attr_text(base, ATTR_DESCRIPTION).split())

        # Модификации с фото → каждая отдельным оффером (без фото — пропускаем)
        entries = []  # (объект-для-цены/имени, картинки)
        for v in get_variants(parent_id):
            imgs = download_images(v["id"], v.get("name", ""), out_dir, "variant")
            if imgs:
                entries.append((v, imgs))

        # Если ни у одной модификации нет фото — берём родительский товар с его фото
        if not entries:
            pimgs = download_images(parent_id, base_name, out_dir, "product")
            if pimgs:
                entries = [(base, pimgs)]
            else:
                print(f"  ⚠ Пропуск (нет фото ни у модификаций, ни у товара): {base_name}")
                skipped_no_photo += 1
                continue

        for obj, imgs in entries:
            counter += 1
            price = get_sale_price(obj) or get_sale_price(base)
            add_offer(
                offers,
                offer_id=f"{art}{counter}"[:20],
                name=obj.get("name", base_name),
                parent_id=parent_id,
                cat_id=cat_id,
                price=price,
                article=article,
                desc=desc,
                images=imgs,
            )

    print(f"Офферов в фиде: {counter}; пропущено без фото: {skipped_no_photo}")

    raw = tostring(catalog, encoding="unicode")
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    body = "\n".join(dom.toprettyxml(indent="  ").splitlines()[1:])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE yml_catalog SYSTEM "shops.dtd">\n'
        + body
    )


if __name__ == "__main__":
    # По умолчанию берём ВСЕ товары и фильтруем по флагу «Выгружать 2gis».
    # ARTICLES (необязательно) — сузить до конкретных артикулов (для тестов).
    articles_env = os.getenv("ARTICLES", "").strip()
    if articles_env:
        arts = [a.strip() for a in articles_env.split(",") if a.strip()]
        print(f"Фильтр по артикулам: {arts}")
        all_products = get_products(arts)
    else:
        print("Загружаю все товары из МойСклад…")
        all_products = get_all_products()
    print(f"Всего товаров получено: {len(all_products)}")

    base_products = [p for p in all_products if attr_flag(p, ATTR_EXPORT_FLAG)]
    print(f"С флагом «{ATTR_EXPORT_FLAG}»: {len(base_products)}")

    if not base_products:
        raise SystemExit("Нет товаров с флагом выгрузки — прерываю")

    out = os.getenv("OUTPUT_FILE", "feed.xml")
    out_dir = os.path.dirname(out) or "."
    os.makedirs(out_dir, exist_ok=True)

    yml = build_yml(base_products, out_dir)
    with open(out, "w", encoding="utf-8") as f:
        f.write(yml)

    print(f"Фид сохранён: {out}")
