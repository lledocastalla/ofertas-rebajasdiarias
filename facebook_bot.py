#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de publicación en la Page de Facebook "Rebajas Diarias".

Mismo patrón que reddit_bot.py: lee el offers.json público, elige UNA oferta
"de titular" que no se haya publicado ya, y la sube como publicación con foto
a la Page vía la Graph API — sin pasar por el navegador.

A diferencia de Reddit, Facebook's /photos endpoint acepta directamente la
URL de la imagen del producto (no hace falta descargarla antes), así que el
script es bastante más simple.

Pensado para publicar varias veces al día (a petición del usuario: ~6/día),
así que se invoca varias veces por cron y cada ejecución publica UNA oferta:

    0 9,11,13,15,17,20 * * * /home/rebajasdiarias/venv/bin/python3 \
        /home/rebajasdiarias/ofertas-rebajasdiarias/facebook_bot.py >> \
        /home/rebajasdiarias/facebook_bot.log 2>&1

Credenciales: igual que TELEGRAM_BOT_TOKEN_PATH / reddit_bot.py — un fichero
fuera del repo, en $HOME, nunca en git. Formato (creado a mano, permisos 600):

    ~/.rebajas_facebook_credentials.json
    {
      "app_id": "...",
      "app_secret": "...",
      "page_id": "...",
      "page_access_token": "..."
    }

El page_access_token se generó el 20 ago 2026 a partir de un token de
usuario de larga duración (60 días) — un Page Access Token derivado así NO
caduca (confirmado con el Depurador de identificadores de acceso de Meta:
"Caduca: Nunca"), así que no hace falta renovarlo salvo que el usuario
cambie su contraseña de Facebook o revoque la app.
"""
import json
import random
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
OFFERS_URL = "https://raw.githubusercontent.com/lledocastalla/ofertas-rebajasdiarias/main/offers.json"
CREDENTIALS_PATH = HOME / ".rebajas_facebook_credentials.json"
GRAPH_VERSION = "v26.0"
# Mismo criterio que Flash/Ofertas del día y que reddit_bot.py (ver
# js/app.js y lib/services/offers_service.dart) — categorías que dominan con
# descuentos altos pero poco interesantes como titular. Se usa como filtro de
# respaldo (ver PREFERRED_CATEGORIES) si algún día no hay suficientes ofertas
# en las categorías preferidas.
BORING_CATEGORIES = {"Alimentación", "Mascotas"}
# Añadido 20 ago, a petición del usuario: para las 12 publicaciones/día en
# Facebook, mejor centrarse en las categorías con más tirón visual (moda,
# calzado deportivo, tecnología) en vez de dejar que cualquier categoría con
# buen descuento salga como titular. Nombres tal cual aparecen en offers.json
# — no existe una categoría "Zapatillas" separada, el calzado deportivo cae
# dentro de "Deporte".
PREFERRED_CATEGORIES = {"Moda Hombre", "Moda Mujer", "Tecnología", "Deporte"}
MIN_DISCOUNT = 45

STATE_FILE = Path(__file__).resolve().parent / "facebook_bot_posted.json"
DISCLOSURE = (
    "Como afiliados de Amazon, ganamos con las compras que cumplen los "
    "requisitos aplicables, sin coste adicional para vosotros."
)
WEBSITE_PLUG = "Más ofertas seleccionadas a diario en rebajasdiarias.es (web + app Android + bot de Telegram)."


def load_offers():
    with urllib.request.urlopen(OFFERS_URL, timeout=20) as r:
        data = json.load(r)
    return data["offers"]


def load_posted_ids():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def save_posted_id(offer_id, posted):
    posted.add(offer_id)
    # Solo guardamos los últimos 200 para que el fichero no crezca sin límite
    trimmed = list(posted)[-200:]
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def pick_offer(offers, posted):
    base = [
        o
        for o in offers
        if o.get("discount_percent", 0) >= MIN_DISCOUNT
        and o.get("image")
        and o["id"] not in posted
    ]
    # Primero intenta solo en las categorías preferidas (moda, deporte,
    # tecnología); si algún día no hay suficientes, cae al filtro antiguo
    # (todo menos Alimentación/Mascotas) para no dejar de publicar.
    candidates = [o for o in base if o["category"] in PREFERRED_CATEGORIES]
    if not candidates:
        candidates = [o for o in base if o["category"] not in BORING_CATEGORIES]
    if not candidates:
        return None
    candidates.sort(key=lambda o: -o["discount_percent"])
    top = candidates[: min(5, len(candidates))]
    return random.choice(top)


def build_caption(o):
    price = f"{o['price']:.2f}".replace(".", ",")
    original = f"{o['original_price']:.2f}".replace(".", ",")
    return (
        f"{o['title'][:100]} — {price}€ (antes {original}€, -{o['discount_percent']}%) "
        f"(enlace de afiliado)\n\n"
        f"{o['url']}\n\n"
        f"{DISCLOSURE}\n\n"
        f"{WEBSITE_PLUG}"
    )


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        sys.exit(
            f"Falta {CREDENTIALS_PATH} — crea el fichero de credenciales a mano "
            "(ver docstring de este script) antes de ejecutar el bot."
        )
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        return json.load(f)


def post_photo(page_id, page_access_token, image_url, caption):
    endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos"
    payload = urllib.parse.urlencode(
        {"url": image_url, "caption": caption, "access_token": page_access_token}
    ).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    creds = load_credentials()

    offers = load_offers()
    posted = load_posted_ids()
    offer = pick_offer(offers, posted)
    if offer is None:
        print("No hay ofertas nuevas que publicar hoy (todas repetidas o filtradas).")
        return

    caption = build_caption(offer)
    result = post_photo(creds["page_id"], creds["page_access_token"], offer["image"], caption)

    post_id = result.get("post_id") or result.get("id")
    print(f"Publicado: https://www.facebook.com/{post_id} ({offer['title'][:60]})")

    save_posted_id(offer["id"], posted)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
