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
# Memoria por TÍTULO además de por id (22 oct 2026). El 36% del catálogo de Amazon son
# variantes del mismo producto con ASIN distinto, así que recordar el id no evita que salga
# lo mismo otra vez con otra referencia.
STATE_TITULOS = Path(__file__).resolve().parent / "facebook_bot_posted_titulos.json"
MAX_RECORDADOS = 200
# Solo Amazon (22 oct 2026, petición expresa del usuario: "son productos de awin cuando
# quiero solo productos de amazon"). Hasta ahora no se filtraba por tienda en absoluto, y
# como el catálogo tiene 40.450 ofertas de las que solo 1.121 son de Amazon, en la práctica
# casi todo lo que salía era de Awin: de las últimas 200 publicaciones, 85 eran de
# deporteoutlet y solo 5 de Amazon. Además el texto legal que se añade dice "Como afiliados
# de Amazon...", que era falso en todas ésas.
# Hay margen de sobra: 355 ofertas de Amazon con -45% o más, 304 productos distintos, o sea
# unos 25 días sin repetir a 12 publicaciones diarias.
SOLO_AMAZON = True
DISCLOSURE = (
    "Como afiliados de Amazon, ganamos con las compras que cumplen los "
    "requisitos aplicables, sin coste adicional para vosotros."
)
WEBSITE_PLUG = "Más ofertas seleccionadas a diario en rebajasdiarias.es (web + app Android + bot de Telegram)."


def load_offers():
    with urllib.request.urlopen(OFFERS_URL, timeout=20) as r:
        data = json.load(r)
    return data["offers"]


def _leer_lista(ruta):
    """Lee una memoria EN ORDEN. Devolver una lista y no un conjunto es el arreglo del 22 oct
    2026 y es importante: `save_posted_id()` hacía `list(posted)[-200:]` sobre un **set**, y
    el orden de iteración de un conjunto de cadenas es arbitrario porque Python aleatoriza el
    hash de las cadenas en cada proceso. Es decir, al llegar al tope no se descartaba el más
    viejo: se descartaba **uno al azar**, y lo descartado volvía a ser publicable. Esa es la
    causa de que Facebook repitiera productos "todo el rato"."""
    if not ruta.exists():
        return []
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [d for d in datos if isinstance(d, str)] if isinstance(datos, list) else []


def _guardar_lista(ruta, valores):
    ruta.write_text(json.dumps(valores[-MAX_RECORDADOS:], ensure_ascii=False),
                    encoding="utf-8")


def clave_titulo(titulo):
    """Misma función que usa el bot de Instagram, para que los dos entiendan lo mismo por
    "el mismo producto". Se importa en vez de copiarse: dos copias acabarían divergiendo."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from instagram_bot import clave_titulo as ct
    return ct(titulo)


def load_posted_ids():
    return _leer_lista(STATE_FILE)


def save_posted_id(offer, posted, titulos):
    posted.append(offer["id"])
    _guardar_lista(STATE_FILE, posted)
    titulos.append(clave_titulo(offer.get("title")))
    _guardar_lista(STATE_TITULOS, titulos)


def pick_offer(offers, posted, titulos=None):
    ya = set(posted)
    ya_tit = set(titulos or [])
    base = [
        o
        for o in offers
        if o.get("discount_percent", 0) >= MIN_DISCOUNT
        and o.get("image")
        and o["id"] not in ya
        and clave_titulo(o.get("title")) not in ya_tit
        and (not SOLO_AMAZON or (o.get("store") or "amazon") == "amazon")
    ]
    # Primero intenta solo en las categorías preferidas (moda, deporte,
    # tecnología); si algún día no hay suficientes, cae al filtro antiguo
    # (todo menos Alimentación/Mascotas) para no dejar de publicar.
    # Filtro de calidad, el MISMO que usa Instagram (23 oct 2026). Hacía falta por algo que
    # salió al medir: al pasar a ordenar por ahorro en euros, los diez primeros eran
    # portátiles sin marca con CERO valoraciones que decían ahorrar 1.296 € y 689 €. O sea,
    # ordenar por ahorro premia justo las fichas con el "antes" más inflado, que son
    # exactamente las que esta marca existe para NO publicar.
    #
    # Con el filtro puesto, los diez primeros pasan a ser Diesel, DKNY, Reebok, Tommy
    # Hilfiger y Calvin Klein. Antes Facebook no miraba las valoraciones para nada.
    sys.path.insert(0, str(Path.home() / "rebajasdiarias-web" / "scripts"))
    try:
        import instagram_tendencias as it
        base = [o for o in base if it.candidata(o)]
    except Exception as e:
        # Si el módulo no está, se sigue sin filtro: mejor publicar algo flojo que no
        # publicar. Pero se dice en voz alta, que un filtro que desaparece en silencio es
        # peor que no tenerlo.
        print(f"AVISO: sin filtro de calidad ({e})")

    candidates = [o for o in base if o["category"] in PREFERRED_CATEGORIES]
    if not candidates:
        candidates = [o for o in base if o["category"] not in BORING_CATEGORIES]
    if not candidates:
        return None
    # Se ordena por AHORRO EN EUROS, no por porcentaje (23 oct 2026). El usuario pidió
    # publicar bolsos buenos y al medirlo salió que no llegaban nunca: ordenando por
    # porcentaje, el décimo candidato ya iba a -71%, y los bolsos caían en las posiciones 61,
    # 75 y 185. No es cosa de bolsos: con este orden, NADA caro puede salir jamás, porque un
    # cacharro de 15 € a -77% siempre gana a un bolso de 99 € a -45%.
    #
    # Y el porcentaje miente sobre lo que de verdad te llevas: ese -77% ahorra 11 €, el -45%
    # ahorra 81 €. Se usa el logaritmo del ahorro, el mismo criterio que ya usaba
    # instagram_tendencias.py, para que lo caro entre sin que un solo producto de 500 €
    # monopolice la cuenta. Empate deshecho por porcentaje.
    import math
    candidates.sort(key=lambda o: (-math.log1p(max(0.0, (o.get("original_price") or 0)
                                                   - (o.get("price") or 0))),
                                   -o["discount_percent"]))
    # Se revalidan 10 contra Amazon en una sola llamada y se sortea entre los que siguen a su
    # precio (22 oct 2026, "si ya no está ese precio se quite"). Antes se sorteaba entre los 5
    # de mayor descuento sin comprobar nada, y los descuentos más altos son justo los que más
    # se caen, así que era donde más probable era anunciar un precio que ya no existía.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import precios
    # 20 y no 10: la ventana de sorteo de abajo es de 12, así que revalidar solo 10 la dejaría
    # coja. Son dos lotes de API en vez de uno, que al lado de las ~7.200 llamadas diarias del
    # ciclo normal no es nada.
    vigentes = precios.filtrar_vigentes(candidates[:20], limite=20)
    if vigentes is None:
        return None
    if not vigentes:
        print("Ninguna de las 10 mejores sigue a su precio; no se publica.")
        return None
    # Se sortea entre 12 y no entre 5 (23 oct 2026): con 12 publicaciones al día, una ventana
    # de 5 hace que la página parezca repetirse aunque técnicamente no repita producto.
    return random.choice(vigentes[: min(12, len(vigentes))])


def build_caption(o):
    price = f"{o['price']:.2f}".replace(".", ",")
    original = f"{o['original_price']:.2f}".replace(".", ",")
    # El enlace va EN LA PRIMERA LÍNEA (21 oct 2026, lo pidió el usuario). Facebook corta el
    # texto con un "Ver más" y lo que queda debajo lo lee mucha menos gente; estaba en la
    # tercera línea, que ya es tarde. Aquí el enlace sí es pulsable, al revés que en Instagram.
    return (
        f"👉 {o['url']}\n\n"
        f"{o['title'][:100]} — {price}€ (antes {original}€, -{o['discount_percent']}%) "
        f"(enlace de afiliado)\n\n"
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
    titulos = _leer_lista(STATE_TITULOS)
    offer = pick_offer(offers, posted, titulos)
    if offer is None:
        print("No hay ofertas nuevas que publicar hoy (todas repetidas o filtradas).")
        return

    caption = build_caption(offer)
    result = post_photo(creds["page_id"], creds["page_access_token"], offer["image"], caption)

    post_id = result.get("post_id") or result.get("id")
    print(f"Publicado: https://www.facebook.com/{post_id} ({offer['title'][:60]})")

    save_posted_id(offer, posted, titulos)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
