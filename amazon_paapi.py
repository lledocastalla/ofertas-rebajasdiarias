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
GET_ITEMS_PATH = "/catalog/v1/getItems"
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

# Marcas conocidas para poder recuperar una búsqueda de dos palabras pegadas sin espacio (29 sep
# 2026, aviso real del usuario: alguien buscó "applewhach" y salieron 10 ofertas sin relación con
# Apple Watch, pero "apple whach" -- con espacio, aunque siga con la errata -- sí encontraba algo
# relevante). Amazon.es hace bien el fuzzy-matching palabra a palabra (tolera "whach"/"whatch"
# como errata de "watch"), pero no separa una sola palabra pegada en dos términos por su cuenta.
# Lista corta a propósito, solo marcas de electrónica/deporte habituales en este catálogo -- no
# un diccionario genérico, para no partir por error una palabra real que empiece igual.
_KNOWN_BRAND_PREFIXES = (
    "apple", "samsung", "xiaomi", "huawei", "adidas", "nike", "sony", "philips",
    "braun", "logitech", "garmin", "fitbit", "nintendo", "playstation", "lenovo",
    "asus", "bosch", "dyson", "jbl", "bose", "gopro", "canon", "nikon",
)


def _split_glued_query(keywords: str):
    """Si `keywords` es una sola palabra (sin espacios) que empieza por una marca conocida y le
    sobra texto de verdad detrás (ej. "applewhach" -> "apple whach"), devuelve la versión con
    espacio. None si no aplica -- consulta ya con espacios, demasiado corta, o no empieza por
    ninguna marca de la lista (nunca se inventa una marca que no está ahí)."""
    q = keywords.strip()
    if " " in q or len(q) < 6:
        return None
    lower = q.lower()
    for brand in _KNOWN_BRAND_PREFIXES:
        if lower.startswith(brand) and len(lower) - len(brand) >= 3:
            return f"{q[:len(brand)]} {q[len(brand):]}"
    return None


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


def _search_items_once(keywords: str, item_count: int, min_saving_percent: int, creds, token):
    """Una única llamada real a SearchItems -- extraído de search_amazon() el 29 sep 2026 para
    poder repetir la petición con una segunda variante de `keywords` (ver _split_glued_query) sin
    duplicar la construcción del payload ni el manejo de errores. Mismo contrato de siempre:
    lista (puede ir vacía) o None si la API no responde bien."""
    # "marketplace" NO va en el cuerpo -- verificado contra el modelo real del SDK
    # (SearchItemsRequestContent no tiene ese campo), solo existe como cabecera x-marketplace.
    payload = {
        "partnerTag": creds["partner_tag"],
        "keywords": keywords,
        # 1 oct 2026, límite corregido: SearchItemsRequestContent del SDK oficial permite
        # itemCount hasta 100 (confirmado contra el modelo real) -- el 10 de antes era el
        # límite de GetItems (otra operación distinta), copiado aquí por error. Con 10 el
        # buscador/alertas daban menos resultados que el scraping de Selenium de antes (hasta
        # 24, ver MAX_PRODUCTS_KEYWORD_ALERT), aviso real del usuario: "parece que encuentre
        # ahora menos que cuando hacía scrapping".
        "itemCount": min(max(item_count, 1), 100),
        "minSavingPercent": min_saving_percent,
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
            # 28 sep 2026, aviso real: "en los resultados de la api no salen las estrellas como
            # en los resultados de la otra búsqueda" -- comprobado en vivo: son nombres de
            # recurso válidos (no dan 400 al pedirlos, vienen listados en el enum real de la
            # API), pero Amazon NO los rellena en la práctica para esta cuenta/mercado (probado
            # con 5 productos reales distintos, ninguno trae `customerReviews` en la respuesta,
            # aunque se pidan). Se piden igualmente por si Amazon empieza a devolverlos más
            # adelante -- offers_from_items() de abajo ya sabe leerlos si algún día aparecen,
            # sin más cambios aquí.
            "customerReviews.starRating",
            "customerReviews.count",
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


def search_amazon(keywords: str, item_count: int = 100, min_saving_percent: int = MIN_DISCOUNT_PERCENT):
    """Busca en Amazon.es por texto libre. Devuelve la lista cruda de 'items' de la Creators
    API (puede estar vacía si de verdad no hay resultados con descuento real), o None si la API
    no está disponible ahora mismo (sin credenciales, sin red, sin acceso -- menos de 10 ventas
    en 30 días, token inválido, cupo agotado...). Nunca lanza.

    `min_saving_percent` es parametrizable (14 sep 2026, ver keyword_alert_search.py) -- las
    alertas de palabra clave usan un umbral mucho más bajo (1%) que el resto del catálogo (30%,
    MIN_DISCOUNT_PERCENT de siempre): es una palabra muy concreta pedida por una persona en
    Ajustes, pedido explícito "desde 1% de descuento hasta el máximo" -- mejor un 5% real que
    nada. El buscador normal de la app (search_requests_listener.py, camino sin
    notifyPush) sigue usando el 30% de siempre, sin tocar nada ahí.

    29 sep 2026, aviso real del usuario: "applewhach" (pegado) no encontraba nada de Apple Watch,
    pero "apple whach" (con espacio) sí. Si `keywords` parece dos palabras pegadas empezando por
    una marca conocida (ver _split_glued_query), se hace TAMBIÉN una segunda búsqueda con la
    versión separada y se combinan los resultados (sin duplicar ASIN) -- puramente aditivo, la
    búsqueda con la palabra pegada de siempre sigue haciéndose igual, esto solo puede añadir
    resultados que antes no aparecían, nunca quitar los que ya salían."""
    creds = _load_credentials()
    if not creds:
        return None

    token = _get_access_token(creds["client_id"], creds["client_secret"])
    if not token:
        return None

    items = _search_items_once(keywords, item_count, min_saving_percent, creds, token)
    if items is None:
        return None

    split_query = _split_glued_query(keywords)
    if split_query:
        extra_items = _search_items_once(split_query, item_count, min_saving_percent, creds, token)
        if extra_items:
            seen_asins = {item.get("asin") for item in items if item.get("asin")}
            for item in extra_items:
                asin = item.get("asin")
                if asin and asin in seen_asins:
                    continue
                items.append(item)
                if asin:
                    seen_asins.add(asin)

    return items[: min(max(item_count, 1), 100)]


def _get_items_once(asins, creds, token):
    """Una única llamada a GetItems -- hasta 10 ASIN por petición (mismo límite que itemCount
    en SearchItems, ver GetItemsRequestContent del SDK oficial, max_length=10). Mismo contrato
    que _search_items_once(): lista de 'items' (un ASIN descatalogado simplemente no aparece en
    la respuesta, no es un error) o None si la petición en sí falla."""
    payload = {
        "partnerTag": creds["partner_tag"],
        "itemIds": asins,
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
            "customerReviews.starRating",
            "customerReviews.count",
        ],
    }
    try:
        resp = requests.post(
            f"{API_BASE}{GET_ITEMS_PATH}",
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
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return (data.get("itemsResult") or {}).get("items", [])


def get_items(asins):
    """Revalida en bloque el precio/stock REAL de una lista de ASIN ya conocidos del catálogo
    (1 oct 2026, pedido explícito: "muchas veces no me coincide en los precios cuando entro en
    amazon, como si hubiesen caducado"). A diferencia de search_amazon() (que DESCUBRE productos
    nuevos por palabra clave, con muestreo aleatorio 1-2 keywords/categoría -- un producto
    concreto podía pasar casi 2 días sin reconfirmarse, ver STALE_AFTER_DAYS en
    update_offers.py), esto solo confirma si los que ya están en offers.json siguen con el mismo
    precio/descuento, en llamadas baratas de hasta 10 ASIN cada una -- sin Selenium, sin abrir
    Chrome, así que no suma ningún riesgo de "parecer un bot" nuevo.

    Devuelve (items, failed_asins): `items` es la lista cruda de la API para los lotes que sí
    respondieron (un ASIN ausente ahí de verdad ya no existe o perdió el descuento real);
    `failed_asins` son los que quedaron sin comprobar porque su lote de 10 falló (red, 429...) --
    quien llama NO debe tratarlos como "ya no existen", solo como "sin novedad esta vez" (mismo
    principio de todo el proyecto: mejor un precio de hace unas horas que romper el catálogo por
    un fallo de red puntual). Devuelve None (nada comprobado) si la API no está disponible en
    absoluto esta vez (sin credenciales, sin token) -- nunca lanza."""
    creds = _load_credentials()
    if not creds:
        return None
    token = _get_access_token(creds["client_id"], creds["client_secret"])
    if not token:
        return None

    items = []
    failed_asins = set()
    for i in range(0, len(asins), 10):
        batch = asins[i:i + 10]
        result = _get_items_once(batch, creds, token)
        if result is None:
            failed_asins.update(batch)
        else:
            items.extend(result)
    return items, failed_asins


def offers_from_items(items, category="Amazon", min_discount_percent: int = MIN_DISCOUNT_PERCENT):
    """Convierte los 'items' crudos de la Creators API en el mismo esquema de oferta que ya usa
    el resto del catálogo (ver _build_kindle_unlimited_offer en update_offers.py: id/title/
    category/price/original_price/discount_percent/image/url) -- así la app/web no necesitan
    ningún caso especial para pintar un resultado de este buscador. Como la petición ya pide
    minSavingPercent, en teoría todo lo que llega aquí ya tiene descuento real -- se
    revalida igualmente por si acaso, nunca fiarse a ciegas de un filtro ajeno.
    `min_discount_percent` debe coincidir con el que se pasó a search_amazon() -- ver
    comentario de ahí."""
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
            if discount_percent < min_discount_percent:
                continue
            title = item["itemInfo"]["title"]["displayValue"]
            image = item.get("images", {}).get("primary", {}).get("large", {}).get("url", "")
            url = item.get("detailPageURL", "")
            # 28 sep 2026: None si Amazon no lo trae (caso real de hoy, ver comentario de
            # search_amazon() sobre customerReviews) -- nunca inventado, mismo criterio que
            # parse_rating()/parse_rating_count() del scraping en update_offers.py.
            reviews = item.get("customerReviews") or {}
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
                "rating": reviews.get("starRating"),
                "rating_count": reviews.get("count"),
            })
        except (KeyError, TypeError, IndexError, ZeroDivisionError):
            continue  # item con una forma inesperada -- se descarta, no debe tumbar el resto
    return offers
