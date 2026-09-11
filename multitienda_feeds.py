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

import concurrent.futures
import csv
import gzip
import heapq
import html
import io
import json
import os
import re
import urllib.parse
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

# Tradedoubler "Feed de Producto" (5 sep 2026, primera vez que se usa esta red para catálogo
# -- hasta ahora solo Awin). Token de cuenta obtenido en "Configuración del Feed" de
# publishers.tradedoubler.com, válido para cualquier fid de nuestros programas adheridos.
TRADEDOUBLER_TOKEN = "76BEEF2B875642D58D8CA9485889C04802806008"

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


def _download_tradedoubler_products(fid, log, timeout=60, max_products=1000):
    """Descarga el feed JSON preconstruido de Tradedoubler para un programa (fid). IMPORTANTE,
    límite real de la API descubierto el 5 sep 2026: pageSize=1000 es el máximo permitido Y
    ADEMÁS no se puede paginar más allá de 1000 productos en total (page=2 con pageSize=1000
    devuelve el error "PF_430 Can't paginate beyond 1000 products") -- así que, a diferencia
    de los feeds de Awin (que traen el catálogo entero de una vez), aquí el catálogo real
    queda SIEMPRE capado a como mucho 1000 productos, sea cual sea el tamaño real de la tienda
    (Desigual tiene 7.115, por ejemplo). Se acepta como límite permanente de la propia API, no
    algo que podamos evitar con más peticiones."""
    url = (
        f"https://api.tradedoubler.com/1.0/products.json;page=1;pageSize={max_products};"
        f"fid={fid}?token={TRADEDOUBLER_TOKEN}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if "products" not in data:
        raise RuntimeError(f"respuesta sin 'products': {data.get('errors', data)}")
    return data["products"]


def _td_field(fields, name):
    """Busca un campo por nombre en la lista 'fields' de un producto de Tradedoubler (lista de
    {"name":..., "value":...}, el value puede faltar del todo en campos vacíos)."""
    for f in fields or []:
        if f.get("name") == name:
            return f.get("value")
    return None


def _download_feed_csv(url, log, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return gzip.decompress(raw).decode("utf-8", errors="replace")


def _stream_feed_rows(url_or_path, log, timeout=180, is_local_file=False):
    """Generador que va dando filas de un feed CSV.GZ SIN materializar nunca el texto
    descomprimido entero en memoria de una vez (26 ago 2026, bug real: la Pi se quedaba sin
    memoria -- y el kernel mataba el proceso con SIGKILL -- procesando el feed más grande de
    Leroy Merlin, ~490k filas/~100MB de CSV descomprimido, aunque el top-N ya estuviera acotado
    con un heap; el propio texto entero + el DictReader sobre él ya era demasiado en una Pi con
    ~900MB de RAM). gzip.GzipFile + TextIOWrapper descomprimen y decodifican en streaming según
    csv.DictReader va pidiendo líneas, así que la huella real es solo el búfer de red/E-S, no
    el feed entero."""
    if is_local_file:
        with gzip.open(url_or_path, "rt", encoding="utf-8", errors="replace", newline="") as f:
            yield from csv.DictReader(f)
        return
    req = urllib.request.Request(url_or_path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with gzip.GzipFile(fileobj=resp) as gz:
            text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace", newline="")
            yield from csv.DictReader(text_stream)


def _fetch_leroy_merlin_feed(feed, log, local_test_file=None, cap=LEROY_MERLIN_MAX_PER_CYCLE):
    """Descarga y filtra UN feed concreto de Leroy Merlin (por su dict de LEROY_MERLIN_FEEDS).
    Compartido por fetch_leroy_merlin_offers() (rotación normal, 1 por ciclo),
    fetch_leroy_merlin_offers_all() (catch-up manual, los 7 de golpe) y
    fetch_leroy_merlin_extended() (catálogo ampliado para el buscador, 26 ago 2026, cap más
    alto). `cap=None` = sin tope, todos los candidatos que cualifiquen."""
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,saving,in_stock"
    )
    if local_test_file:
        reader = _stream_feed_rows(local_test_file, log, is_local_file=True)
    else:
        url = _awin_feed_url(AWIN_API_KEY, feed["fid"], columns)
        reader = _stream_feed_rows(url, log)

    # Top-N acotado con un min-heap en vez de "meterlo todo en una lista y luego ordenar y
    # recortar" (26 ago 2026, bug real: la Pi se quedaba sin memoria y el proceso entero moría
    # a mitad de generar el catálogo ampliado -- algunos feeds tienen 150-180k candidatos
    # cualificando, mantenerlos todos en memoria a la vez para acabar quedándose solo con 3000
    # es un desperdicio real en una Pi con ~900MB de RAM). Con cap puesto, la memoria queda
    # acotada a ~cap candidatos en todo momento, no al total de cualificantes del feed. El
    # contador `n` como segundo elemento de la tupla evita que heapq intente comparar
    # diccionarios cuando dos candidatos empatan en descuento (no son comparables entre sí).
    heap = []  # [(discount_percent, n, candidate), ...] -- min-heap por discount_percent
    n = 0
    total_qualifying = 0
    try:
        for row in reader:
            # in_stock puede no venir informado en este feed (visto vacío en la muestra real
            # del 24 ago) — solo se descarta si viene explícitamente a "0", nunca por ausencia
            # del dato.
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

            total_qualifying += 1
            category = _leroy_merlin_map_category(feed["category"], row.get("merchant_category"))
            candidate = {
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
            }
            n += 1
            if cap is None or len(heap) < cap:
                heapq.heappush(heap, (candidate["discount_percent"], n, candidate))
            elif candidate["discount_percent"] > heap[0][0]:
                heapq.heapreplace(heap, (candidate["discount_percent"], n, candidate))
    except Exception as e:
        log(f"[leroy_merlin] error descargando/procesando feed '{feed['name']}': {e}")
        if n == 0:
            return {}
        log(f"[leroy_merlin] se sigue con los {len(heap)} candidatos ya vistos antes del fallo")

    top = [c for _, _, c in sorted(heap, key=lambda t: t[0], reverse=True)]
    log(f"[leroy_merlin] feed '{feed['name']}' (fid {feed['fid']}) -> "
        f"{total_qualifying} candidatos 30-80%, {len(top)} publicados esta vez")
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


# Catálogo ampliado para el buscador (26 ago 2026, "por que no podimaos meter todo el catalogo
# de leroy y de comas en algún sitio... que el buscador lo pueda encontrar el producto"):
# comprobado con los 7 feeds reales sin tope -> 682.332 productos cualificando (30-80% dto.) en
# total, inviable como archivo único (decenas de MB, mal para datos móviles y para el historial
# de git si se reescribe cada día). Recorte pragmático: top N por descuento de CADA feed (no
# el catálogo 100% completo, pero ~40x más que el ~50/ciclo normal, y sigue siendo "lo más
# rebajado de verdad" de cada categoría, que es lo que de verdad importa para una búsqueda).
EXTENDED_CATALOG_LEROY_CAP_PER_FEED = 3000


def fetch_leroy_merlin_extended(log):
    """Catálogo ampliado (no la rotación normal de 1 feed/ciclo): los 7 feeds de golpe, top
    EXTENDED_CATALOG_LEROY_CAP_PER_FEED por feed. Se llama solo de vez en cuando (ver throttle
    en update_offers.py), nunca en cada ciclo normal -- descargar los 7 feeds completos tarda
    ~2 minutos, no es para cada pasada de 3 horas."""
    result = {}
    for feed in LEROY_MERLIN_FEEDS:
        result.update(_fetch_leroy_merlin_feed(feed, log, cap=EXTENDED_CATALOG_LEROY_CAP_PER_FEED))
    return result


# ---------------------------------------------------------------------------
# Stylevana (1 solo feed, sin rotación — 23.7k filas, ligero, se puede pedir entero cada ciclo)
# ---------------------------------------------------------------------------

# STYLEVANA_MAX_PER_CYCLE (50) retirado del todo el 8 sep 2026 -- mismo motivo que OBI/4
# Elementos/Perfumería Comas: "todas las tiendas tengan todo el catálogo", sin scraping de por
# medio no hay riesgo real. Comprobado: 7.841 candidatos reales, manejable.
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
    log(f"[stylevana] {len(candidates)} candidatos 30-80%, {len(candidates)} publicados esta vez")
    return {o["id"]: o for o in candidates}


# ---------------------------------------------------------------------------
# Zapatos OBI ES (31 ago 2026, aprobada en Awin) -- 1 solo feed (el mismo catálogo se ofrece
# repetido en 5 idiomas/países, fid 116697 es la versión España en español; los otros 4
# -Francia/Alemania/Italia/Portugal- se ignoran, mismo producto). Sin rotación, mismo patrón
# que Stylevana. Calzado multimarca (Skechers, Victoria, Birkenstock, New Balance, Ugg... y
# marca propia OBI SHOES) para mujer/hombre/niño -- el feed trae una fila por talla de cada
# modelo, así que la mayoría de filas están agotadas (comprobado 31 ago: 7833 candidatos
# 30-80% de descuento, solo 728 con in_stock=1). El título de producto ya incluye la marca de
# fábrica (p.ej. "Botines PRIMIGI PHLGT..."), a diferencia de Perfumería Comas, así que no
# hace falta anteponerla a mano.
# ---------------------------------------------------------------------------

# OBI_MAX_PER_CYCLE (subido de 50 a 150 el 1 sep 2026, "faltan ofertas") retirado del todo el
# 8 sep 2026 -- pedido explícito: "todas las tiendas [que no sean Amazon] tengan todo el
# catálogo" (sin scraping de por medio, el feed viene de Awin/Tradedoubler, no hay riesgo real
# de "que nos pillen" a diferencia de Amazon). Comprobado ese día: 665 candidatos 30-80% de
# descuento reales -- tamaño de sobra manejable, mismo criterio que ya se aplicaba sin pensarlo
# a Adidas/Foot Locker/etc. desde que se integraron. fetch_obi_offers() ya no recibe ningún
# tope por defecto (cap=None), ver más abajo.
OBI_FID = "116697"  # "España Feed" -- ver feedList, mismo catálogo que 116481/116483/116570/116696


def _obi_map_category(merchant_category):
    """merchant_category viene a veces como texto plano ('Mujer > Botas') y a veces como una
    lista en formato JSON-string ('["niña","outlet niña"] > Botines') -- basta con mirar si
    "niñ" aparece en el primer tramo (antes de '>') para detectar sección infantil, sin
    necesidad de parsear el JSON de verdad."""
    top = (merchant_category or "").split(">")[0].strip().lower()
    if "niñ" in top or "bebe" in top:
        return "Bebés"
    if "hombre" in top or top in ("compl. cab", "textil cab"):
        return "Moda Hombre"
    return "Moda Mujer"  # Mujer, Compl. sra, Textil sra, General... mayoría real del feed


def _obi_subcategory(merchant_category):
    """Tipo de calzado/complemento real (1 sep 2026, pedido explícito: "deberían estar mejor
    organizadas por categorías y subcategorías") -- el tramo después de '>' ('Mujer > Botas'
    -> 'Botas'), ya legible tal cual sin parsear el JSON del primer tramo. Sin '>' (fila
    'General' suelta) se deja vacío -- el frontend no crea subsección para eso."""
    parts = (merchant_category or "").split(">")
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def fetch_obi_offers(log, local_test_file=None, cap=None):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,rrp_price,in_stock,currency"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, OBI_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[obi] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if row.get("in_stock") != "1":
            continue
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue
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
            "id": f"ob_{pid}",
            "title": title[:180],
            "category": _obi_map_category(row.get("merchant_category")),
            "subcategory": _obi_subcategory(row.get("merchant_category")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "zapatosobi",
            "store_label": "Zapatos OBI",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[obi] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_obi_extended(log):
    """Catálogo ampliado (31 ago 2026): igual que Perfumería Comas, cabe entero sin recorte --
    solo ~728 productos cualificando con stock real de una comprobación real."""
    return fetch_obi_offers(log, cap=None)


# ---------------------------------------------------------------------------
# 4 Elementos (1 sep 2026, aprobada en Awin) -- 1 solo feed (fid 114028, "Google Sheet - CDO
# SOLUTIONS" pese al nombre, es un feed nativo de Awin con columnas normales, no formato
# Google), mismo patrón que Stylevana/OBI. Streetwear/sneakers multimarca (Carhartt W.I.P.,
# Nike, Jordan, New Balance, Adidas, ASICS...), sin distinción de sección por sexo en
# merchant_category (a diferencia de OBI) -- todo a "Moda Hombre" por defecto, mismo criterio
# que el catch-all de Stylevana. Comprobado 1 sep 2026: 30.463 filas, 14.735 candidatos
# 30-80% de descuento, 1.181 con stock real (in_stock=1).
# ---------------------------------------------------------------------------

# ELEMENTOS4_MAX_PER_CYCLE (subido de 50 a 150 el 1 sep 2026) retirado del todo el 8 sep 2026 --
# mismo motivo que OBI_MAX_PER_CYCLE más arriba: "todas las tiendas tengan todo el catálogo",
# sin scraping de por medio no hay riesgo real. Comprobado: 1.421 candidatos reales, manejable.
ELEMENTOS4_FID = "114028"


def fetch_4elementos_offers(log, local_test_file=None, cap=None):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,rrp_price,in_stock,currency"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, ELEMENTOS4_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[4elementos] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if row.get("in_stock") != "1":
            continue
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue
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
            "id": f"4e_{pid}",
            "title": title[:180],
            "category": "Moda Hombre",
            # Tipo real de producto (1 sep 2026, "categorías y subcategorías") -- aquí
            # merchant_category YA es el tipo directo ("Zapatillas", "Pantalones"...), sin
            # jerarquía "Sección > Tipo" como en OBI, así que se usa tal cual.
            "subcategory": (row.get("merchant_category") or "").strip(),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "4elementos",
            "store_label": "4 Elementos",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[4elementos] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_4elementos_extended(log):
    """Catálogo ampliado (1 sep 2026): a diferencia de OBI/Comas, aquí SÍ hace falta recortar
    -- 1.181 candidatos con stock real es manejable entero, se deja sin cap de todas formas
    igual que las otras tiendas de un solo feed."""
    return fetch_4elementos_offers(log, cap=None)


# ---------------------------------------------------------------------------
# Adidas (2 sep 2026, aprobada en Awin -- ya estaba "Adherido" de antes, sin que quedara
# registrado en este documento/repo) -- 1 solo feed de los dos que ofrece el anunciante (fid
# 92152 "Adidas ES - variant", 46.387 filas; el otro, fid 92566 "Adidas ES 2", NO trae precio
# de referencia, descartado). IMPORTANTE, quirk real del feed: pese al nombre del campo,
# `product_price_old` NO es el precio antiguo sino el precio de VENTA actual, y `search_price`
# es el precio de referencia/original -- comprobado en las 8.999 filas donde difieren, SIEMPRE
# search_price >= product_price_old, nunca al revés. Categoría fija a "Deporte" (marca
# deportiva, sin distinción de sección por sexo en merchant_category, igual que 4 Elementos)
# -- subcategoría real del segundo tramo separado por comas ("Calzado,Zapatillas" ->
# "Zapatillas"). Comprobado 2 sep 2026: 7.895 candidatos 30-80% de descuento, todos con stock.
# ---------------------------------------------------------------------------

# Sin tope real (2 sep 2026, pedido explícito: "con adidas sube casi todas las ofertas que
# tenga") -- a diferencia del resto de tiendas de un solo feed (150/ciclo), aquí el ciclo
# normal publica el catálogo cualificado ENTERO (~7.895 comprobado) para que no decaiga por
# poda de STALE_AFTER_DAYS entre ejecuciones -- el feed se descarga igual cada ciclo, así que
# no cuesta red extra (mismo razonamiento que ya usa Perfumería Comas para su tope).
ADIDAS_MAX_PER_CYCLE = None
ADIDAS_FID = "92152"


def _adidas_subcategory(merchant_category):
    parts = (merchant_category or "").split(",")
    return parts[1].strip() if len(parts) > 1 else ""


def fetch_adidas_offers(log, local_test_file=None, cap=ADIDAS_MAX_PER_CYCLE):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,product_price_old,in_stock,currency"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, ADIDAS_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[adidas] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if row.get("in_stock") != "1":
            continue
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue
        sp = row.get("search_price") or ""
        old = row.get("product_price_old") or ""
        if not sp.strip() or not old.strip():
            continue
        try:
            # Quirk real del feed: search_price = original, product_price_old = precio de
            # venta actual (al revés de lo que sugiere el nombre del campo).
            original = float(sp)
            actual = float(old)
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
            "id": f"ad_{pid}",
            "title": title[:180],
            "category": "Deporte",
            "subcategory": _adidas_subcategory(row.get("merchant_category")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "adidas",
            "store_label": "Adidas",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[adidas] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_adidas_extended(log):
    """Catálogo ampliado (2 sep 2026): 7.895 candidatos con stock real, manejable entero igual
    que las otras tiendas de un solo feed."""
    return fetch_adidas_offers(log, cap=None)


# ---------------------------------------------------------------------------
# Foot Locker (5 sep 2026, ya estaba "Adherido" en Awin de antes, sin integrar -- encontrada
# al repasar "Mis programas" a petición del usuario: "mirar en awin creo que tenemos alguna
# marca buena... hay que crear las tiendas"). Feed único (fid 78257, 51.344 filas, Moda).
# Igual que Foot Locker cuenta con feed de zapatillas, ropa y accesorios (marcas propias +
# Adidas/Nike/etc revendidas), sin la reversión rara de nombres de campo que tiene Adidas:
# aquí `product_price_old` SÍ es el precio ORIGINAL de verdad y `search_price` el de VENTA
# actual (comprobado 5 sep 2026: en las 9.058 filas donde difieren, siempre
# product_price_old > search_price, nunca al revés). `rrp_price` viene siempre vacío en este
# feed, no sirve. Categoría fija a "Deporte" (mismo criterio que Adidas/4 Elementos), con
# subcategoría traducida del último tramo de `merchant_category` (viene en inglés sin
# traducir, p.ej. "men>shoes>court" -> "court") vía _FOOTLOCKER_SUBCATEGORY_ES -- tramos sin
# traducción conocida caen en Title Case con los guiones cambiados a espacios, para no perder
# ningún producto por vocabulario nuevo que añada el feed más adelante. 5.595 candidatos
# 30-80% de descuento con stock, comprobado 5 sep 2026.
# ---------------------------------------------------------------------------

FOOTLOCKER_FID = "78257"

# Traducciones del último tramo de merchant_category (5 sep 2026, ver comentario de arriba) --
# solo los que de verdad aparecen en el feed real (comprobado con los 5.595 candidatos), el
# resto cae al fallback genérico de _footlocker_subcategory().
_FOOTLOCKER_SUBCATEGORY_ES = {
    "court": "Zapatillas Court",
    "running": "Running",
    "basketball": "Baloncesto",
    "shoes": "Calzado",
    "shorts": "Pantalones cortos",
    "shirts": "Camisetas",
    "pants": "Pantalones",
    "canvas-skate": "Zapatillas de skate",
    "tracksuits": "Chándales",
    "socks": "Calcetines",
    "hoodies": "Sudaderas con capucha",
    "casual": "Casual",
    "caps": "Gorras",
    "caps-hats": "Gorras",
    "slides-and-sandals": "Sandalias y chanclas",
    "track-tops": "Sudaderas",
    "sweatshirts": "Sudaderas",
    "infants": "Bebé",
    "jackets": "Chaquetas",
    "boots": "Botas",
    "swimwear": "Baño",
    "jerseys/replicas": "Camisetas de equipación",
    "grade-school": "Niños",
    "trucker": "Gorras trucker",
    "shoecare": "Cuidado del calzado",
    "accessories": "Accesorios",
    "gift-sets": "Sets de regalo",
    "knitted-hats-beanies": "Gorros",
    "insoles": "Plantillas",
    "sport-equipment": "Equipamiento deportivo",
}


def _footlocker_subcategory(merchant_category):
    last = (merchant_category or "").split(">")[-1].strip()
    if not last:
        return ""
    return _FOOTLOCKER_SUBCATEGORY_ES.get(last, last.replace("-", " ").title())


# Verificación de enlaces caducados (9 sep 2026, aviso real del usuario: "en foot locker me
# salen muchas veces ofertas que ya no están"). Causa raíz comprobada: el feed de Awin sigue
# marcando in_stock=1 productos que ya no existen de verdad en footlocker.es -- STALE_AFTER_DAYS
# (update_offers.py) solo retira lo que deja de APARECER EN EL FEED, no detecta que el feed
# mismo esté desactualizado. Dos formas distintas de estar "muerto", las dos con estado HTTP 200
# (nunca un 404 de verdad):
#   1. El redirect de Awin cae directo a la portada de footlocker.es en vez de a una ficha de
#      producto -- se detecta solo con la URL final tras seguir el redirect (barato, sin leer
#      el cuerpo: "/product/" ausente de la URL).
#   2. La URL final SIGUE siendo una ficha de producto (con "/product/"), pero la propia página
#      diagnostica el producto como inactivo -- comprobado con un caso real, "el 404 | Product
#      Not Found" que se ve en el navegador (JS) NO aparece así en el HTML plano, pero el motivo
#      real SÍ viene embebido en el estado inicial que trae el HTML servido por el propio
#      servidor: `"getDetails":{"status":"@api/FAILED",...,"statusCode":404,...}` (vivo:
#      `"@api/SUCCEEDED"`). Para esto hace falta el CUERPO entero de la página (~580 KB),
#      no solo la cabecera.
#
# Solo Foot Locker por ahora (pedido explícito: "empezamos solo por foot locker, no por todas
# las tiendas de golpe"). Con las ~5.400 candidatas de hoy, el caso 2 exige ~3 GB de descarga
# por ciclo -- aceptado con conocimiento de causa ("tengo bastante velocidad de red"), pero con
# cuidado de memoria: la Pi de verdad es un Raspberry Pi 3 con menos de 1 GB de RAM total. Cada
# hilo procesa y descarta su página al momento (nunca se guardan los ~580 KB en el resultado,
# solo el `bool` de si está viva) -- con max_workers=20 el pico real son unos 20 × 580 KB ≈
# 12 MB en vuelo, nada comparado con lo que ya maneja el propio scraper (el catálogo entero,
# ~27.000 ofertas, en memoria). Fallos de red (timeout, DNS...) NO cuentan como enlace muerto --
# se publican igual esta vez, por precaución, mismo criterio que _is_fresh() en update_offers.py
# ante una fecha ilegible.
_FOOTLOCKER_DEAD_MARKER = '"getDetails":{"status":"@api\\u002FFAILED"'


def _footlocker_link_alive(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if "/product/" not in resp.url:
                return False  # caso 1: redirigido a portada
            body = resp.read().decode("utf-8", errors="replace")
            return _FOOTLOCKER_DEAD_MARKER not in body  # caso 2: ficha marcada inactiva
    except Exception:
        return True


def _filter_dead_footlocker_links(candidates, log, max_workers=20):
    if not candidates:
        return candidates
    alive = []
    removed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_candidate = {
            executor.submit(_footlocker_link_alive, c["url"]): c for c in candidates
        }
        for future in concurrent.futures.as_completed(future_to_candidate):
            candidate = future_to_candidate[future]
            try:
                if future.result():
                    alive.append(candidate)
                else:
                    removed += 1
            except Exception:
                alive.append(candidate)
    if removed:
        log(f"[footlocker] {removed} enlaces caducados detectados y retirados (el feed los "
            f"seguía marcando in_stock, pero redirigen a la portada en vez de a un producto)")
    return alive


def fetch_footlocker_offers(log, local_test_file=None, cap=None):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_category,search_price,product_price_old,in_stock,currency"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, FOOTLOCKER_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[footlocker] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if row.get("in_stock") != "1":
            continue
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue
        sp = row.get("search_price") or ""
        old = row.get("product_price_old") or ""
        if not sp.strip() or not old.strip():
            continue
        try:
            actual = float(sp)
            original = float(old)
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
            "id": f"fl_{pid}",
            "title": title[:180],
            "category": "Deporte",
            "subcategory": _footlocker_subcategory(row.get("merchant_category")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "footlocker",
            "store_label": "Foot Locker",
        })

    before_link_check = len(candidates)
    candidates = _filter_dead_footlocker_links(candidates, log)
    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[footlocker] {before_link_check} candidatos 30-80% con stock, "
        f"{len(candidates)} con enlace vivo, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_footlocker_extended(log):
    """Catálogo ampliado (5 sep 2026): 5.595 candidatos con stock real, manejable entero igual
    que Adidas/4 Elementos/Comas."""
    return fetch_footlocker_offers(log, cap=None)


# ---------------------------------------------------------------------------
# Tradedoubler (5 sep 2026, primera vez que se integra esta red al catálogo -- hasta ahora
# solo Awin). Encontradas repasando "Mis programas" a petición del usuario: "mirar en awin...
# y luego en tradedoubler y las mejores marcas hay que crear las tiendas". De los 8 programas
# adheridos, TotMoble ya se descartó antes (ver RASPI_REBAJASDIARIAS.md, sin descuentos
# reales) y DC Shoes/L'Occitane se quedan fuera por lo mismo (sus feeds NO traen ningún precio
# de referencia, solo el precio actual). HP Store y Desigual SÍ traen precio original real.
# Bershka y Huawei son casos especiales, ver sus comentarios respectivos más abajo.
# ---------------------------------------------------------------------------

HPSTORE_FID = "38866"


def fetch_hpstore_offers(log, cap=None):
    """976 productos en total (cabe entero en 1 sola página, sin el límite de 1000 de
    Tradedoubler). Campo `old_price` = precio ORIGINAL real (a diferencia de Huawei/Bershka,
    aquí si hay diferencia real: 160 de 976 productos, comprobado 5 sep 2026). Categoría fija
    a "Tecnología", subcategoría del campo `producttype` (ya viene en español/directo:
    "Laptops", etc). 24 candidatos 30-80% de descuento con stock, comprobado 5 sep 2026."""
    try:
        products = _download_tradedoubler_products(HPSTORE_FID, log)
    except Exception as e:
        log(f"[hpstore] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        try:
            actual = float(offer["priceHistory"][0]["price"]["value"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        old = _td_field(fields, "old_price")
        try:
            original = float(old) if old else None
        except ValueError:
            original = None
        if not original or original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or _td_field(fields, "image400") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue

        candidates.append({
            "id": f"hp_{pid}",
            "title": title[:180],
            "category": "Tecnología",
            "subcategory": (_td_field(fields, "producttype") or "").strip(),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "hpstore",
            "store_label": "HP Store",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[hpstore] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


DESIGUAL_FID = "256429"

# Traducción de prenda (último tramo de categories[0].name, viene en inglés) -- solo las que
# aparecen de verdad en el feed (comprobado 5 sep 2026 sobre 1.000 productos, el máximo que
# permite paginar la API de Tradedoubler, ver _download_tradedoubler_products).
_DESIGUAL_SUBCATEGORY_ES = {
    "T-Shirt": "Camisetas",
    "Dress": "Vestidos",
    "Shirt": "Camisas",
    "Trousers": "Pantalones",
    "Coat": "Abrigos",
    "Denim Trousers": "Vaqueros",
    "Skirt": "Faldas",
    "Sweat": "Sudaderas",
    "Hosiery": "Medias",
    "Pullover": "Jerséis",
    "Polo": "Polos",
    "Bags": "Bolsos",
    "Backpack": "Mochilas",
    "Swimwear": "Baño",
    "Blazer": "Blazers",
}


def _desigual_category_subcategory(categories):
    """categories[0].name viene como "Man > Denim Trousers"/"Girl > T-Shirt" -- primer tramo
    decide Moda Hombre/Mujer (Boy cae en Hombre, Girl en Mujer, con "Niño "/"Niña " delante en
    la subcategoría para no perder ese matiz), segundo tramo es la prenda."""
    name = (categories or [{}])[0].get("name", "")
    parts = [p.strip() for p in name.split(">")]
    group = parts[0] if parts else ""
    garment = parts[1] if len(parts) > 1 else ""
    garment_es = _DESIGUAL_SUBCATEGORY_ES.get(garment, garment.replace("-", " ").title())
    if group == "Boy":
        return "Moda Hombre", f"Niño {garment_es}".strip()
    if group == "Girl":
        return "Moda Mujer", f"Niña {garment_es}".strip()
    if group == "Woman":
        return "Moda Mujer", garment_es
    return "Moda Hombre", garment_es


def fetch_desigual_offers(log, cap=None):
    """7.115 productos en el catálogo real, pero la API de Tradedoubler NO deja paginar más
    allá de 1.000 productos en total (ver _download_tradedoubler_products) -- límite
    permanente de la propia API, no algo que podamos evitar. Campo `Sale price` = precio de
    VENTA actual, el precio de `offers[0].priceHistory` es el ORIGINAL (al revés que Adidas:
    aquí el nombre del campo extra SÍ es el que está rebajado). 628 de los primeros 1.000
    candidatos 30-80% de descuento, comprobado 5 sep 2026 -- proporción altísima, se publican
    todos los que quepan (sin cap real necesario, igual que Adidas)."""
    try:
        products = _download_tradedoubler_products(DESIGUAL_FID, log)
    except Exception as e:
        log(f"[desigual] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        try:
            original = float(offer["priceHistory"][0]["price"]["value"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        sale = _td_field(fields, "Sale price")
        try:
            actual = float(sale) if sale else None
        except ValueError:
            actual = None
        if not actual or original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue
        category, subcategory = _desigual_category_subcategory(p.get("categories"))

        candidates.append({
            "id": f"dsg_{pid}",
            "title": title[:180],
            "category": category,
            "subcategory": subcategory,
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "desigual",
            "store_label": "Desigual",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[desigual] {len(candidates)} candidatos 30-80% con stock (de un máximo de 1.000 "
        f"que deja ver la API), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


BERSHKA_FID = "35429"


def fetch_bershka_offers(log, cap=None):
    """19.708 productos en el catálogo real, pero igual que Desigual la API de Tradedoubler
    solo deja ver los primeros 1.000 (ver _download_tradedoubler_products). A diferencia de
    Desigual/HP Store, aquí el feed NO trae ningún precio de referencia en ningún campo --
    solo el precio actual y una etiqueta `custom_label_1` "promo"/"no_promo" sin más detalle
    (comprobado 5 sep 2026). Sin precio "antes" no se puede calcular ni verificar ningún %, así
    que estas ofertas NO llevan discount_percent/original_price (quedan a 0, igual que una
    oferta gratis) -- en su lugar se marcan `is_promo=True` para que la web/app pinten una
    insignia "PROMO" en vez de un "-X%" que no podríamos demostrar (pedido explícito del
    usuario: "le ponemos como una tienda con promo"). Categoría por género (`gender`), sin
    Niño/Niña -- Bershka no distingue línea infantil en el feed. Subcategoría directa de
    `custom_label_0`, que ya viene en español ("Pantalones", "Camisetas"...). 97 de los
    primeros 1.000 candidatos vienen marcados "promo", comprobado 5 sep 2026."""
    try:
        products = _download_tradedoubler_products(BERSHKA_FID, log)
    except Exception as e:
        log(f"[bershka] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        if _td_field(fields, "custom_label_1") != "promo":
            continue
        try:
            price = float(offer["priceHistory"][0]["price"]["value"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if price < MIN_PRICE_EUR:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue
        gender = _td_field(fields, "gender")
        category = "Moda Mujer" if gender == "female" else "Moda Hombre"
        subcategory = (_td_field(fields, "custom_label_0") or "").strip().title()

        candidates.append({
            "id": f"bsk_{pid}",
            "title": title[:180],
            "category": category,
            "subcategory": subcategory,
            "price": round(price, 2),
            "original_price": 0,
            "discount_percent": 0,
            "is_promo": True,
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "bershka",
            "store_label": "Bershka",
        })

    top = candidates if cap is None else candidates[:cap]
    log(f"[bershka] {len(candidates)} en promo con stock (de un máximo de 1.000 que deja ver "
        f"la API), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


HUAWEI_FID = "39841"

# "CÓDIGO (descripción)" -> separa el código del texto explicativo (5 sep 2026, ver
# fetch_huawei_offers). Ejemplo real: "A8IDEALOALL (Código de descuento del 8% - válido hasta
# el 31/12/2026)".
_HUAWEI_VOUCHER_RE = re.compile(r"^\s*([A-Z0-9]+)\s*\((.+)\)\s*$")

# Último tramo de `ProductGroupinShop` (viene en inglés) -- solo los 3 que aparecen de verdad
# en el feed (123 productos, comprobado 5 sep 2026), el resto cae tal cual.
_HUAWEI_SUBCATEGORY_ES = {
    "Wireless Headphones": "Auriculares inalámbricos",
    "Smartwatches": "Relojes inteligentes",
    "Tablet Computers": "Tablets",
}


def fetch_huawei_offers(log, cap=None):
    """Solo 123 productos, cabe entero en 1 página. El feed NO trae precio de referencia
    tradicional (`DiscountPrice` es siempre igual al precio actual, comprobado 5 sep 2026) --
    pero SÍ trae códigos de descuento reales y verificables en el campo `voucher` (p.ej.
    "A8IDEALOALL (Código de descuento del 8% - válido hasta el 31/12/2026)") junto con
    `voucher_price`, el precio ya aplicado el código. El % real que dan estos códigos no llega
    nunca al 30% mínimo del resto del catálogo (máximo real ~23%, comprobado 5 sep 2026) --
    pedido explícito del usuario: entra de todas formas ("Huawei al tener cupones nos interesa
    también y son marcas internacionales importantes que nos pueden traer grandes
    comisiones"), con el código destacado en la oferta (coupon_code/coupon_label) en vez del
    filtro de descuento normal. original_price = precio ANTES del código, price = precio CON
    el código aplicado."""
    try:
        products = _download_tradedoubler_products(HUAWEI_FID, log)
    except Exception as e:
        log(f"[huawei] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        # Huawei trae el stock dentro de "fields" (nombre igual que en otras tiendas, pero
        # NO es un campo del offer aquí, a diferencia de HP Store/Desigual) -- comprobado 5
        # sep 2026: valores "In Stock"/"out of stock", con mayúsculas inconsistentes.
        stock = (_td_field(fields, "availability") or "").strip().lower()
        if stock != "in stock":
            continue
        voucher = _td_field(fields, "voucher")
        voucher_price = _td_field(fields, "voucher_price")
        if not voucher or not voucher_price:
            continue
        try:
            original = float(offer["priceHistory"][0]["price"]["value"])
            actual = float(voucher_price)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue

        m = _HUAWEI_VOUCHER_RE.match(voucher)
        coupon_code = m.group(1) if m else voucher.strip()
        coupon_label = m.group(2) if m else ""
        pct = (original - actual) / original * 100

        candidates.append({
            "id": f"hw_{pid}",
            "title": title[:180],
            "category": "Tecnología",
            "subcategory": _HUAWEI_SUBCATEGORY_ES.get(
                (_td_field(fields, "ProductGroupinShop") or "").split("&gt;")[-1].strip(),
                (_td_field(fields, "ProductGroupinShop") or "").split("&gt;")[-1].strip(),
            ),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "coupon_code": coupon_code,
            "coupon_label": coupon_label,
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "huawei",
            "store_label": "Huawei",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[huawei] {len(candidates)} con código de descuento real y stock, {len(top)} "
        f"publicados esta vez")
    return {o["id"]: o for o in top}


# ---------------------------------------------------------------------------
# Segunda ronda de Tradedoubler (8 sep 2026) -- de la tanda de grandes marcas solicitada el 7
# sep (ver RASPI_REBAJASDIARIAS.md), aceptadas de un día para otro: AEG, Electrolux y
# MediaMarkt SÍ traen precio de referencia real, comprobado sobre el feed entero/una muestra
# de 1.000 productos el 8 sep 2026. Tiendanimal (aceptada desde antes, nunca conectada) tiene
# precio de referencia real pero con un rendimiento mucho más bajo (los descuentos reales de
# su feed rara vez llegan al 30%, la mayoría rondan el 20%). DC Shoes y L'Occitane se
# revisaron otra vez y siguen sin traer ningún precio de referencia en ningún campo (mismo
# resultado que el 5 sep) -- se quedan fuera, igual que entonces. Braun, Rowenta, Tefal, WMF y
# Philips Home Appliances (también aceptadas en esta tanda) tampoco traen precio de
# referencia en ningún campo -- podrían entrar más adelante marcadas "PROMO" como Bershka si
# se decide que interesa, pero de momento se quedan fuera por no cumplir el criterio de
# descuento real verificable.
# ---------------------------------------------------------------------------

AEG_FID = "44055"

# Última parte de categories[0].name (viene en inglés, "Home & Garden > ... > X") -- solo las
# 22 que aparecen de verdad en el feed (240 productos, comprobado 8 sep 2026).
_AEG_SUBCATEGORY_ES = {
    "Vacuums": "Aspiradoras",
    "Dryers": "Secadoras",
    "Garment Steamers": "Centros de planchado",
    "Irons & Ironing Systems": "Planchas",
    "Laundry Combo Units": "Lavadoras-secadoras",
    "Washing Machines": "Lavadoras",
    "Coffee Makers & Espresso Machines": "Cafeteras",
    "Cooking Ranges": "Cocinas",
    "Deep Fryers": "Freidoras",
    "Dishwashers": "Lavavajillas",
    "Electric Kettles": "Hervidores",
    "Food Mixers & Blenders": "Batidoras",
    "Food Warmers": "Calientaplatos",
    "Freezers": "Congeladores",
    "Hobs": "Placas de cocina",
    "Microwave Ovens": "Microondas",
    "Ovens": "Hornos",
    "Range Hoods": "Campanas extractoras",
    "Refrigerators": "Frigoríficos",
    "Toasters": "Tostadoras",
    "Vacuum Sealers": "Envasadoras al vacío",
    "Household Appliances": "Otros electrodomésticos",
}


def _aeg_subcategory(categories):
    name = (categories or [{}])[0].get("name", "")
    last = name.split(">")[-1].strip()
    return _AEG_SUBCATEGORY_ES.get(last, last)


def fetch_aeg_offers(log, cap=None):
    """240 productos, cabe entero en 1 página. Campo `sale price` = precio de VENTA actual
    (vacío/sin `value` cuando el producto NO está rebajado, no se puede asumir que siempre
    tenga valor), `original price` = precio de referencia real. 178 de 240 candidatos 30-80%
    de descuento con stock, comprobado 8 sep 2026 -- proporción altísima, se publican todos
    los que quepan."""
    try:
        products = _download_tradedoubler_products(AEG_FID, log)
    except Exception as e:
        log(f"[aeg] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        sale = _td_field(fields, "sale price")
        original = _td_field(fields, "original price")
        try:
            actual = float((sale or "").split()[0]) if sale else None
            orig = float((original or "").split()[0]) if original else None
        except (ValueError, IndexError):
            continue
        if not actual or not orig or orig <= 0 or actual <= 0 or actual >= orig or actual < MIN_PRICE_EUR:
            continue
        pct = (orig - actual) / orig * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue

        candidates.append({
            "id": f"aeg_{pid}",
            "title": title[:180],
            "category": "Hogar y jardín",
            "subcategory": _aeg_subcategory(p.get("categories")),
            "price": round(actual, 2),
            "original_price": round(orig, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "aeg",
            "store_label": "AEG",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[aeg] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


ELECTROLUX_FID = "258481"


def fetch_electrolux_offers(log, cap=None):
    """Solo 55 productos, cabe entero en 1 página. A diferencia de AEG, aquí `categories` ya
    viene en español (2-3 niveles, se usa el más específico) -- algunos nombres traen
    "Tendencia" pegado sin espacio al final (p.ej. "HornosTendencia"), se recorta. Campos
    `raw_price` (original) / `raw_sale_price` (venta actual) traen el precio de referencia
    real. 35 de 55 candidatos 30-80% de descuento con stock, comprobado 8 sep 2026."""
    try:
        products = _download_tradedoubler_products(ELECTROLUX_FID, log)
    except Exception as e:
        log(f"[electrolux] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        orig = _td_field(fields, "raw_price")
        sale = _td_field(fields, "raw_sale_price")
        try:
            original = float(orig) if orig else None
            actual = float(sale) if sale else None
        except ValueError:
            continue
        if not original or not actual or original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue
        categories = p.get("categories") or [{}]
        subcategory = re.sub(r"Tendencia$", "", categories[-1].get("name", "")).strip()

        candidates.append({
            "id": f"elx_{pid}",
            "title": title[:180],
            "category": "Hogar y jardín",
            "subcategory": subcategory,
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "electrolux",
            "store_label": "Electrolux",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[electrolux] {len(candidates)} candidatos 30-80% con stock, {len(top)} publicados "
        f"esta vez")
    return {o["id"]: o for o in top}


MEDIAMARKT_FID = "24915"


def fetch_mediamarkt_offers(log, cap=None):
    """16.501 productos en el catálogo real, pero la API de Tradedoubler solo deja ver los
    primeros 1.000 (ver _download_tradedoubler_products). Campo `strike_price` = precio de
    referencia ORIGINAL, el precio de `offers[0].priceHistory` es el de VENTA actual (al
    revés que AEG/Electrolux, aquí el campo extra es el de ANTES). 489 de los primeros 1.000
    candidatos 30-80% de descuento con stock, comprobado 8 sep 2026 -- se cierra el bloqueo
    "PF_392 Requester is not connected to Feed" que había el 7 sep, resuelto solo de un día
    para otro."""
    try:
        products = _download_tradedoubler_products(MEDIAMARKT_FID, log)
    except Exception as e:
        log(f"[mediamarkt] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        try:
            actual = float(offer["priceHistory"][0]["price"]["value"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        strike = _td_field(fields, "strike_price")
        try:
            original = float(strike) if strike else None
        except ValueError:
            original = None
        if not original or original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue
        subcategory = (
            _td_field(fields, "category_third_level")
            or _td_field(fields, "category_second_level")
            or ""
        ).strip()

        candidates.append({
            "id": f"mm_{pid}",
            "title": title[:180],
            "category": "Tecnología",
            "subcategory": subcategory,
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "mediamarkt",
            "store_label": "MediaMarkt",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[mediamarkt] {len(candidates)} candidatos 30-80% con stock (de un máximo de 1.000 "
        f"que deja ver la API sobre 16.501 reales), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


TIENDANIMAL_FID = "50625"

# custom_label_0 viene en singular/plural mezclado y a veces con dos valores separados por
# ";" (se usa solo el primero) -- comprobado 8 sep 2026 sobre 1.000 productos.
_TIENDANIMAL_SUBCATEGORY_ES = {
    "Perro": "Perros",
    "Gato": "Gatos",
    "Aves": "Aves",
    "Peces": "Peces",
    "Reptiles": "Reptiles",
    "Pequeño Mamífero": "Pequeños mamíferos",
}


def fetch_tiendanimal_offers(log, cap=None):
    """15.454 productos en el catálogo real, pero la API de Tradedoubler solo deja ver los
    primeros 1.000. A diferencia del resto de tiendas de Tradedoubler, aquí `availability`
    viene como "in_stock" (con guion bajo, no "in stock"). Campo `sale_price` = precio de
    VENTA actual, `offers[0].priceHistory` = precio ORIGINAL. Rendimiento mucho más bajo que
    el resto: la mayoría de descuentos reales del feed rondan el 20%, por debajo del 30%
    mínimo -- solo 5 de 1.000 candidatos califican 30-80%, comprobado 8 sep 2026. Primera
    tienda de Mascotas del catálogo (categoría ya existente, usada hasta ahora solo por
    Amazon)."""
    try:
        products = _download_tradedoubler_products(TIENDANIMAL_FID, log)
    except Exception as e:
        log(f"[tiendanimal] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in_stock":
            continue
        try:
            original = float(offer["priceHistory"][0]["price"]["value"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        sale = _td_field(fields, "sale_price")
        try:
            actual = float((sale or "").split()[0]) if sale else None
        except (ValueError, IndexError):
            actual = None
        if not actual or original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue
        animal = (_td_field(fields, "custom_label_0") or "").split(";")[0].strip()
        subcategory = _TIENDANIMAL_SUBCATEGORY_ES.get(animal, animal)

        candidates.append({
            "id": f"tda_{pid}",
            "title": title[:180],
            "category": "Mascotas",
            "subcategory": subcategory,
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "tiendanimal",
            "store_label": "Tiendanimal",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[tiendanimal] {len(candidates)} candidatos 30-80% con stock (de un máximo de 1.000 "
        f"que deja ver la API sobre 15.454 reales), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


# ---------------------------------------------------------------------------
# Deporte Outlet ES (8 sep 2026, invitación de Awin aceptada -- ver RASPI_REBAJASDIARIAS.md).
# Feed único (fid 41767 "Standard Produktexport ES" -- hay un segundo fid 99907 "...neu" con
# columnas casi idénticas, se usa el primero por tener menos columnas raras). Comprobado 8 sep
# 2026: 7.835 productos, 6.296 con precio de venta < precio original real, 4.962 en el rango
# 30-80% -- todos con `stock_status` = "Envío inmediato" (único valor que aparece en las 7.835
# filas, no hace falta filtrar por él, el feed ya solo trae disponibles). Sin la reversión rara
# de nombres de campo que tiene Adidas: aquí `search_price` es el precio de VENTA actual y
# `product_price_old` el ORIGINAL, igual que Foot Locker/Tiendanimal (comprobado: 0 filas con
# search_price > product_price_old). Quirk real SÍ encontrado: product_price_old usa coma
# decimal ("139,95") mientras que search_price usa punto ("89.99") -- inconsistente dentro del
# mismo feed, de ahí el .replace(",", ".") antes de convertir a float. Categoría de producto
# (`merchant_product_category_path`, tramos separados por ">") viene ya en español y bien
# formada en el 46% de las filas ("Otros deportes>Baloncesto>Ropa de baloncesto") -- se usa el
# último tramo tal cual, sin diccionario de traducción (a diferencia de Foot Locker, que sí lo
# necesitaba por venir en inglés sin traducir).
# ---------------------------------------------------------------------------

DEPORTE_OUTLET_FID = "41767"


def _deporte_outlet_subcategory(category_path):
    return (category_path or "").split(">")[-1].strip()


def fetch_deporte_outlet_offers(log, local_test_file=None, cap=None):
    columns = (
        "aw_deep_link,product_name,aw_product_id,merchant_product_id,"
        "merchant_image_url,merchant_product_category_path,search_price,product_price_old,"
        "currency"
    )
    try:
        if local_test_file:
            with gzip.open(local_test_file, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = _awin_feed_url(AWIN_API_KEY, DEPORTE_OUTLET_FID, columns)
            text = _download_feed_csv(url, log)
    except Exception as e:
        log(f"[deporte_outlet] error descargando feed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(text))
    candidates = []
    for row in reader:
        if (row.get("currency") or "").strip().upper() not in ("", "EUR"):
            continue
        sp = row.get("search_price") or ""
        old = row.get("product_price_old") or ""
        if not sp.strip() or not old.strip():
            continue
        try:
            actual = float(sp)
            # Quirk real del feed: product_price_old usa coma decimal ("139,95"), a
            # diferencia de search_price que usa punto -- sin este .replace() float()
            # lanza ValueError en TODAS las filas.
            original = float(old.replace(",", "."))
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
            "id": f"do_{pid}",
            "title": title[:180],
            "category": "Deporte",
            "subcategory": _deporte_outlet_subcategory(
                row.get("merchant_product_category_path")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": "deporteoutlet",
            "store_label": "Deporte Outlet",
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[deporte_outlet] {len(candidates)} candidatos 30-80% con stock, "
        f"{len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_deporte_outlet_extended(log):
    """Catálogo ampliado (8 sep 2026): 4.962 candidatos con stock real, manejable entero igual
    que Adidas/Foot Locker."""
    return fetch_deporte_outlet_offers(log, cap=None)


# ---------------------------------------------------------------------------
# Balay y Bosch ES (9 sep 2026, aceptadas en Tradedoubler el 7-8 sep -- ver
# RASPI_REBAJASDIARIAS.md, bloqueadas unos días por PF_392 "Requester is not connected to
# Feed" hasta que la API terminó de propagar el alta). Mismo grupo BSH que AEG/Electrolux, pero
# con un esquema de precio DISTINTO a esas dos: el campo custom `previous_price` es el precio
# ORIGINAL (no el de venta) y el precio de venta actual vive en `offers[0].priceHistory[0].
# price.value` -- comprobado con 57/186 (Balay) y 76/318 (Bosch) productos con
# previous_price > priceHistory, siempre en esa dirección, nunca al revés. `categories[0].name`
# ya viene en español de fábrica (a diferencia de AEG, que lo trae en inglés sin traducir), se
# usa tal cual como subcategoría.
#
# Rendimiento honesto (comprobado 9 sep 2026): de esos productos con descuento real, solo 2
# (Balay) y 3 (Bosch) caen en el rango 30-80% del resto del catálogo -- la mayoría ronda un
# 10% de descuento, por debajo del mínimo. Mismo orden de magnitud que Electrolux (5
# candidatos) cuando se integró, así que se publican igual -- pocas ofertas reales, pero
# reales, no un motivo para descartar la tienda.
# ---------------------------------------------------------------------------

BALAY_FID = "88088"
BOSCH_FID = "44285"


def _bsh_subcategory(categories):
    """Última parte de categories[0].name -- a diferencia de AEG, Balay/Bosch ya lo traen en
    español de fábrica, no hace falta diccionario de traducción."""
    name = (categories or [{}])[0].get("name", "")
    return name.split(">")[-1].strip()


def _bsh_extract_real_url(tracking_url):
    """`offer.productUrl` del feed de Tradedoubler es un enlace de seguimiento
    (pdt.tradedoubler.com/click?...url(<destino real, URL-encoded>)) con una página intermedia
    de 2s (meta refresh) antes de llegar de verdad a balay.es/bosch-home.es -- SIEMPRE responde
    200 aunque el producto de debajo ya no exista, así que para comprobar si sigue vivo de
    verdad hay que mirar la página REAL, no la de seguimiento."""
    # El destino real va URL-encoded dentro del paréntesis ("url(https%3A%2F%2F...)"), no en
    # texto plano -- comprobado 11 sep 2026: un regex que buscaba "https?://" literal ahí
    # dentro nunca hacía match (0 de 2 candidatos reales), dejando pasar sin comprobar nada.
    m = re.search(r"url\(([^)]+)\)", tracking_url)
    return urllib.parse.unquote(m.group(1)) if m else None


def _bsh_product_alive(tracking_url, timeout=10):
    """Balay/Bosch (BSH) marcan el producto agotado/descatalogado con
    `"availability":"https://schema.org/OutOfStock"` en su propio HTML (ficha schema.org
    Product), aunque la página real siga respondiendo 200 -- comprobado 11 sep 2026, aviso
    real del usuario ("la campana y la lavadora ya no están disponibles") sobre las dos únicas
    ofertas que se habían publicado ese ciclo, las dos agotadas de verdad en balay.es. Mismo
    concepto que _footlocker_link_alive (feed marcándolo in stock cuando ya no lo está), pero
    aquí el marcador es un campo schema.org en vez de un estado embebido a medias -- y el
    volumen es tan bajo (2-5 candidatos tras el filtro 30-80%, no miles) que no hace falta
    paralelizar con hilos. Fallos de red NO cuentan como agotado (se publica igual esta vez,
    por precaución) -- solo cuenta el marcador explícito de OutOfStock leído con éxito."""
    real_url = _bsh_extract_real_url(tracking_url)
    if not real_url:
        return True
    try:
        req = urllib.request.Request(real_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return '"availability":"https://schema.org/OutOfStock"' not in body
    except Exception:
        return True


def _fetch_bsh_offers(fid, store_key, store_label, log, cap=None):
    """Compartida por Balay y Bosch -- mismo feed BSH, mismo esquema de precio (ver comentario
    de la sección de arriba), solo cambian fid/store."""
    try:
        products = _download_tradedoubler_products(fid, log)
    except Exception as e:
        log(f"[{store_key}] error descargando feed: {e}")
        return {}

    candidates = []
    for p in products:
        fields = p.get("fields") or []
        offers = p.get("offers") or []
        if not offers:
            continue
        offer = offers[0]
        if offer.get("availability") != "in stock":
            continue
        price_history = offer.get("priceHistory") or []
        if not price_history:
            continue
        prev = _td_field(fields, "previous_price")
        if not prev:
            continue
        try:
            original = float(prev)
            actual = float(price_history[0]["price"]["value"])
        except (ValueError, KeyError, TypeError):
            continue
        if original <= 0 or actual <= 0 or actual >= original or actual < MIN_PRICE_EUR:
            continue
        pct = (original - actual) / original * 100
        if pct < MIN_DISCOUNT_PERCENT or pct > MAX_DISCOUNT_PERCENT:
            continue

        title = (p.get("name") or "").strip()
        pid = offer.get("sourceProductId") or ""
        image = (p.get("productImage") or {}).get("url") or ""
        aff_url = offer.get("productUrl") or ""
        if not title or not pid or not aff_url:
            continue

        candidates.append({
            "id": f"{store_key}_{pid}",
            "title": title[:180],
            "category": "Hogar y jardín",
            "subcategory": _bsh_subcategory(p.get("categories")),
            "price": round(actual, 2),
            "original_price": round(original, 2),
            "discount_percent": int(round(pct)),
            "is_flash": False,
            "image": image,
            "url": aff_url,
            "store": store_key,
            "store_label": store_label,
        })

    alive_candidates = []
    dead = 0
    for c in candidates:
        if _bsh_product_alive(c["url"]):
            alive_candidates.append(c)
        else:
            dead += 1
    if dead:
        log(f"[{store_key}] {dead} producto(s) agotado(s)/descatalogado(s) de verdad "
            f"retirados (el feed los seguía marcando in stock)")
    candidates = alive_candidates

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates if cap is None else candidates[:cap]
    log(f"[{store_key}] {len(candidates)} candidatos 30-80% con stock real verificado, "
        f"{len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_balay_offers(log, cap=None):
    return _fetch_bsh_offers(BALAY_FID, "balay", "Balay", log, cap=cap)


def fetch_bosch_offers(log, cap=None):
    return _fetch_bsh_offers(BOSCH_FID, "bosch", "Bosch", log, cap=cap)


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

# PERFUMERIA_COMAS_MAX_PER_CYCLE (150) retirado del todo el 8 sep 2026 -- mismo motivo que
# OBI/4 Elementos más arriba: "todas las tiendas tengan todo el catálogo", sin scraping de por
# medio no hay riesgo real de "que nos pillen" (a diferencia de Amazon). Comprobado: 3.695
# candidatos reales, manejable igual que el resto.
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


def fetch_perfumeria_comas_offers(log, local_test_file=None, cap=None):
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
    top = candidates if cap is None else candidates[:cap]
    log(f"[perfumeria_comas] {len(candidates)} candidatos 30-80% (tras filtro de marca en "
        f"Perfumes), {len(top)} publicados esta vez")
    return {o["id"]: o for o in top}


def fetch_perfumeria_comas_extended(log):
    """Catálogo ampliado (26 ago 2026): a diferencia de Leroy Merlin, aquí SÍ cabe el catálogo
    entero sin recorte -- ~4.359 productos cualificando de una comprobación real, unos
    1-1.5 MB en JSON, nada que ver con el volumen de Leroy."""
    return fetch_perfumeria_comas_offers(log, cap=None)


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
        ("obi", fetch_obi_offers, "obi"),
        ("4elementos", fetch_4elementos_offers, "4elementos"),
        ("adidas", fetch_adidas_offers, "adidas"),
        ("footlocker", fetch_footlocker_offers, "footlocker"),
        ("hpstore", fetch_hpstore_offers, "hpstore"),
        ("desigual", fetch_desigual_offers, "desigual"),
        ("bershka", fetch_bershka_offers, "bershka"),
        ("huawei", fetch_huawei_offers, "huawei"),
        ("aeg", fetch_aeg_offers, "aeg"),
        ("electrolux", fetch_electrolux_offers, "electrolux"),
        ("mediamarkt", fetch_mediamarkt_offers, "mediamarkt"),
        ("tiendanimal", fetch_tiendanimal_offers, "tiendanimal"),
        ("deporte_outlet", fetch_deporte_outlet_offers, "deporteoutlet"),
        ("balay", fetch_balay_offers, "balay"),
        ("bosch", fetch_bosch_offers, "bosch"),
        ("perfumeria_comas", fetch_perfumeria_comas_offers, "perfumeriacomas"),
    ]
    for name, fetch_fn, key in stores:
        try:
            result.update(fetch_fn(log, local_test_files.get(key)))
        except Exception as e:
            log(f"[{name}] aviso: fallo inesperado, se omite esta tienda este ciclo "
                f"(las demás no se ven afectadas): {e!r}")
    return result


def generate_extended_catalog(log):
    """Catálogo ampliado para el buscador de la web/app (26 ago 2026, ver
    RASPI_REBAJASDIARIAS.md §8 punto 17): Leroy Merlin (top 3.000/feed) + Perfumería Comas
    (catálogo entero) + Zapatos OBI (31 ago 2026, catálogo entero, ver fetch_obi_extended) +
    4 Elementos (1 sep 2026, catálogo entero, ver fetch_4elementos_extended). NO se mezcla con
    offers.json -- es un archivo aparte (catalog_extended.json) que la web/app solo piden
    cuando una búsqueda no encuentra nada en el catálogo curado normal. Aislado por tienda
    igual que fetch_multitienda_offers(): un fallo en una no debe tirar la otra."""
    result = {}
    for name, fetch_fn in [
        ("leroy_merlin_extended", fetch_leroy_merlin_extended),
        ("perfumeria_comas_extended", fetch_perfumeria_comas_extended),
        ("obi_extended", fetch_obi_extended),
        ("4elementos_extended", fetch_4elementos_extended),
        ("adidas_extended", fetch_adidas_extended),
        ("footlocker_extended", fetch_footlocker_extended),
        ("hpstore_extended", fetch_hpstore_offers),
        ("desigual_extended", fetch_desigual_offers),
        ("bershka_extended", fetch_bershka_offers),
        ("huawei_extended", fetch_huawei_offers),
        ("aeg_extended", fetch_aeg_offers),
        ("electrolux_extended", fetch_electrolux_offers),
        ("mediamarkt_extended", fetch_mediamarkt_offers),
        ("tiendanimal_extended", fetch_tiendanimal_offers),
        ("deporte_outlet_extended", fetch_deporte_outlet_extended),
        ("balay_extended", fetch_balay_offers),
        ("bosch_extended", fetch_bosch_offers),
    ]:
        try:
            result.update(fetch_fn(log))
        except Exception as e:
            log(f"[{name}] aviso: fallo inesperado generando el catálogo ampliado, se omite "
                f"esta tienda esta vez: {e!r}")
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
