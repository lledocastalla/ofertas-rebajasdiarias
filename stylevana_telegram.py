#!/usr/bin/env python3
"""
Script INDEPENDIENTE de update_offers.py. Nunca toca offers.json, ni lo lee ni lo escribe —
solo publica ofertas de Stylevana en el tema "Belleza" del grupo de Telegram. Si este script
falla o se borra entero, Amazon/la app/la web siguen funcionando exactamente igual.

Qué hace cada ciclo:
  1. Descarga el feed real de Stylevana ES desde Awin (23.804 productos, precio/stock/imagen/
     enlace de afiliado ya incluidos por producto).
  2. Filtra: en stock, EUR, descuento real entre MIN_DISCOUNT-MAX_DISCOUNT (mismo criterio que
     ya usa Amazon en update_offers.py, no hay motivo para uno distinto).
  3. Descarta lo que ya se publicó antes (estado propio, ver STATE_PATH) — nunca repite el mismo
     producto sin más.
  4. Publica como máximo MAX_NEW_PER_RUN productos nuevos por ciclo (evita inundar el grupo si
     el feed trae de golpe muchos que cumplen el filtro).
  5. Igual que update_offers.py: nunca debe tumbarse por un fallo de red puntual, se registra y
     sigue.

Uso: python3 stylevana_telegram.py [--dry-run]
  --dry-run: descarga y filtra igual, pero no publica nada ni actualiza el estado — para
  comprobar qué mandaría antes de dejarlo en cron.
"""
import csv
import io
import json
import gzip
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TELEGRAM_BOT_TOKEN = "8853076314:AAGld1wicuDIAgklHwrlq36frqihSOwLelo"
TELEGRAM_GROUP_CHAT_ID = "-1002409782408"
TELEGRAM_BELLEZA_THREAD_ID = 112095  # tema fijo, todo Stylevana es belleza/cuidado personal hoy

# Mismo criterio de descuento que Amazon (MIN/MAX_DISCOUNT_PERCENT en update_offers.py) — sin
# motivo para que Stylevana tenga un listón distinto.
MIN_DISCOUNT_PERCENT = 30
MAX_DISCOUNT_PERCENT = 80
MIN_PRICE_EUR = 3.0  # evita artículos de céntimos poco relevantes aunque el % cuadre

# Awin "Crea-un-feed": feed real de Stylevana ES, verificado a mano el 14 ago 2026 (23.804
# productos). El token de la URL es el mismo que aparece en "Descargar lista" dentro de
# Herramientas > Crea-un-feed en ui.awin.com — no es secreto de cuenta, es específico de este feed.
FEED_URL = (
    "https://productdata.awin.com/datafeed/download/apikey/"
    "f239974da24e881acd5d3cdfa614a45e/language/es/fid/88396/format/csv/"
    "delimiter/%2C/compression/gzip/adultcontent/1/"
)

HOME = str(Path.home())
STATE_PATH = f"{HOME}/.rebajas_stylevana_telegram_state.json"
LOG_PATH = f"{HOME}/stylevana_telegram.log"

MAX_NEW_PER_RUN = 6  # tope por ciclo, mismo espíritu que MAX_WATCHED_DIRECT_VISITS_PER_RUN

# Marcas con tirada real confirmada (investigación TikTok/reputación, 14 ago 2026) o ya
# publicadas con buen resultado — priorizadas sobre el resto para que el grupo no se llene de
# marcas genéricas/desconocidas solo porque tengan el % más alto (pedido por el usuario, 14 ago
# 2026, tras ver que el orden por descuento puro sacaba primero marcas como "RiRe"/"Jigott").
PRIORITY_BRANDS = {
    "anua", "laneige", "beauty of joseon", "skin1004", "cosrx", "isntree", "medicube",
    "shiseido", "sulwhasoo", "goodal", "round lab", "torriden", "purito seoul", "numbuzin",
    "illiyoon", "haruharu wonder", "dr.ceuracle", "abib", "tocobo", "biohealboh",
    "mediheal", "innisfree",
}


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"posted": {}}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"aviso: no se pudo guardar el estado: {e}")


def download_feed():
    """Descarga y descomprime el feed. Nunca lanza fuera de main() — si falla, se registra y
    el ciclo se salta entero (nunca publica con datos a medias)."""
    with urllib.request.urlopen(FEED_URL, timeout=120) as r:
        raw = r.read()
    text = gzip.decompress(raw).decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def qualifying_offers(rows):
    out = []
    for row in rows:
        try:
            if row.get("in_stock") != "1":
                continue
            if row.get("currency") != "EUR":
                continue
            price = float(row.get("search_price") or 0)
            rrp = float(row.get("rrp_price") or 0)
            if price <= 0 or rrp <= 0 or rrp <= price:
                continue
            if price < MIN_PRICE_EUR:
                continue
            discount = round((1 - price / rrp) * 100)
            if not (MIN_DISCOUNT_PERCENT <= discount <= MAX_DISCOUNT_PERCENT):
                continue
            url = row.get("aw_deep_link")
            image = row.get("merchant_image_url")
            title = row.get("product_name")
            pid = row.get("merchant_product_id")
            if not (url and title and pid):
                continue
            out.append({
                "id": pid,
                "title": title,
                "price": price,
                "orig": rrp,
                "discount": discount,
                "url": url,
                "image": image,
                "brand": (row.get("brand_name") or "").strip().lower(),
            })
        except (ValueError, TypeError):
            continue
    return out


def _html_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _discount_badge(discount):
    if discount >= 70:
        return "🔴"
    if discount >= 60:
        return "🟠"
    return "🟡"


def _telegram_api(method, params, _retried=False):
    """Mismo patrón que _telegram_api() de update_offers.py: nunca lanza, reintenta una vez
    ante un 429 respetando el retry_after que indica la propia API."""
    try:
        data = urllib.parse.urlencode(params).encode()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        retry_after = (body.get("parameters") or {}).get("retry_after", 5)
        if e.code == 429 and not _retried:
            log(f"  aviso: Telegram {method} limitado (429), espero {retry_after}s y reintento...")
            time.sleep(retry_after + 1)
            return _telegram_api(method, params, _retried=True)
        log(f"  aviso: fallo llamando a Telegram {method}: HTTP {e.code} {body}")
        return None
    except Exception as e:
        log(f"  aviso: fallo llamando a Telegram {method}: {e}")
        return None


def post_offer(offer, with_intro=False):
    """Mismo formato de tarjeta que Amazon en update_offers.py (tras el cambio del 14 ago 2026:
    botón real de Telegram en vez de enlace de texto)."""
    badge = _discount_badge(offer["discount"])
    price_line = (
        f"{badge} <b>{offer['price']:.2f}€</b>  "
        f"<s>{offer['orig']:.2f}€</s>  (-{offer['discount']}%)"
    )
    intro = (
        "🆕 A partir de hoy también traemos ofertas reales de otras tiendas, no solo Amazon 👇\n\n"
        if with_intro else ""
    )
    caption = (
        f"{intro}"
        f"<b>🎯 OFERTA DEL DÍA</b>\n\n"
        f"🛍 <b>{_html_escape(offer['title'])}</b>\n\n"
        f"{price_line}"
    )
    reply_markup = json.dumps({
        "inline_keyboard": [[{"text": "👉 Ver oferta en Stylevana", "url": offer["url"]}]]
    })
    params = {
        "chat_id": TELEGRAM_GROUP_CHAT_ID,
        "message_thread_id": TELEGRAM_BELLEZA_THREAD_ID,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if offer.get("image"):
        result = _telegram_api("sendPhoto", {**params, "photo": offer["image"], "caption": caption})
        if result and result.get("ok"):
            return True
        log(f"  aviso: sendPhoto falló para {offer['id']}, se prueba como texto")
    result = _telegram_api("sendMessage", {**params, "text": caption})
    return bool(result and result.get("ok"))


def main():
    dry_run = "--dry-run" in sys.argv
    log(f"--- ciclo Stylevana→Telegram {'(DRY RUN)' if dry_run else ''} ---")

    try:
        rows = download_feed()
    except Exception as e:
        log(f"error descargando el feed, se aborta el ciclo sin publicar nada: {e}")
        return

    offers = qualifying_offers(rows)
    log(f"feed: {len(rows)} productos totales, {len(offers)} cumplen 30-80% en stock/EUR")

    state = load_state()
    posted = state.get("posted", {})
    new_offers = [o for o in offers if o["id"] not in posted]
    # Marcas con tirada conocida primero (y entre ellas, mejor descuento primero); el resto
    # (marcas genéricas/desconocidas) solo entra si no hay suficientes de las prioritarias.
    new_offers.sort(key=lambda o: (o["brand"] not in PRIORITY_BRANDS, -o["discount"]))
    batch = new_offers[:MAX_NEW_PER_RUN]

    log(f"{len(new_offers)} son nuevos (no publicados antes), publicando {len(batch)} este ciclo")

    if dry_run:
        for o in batch:
            log(f"  [dry-run] publicaría: {o['title']} — {o['price']:.2f}€ (-{o['discount']}%)")
        return

    for o in batch:
        ok = post_offer(o)
        log(f"  {'OK' if ok else 'FALLO'}: {o['title'][:50]} (-{o['discount']}%)")
        if ok:
            posted[o["id"]] = {
                "title": o["title"],
                "discount": o["discount"],
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
        time.sleep(3)  # margen entre llamadas, mismo criterio que update_offers.py

    state["posted"] = posted
    save_state(state)
    log("--- ciclo terminado ---")


if __name__ == "__main__":
    main()
