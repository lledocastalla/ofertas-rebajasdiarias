#!/usr/bin/env python3
"""
groupeseb_toysrus_scraper.py — Rowenta ES, Tefal ES y ToysRus ES (20 sep 2026, pedido explícito
del usuario tras ver que ninguna llega al 30% mínimo del resto del catálogo: "bajar el mínimo
solo para estas 3" -- son marcas reconocidas, mejor tenerlas con su descuento real aunque sea
menor que perderlas del todo).

Por qué un scraper aparte de quiksilver_roxy_scraper.py: aunque el mecanismo de fondo es el
mismo (feed de Tradedoubler aceptado pero sin precio de referencia -> se escrapea la web de
rebajas directamente, Selenium), la plantilla web es DISTINTA en los dos casos:
- Rowenta/Tefal (mismo grupo Groupe SEB, misma plantilla Angular): el precio no está en un
  <a href> normal -- el "click" de producto lo gestiona un router de Angular en JS, no hay URL
  real en el DOM hasta que se navega. Comprobado el 20 sep 2026: SÍ hay un bloque
  <script type="application/ld+json"> con @type ItemList que trae la URL real de cada producto
  en el MISMO ORDEN en que aparecen las tarjetas con descuento en el DOM -- se cruza por índice
  (nunca por nombre/id, no hay uno común entre las dos fuentes). Sin imagen fiable por producto
  (la estructura de componentes web anida las tarjetas de forma que no hay un contenedor propio
  y aislado por producto al que agarrarse -- probado varias formas, todas devuelven la imagen
  del primer producto de la página para todos). Se publica sin imagen (fallback ya existe en
  las tarjetas de oferta, `Icon(Icons.image_not_supported)`).
- ToysRus (SAP Commerce/Hybris, "yCmsComponent" en las clases): al contrario que Rowenta/Tefal,
  aquí SÍ hay HTML de servidor normal -- el <a> del producto y la <img> viven dentro del mismo
  contenedor .product-item que el precio, encontrados con querySelector normal (no hacía falta
  ningún truco de índice). **DESACTIVADA de todas formas** (ver _scrape_toysrus() y
  fetch_groupeseb_toysrus_offers() más abajo): su web está detrás de Cloudflare, que bloquea el
  Chrome automatizado con una pantalla de verificación ("Un momento…") -- confirmado en la Pi 5
  el 20 sep 2026. El código se deja escrito (funciona bien cuando SÍ carga, probado a mano en un
  navegador real) por si algún día cambia su protección, pero no se llama en producción.

MIN_DISCOUNT_PERCENT más bajo que el resto del catálogo (30%) a propósito -- ver arriba. El
resto de reglas de calidad (precio mínimo, un precio "antes" real y mayor que el actual) se
mantienen igual que en cualquier otra tienda.
"""

import html
import re
import time
import urllib.parse

MIN_DISCOUNT_PERCENT = 15  # más bajo que el estándar (30%) a propósito, pedido explícito -- ver
# docstring de arriba. Sigue habiendo un mínimo: descarta el ruido real que se vio en Rowenta
# (descuentos de 1-4% mezclados con los genuinos de hasta 30%).
MIN_PRICE_EUR = 3.0
MAX_PER_SECTION = 30

TRADEDOUBLER_SITE_ID = "3496538"  # "Rebajas Diarias", mismo site usado en toda la cuenta

_GROUPESEB_SECTIONS = [
    {"store": "rowenta", "label": "Rowenta", "program_id": "331320",
     "url": "https://www.rowenta.es/Rebajas/c/ofertas", "category": "Hogar"},
    {"store": "tefal", "label": "Tefal", "program_id": "331322",
     "url": "https://www.tefal.es/Rebajas/c/rebajas", "category": "Hogar"},
]

TOYSRUS_SECTION = {
    "store": "toysrus", "label": "Toys\"R\"Us", "program_id": "211811",
    "url": "https://www.toysrus.es/Promociones/c/Promociones", "category": "Juguetes",
}


def log(msg):
    print(f"[groupeseb_toysrus] {msg}", flush=True)


def _build_affiliate_url(program_id, real_url):
    encoded = urllib.parse.quote(real_url, safe="")
    return f"https://clk.tradedoubler.com/click?p={program_id}&a={TRADEDOUBLER_SITE_ID}&url={encoded}"


def _sku_from_url(real_url):
    tail = real_url.rstrip("/").split("/")[-1]
    return re.sub(r"\.html?$", "", tail)


# ver docstring del módulo -- ItemList (JSON-LD) trae la URL real en el mismo orden que las
# tarjetas .c__discounted-price del DOM, se cruzan por índice.
_GROUPESEB_JS = """
function parsePrice(s){
  const m = s.replace(/\\./g,'').match(/([\\d,]+)\\s*€/g);
  if(!m) return [];
  return m.map(x => parseFloat(x.replace(',', '.').replace('€','').trim()));
}
const ldScripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
let itemList = [];
for (const s of ldScripts) {
  try {
    const parsed = JSON.parse(s.textContent);
    if (parsed && parsed['@type'] === 'ItemList') { itemList = parsed.itemListElement; break; }
  } catch (e) {}
}
const priceNodes = Array.from(document.querySelectorAll('.c__discounted-price'));
return priceNodes.map((n, i) => {
  const nums = parsePrice(n.textContent);
  const item = itemList[i];
  if (nums.length < 2 || !item || !item.url) return null;
  return {reduced: nums[0], initial: nums[1], relUrl: item.url, name: item.name || ''};
}).filter(x => x !== null);
"""


def _scrape_groupeseb_section(driver, section):
    import update_offers as uo  # import diferido, mismo motivo que quiksilver_roxy_scraper.py

    driver.get(section["url"])
    time.sleep(3)

    try:
        rows = driver.execute_script(_GROUPESEB_JS)
    except Exception as e:
        log(f"error leyendo tarjetas de {section['url']}: {e}")
        return []

    from urllib.parse import urlsplit
    root = f"{urlsplit(section['url']).scheme}://{urlsplit(section['url']).netloc}"

    candidates = []
    seen_skus = set()
    for row in rows:
        reduced, initial = row["reduced"], row["initial"]
        if not initial or initial <= reduced:
            continue
        if reduced < MIN_PRICE_EUR:
            continue
        discount = round((1 - reduced / initial) * 100)
        if discount < MIN_DISCOUNT_PERCENT:
            continue
        real_url = root + row["relUrl"]
        sku = _sku_from_url(real_url)
        if not sku or sku in seen_skus:
            continue
        seen_skus.add(sku)
        name = re.sub(r"\s+", " ", html.unescape(row["name"])).strip()
        if not name:
            continue

        candidates.append({
            "id": f"{section['store']}_{sku}",
            "title": name[:180],
            "category": section["category"],
            "price": round(reduced, 2),
            "original_price": round(initial, 2),
            "discount_percent": discount,
            "is_flash": False,
            "image": "",  # ver docstring del módulo -- sin imagen fiable en esta plantilla
            "url": _build_affiliate_url(section["program_id"], real_url),
            "store": section["store"],
            "store_label": section["label"],
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:MAX_PER_SECTION]
    log(f"{section['label']}: {len(candidates)} con {MIN_DISCOUNT_PERCENT}%+ de descuento real, "
        f"{len(top)} publicados esta vez")
    return top


_TOYSRUS_JS = """
function parsePrice(s){
  const m = s.replace(/\\./g,'').match(/([\\d,]+)\\s*€/g);
  if(!m) return [];
  return m.map(x => parseFloat(x.replace(',', '.').replace('€','').trim()));
}
const strikeNodes = Array.from(document.querySelectorAll('.strike-price'));
return strikeNodes.map(n => {
  const card = n.closest('.product-item');
  if (!card) return null;
  const priceDiv = n.closest('.price');
  const nums = priceDiv ? parsePrice(priceDiv.textContent) : [];
  const a = card.querySelector('a[href]');
  const img = card.querySelector('img');
  const nameEl = card.querySelector('[class*="title"], [class*="name"]') || a;
  if (nums.length < 2 || !a) return null;
  return {
    current: nums[0], original: nums[1],
    url: a.href,
    image: img ? (img.src || img.getAttribute('data-src') || '') : '',
    name: nameEl ? nameEl.textContent.trim().replace(/\\s+/g, ' ') : '',
  };
}).filter(x => x !== null);
"""


def _scrape_toysrus(driver):
    import update_offers as uo  # import diferido, mismo motivo que quiksilver_roxy_scraper.py

    section = TOYSRUS_SECTION
    # 20 sep 2026, inestabilidad real comprobada a mano: la primera carga a veces rebota a la
    # portada en vez de la página de promociones (probablemente algo del lado de Cloudflare) --
    # un segundo intento con más espera lo resuelve siempre en las pruebas hechas. Si el primer
    # intento no encuentra ninguna tarjeta, se reintenta una vez antes de rendirse.
    rows = []
    for attempt in range(2):
        driver.get(section["url"])
        time.sleep(3 if attempt == 0 else 5)
        try:
            rows = driver.execute_script(_TOYSRUS_JS)
        except Exception as e:
            log(f"error leyendo tarjetas de ToysRus (intento {attempt + 1}): {e}")
            rows = []
        if rows:
            break

    candidates = []
    seen_skus = set()
    for row in rows:
        current, original = row["current"], row["original"]
        if not original or original <= current:
            continue
        if current < MIN_PRICE_EUR:
            continue
        discount = round((1 - current / original) * 100)
        if discount < MIN_DISCOUNT_PERCENT:
            continue
        real_url = row["url"]
        sku = _sku_from_url(real_url)
        if not sku or sku in seen_skus or not row["name"]:
            continue
        seen_skus.add(sku)

        candidates.append({
            "id": f"toysrus_{sku}",
            "title": row["name"][:180],
            "category": section["category"],
            "price": round(current, 2),
            "original_price": round(original, 2),
            "discount_percent": discount,
            "is_flash": False,
            "image": row["image"],
            "url": _build_affiliate_url(section["program_id"], real_url),
            "store": section["store"],
            "store_label": section["label"],
        })

    candidates.sort(key=lambda o: o["discount_percent"], reverse=True)
    top = candidates[:MAX_PER_SECTION]
    log(f"ToysRus: {len(candidates)} con {MIN_DISCOUNT_PERCENT}%+ de descuento real, "
        f"{len(top)} publicados esta vez")
    return top


def fetch_groupeseb_toysrus_offers(log_fn=None):
    """Punto de entrada único, mismo patrón que fetch_quiksilver_roxy_offers() -- un fallo en
    una sección nunca debe tumbar las demás ni el ciclo entero de update_offers.py."""
    import update_offers as uo  # import diferido, mismo motivo que quiksilver_roxy_scraper.py

    _log = log_fn or log
    driver = None
    result = {}
    try:
        driver = uo.build_driver()
        for section in _GROUPESEB_SECTIONS:
            try:
                offers = _scrape_groupeseb_section(driver, section)
                for o in offers:
                    result[o["id"]] = o
            except Exception as e:
                _log(f"aviso: fallo en {section['label']}, se omite esta tienda: {e}")
        # ToysRus DESACTIVADA (20 sep 2026, comprobado en la Pi 5): su web está detrás de
        # Cloudflare y bloquea el Chrome automatizado con una pantalla de verificación ("Un
        # momento…") que nunca llega a cargar la página real -- confirmado con
        # driver.title == "Un momento…" tras esperar. No es una tienda a la que forzar (evadir
        # protecciones anti-bot no es el camino) -- _scrape_toysrus() se deja escrita por si
        # algún día cambian su protección y vuelve a ser viable, pero no se llama.
    except Exception as e:
        _log(f"aviso: fallo abriendo Chrome, se omite Rowenta/Tefal este ciclo: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return result


if __name__ == "__main__":
    offers = fetch_groupeseb_toysrus_offers()
    print(f"\nTotal: {len(offers)} ofertas")
    for o in list(offers.values())[:8]:
        print(f"  {o['store_label']} | {o['title'][:50]} | {o['price']}€ (antes "
              f"{o['original_price']}€, -{o['discount_percent']}%)")
