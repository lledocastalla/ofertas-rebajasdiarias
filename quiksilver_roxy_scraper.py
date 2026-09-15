#!/usr/bin/env python3
"""
quiksilver_roxy_scraper.py — Quiksilver ES y Roxy ES (15 sep 2026, pedido explícito del usuario:
"vamos con las tiendas, alguna forma tendremos de meterlas en el app ya que son marcas
importantes").

Por qué esto es un scraper aparte y no una tienda más de multitienda_feeds.py: las dos están
aceptadas en Tradedoubler (fid 42467/43218) y ya se probó su feed de productos vía API real --
pero, a diferencia de Desigual/HP Store/Bershka, **ninguno de los 2000 productos del feed trae
precio de referencia** (`previousPrice` vacío, `priceHistory` de una sola entrada) -- sin un
"antes" real, no se puede calcular ningún descuento honesto desde ahí (mismo motivo por el que
se descartó Sparkle GmbH/Hey Happiness el 11 sep). En cambio, sus propias webs (quiksilver.es/
rebajas/, roxy.es/rebajas/) SÍ muestran precio tachado + precio actual + "-X%" real en sus
páginas de rebajas -- así que aquí se escrapea la web directamente (Selenium, mismo motor que
Amazon en update_offers.py), no el feed.

El enlace de afiliado tampoco sale del feed (su SKU con formato barcode largo, ej.
"3616751624821", no coincide con el ID de producto que usa la web, ej. "ERJX103798-KVJ0" -- son
sistemas de identificación distintos, comprobado el 15 sep 2026) -- se construye con el
"Generador de deeplinks" de Tradedoubler, que sí acepta cualquier URL de destino:
    https://clk.tradedoubler.com/click?p=<program_id>&a=<site_id>&url=<URL codificada>
Formato confirmado a mano en el panel real (Herramientas > Generador de deeplinks) para el
programa QUIKSILVER ES (319666) -- funciona igual para cualquier programa/tienda de Tradedoubler,
no algo específico de esta marca.
"""

import re
import time
import urllib.parse

# 15 sep 2026, fallo real encontrado en producción: "import update_offers as uo" aquí arriba
# (import circular a nivel de módulo, ya que update_offers.py importa esta función) funcionaba
# en una prueba local (`python3 -c "import update_offers"`) pero rompía en la Pi de verdad --
# cuando update_offers.py se ejecuta como script principal (`python3 update_offers.py`, como
# hace el cron), Python lo registra como "__main__", NO como "update_offers". Al llegar aquí a
# "import update_offers as uo", Python no lo encuentra ya cargado bajo ese nombre y vuelve a
# ejecutar el archivo entero desde cero como un módulo aparte -- que a su vez intenta importar
# esta función de este mismo archivo, todavía a medio cargar (ni siquiera ha llegado a
# definirla) -> ImportError real, confirmado en el log de la Pi. Arreglado con import diferido
# (dentro de las funciones, no aquí arriba) -- para cuando de verdad se llama a fetch_
# quiksilver_roxy_offers(), update_offers.py ya ha terminado de cargar del todo, sea cual sea
# el nombre bajo el que esté registrado.

MIN_DISCOUNT_PERCENT = 30  # mismo umbral real que el resto del catálogo (ver multitienda_feeds.py)
MIN_PRICE_EUR = 3.0

TRADEDOUBLER_SITE_ID = "3496538"  # "Rebajas Diarias", mismo site usado en toda la cuenta

# Cada entrada es una sección de rebajas de una tienda+género -- separadas a propósito (en vez
# de rascar solo /rebajas/) para poder asignar la categoría real sin adivinar por el título.
# Niño/Niña quedan fuera a propósito: no existe una categoría "Moda Niños" en el catálogo hoy
# (ver PRIORITY_CATEGORIES en update_offers.py, solo Moda Hombre/Moda Mujer) -- añadir una
# categoría nueva para esto es una decisión aparte, no algo que colar de rebote aquí.
_SALE_SECTIONS = [
    {"store": "quiksilver", "label": "Quiksilver", "program_id": "319666",
     "url": "https://www.quiksilver.es/rebajas-hombre/", "category": "Moda Hombre"},
    {"store": "quiksilver", "label": "Quiksilver", "program_id": "319666",
     "url": "https://www.quiksilver.es/rebajas-mujer/", "category": "Moda Mujer"},
    {"store": "roxy", "label": "Roxy", "program_id": "319668",
     "url": "https://www.roxy.es/rebajas-mujer/", "category": "Moda Mujer"},
]

MAX_SCROLLS = 6  # cada scroll carga más tarjetas (scroll infinito, sin paginación real) --
# capado para no tirar de una Pi de 905MB de más ni tardar de más; con 6 suele haber de sobra
# para encontrar los productos con descuento real de verdad (la mayoría del catálogo NO está
# rebajado ni con el 30% mínimo que exige el resto de la app).
SCROLL_PAUSE_SECONDS = 1.5
MAX_PER_SECTION = 40  # tope de publicados por sección, igual de espíritu que el `cap` del
# resto de tiendas de multitienda_feeds.py -- no hace falta publicar los 900 de golpe.

_TILE_JS = """
return Array.from(document.querySelectorAll('div.product.producttile')).map(tile => {
    const link = tile.querySelector('a.js-target-plink');
    const img = tile.querySelector('img');
    const nameEl = tile.querySelector('[class*="name"], [class*="title"]');
    const salesEl = tile.querySelector('.salesprice');
    const standardEl = tile.querySelector('.standardprice');
    return {
        url: link ? link.href : null,
        name: nameEl ? nameEl.textContent.trim().replace(/\\s+/g, ' ') : null,
        image: img ? img.getAttribute('src') : null,
        salesText: salesEl ? salesEl.textContent.trim() : null,
        standardText: standardEl ? standardEl.textContent.trim() : null,
    };
});
"""


def log(msg):
    print(f"[quiksilver_roxy] {msg}", flush=True)


def _build_affiliate_url(program_id, real_url):
    encoded = urllib.parse.quote(real_url, safe="")
    return f"https://clk.tradedoubler.com/click?p={program_id}&a={TRADEDOUBLER_SITE_ID}&url={encoded}"


def _sku_from_url(real_url):
    """Último tramo antes de ".html", ej. ".../EQYJV04217-KVJ7.html" -> "EQYJV04217-KVJ7"."""
    tail = real_url.rstrip("/").split("/")[-1]
    return re.sub(r"\.html?$", "", tail)


def _scrape_section(driver, section):
    import update_offers as uo  # ver nota de import diferido más arriba

    driver.get(section["url"])
    time.sleep(2)
    for _ in range(MAX_SCROLLS):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE_SECONDS)

    try:
        tiles = driver.execute_script(_TILE_JS)
    except Exception as e:
        log(f"error leyendo tarjetas de {section['url']}: {e}")
        return []

    candidates = []
    seen_skus = set()
    for t in tiles:
        real_url = t.get("url")
        name = t.get("name")
        if not real_url or not name:
            continue
        sku = _sku_from_url(real_url)
        if not sku or sku in seen_skus:
            continue
        seen_skus.add(sku)

        price = uo.parse_price(t.get("salesText"))
        original = uo.parse_price(t.get("standardText"))
        # Sin precio tachado -> no hay descuento que demostrar, se descarta (mismo criterio que
        # el resto del catálogo: nada de "ofertas" sin un antes/después real).
        if price is None or original is None or original <= price:
            continue
        if price < MIN_PRICE_EUR:
            continue
        discount = round((1 - price / original) * 100)
        if discount < MIN_DISCOUNT_PERCENT:
            continue

        candidates.append({
            "id": f"{section['store']}_{sku}",
            "title": name[:180],
            "category": section["category"],
            "price": round(price, 2),
            "original_price": round(original, 2),
            "discount_percent": discount,
            "is_flash": False,
            "image": t.get("image") or "",
            "url": _build_affiliate_url(section["program_id"], real_url),
            "store": section["store"],
            "store_label": section["label"],
        })

    # Los descuentos más grandes primero -- si hay más candidatos que MAX_PER_SECTION, que se
    # queden los mejores, no los primeros que haya cargado el scroll (orden de la web = "más
    # nuevo primero" normalmente, sin relación con cuánto descuento real llevan).
    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:MAX_PER_SECTION]
    log(f"{section['label']} ({section['category']}): {len(candidates)} con {MIN_DISCOUNT_PERCENT}%+ "
        f"de descuento real de {len(tiles)} tarjetas cargadas, {len(top)} publicados esta vez")
    return top


def fetch_quiksilver_roxy_offers(log_fn=None):
    """Punto de entrada único, mismo patrón que fetch_multitienda_offers() -- un fallo en una
    sección nunca debe tumbar las demás ni el ciclo entero de update_offers.py."""
    import update_offers as uo  # ver nota de import diferido más arriba

    _log = log_fn or log
    driver = None
    result = {}
    try:
        driver = uo.build_driver()
        for section in _SALE_SECTIONS:
            try:
                offers = _scrape_section(driver, section)
                for o in offers:
                    result[o["id"]] = o
            except Exception as e:
                _log(f"aviso: fallo en {section['label']} ({section['category']}), se omite "
                     f"esta sección: {e}")
    except Exception as e:
        _log(f"aviso: fallo abriendo Chrome, se omite Quiksilver/Roxy este ciclo: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return result


if __name__ == "__main__":
    offers = fetch_quiksilver_roxy_offers()
    print(f"\nTotal: {len(offers)} ofertas")
    for o in list(offers.values())[:5]:
        print(f"  {o['store_label']} | {o['title'][:50]} | {o['price']}€ (antes {o['original_price']}€, "
              f"-{o['discount_percent']}%)")
