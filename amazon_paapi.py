"""
amazon_paapi.py — Amazon Creators API, operación SearchItems (28 ago 2026, pedido explícito
del usuario: buscador de texto libre como en una versión antigua de la app, "adidas 42 camisa
tommy hombre... siempre me encontraba las cosas con descuento").

IMPORTANTE (reescrito el mismo día): la primera versión de este fichero portaba el algoritmo de
firma AWS Signature V4 de lib/services/amazon_service.dart de aquel proyecto antiguo
(~/Downloads/rebajasdiarias2bueno.zip) -- pero las credenciales nuevas del usuario
(afiliados.amazon.es/creatorsapi) resultaron ser de un sistema DISTINTO: la Creators API,
que sustituye a la PA-API 5.0 clásica y usa OAuth 2.0 (Login with Amazon) en vez de firma
manual. Esta versión usa ese flujo real, documentado en
afiliados.amazon.es/creatorsapi/docs/en-us/ (revisado a mano el 28 ago 2026).

Igual que la primera versión, la clave (aquí client_id/client_secret) vive solo en la Pi, en un
fichero fuera del repo (mismo patrón que FIREBASE_CREDENTIALS_PATH en update_offers.py) --
nunca viaja al móvil ni al navegador de nadie, ni queda en ningún commit.

El usuario eligió explícitamente NO pagar Firebase Cloud Functions (plan Blaze) para tener
búsqueda instantánea -- por eso esto lo consume un cron de la Pi (ver check_search_requests.py),
no una función en la nube. "Como las API no siempre hay acceso, no pasa nada, el buscador
sigue con otras cosas" (palabras del usuario, 28 ago 2026): Amazon exige un mínimo de 10 ventas
válidas en los últimos 30 días para mantener el acceso -- si no responde 200 (cupo agotado,
acceso suspendido, token inválido...), search_amazon() devuelve None y quien llama debe
tratarlo como "no disponible ahora", nunca como "sin resultados" ni como un error que deba
propagarse.

Endpoint de token y ruta de SearchItems verificados a mano contra el código fuente real del SDK
oficial de Python de Amazon (creatorsapi-python-sdk.zip, descargado y revisado el 28 ago 2026 --
ver auth/oauth2_config.py:determine_token_endpoint() y api/default_api.py:search_items(),
resource_path='/catalog/v1/searchItems') -- no hizo falta añadir el SDK entero como dependencia
de la Pi, este fichero ya acertaba en los dos puntos críticos antes de la verificación.
"""

import json
import os
import time

import requests

HOME = os.path.expanduser("~")
AMAZON_CREDENTIALS_PATH = f"{HOME}/amazon-paapi-credentials.json"

# Región EU (credenciales versión 3.2, marketplace www.amazon.es) -- OJO, api.amazon.com es
# solo para la región NA (versión 3.1), un error fácil de cometer copiando ejemplos de la
# documentación en inglés (casi todos usan la región NA).
TOKEN_ENDPOINT = "https://api.amazon.co.uk/auth/o2/token"
API_BASE = "https://creatorsapi.amazon"
SEARCH_ITEMS_PATH = "/catalog/v1/searchItems"
MARKETPLACE = "www.amazon.es"

# Mismo umbral que el resto del catálogo (ver MIN_DISCOUNT_PERCENT en update_offers.py) -- se
# lo pasamos al propio Amazon como minSavingPercent, así el filtrado lo hace la API y no hace
# falta pedir de más para luego descartar la mitad a mano.
MIN_DISCOUNT_PERCENT = 30

# Token cacheado en memoria del proceso -- cada ejecución de check_search_requests.py es un
# proceso nuevo (cron), así que esto solo ahorra llamadas dentro de un mismo ciclo si hay varias
# búsquedas pendientes a la vez (ver SEARCH_REQUESTS_MAX_PER_CYCLE). No hace falta persistirlo
# en disco: pedir un token de más de vez en cuando no cuesta nada.
_cached_token = None
_cached_token_expires_at = 0


def _load_credentials():
    """None si el fichero no existe todavía (el usuario no lo ha creado) o está incompleto --
    en ambos casos search_amazon() debe devolver None sin lanzar nada."""
    if not os.path.isfile(AMAZON_CREDENTIALS_PATH):
        return None
    try:
        with open(AMAZON_CREDENTIALS_PATH, encoding="utf-8") as f:
            creds = json.load(f)
        if not all(k in creds for k in ("client_id", "client_secret", "partner_tag")):
            return None
        return creds
    except Exception:
        return None


def _get_access_token(client_id: str, client_secret: str):
    """Token OAuth de Login with Amazon, cacheado en memoria hasta ~1 min antes de caducar
    (recomendación oficial de Amazon: reutilizar el token en vez de pedir uno nuevo por
    petición). Devuelve None si el login falla (credencial revocada, secreto incorrecto...)."""
    global _cached_token, _cached_token_expires_at
    if _cached_token and time.time() < _cached_token_expires_at:
        return _cached_token

    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "creatorsapi::default",
            }),
            timeout=15,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
    except (ValueError, KeyError):
        return None

    _cached_token = token
    _cached_token_expires_at = time.time() + expires_in - 60  # margen de 1 min
    return token


def search_amazon(keywords: str, item_count: int = 10):
    """Busca en Amazon.es por texto libre. Devuelve la lista cruda de 'items' de la Creators
    API (puede estar vacía si de verdad no hay resultados con descuento real), o None si la API
    no está disponible ahora mismo (sin credenciales, sin red, sin acceso -- menos de 10 ventas
    en 30 días, token inválido, cupo agotado...). Nunca lanza."""
    creds = _load_credentials()
    if not creds:
        return None

    token = _get_access_token(creds["client_id"], creds["client_secret"])
    if not token:
        return None

    # "marketplace" NO va en el cuerpo -- verificado contra el modelo real del SDK
    # (SearchItemsRequestContent no tiene ese campo), solo existe como cabecera x-marketplace.
    payload = {
        "partnerTag": creds["partner_tag"],
        "keywords": keywords,
        "itemCount": min(max(item_count, 1), 10),
        "minSavingPercent": MIN_DISCOUNT_PERCENT,
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
        ],
    }

    try:
        resp = requests.post(
            f"{API_BASE}{SEARCH_ITEMS_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-marketplace": MARKETPLACE,
            },
            data=json.dumps(payload),
            timeout=15,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        # 429 (límite de peticiones), 401/403 (sin acceso -- menos de 10 ventas en 30 días,
        # token caducado...), 404 (ruta equivocada, ver aviso al principio del fichero) o
        # cualquier otro fallo: "no disponible ahora mismo", nunca un error visible.
        return None

    try:
        data = resp.json()
    except ValueError:
        return None
    return data.get("searchResult", {}).get("items", [])


def offers_from_items(items, category="Amazon"):
    """Convierte los 'items' crudos de la Creators API en el mismo esquema de oferta que ya usa
    el resto del catálogo (ver _build_kindle_unlimited_offer en update_offers.py: id/title/
    category/price/original_price/discount_percent/image/url) -- así la app/web no necesitan
    ningún caso especial para pintar un resultado de este buscador. Como la petición ya pide
    minSavingPercent, en teoría todo lo que llega aquí ya tiene descuento real -- se
    revalida igualmente por si acaso, nunca fiarse a ciegas de un filtro ajeno."""
    offers = []
    for item in items or []:
        try:
            listing = item["offersV2"]["listings"][0]
            price = listing["price"]["money"]["amount"]
            saving_basis = listing["price"].get("savingBasis", {}).get("money", {}).get("amount")
            if not saving_basis or saving_basis <= price:
                continue
            discount_percent = listing["price"].get("savings", {}).get("percentage")
            if discount_percent is None:
                discount_percent = round((1 - price / saving_basis) * 100)
            if discount_percent < MIN_DISCOUNT_PERCENT:
                continue
            title = item["itemInfo"]["title"]["displayValue"]
            image = item.get("images", {}).get("primary", {}).get("large", {}).get("url", "")
            url = item.get("detailPageURL", "")
            offers.append({
                "id": item.get("asin", title[:40]),
                "title": title[:180],
                "category": category,
                "price": round(price, 2),
                "original_price": round(saving_basis, 2),
                "discount_percent": discount_percent,
                "image": image,
                "url": url,
                "store": "amazon",
            })
        except (KeyError, TypeError, IndexError, ZeroDivisionError):
            continue  # item con una forma inesperada -- se descarta, no debe tumbar el resto
    return offers
