#!/usr/bin/env python3
"""
Prototipo final de ingesta multi-tienda (Leroy Merlin + Stylevana) para RebajasDiarias.

fetch_multitienda_offers() es el punto de entrada: devuelve un dict {id: offer} con el
MISMO esquema que scrape_keyword() ya usa para Amazon, listo para mezclarse en
new_or_updated antes del merge/poda/output habitual de update_offers.py, sin tocar esa
lógica. Solo depende de la librería estándar (igual que el resto del script real).

Todos los textos que se muestran al usuario (título, categoría) quedan en español: el
título viene tal cual del feed (idioma=es solicitado a Awin), y la categoría SIEMPRE se
resuelve a través de nuestro propio mapeo (nunca se usa merchant_category en crudo, que a
veces viene en inglés sin traducir en el feed de origen).
"""

import csv
import gzip
import io
import json
import os
import re
import urllib.request

HOME = os.path.expanduser("~")

MIN_DISCOUNT_PERCENT = 30
MAX_DISCOUNT_PERCENT = 80
MIN_PRICE_EUR = 3.0  # descarta artículos de céntimos poco relevantes aunque el % cuadre,
                     # mismo criterio que ya usa stylevana_telegram.py

# Awin "Crea-un-feed": mismo token que ya usa stylevana_telegram.py (comprobado 14 ago 2026,
# no es secreto de cuenta, es específico de la descarga de feeds) — no duplicar en un archivo
# de secretos aparte, un solo sitio de verdad.
AWIN_API_KEY = "f239974da24e881acd5d3cdfa614a45e"

# ---------------------------------------------------------------------------
# Leroy Merlin (7 feeds por categoría, rotación 1 por ciclo)
# ---------------------------------------------------------------------------

LEROY_MERLIN_MAX_PER_CYCLE = 30
LEROY_MERLIN_STATE_PATH = f"{HOME}/.rebajas_leroy_feed_state.json"

LEROY_MERLIN_FEEDS = [
    {"fid": "84166", "name": "1P (productos propios)", "category": None},
    {"fid": "93436", "name": "Decoración", "category": "Decoración"},
    {"fid": "93437", "name": "Jardín y terraza", "category": "Jardín"},
    {"fid": "93438", "name": "Herramientas/Electricidad/Domótica/Ferretería/Seguridad/Pintura", "category": "Bricolaje"},
    {"fid": "93440", "name": "Cocinas/Baños/Iluminación", "category": "Cocinas y Baños"},
    {"fid": "93441", "name": "Resto de Productos", "category": None},
    {"fid": "113980", "name": "Muebles/Armarios y Ordenación", "category": "Muebles"},
]

LEROY_MERLIN_CATEGORY_MAP = {
    "Puertas, ventanas y escaleras": "Bricolaje",
    "Climatización y calefacción": "Bricolaje",
    "Calefacción y climatización": "Bricolaje",
    "Fontanería": "Bricolaje",
    "Construcción": "Bricolaje",
    "Cerámica": "Bricolaje",
    "Madera": "Bricolaje",
    "Reparación de la madera": "Bricolaje",
    "Suelos": "Bricolaje",
    "Energías renovables": "Bricolaje",
    "Selección productos": "Bricolaje",
    "Decoración": "Decoración",
    "Jardín y terraza": "Jardín",
    "Cocinas": "Cocinas y Baños",
    "Baños": "Cocinas y Baños",
    "Iluminación": "Cocinas y Baños",
    "Muebles": "Muebles",
    "Armarios y Ordenación": "Muebles",
}


def _leroy_merlin_next_feed_index():
    idx = 0
    try:
        with open(LEROY_MERLIN_STATE_PATH) as f:
            idx = json.load(f).get("index", 0)
    except Exception:
        idx = 0
    next_idx = (idx + 1) % len(LEROY_MERLIN_FEEDS)
    try:
        with open(LEROY_MERLIN_STATE_PATH, "w") as f:
            json.dump({"index": next_idx}, f)
    except Exception:
        pass
    return idx % len(LEROY_MERLIN_FEEDS)


def _leroy_merlin_map_category(feed_category, merchant_category):
    if feed_category:
        return feed_category
    return LEROY_MERLIN_CATEGORY_MAP.get((merchant_category or "").strip(), "Bricolaje")


def _awin_feed_url(api_key, fid, columns):
    return (
        f"https://productdata.awin.com/datafeed/download/apikey/{api_key}/language/es/"
        f"fid/{fid}/rid/0/hasEnhancedFeeds/0/columns/{columns}/format/csv/delimiter/%2C/"
        f"compression/gzip/adultcontent/1/"
    )


def _download_feed_csv(url, log, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return gzip.decompress(raw).decode("utf-8", errors="replace")


def fetch_leroy_merlin_offers(log, local_test_file=None):
    idx = _leroy_merlin_next_feed_index()
    feed = LEROY_MERLIN_FEEDS[idx]
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,saving,in_stock"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, feed["fid"], columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[leroy_merlin] error descargando feed '{feed['name']}': {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        # in_stock puede no venir informado en este feed (visto vacío en la muestra real del
        # 24 ago) — solo se descarta si viene explícitamente a "0", nunca por ausencia del dato.
        if (row.get("in_stock") or "1").strip() == "0":
            continue
        sp = row.get("search_price") or ""
        sv = row.get("saving") or ""
        if not sv.strip():
            continue
        try:
            original = float(sp)
            actual = float(re.sub(r"[^0-9.]", "", sv))
        except ValueError:
            continue
        if original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (row.get("product_name") or "").strip()
        pid = (row.get("aw_product_id") or row.get("merchant_product_id") or "").strip()
        image = (row.get("merchant_image_url") or "").strip()
        aff_url = (row.get("aw_deep_link") or "").strip()
        if not title or not pid or not aff_url:
            continue

        category = _leroy_merlin_map_category(feed["category"], row.get("merchant_category"))
        candidates.append({
            "id": f"lm_{pid}",
            "title": title[:180],
            "category": category,
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "leroymerlin",
            "store_label": "Leroy Merlin",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:LEROY_MERLIN_MAX_PER_CYCLE]
    log(f"[leroy_merlin] feed '{feed['name']}' (fid {feed['fid']}) -> "
        f"{len(candidates)} candidatos 30-80%, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


# ---------------------------------------------------------------------------
# Stylevana (1 solo feed, sin rotación — 23.7k filas, ligero, se puede pedir entero cada ciclo)
# ---------------------------------------------------------------------------

STYLEVANA_MAX_PER_CYCLE = 30
STYLEVANA_FID = "88396"


def _stylevana_map_category(merchant_category):
    top_level = (merchant_category or "").split(">")[0].strip().lower()
    if top_level in ("ropa y accesorios", "clothing", "clothing & accessories"):
        return "Moda Mujer"
    return "Belleza"  # catch-all: salud y belleza / health & beauty / vacío


def fetch_stylevana_offers(log, local_test_file=None):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,rrp_price,currency,in_stock"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, STYLEVANA_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[stylevana] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if row.get("in_stock") == "0":  # mismo criterio que stylevana_telegram.py
            continue
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue  # por seguridad, si algún día mezclan divisas no convertimos a ciegas
        sp = row.get("search_price") or ""
        rrp = row.get("rrp_price") or ""
        if not sp.strip() or not rrp.strip():
            continue
        try:
            actual = float(sp)
            original = float(rrp)
        except ValueError:
            continue
        if original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (row.get("product_name") or "").strip()
        pid = (row.get("aw_product_id") or row.get("merchant_product_id") or "").strip()
        image = (row.get("merchant_image_url") or "").strip()
        aff_url = (row.get("aw_deep_link") or "").strip()
        if not title or not pid or not aff_url:
            continue

        candidates.append({
            "id": f"sv_{pid}",
            "title": title[:180],
            "category": _stylevana_map_category(row.get("merchant_category")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "stylevana",
            "store_label": "Stylevana",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:STYLEVANA_MAX_PER_CYCLE]
    log(f"[stylevana] {len(candidates)} candidatos 30-80%, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_multitienda_offers(log, local_test_files=None):
    """Punto de entrada único, llamado desde main() de update_offers.py. local_test_files
    (dict opcional {'leroymerlin': path, 'stylevana': path}) solo para pruebas locales sin
    red — en producción se omite y se descarga de Awin de verdad."""
    local_test_files = local_test_files or {}
    result = {}
    result.update(fetch_leroy_merlin_offers(log, local_test_files.get("leroymerlin")))
    result.update(fetch_stylevana_offers(log, local_test_files.get("stylevana")))
    return result
    by_cat = {}
    for o in result.values():
        by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
    print("Por categoría:", by_cat)
