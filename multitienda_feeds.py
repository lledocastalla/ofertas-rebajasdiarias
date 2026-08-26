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
import html
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

LEROY_MERLIN_MAX_PER_CYCLE = 50
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


def _fetch_leroy_merlin_feed(feed, log, local_test_file=None):
    """Descarga y filtra UN feed concreto de Leroy Merlin (por su dict de LEROY_MERLIN_FEEDS).
    Compartido por fetch_leroy_merlin_offers() (rotación normal, 1 por ciclo) y
    fetch_leroy_merlin_offers_all() (catch-up manual, los 7 de golpe)."""
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


def fetch_leroy_merlin_offers(log, local_test_file=None):
    """Uso normal (dentro de main() en cada ciclo de la Pi): rota 1 de los 7 feeds."""
    idx = _leroy_merlin_next_feed_index()
    feed = LEROY_MERLIN_FEEDS[idx]
    return _fetch_leroy_merlin_feed(feed, log, local_test_file)


def fetch_leroy_merlin_offers_all(log):
    """Catch-up manual (24 ago 2026): descarga los 7 feeds de golpe en vez de esperar a que
    la rotación normal (1 por ciclo, ~1 feed/día) los cubra todos con el tiempo — para no
    tener el catálogo cojo (solo Jardín/Bricolaje) mientras rota. No toca el índice de
    rotación (_leroy_merlin_next_feed_index), así el ciclo normal de la Pi sigue su ritmo de
    siempre después de este catch-up puntual."""
    result = {}
    for feed in LEROY_MERLIN_FEEDS:
        result.update(_fetch_leroy_merlin_feed(feed, log))
    return result


# ---------------------------------------------------------------------------
# Stylevana (1 solo feed, sin rotación — 23.7k filas, ligero, se puede pedir entero cada ciclo)
# ---------------------------------------------------------------------------

STYLEVANA_MAX_PER_CYCLE = 50
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


# ---------------------------------------------------------------------------
# Perfumería Comas (25 ago 2026, aprobada en Awin -- ver RASPI_REBAJASDIARIAS.md) -- 1 solo
# feed, sin rotación, mismo patrón que Stylevana. IMPORTANTE: el feed NATIVO de Awin
# ("Crea-un-feed", formato Awin/CSV normal) NO trae rrp_price ni saving poblados para este
# anunciante en absoluto (comprobado 25 ago 2026: 0 de 6.859 filas) -- sin precio de
# referencia no se puede calcular descuento real, así que en su lugar se usa el feed
# alternativo en FORMATO GOOGLE SHOPPING que Awin genera para el mismo anunciante (fid Google
# "F4298"), que sí trae price/sale_price reales (4.359 de 7.370 productos con ≥30% de
# descuento real en la comprobación). URL de descarga distinta a _awin_feed_url() porque es
# un feed pre-generado por Awin (Descargar lista / "Crea-un-feed"), no un feed nativo
# parametrizable por columnas.
# ---------------------------------------------------------------------------

PERFUMERIA_COMAS_MAX_PER_CYCLE = 150  # "todos los más vendidos" (pedido explícito) -- feed
                                       # entero se descarga igual cada ciclo (como Stylevana),
                                       # así que subir el tope no cuesta red extra, solo da
                                       # más variedad real por ciclo en vez de quedarse
                                       # siempre con el mismo top-60 fijo
PERFUMERIA_COMAS_GOOGLE_FEED_ID = "F4298"
PERFUMERIA_COMAS_FEED_URL = (
    f"https://ui.awin.com/productdata-darwin-download/publisher/3029543/"
    f"{AWIN_API_KEY}/1/feed/{PERFUMERIA_COMAS_GOOGLE_FEED_ID}.csv.gz"
)

# "Quiero los mejores perfumes, todos los más vendidos en España" (pedido explícito del
# usuario, 25 ago) -- igual que las búsquedas por marca de Amazon (KEYWORDS_BY_CATEGORY), se
# filtra el feed a una lista curada de marcas de perfumería reconocidas/best-seller en España,
# en vez de aceptar cualquier marca que traiga el feed sin filtrar. Comprobado contra el feed
# real: Chanel/Dior/Guerlain NO aparecen en este anunciante (distribución propia, normal en
# perfumería de lujo); las de abajo sí, con volumen real. Solo aplica a la categoría
# "Perfumes" del feed -- maquillaje/cosmética/cabello no se filtran por marca, ahí interesa
# más variedad que exclusividad.
PERFUMERIA_COMAS_PERFUME_BRAND_ALLOWLIST = {
    html.unescape(b).strip().upper()
    for b in [
        "Paco Rabanne", "Giorgio Armani", "Carolina Herrera", "Jean Paul Gaultier",
        "Yves Saint Laurent", "Givenchy", "Dolce & Gabbana", "Dolce & Gabanna",
        "Hermès", "Hugo Boss", "Lancôme", "Calvin Klein", "Issey Miyake", "Kenzo",
        "Mugler", "Prada", "Narciso Rodriguez", "Versace", "Sisley", "Rochas",
        "Cacharel", "Zadig & Voltaire", "Ralph Lauren", "Valentino", "Chloé",
        "Gucci", "Viktor & Rolf", "Nina Ricci", "Elie Saab", "Montblanc",
        "Jimmy Choo", "Azzaro", "Lacoste", "Diesel", "DKNY", "Moschino",
        "Burberry", "Tom Ford",
    ]
}


def _perfumeria_comas_map_category(product_type, brand_upper):
    """product_type viene como 'Perfumes &gt; Perfumes de Mujer &gt; ...' (jerarquía separada
    por '>', primer nivel es lo único que hace falta). Perfumería aparte de Belleza (pedido
    explícito: 'perfumeria aparte pero también tienda' -- categoría Y filtro de tienda a la
    vez, no una cosa u otra, ver store/store_label más abajo)."""
    top = html.unescape(product_type or "").split(">")[0].strip().lower()
    if top.startswith("perfum"):
        return "Perfumería", brand_upper in PERFUMERIA_COMAS_PERFUME_BRAND_ALLOWLIST
    if top == "parafarmacia":
        return "Salud", True
    return "Belleza", True  # maquillaje, cosmética, cabello, estuches, cualquier otro


def fetch_perfumeria_comas_offers(log, local_test_file=None):
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        else:
            text = _download_feed_csv(PERFUMERIA_COMAS_FEED_URL, log)
            if text.startswith("﻿"):
                text = text[1:]
    except Exception as e:
        log(f"[perfumeria_comas] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    row_errors = 0
    for row in reader:
        # Fila a fila, sin dejar que UNA fila rara (formato inesperado en un campo concreto,
        # distinto al de la muestra usada al construir esto) tumbe el lote entero de 150 -- 26
        # ago 2026, ver comentario de fetch_multitienda_offers() sobre el bug real que causó
        # esto en producción.
        try:
            price_raw = (row.get("price") or "").split()[:1]
            sale_raw = (row.get("sale_price") or "").split()[:1]
            if not price_raw or not sale_raw:
                continue
            try:
                original = float(price_raw[0])
                actual = float(sale_raw[0])
            except ValueError:
                continue
            if original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
                continue
            pct = (original - actual) / original * 100
            if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
                continue

            brand_upper = html.unescape(row.get("brand") or "").strip().upper()
            category, keep = _perfumeria_comas_map_category(row.get("product_type"), brand_upper)
            if not keep:
                continue

            # El campo "title" del feed es solo el nombre de la fragancia/producto, sin marca
            # ("Girl", "One", "Boss In Motion 100 ml") -- ilegible suelto en una tarjeta, se le
            # antepone la marca (ya en mayúsculas por brand_upper; .title() la deja legible,
            # p.ej. "HUGO BOSS" -> "Hugo Boss", "DOLCE & GABBANA" -> "Dolce & Gabbana").
            raw_title = html.unescape((row.get("title") or "").strip())
            brand_display = brand_upper.title()
            title = f"{brand_display} {raw_title}".strip() if brand_display else raw_title
            pid = (row.get("id") or "").strip()
            image = (row.get("image_link") or "").strip()
            aff_url = (row.get("aw_deep_link") or row.get("link") or "").strip()
            if not title or not pid or not aff_url:
                continue

            candidates.append({
                "id": f"pc_{pid}",
                "title": title[:180],
                "category": category,
                "price": round(actual, 2),
                "original_price": round(original, 2),
                "discount_percent": int(round(pct)),
                "is_flash": False,
                "image": image,
                "url": aff_url,
                "store": "perfumeriacomas",
                "store_label": "Perfumería Comas",
            })
        except Exception as e:
            row_errors += 1
            if row_errors <= 3:  # unas pocas de muestra, no inundar el log si se repite mucho
                log(f"[perfumeria_comas] aviso: fila descartada por error de parseo: {e!r}")
            continue

    if row_errors:
        log(f"[perfumeria_comas] {row_errors} fila(s) descartadas por error de parseo de {reader.line_num} totales")

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:PERFUMERIA_COMAS_MAX_PER_CYCLE]
    log(f"[perfumeria_comas] {len(candidates)} candidatos 30-80% (tras filtro de marca en "
        f"Perfumes), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_multitienda_offers(log, local_test_files=None):
    """Punto de entrada único. local_test_files (dict opcional {'leroymerlin': path, 'stylevana': path,
    'perfumeriacomas': path}) solo para pruebas locales sin red — en producción se omite y se
    descarga de Awin de verdad.

    26 ago 2026, bug real encontrado en producción: Perfumería Comas nunca llegó a publicarse
    en ningún ciclo desde que se integró (24h+ después, 0 ofertas), pese a que la misma función
    probada a mano en local funcionaba perfectamente. Causa raíz: fetch_perfumeria_comas_offers()
    solo tenía try/except alrededor de la DESCARGA, no del bucle de parseo entero -- cualquier
    excepción ahí (fila rara del feed real, distinta a la muestra usada al construirlo) subía
    sin capturar hasta el try/except genérico de update_offers.py ("fallo en la ingesta
    multi-tienda"), que descarta TODO el resultado de fetch_multitienda_offers() de golpe --
    Leroy Merlin y Stylevana también se perdían ese ciclo entero, no solo Comas (se notaba
    menos porque sus ofertas previas seguían vivas dentro de STALE_AFTER_DAYS). Cada tienda va
    ahora en su propio try/except: un fallo en una nunca se lleva a las demás por delante,
    mismo principio que ya protege multi-tienda frente a un fallo del scraping de Amazon."""
    local_test_files = local_test_files or {}
    result = {}
    stores = [
        ("leroy_merlin", fetch_leroy_merlin_offers, "leroymerlin"),
        ("stylevana", fetch_stylevana_offers, "stylevana"),
        ("perfumeria_comas", fetch_perfumeria_comas_offers, "perfumeriacomas"),
    ]
    for name, fetch_fn, key in stores:
        try:
            result.update(fetch_fn(log, local_test_files.get(key)))
        except Exception as e:
            log(f"[{name}] aviso: fallo inesperado, se omite esta tienda este ciclo "
                f"(las demás no se ven afectadas): {e!r}")
    return result


if __name__ == "__main__":
    def _log(msg):
        print(msg)

    result = fetch_multitienda_offers(_log, local_test_files={
        "leroymerlin": "/private/tmp/claude-501/-Users-lledo/5baec47c-9e26-4d09-8ab2-329e33f4fed3/scratchpad/leroy_merlin_resto.csv.gz",
        "stylevana": "/private/tmp/claude-501/-Users-lledo/5baec47c-9e26-4d09-8ab2-329e33f4fed3/scratchpad/stylevana.csv.gz",
    })
    print(f"\nTotal ofertas multi-tienda construidas: {len(result)}")
    by_store = {}
    for o in result.values():
        by_store[o["store"]] = by_store.get(o["store"], 0) + 1
    print("Por tienda:", by_store)
    by_cat = {}
    for o in result.values():
        by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
    print("Por categoría:", by_cat)
