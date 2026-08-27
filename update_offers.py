#!/usr/bin/env python3
"""
update_offers.py — RebajasDiarias: busca ofertas reales en Amazon.es y actualiza
offers.json en el repo lledocastalla/ofertas-rebajasdiarias.

Diseño (ver RASPI_REBAJASDIARIAS.md):
- Usa la sesión de Chromium ya logueada en Amazon (perfil persistente en
  ~/.rebajas_chrome_profile, logueado manualmente una vez por VNC), en modo headless.
- Cada ejecución sondea un subconjunto ALEATORIO de keywords por categoría (para no tardar
  horas en una Raspberry Pi 3B) y FUSIONA los resultados nuevos con los ya existentes en
  offers.json por ASIN — nunca sustituye el catálogo entero de golpe, así que categorías no
  tocadas en una ejecución concreta no pierden sus ofertas.
- Filtra descuento mínimo 30%.
- Regla de seguridad crítica: si el scraping falla (Amazon bloquea, cambia el HTML, sin red,
  etc.) o el catálogo resultante tiene menos del mínimo aceptable de productos, ABORTA sin
  tocar offers.json ni hacer commit/push. Nunca se deja la app sin ofertas por un fallo puntual.
"""

import fcntl
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException, TimeoutException

from multitienda_feeds import fetch_multitienda_offers

# --- Configuración ---
HOME = os.path.expanduser("~")
PROFILE_DIR = f"{HOME}/.rebajas_chrome_profile"
REPO_DIR = f"{HOME}/ofertas-rebajasdiarias"
OFFERS_PATH = f"{REPO_DIR}/offers.json"
# Candado compartido con check_submissions.py (26 ago 2026, ver ese archivo): evita que los dos
# procesos hagan git pull/commit/push sobre el mismo repo a la vez -- este script (ciclo normal
# de 3h) espera su turno (lock bloqueante); check_submissions.py, que dispara cada pocos
# minutos, se sale sin más si lo encuentra ocupado y lo reintenta en su próximo disparo.
REPO_LOCK_PATH = f"{HOME}/.rebajas_update_lock"
CHROMEDRIVER_PATH = "/usr/bin/chromedriver"
CHROMIUM_PATH = "/usr/bin/chromium"
AFFILIATE_TAG = "rebajasdiar05-21"

# Catálogo ampliado para el buscador (26 ago 2026, ver generate_extended_catalog() en
# multitienda_feeds.py y RASPI_REBAJASDIARIAS.md §8 punto 17): archivo APARTE de offers.json,
# no se fusiona ni se poda como el catálogo normal -- cada vez que se regenera se sustituye
# entero (es un volcado del feed de origen filtrado por descuento, no algo que "se descubra"
# progresivamente). Solo se regenera cada EXTENDED_CATALOG_MIN_HOURS (descargar los 7 feeds
# completos de Leroy Merlin tarda ~2 minutos, no es para cada ciclo de 3 horas) -- throttle por
# archivo de estado, mismo patrón que NEW_BADGE_MIN_GAP_HOURS.
EXTENDED_CATALOG_PATH = f"{REPO_DIR}/catalog_extended.json"
EXTENDED_CATALOG_STATE_PATH = f"{HOME}/.rebajas_catalog_extended_state.json"
EXTENDED_CATALOG_MIN_HOURS = 20
EXTENDED_CATALOG_MIN_ITEMS = 500  # red de seguridad: si algo raro pasa y sale casi vacío, no
                                   # se publica (mismo espíritu que MIN_TOTAL_OFFERS de abajo)


def _should_regenerate_extended_catalog():
    try:
        with open(EXTENDED_CATALOG_STATE_PATH) as f:
            last = datetime.fromisoformat(json.load(f)["last_generated"])
    except Exception:
        return True
    return (datetime.now().astimezone() - last).total_seconds() >= EXTENDED_CATALOG_MIN_HOURS * 3600


def _mark_extended_catalog_generated(now_iso):
    try:
        with open(EXTENDED_CATALOG_STATE_PATH, "w") as f:
            json.dump({"last_generated": now_iso}, f)
    except Exception:
        pass

# Aviso por Telegram cuando se suben ofertas nuevas (bot creado 6 ago 2026, ver
# RASPI_REBAJASDIARIAS.md). Sin librerías nuevas: urllib de la stdlib basta para un POST simple.
# Token fuera de git desde el 14 ago 2026 (aviso de GitGuardian, estaba hardcodeado aquí) —
# mismo patrón que FIREBASE_CREDENTIALS_PATH: archivo local fuera del repo, con el token en
# texto plano y nada más.
TELEGRAM_BOT_TOKEN_PATH = f"{HOME}/.rebajas_telegram_bot_token"


def _load_telegram_bot_token():
    try:
        with open(TELEGRAM_BOT_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


TELEGRAM_BOT_TOKEN = _load_telegram_bot_token()
TELEGRAM_CHAT_ID = "1338347086"

# Grupo público "REBAJAS DIARIAS" (supergrupo con Temas/forum activado), añadido 11 ago 2026 a
# petición del usuario para publicar ahí las ofertas más destacadas de la app/web, repartidas
# por tema según categoría. A diferencia de TELEGRAM_CHAT_ID de arriba (aviso privado al
# usuario en cada push), esto es contenido público para los miembros del grupo.
TELEGRAM_GROUP_CHAT_ID = "-1002409782408"
# Mismo criterio que "Ofertas del día"/Flash en la app (ver OffersService.flashOffers /
# dailyDeals en lib/services/offers_service.dart): solo lo más llamativo, no el catálogo entero.
TELEGRAM_DEAL_DISCOUNT_THRESHOLD = 50
# Nada fijo: el usuario pidió explícitamente (11 ago 2026) que los temas del grupo se creen
# solo cuando una categoría tiene ofertas destacadas activas y se BORREN en cuanto se quede sin
# ninguna, en vez de tener un tema permanente por categoría. Este archivo (fuera del repo, local
# a la Pi, mismo patrón que GROUP_STATE_PATH) recuerda qué tema (message_thread_id) hay abierto
# ahora mismo para cada categoría y qué ASINs ya se han publicado en él, para no repetir el
# mismo producto en cada ejecución del cron mientras siga siendo destacado.
TELEGRAM_TOPICS_STATE_PATH = f"{HOME}/.rebajas_telegram_topics.json"
# Únicos 7 valores de icon_color que acepta la Bot API para temas de un forum (paleta fija de
# Telegram, no vale cualquier RGB). Cada categoría recibe siempre el mismo color (hash estable
# del nombre, no random.choice, para que no cambie de un tema borrado/recreado al siguiente) —
# así cada categoría es visualmente distinta y reconocible en la lista de temas del grupo.
TELEGRAM_TOPIC_ICON_COLORS = [
    0x6FB9F0,  # azul
    0xFFD67E,  # amarillo
    0xCB86DB,  # morado
    0x8EEE98,  # verde
    0xFF93B2,  # rosa
    0xFB6F5F,  # rojo
    0xCC46D8,  # naranja/magenta (7º valor válido, visto en la respuesta real de la API)
]
# Tema especial (11 ago 2026, a petición del usuario: "en todas las ofertas de telegram deben
# de estar todas"): a diferencia del resto de temas por categoría (solo lo destacado, ver
# _is_standout_offer), este reúne TODO el catálogo, igual que la pestaña "Todas las Ofertas" de
# la app. No es una categoría real de KEYWORDS_BY_CATEGORY, se trata aparte en
# sync_telegram_group_topics().
TELEGRAM_ALL_OFFERS_CATEGORY = "Todas las Ofertas"

MIN_DISCOUNT_PERCENT = 30
# Tope de sensatez: un descuento calculado por encima de esto casi seguro viene de un "precio
# anterior" inflado por el vendedor (List Price/RRP nunca vendido de verdad), no de una rebaja
# real — un -94% no es creíble. Se descarta la oferta entera en vez de publicarla con un
# descuento que no nos consta que sea genuino (detectado el 7 ago 2026, el usuario vio ofertas así
# en la web y preguntó por qué). NO se aplica a productos realmente gratis (precio 0€, típico de
# libros Kindle en promoción) — ahí un -100% es literal, no un precio de referencia inflado; ver
# el `price > 0` en la condición de más abajo.
MAX_DISCOUNT_PERCENT = 80
KEYWORDS_PER_CATEGORY_RANGE = (1, 2)  # nº de keywords por categoría, aleatorio cada ejecución
                                       # (Pi 3B con 1GB RAM: mejor pocas por ejecución y que la
                                       # fusión por ASIN vaya cubriendo el resto en el tiempo)
CAMPAIGN_KEYWORDS_MAX = 12             # keywords para la categoría en "boost" durante una campaña
                                        # activa (ver _active_campaign_categories) -- barre hasta
                                        # esta cantidad de golpe (antes 3-4 al azar, "un ciclo
                                        # especial" pedido por el usuario para Vuelta al Cole:
                                        # con esto, una categoría de campaña con <=12 keywords
                                        # -- su caso -- se cubre ENTERA en el primer ciclo que
                                        # toque en vez de necesitar varios ciclos al azar; para
                                        # una categoría de campaña más grande, sigue acotado a
                                        # 12 por ciclo, no la barre entera de golpe)
MAX_PRODUCTS_PER_KEYWORD = 8
MIN_TOTAL_OFFERS = 15           # si el catálogo final (tras fusionar) tiene menos, aborta
PAGE_LOAD_TIMEOUT = 75           # medido en la propia Pi 3B: una búsqueda tarda ~50s de media
STALE_AFTER_DAYS = 2             # una oferta que lleva sin verse este tiempo se retira (probablemente
                                  # ya no exista o el descuento haya desaparecido). Antes 10 días,
                                  # bajado a 2 a petición del usuario (sesión 5 ago 2026) — las
                                  # favoritas de alguien no se ven afectadas por esto: siguen vivas
                                  # aparte en watched_prices.json (ver favoritos vigilados más abajo)
INTER_SEARCH_DELAY_RANGE = (4, 15)  # pausa entre cada búsqueda individual dentro de una ejecución
                                     # (antes 1.5-3.5s; más larga = menos parece un bot golpeando
                                     # Amazon a ritmo constante)

# El cron dispara este script varias veces al día, pero arrancar SIEMPRE justo a la hora exacta,
# con el mismo patrón de búsquedas, es justo lo que delataría un bot ante Amazon. Para que cada
# ejecución parezca una sesión de compra normal y no una tarea programada:
#  - se espera un retraso aleatorio antes de tocar nada (JITTER_MAX_MINUTES)
#  - el número de keywords y qué categorías/orden se recorren varía cada vez (ver main())
#  - solo se recorre un subconjunto rotativo de categorías por ejecución, no las 20 (ver
#    CATEGORY_GROUPS / _next_group_index)
JITTER_MAX_MINUTES = 45

# Franja horaria activa: nada de scraping de madrugada (00:00-07:59). El cron ya solo dispara
# dentro de esta franja, pero esta comprobación es un segundo cinturón de seguridad — por si el
# @reboot dispara tras un corte de luz a horas raras, o el jitter empuja el inicio fuera de rango.
ACTIVE_HOUR_START = 8   # inclusive
ACTIVE_HOUR_END = 23    # exclusive (última ejecución posible: 22:xx)

# Las 20 categorías repartidas en 3 grupos, para que cada ejecución solo "toque" 6-7 categorías
# (unas ~10-15 búsquedas) en vez de las 20 (~30) — un golpe más pequeño y menos identificable.
# GROUP_STATE_PATH guarda qué grupo toca a continuación y rota con cada ejecución, así en 3
# ejecuciones (aprox. 1 día con el cron actual) se cubren las 20 categorías igualmente.
CATEGORY_GROUPS = [
    ['Bebés', 'Moda Hombre', 'Moda Mujer', 'Hogar', 'Juguetes', 'Tecnología', 'Mascotas'],
    ['Deporte', 'Gafas de Sol', 'Gaming', 'Música', 'Libros', 'Belleza', 'Alimentación', 'Perfumería'],
    ['Jardín', 'Oficina', 'Salud', 'Automóviles', 'Relojes'],
]
GROUP_STATE_PATH = f"{HOME}/.rebajas_group_state.json"

# Moda Hombre y Moda Mujer son las categorías que más venden (petición del usuario, 15 ago 2026)
# — se buscan en TODOS los ciclos además del grupo rotativo que toque ese turno, en vez de
# aparecer solo 1 de cada 3 ejecuciones como el resto de categorías (ver PRIORITY_CATEGORIES más
# abajo, en main()). No cambia la cadencia de avisos: el push a la app y la publicación en
# Telegram ya saltan en cada ciclo con cambios reales, sea cual sea el grupo — esto solo hace que
# más de esos ciclos traigan ofertas nuevas de estas dos categorías.
PRIORITY_CATEGORIES = ['Moda Hombre', 'Moda Mujer', 'Libros']
CAMPAIGN_PATH_CANDIDATES = ["campaign.json"]  # relativo a REPO_DIR (mismo repo que offers.json)

# Libros gratis (26 ago 2026, pedido explícito del usuario: "te dije que buscar libros gratis
# y todo eso y no tengo ninguno... sigue buscando aparte de las ofertas libros gratis").
# KEYWORDS_BY_CATEGORY['Libros'] son nombres de editorial -- eso nunca saca libros Kindle
# realmente gratis, es pura coincidencia si sale alguno. Comprobado en vivo en amazon.es (26
# ago): buscar "kindle gratis" SÍ saca libros con precio real 0,00€ y un PVP tachado real
# (p.ej. "La chica sola" — 0,00€, PVP ed. digital: 2,99€) -- exactamente lo que exige el filtro
# de descuento de scrape_keyword() (price==0 con original_price>0 real, no fabricado). Se
# añaden SIEMPRE (no sujetos al muestreo aleatorio de 1-2 keywords de KEYWORDS_PER_CATEGORY_RANGE,
# ver el bucle de main()) para que esto no dependa de la suerte del sorteo cada ciclo.
LIBROS_GRATIS_KEYWORDS = ["kindle gratis", "ebook gratis", "libro gratis kindle", "novela gratis kindle"]

# "Que esté siempre el primero" (26 ago 2026, libro de un familiar sugerido por el usuario,
# gratis con Kindle Unlimited -- ver _check_kindle_unlimited_free). Lista corta y visible a
# propósito, no escondida en ningún sitio dentro de la lógica de sugerencias -- añadir aquí un
# id de oferta es lo único que hace falta para fijarlo siempre primero en su sección (web:
# renderCatalog() en js/app.js; app: _buildCategorySections() en offers_screen.dart).
PINNED_OFFER_IDS = {"B0CTYHNVSD"}  # "Pierde el Miedo de Alquilar tu Inmueble" (edición Kindle)

# Favoritos vigilados (sesión 5 ago 2026, ver RASPI_REBAJASDIARIAS.md): la app reporta a
# Firestore, de forma anónima, qué ASINs tiene la gente en favoritos (colección
# watched_offers, ver firestore.rules). Aquí se lee esa lista para seguir comprobando el
# precio de esos productos aunque dejen de salir en las búsquedas normales por categoría y
# caduquen del catálogo público (offers.json). Los resultados van a un archivo aparte
# (watched_prices.json) para no mezclarlos con el filtro de descuento mínimo del catálogo.
WATCHED_PATH = f"{REPO_DIR}/watched_prices.json"
FIREBASE_CREDENTIALS_PATH = f"{HOME}/firebase-service-account.json"
WATCHED_LAST_REPORTED_MAX_DAYS = 20  # ignora entradas de Firestore que nadie renueva ya

# Push de "catálogo actualizado" (11 ago 2026, ver RASPI_REBAJASDIARIAS.md §3.3): la app se
# suscribe sola a este topic al arrancar (lib/services/push_service.dart) — mandar aquí evita
# tener que guardar/gestionar un token por dispositivo. Mismo proyecto/credenciales de Firebase
# que ya se usan arriba para Firestore.
CATALOG_UPDATES_TOPIC = "catalog_updates"
MAX_WATCHED_DIRECT_VISITS_PER_RUN = 15  # límite de visitas directas por ejecución (además de
                                         # las búsquedas normales) — cinturón de seguridad por
                                         # si algún día hay muchos favoritos vigilados a la vez

# Alertas por palabra clave (27 ago 2026, pedido explícito: "si lo que tenemos en alertas lo
# hacemos que entre dentro del ciclo de la Pi"). Hasta ahora notify_keyword_alerts() solo
# comparaba contra lo que YA salía de las búsquedas normales por categoría -- si alguien pone
# una alerta que no coincide con ningún KEYWORDS_BY_CATEGORY existente (ej. "PS5", "freidora de
# aire"), nunca se buscaba de verdad, así que casi nunca podía llegar a coincidir con nada.
# Ahora se buscan de verdad, como una categoría más ("Alertas") -- tope aparte, no sujeto al
# sorteo de 1-2 por categoría normal (aquí interesa cubrir TODAS las alertas activas, no una
# muestra), pero con límite propio para no multiplicar peticiones a Amazon sin control si un
# día hay muchas alertas distintas a la vez.
MAX_ALERT_KEYWORDS_PER_RUN = 20

# "Sugerir una oferta" (26 ago 2026, pedido explícito del usuario: "los usuarios puedan subir
# ofertas y que si realmente es una oferta el enlace se haga de afiliado mio automatico y sino
# que no se publique" — necesita un admin, "eso lleva un admin"). Cualquier usuario logueado
# escribe una URL en Firestore (colección submissions, ver firestore.rules en
# rebajasdiarias-web); aquí, cada ciclo, se visita cada URL pendiente con el mismo Selenium ya
# abierto para el scraping normal, se comprueba el descuento real (mismo umbral que el resto
# del catálogo) y si cualifica se publica con el enlace de afiliado real — igual que cualquier
# otra oferta, mezclada en el mismo new_or_updated. Si no cualifica, NUNCA se publica; se deja
# constancia del motivo en Firestore para que el usuario que la sugirió sepa por qué.
SUBMISSIONS_MAX_PER_CYCLE = 10  # cada una es una visita real a Amazon con Selenium -- tope
                                 # para no alargar demasiado un ciclo normal en una Pi 3B
SUBMISSION_CATEGORY = "Sugerencias"  # solo como último recurso si no se puede identificar la
                                     # categoría real del producto (ver _detect_amazon_category
                                     # más abajo) -- 26 ago 2026, pedido explícito: "aparte de
                                     # lo que el usuario proponga que vaya a sugerencias que
                                     # vaya a su seccion" -- el libro debe estar en "Libros" de
                                     # verdad, "Sugerencias" pasa a ser un distintivo aparte
                                     # (offer['submitted_by'], ver más abajo), no la categoría.

# Primer nivel de las migas de pan de Amazon (#wayfinding-breadcrumbs_feature_div) -> nuestra
# taxonomía de categorías. Comprobado en vivo el 26 ago 2026 contra un caso real ("Libros" tal
# cual). Solo se usa para sugerencias de usuarios -- si no hay coincidencia clara, se cae a
# SUBMISSION_CATEGORY en vez de adivinar mal (mismo principio que el resto del proyecto: nunca
# fabricar algo que no se sabe seguro).
AMAZON_BREADCRUMB_CATEGORY_MAP = {
    "libros": "Libros",
    "electrónica": "Tecnología",
    "informática": "Tecnología",
    "videojuegos": "Gaming",
    "hogar y cocina": "Hogar",
    "bricolaje, construcción y herramientas": "Bricolaje",
    "jardín": "Jardín",
    "salud y cuidado personal": "Salud",
    "belleza": "Belleza",
    "juguetes y juegos": "Juguetes",
    "bebé": "Bebés",
    "deportes y aire libre": "Deporte",
    "coche y moto": "Automóviles",
    "alimentación y bebidas": "Alimentación",
    "instrumentos musicales": "Música",
    "oficina y papelería": "Oficina",
    "productos para mascotas": "Mascotas",
    "relojes": "Relojes",
    "equipaje": "Viajes",
}
# "Ropa, calzado y joyería" -> ambiguo sin más contexto (Moda Hombre/Mujer/Gafas de Sol...) --
# se mira el SEGUNDO nivel de la miga de pan para desambiguar en vez de adivinar a ciegas.
AMAZON_BREADCRUMB_GENDER_MAP = {
    "hombre": "Moda Hombre",
    "mujer": "Moda Mujer",
}


def _detect_amazon_category(driver):
    """Intenta identificar la categoría real del producto ya cargado en `driver` a partir de
    sus migas de pan de Amazon. Devuelve el nombre de nuestra categoría si hay una
    correspondencia razonablemente segura, o None si no (mejor no adivinar que adivinar mal).
    Nunca lanza."""
    try:
        segments = driver.execute_script(
            "var el = document.querySelector('#wayfinding-breadcrumbs_feature_div');"
            "if (!el) return null;"
            "return Array.from(el.querySelectorAll('a, .a-color-state')).map("
            "  function(n) { return n.textContent.trim(); }"
            ").filter(Boolean);"
        )
        if not segments:
            return None
        top = segments[0].strip().lower()
        if top.startswith("ropa, calzado y joyería") or top == "moda":
            for segment in segments[1:3]:
                seg_lower = segment.strip().lower()
                for keyword, category in AMAZON_BREADCRUMB_GENDER_MAP.items():
                    if keyword in seg_lower:
                        return category
            return None  # ambiguo, no se adivina
        return AMAZON_BREADCRUMB_CATEGORY_MAP.get(top)
    except Exception:
        return None
SUBMISSION_ALLOWED_HOSTS = {"amazon.es", "www.amazon.es"}  # solo Amazon en esta primera
                                                            # versión -- Leroy Merlin/Perfumería
                                                            # Comas se podrían añadir más
                                                            # adelante comprobando contra sus
                                                            # feeds de Awin en vez de scraping

# Rastro local de qué oferta viva viene de qué sugerencia aprobada -- {offer_id: doc_id} en
# submission_offers.json (mismo repo, mismo patrón que watched_prices.json). Necesario para
# "que ellos las puedan eliminar" (26 ago 2026): el usuario borra su propia sugerencia desde la
# web/app en cualquier momento (firestore.rules ya lo permite), pero borrar el documento de
# Firestore no toca offers.json solo -- este archivo es lo que le permite a la Pi, en el
# siguiente ciclo, notar que el documento ya no existe y retirar esa oferta del catálogo
# también (ver _reconcile_deleted_submissions()).
SUBMISSION_OFFERS_PATH = f"{REPO_DIR}/submission_offers.json"

# Mismas categorías y keywords que lib/categorias.json del proyecto Flutter.
# IMPORTANTE: si cambian ahí, cambiar también aquí (los nombres de categoría deben ser
# EXACTOS o la app no filtra bien). 'Todas las Ofertas' no se busca aparte, es un
# agregado de todas las demás dentro de la propia app.
KEYWORDS_BY_CATEGORY = {
    'Bebés': [
        "Chicco bebé", "Nuk bebé", "Bebeconfort", "Cybex bebé", "Jané bebé",
        "Philips Avent", "Tommee Tippee", "Pampers", "Dodot", "Suavinex",
        "Munchkin bebé", "Fisher-Price bebé",
    ],
    'Moda Hombre': [
        "zapatillas de hombre", "polos de hombre", "Lacoste de hombre",
        "converse de hombre", "Lee de hombre", "Lois de hombre",
        "bañador de hombre", "camisetas de hombre",
        "Tommy Hilfiger de hombre", "Moschino de hombre", "Scalpers de hombre",
        "G-Star de hombre", "New balance de hombre", "Puma de hombre",
        "Calvin klein de hombre", "The north face de hombre", "Reebok de hombre",
        "Nike de hombre", "Adidas de hombre", "Diesel de hombre", "Geox de hombre",
        "Pepe Jeans de hombre",
    ],
    'Moda Mujer': [
        "zapatillas de mujer", "polos de mujer", "converse de mujer",
        "Lee de mujer", "Lois de mujer", "bikini de mujer", "bañador de mujer",
        "camisetas de mujer", "bolso bimba y lola mujer",
        "Tous", "Nike de mujer", "Adidas de mujer", "Tommy Hilfiger de mujer",
        "New balance de mujer", "Desigual de mujer", "Calvin klein de mujer",
        "Roxy de mujer", "Geox de mujer", "Women´secret de mujer",
        "Springfield de mujer", "Diesel de mujer", "Moschino de mujer",
        "Pepe Jeans de mujer",
    ],
    'Hogar': [
        "Philips hogar", "Bosch electrodomésticos", "Balay", "Moulinex", "Tefal",
        "Rowenta", "Cecotec", "Karcher", "Dyson", "Samsung electrodomésticos",
        "LG electrodomésticos", "Braun hogar",
    ],
    'Juguetes': [
        "LEGO", "Playmobil", "Fisher-Price", "Barbie", "Hot Wheels", "Nerf",
        "Hasbro", "Mattel", "Disney", "Pop! Funko", "Clementoni", "Famosa",
        "Bandai Namco",
    ],
    'Tecnología': [
        "Samsung tecnología", "Xiaomi", "Apple accesorios", "Sony electrónica",
        "HP portátiles", "Lenovo", "Asus", "Logitech", "JBL tecnología", "Bose",
        "Huawei", "LG tecnología",
    ],
    'Mascotas': [
        "Purina", "Royal Canin", "Hill's mascotas", "Pedigree", "Whiskas",
        "Kong perro", "Trixie mascotas", "Ferplast", "Advance mascotas",
    ],
    'Deporte': [
        "Nike deporte", "Adidas deporte", "Puma fitness", "Under Armour",
        "Reebok deporte", "New Balance running", "Salomon outdoor",
        "Asics running", "Wilson deportes", "Columbia outdoor",
    ],
    'Gafas de Sol': [
        "gafas de sol Ray-Ban", "gafas de sol hawkers", "gafas de sol Versace",
        "gafas de sol Carolina Herrera", "gafas de sol Oakley",
        "gafas de sol Guess", "gafas de sol Lacoste",
        "gafas de sol Emporio Armani", "gafas de sol Vogue",
        "gafas de sol Polaroid", "gafas de sol Prada",
    ],
    'Gaming': [
        "PS5 Sony", "Xbox Series X Microsoft", "Nintendo Switch", "Razer gaming",
        "Logitech G gaming", "HyperX gaming", "SteelSeries", "Corsair gaming",
    ],
    'Música': [
        "JBL altavoces", "Bose audio", "Sony auriculares", "Fender guitarras",
        "Yamaha música", "Marshall altavoces", "Sennheiser auriculares",
        "Shure micrófonos", "Roland pianos digitales",
    ],
    'Libros': [
        "Planeta libros", "Penguin Random House", "Alfaguara", "Anaya libros",
        "Santillana", "Salamandra libros", "SM libros", "DeBolsillo",
    ],
    'Belleza': [
        "L'Oréal", "Nivea", "Maybelline", "NYX cosmetics", "Garnier", "Revlon",
        "La Roche-Posay", "CeraVe", "Neutrogena",
    ],
    # Perfumería (26 ago 2026, pedido explícito: "en perfumeria solo salen de comas también
    # hay perfumes en amazon que están en moda") -- "perfume de hombre"/"perfume de mujer"
    # vivían en Moda Hombre/Moda Mujer, así que sus resultados se etiquetaban con esa categoría
    # en vez de "Perfumería" (que hasta ahora solo tenía productos de Perfumería Comas, ver
    # multitienda_feeds.py). Movidos aquí para que Amazon y Comas compartan categoría de
    # verdad.
    'Perfumería': [
        "perfume de hombre", "perfume de mujer", "colonia de hombre", "eau de parfum mujer",
        "Carolina Herrera perfume", "Paco Rabanne perfume", "Giorgio Armani perfume",
        "Hugo Boss perfume", "Calvin Klein perfume", "Lancôme perfume",
    ],
    'Alimentación': [
        "Nestlé", "Danone", "Central Lechera Asturiana", "Gullón galletas",
        "Cola Cao", "Lindt chocolate", "Ferrero", "Pascual",
    ],
    'Jardín': [
        "Gardena jardín", "Bosch jardín", "Black+Decker jardín", "Fiskars",
        "McCulloch", "Hozelock riego",
    ],
    'Oficina': [
        "HP impresoras", "Epson", "Canon impresoras", "BIC", "Stabilo",
        "Pilot bolígrafos", "Faber-Castell", "Leitz oficina",
    ],
    'Salud': [
        "Braun salud", "Omron tensiómetro", "Beurer", "Compeed",
        "Centrum vitaminas", "Vicks",
    ],
    # Categoria 'Viajes' quitada del todo (21 ago, peticion del usuario:
    # "que no busque mas maletas de amazon, no se venden") - eran solo
    # marcas de maletas (Samsonite, American Tourister, Delsey, Eastpak,
    # Antler, Travelite), sin ninguna venta real pese a salir a menudo con
    # descuentos altos. Las que ya estan en offers.json se iran cayendo
    # solas por antiguedad (STALE_AFTER_DAYS), no se borran a mano.
    'Automóviles': [
        "Bosch coche", "Michelin", "Osram automoción", "Philips coche", "Sparco",
        "Thule", "Continental neumáticos",
    ],
    'Relojes': [
        "Casio relojes", "Citizen relojes", "Seiko relojes", "Fossil relojes",
        "Garmin smartwatch", "Amazfit", "Michael Kors relojes", "Lotus relojes",
        "Viceroy relojes",
    ],
    # Vuelta al Cole (25 ago 2026, pedido explícito del usuario: "que vaya actualizando también
    # de vuelta al cole... y creariamos esa categoria") -- categoria ESTACIONAL, activada vía
    # campaign.json (categoryTarget, ver _active_campaign_categories) en vez de vivir en
    # CATEGORY_GROUPS/PRIORITY_CATEGORIES: mientras la campaña esté activa se refuerza en TODOS
    # los ciclos, hasta CAMPAIGN_KEYWORDS_MAX keywords de golpe (con las 18 de aquí, cubre casi
    # todas en el primer ciclo, el resto en el segundo). Al desactivar la campaña, deja de
    # buscarse y las ofertas ya encontradas se retiran solas por antigüedad
    # (STALE_AFTER_DAYS), sin dejar una categoría "fantasma" el resto del año.
    # Ampliada el mismo día (25 ago, aviso real del usuario tras ver el primer barrido: "pero
    # casi todo son mochilas debería haber más cosas") -- las 4 keywords de mochila (Safta/
    # Totto/Movom/genérica) por sí solas ya daban 12 de las 25 ofertas encontradas, el resto de
    # categoría escolar necesitaba más variedad de keywords propias para competir en las
    # búsquedas, no solo "material escolar"/"estuche escolar" genéricos. Evita solaparse
    # demasiado con 'Oficina' (BIC/Stabilo/Pilot/Faber-Castell genéricos) usando siempre el
    # matiz "escolar"/"infantil".
    'Vuelta al Cole': [
        "mochila escolar", "Safta mochila", "Totto mochila", "Movom mochila",
        "estuche escolar", "material escolar", "agenda escolar",
        "cuaderno escolar", "libreta escolar", "carpeta escolar",
        "rotuladores escolares", "lápices de colores", "calculadora científica",
        "Milan escolar", "Liderpapel", "Pelikan escolar", "Faber-Castell escolar",
        "fiambrera infantil",
    ],
}


def log(msg):
    print(f"[update_offers] {msg}", flush=True)


def notify_telegram(msg):
    """Manda un mensaje al Telegram del usuario. Nunca debe tumbar el script si falla (p.ej.
    sin internet en ese momento) — se registra el error en el log y se sigue sin más."""
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            r.read()
    except Exception as e:
        log(f"aviso: no se pudo notificar por Telegram: {e}")


def _telegram_api(method, params, _retried=False):
    """Llama a un método cualquiera de la Bot API de Telegram. Nunca lanza: cualquier fallo de
    red/API se registra y se devuelve None, igual que notify_telegram(). Si Telegram responde
    429 (demasiadas peticiones — visto en la primera prueba en vivo del 11 ago 2026 al crear 16
    temas y mandar ~96 mensajes seguidos), espera el `retry_after` que indica la propia API y
    reintenta UNA vez antes de rendirse."""
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


def _html_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_standout_offer(offer):
    """Mismo criterio que 'Ofertas del día'/Flash en la app — ver
    OffersService.flashOffers/dailyDeals en lib/services/offers_service.dart. Si ese criterio
    cambia ahí, cambiarlo también aquí para que el grupo de Telegram muestre lo mismo que la app."""
    return bool(offer.get("is_flash")) or offer.get("discount_percent", 0) > TELEGRAM_DEAL_DISCOUNT_THRESHOLD


def _load_telegram_topics_state():
    try:
        with open(TELEGRAM_TOPICS_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_telegram_topics_state(state):
    try:
        with open(TELEGRAM_TOPICS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  aviso: no se pudo guardar el estado de temas de Telegram: {e}")


def _discount_badge(discount):
    """Cuadrito de color según lo fuerte que sea el descuento — Telegram no permite texto en
    color de verdad en captions, así que el "color" se consigue con estos emoji (a petición del
    usuario, 11 ago 2026: "que llame la atención" al entrar al grupo)."""
    if discount >= 70:
        return "🔴"
    if discount >= 60:
        return "🟠"
    return "🟡"


def _send_telegram_offer(offer, thread_id):
    """Publica un producto destacado en el tema (message_thread_id) de su categoría, con foto
    si hay imagen disponible (si sendPhoto falla, se reintenta como mensaje de texto normal)."""
    title = _html_escape(offer.get("title", ""))
    price = offer.get("price")
    original_price = offer.get("original_price")
    discount = offer.get("discount_percent", 0)
    url = offer.get("url", "")
    image = offer.get("image", "")
    header = "⚡️ CHOLLO FLASH" if offer.get("is_flash") else "🎯 OFERTA DEL DÍA"
    badge = _discount_badge(discount)
    # Precio y precio anterior tachado en la MISMA línea (no uno debajo del otro): con el emoji
    # de color delante, dos líneas seguidas no quedaban alineadas porque el tachado no lleva
    # emoji propio (visto en la primera prueba en vivo, 11 ago 2026) — en una sola línea no
    # depende de que el emoji mida igual en cada móvil.
    price_line = ""
    if isinstance(price, (int, float)):
        price_line = f"{badge} <b>{price:.2f}€</b>"
        if isinstance(original_price, (int, float)) and original_price > price:
            price_line += f"  <s>{original_price:.2f}€</s>"
        price_line += f"  (-{discount}%)"
    # Formato "tarjeta" (a petición del usuario, 11 ago 2026: que llame la atención y se vea
    # "estilo profesional"): cabecera destacada, producto, precio y por último el botón.
    # Boton real de Telegram (inline keyboard) en vez de enlace de texto enmascarado dentro
    # del caption — a petición del usuario (14 ago 2026, probado primero con las ofertas de
    # Stylevana): se ve más "de app" que un link suelto. El separador de antes ya no hace
    # falta: el botón queda visualmente aparte del texto solo, es Telegram quien lo pinta
    # debajo del mensaje.
    caption = (
        f"<b>{header}</b>\n\n"
        f"🛍 <b>{title}</b>\n\n"
        f"{price_line}"
    )
    # store_label solo existe en ofertas que no son de Amazon (multi-tienda, ver
    # multitienda_feeds.py) — si no está, es Amazon, mismo texto de siempre.
    store_label = offer.get("store_label") or "Amazon"
    reply_markup = json.dumps({
        "inline_keyboard": [[{"text": f"👉 Ver oferta en {store_label}", "url": url}]]
    })

    params = {
        "chat_id": TELEGRAM_GROUP_CHAT_ID,
        "message_thread_id": thread_id,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if image:
        result = _telegram_api("sendPhoto", {**params, "photo": image, "caption": caption})
        if result and result.get("ok"):
            return True
        log(f"  aviso: sendPhoto falló para {offer.get('id')}, se prueba como texto")
    result = _telegram_api("sendMessage", {**params, "text": caption})
    return bool(result and result.get("ok"))


def sync_telegram_group_topics(all_offers):
    """Sincroniza los temas del grupo público de Telegram con las ofertas destacadas
    actuales (ver _is_standout_offer): crea el tema de una categoría en cuanto tiene alguna
    oferta destacada y lo BORRA en cuanto se queda sin ninguna — nada permanente, a petición
    del usuario (11 ago 2026). Nunca debe tumbar el script si algo falla aquí."""
    try:
        state = _load_telegram_topics_state()
        by_category = {}
        for offer in all_offers:
            if _is_standout_offer(offer):
                by_category.setdefault(offer.get("category") or "Otros", []).append(offer)
        # "Todas las Ofertas": el catálogo completo, no solo lo destacado (ver constante arriba).
        by_category[TELEGRAM_ALL_OFFERS_CATEGORY] = list(all_offers)

        for category in set(KEYWORDS_BY_CATEGORY) | {TELEGRAM_ALL_OFFERS_CATEGORY} | set(state) | set(by_category):
            current = by_category.get(category, [])
            current_ids = {o["id"] for o in current if o.get("id")}
            entry = state.get(category) or {}
            thread_id = entry.get("thread_id")
            posted = set(entry.get("posted", []))

            if not current:
                if thread_id:
                    _telegram_api(
                        "deleteForumTopic",
                        {"chat_id": TELEGRAM_GROUP_CHAT_ID, "message_thread_id": thread_id},
                    )
                    log(f"Telegram: tema '{category}' borrado (sin ofertas destacadas).")
                state.pop(category, None)
                continue

            if not thread_id:
                # Color estable por categoría (no aleatorio, ver TELEGRAM_TOPIC_ICON_COLORS) +
                # emoji en el propio nombre — para que se note nada más entrar al grupo (a
                # petición del usuario, 11 ago 2026).
                icon_color = TELEGRAM_TOPIC_ICON_COLORS[
                    zlib.crc32(category.encode()) % len(TELEGRAM_TOPIC_ICON_COLORS)
                ]
                result = _telegram_api(
                    "createForumTopic",
                    {
                        "chat_id": TELEGRAM_GROUP_CHAT_ID,
                        "name": f"🔥 {category}",
                        "icon_color": icon_color,
                    },
                )
                if not result or not result.get("ok"):
                    log(f"  aviso: no se pudo crear el tema de Telegram para '{category}'")
                    continue
                thread_id = result["result"]["message_thread_id"]
                posted = set()
                log(f"Telegram: tema '{category}' creado (id {thread_id}).")
                time.sleep(2.5)  # margen antes de empezar a publicar en el tema recién creado

            for offer in current:
                if not offer.get("id") or offer["id"] in posted:
                    continue
                if _send_telegram_offer(offer, thread_id):
                    posted.add(offer["id"])
                else:
                    log(f"  aviso: no se pudo publicar {offer['id']} en '{category}', se "
                        f"reintentará en la próxima ejecución")
                time.sleep(2.5)  # margen frente a los límites de la Bot API (ver _telegram_api)

            state[category] = {"thread_id": thread_id, "posted": sorted(posted & current_ids)}

        _save_telegram_topics_state(state)
    except Exception as e:
        log(f"  aviso: fallo sincronizando temas de Telegram: {e}")


def _next_group_index():
    """Lee y avanza el índice del grupo de categorías rotativo (ver CATEGORY_GROUPS).
    Se guarda en disco para que la rotación sobreviva a reinicios/cortes de luz."""
    idx = 0
    try:
        with open(GROUP_STATE_PATH, encoding="utf-8") as f:
            idx = json.load(f).get("next", 0) % len(CATEGORY_GROUPS)
    except Exception:
        idx = 0
    try:
        with open(GROUP_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"next": (idx + 1) % len(CATEGORY_GROUPS)}, f)
    except Exception as e:
        log(f"  aviso: no se pudo guardar el estado de rotación de grupos: {e}")
    return idx


def _active_campaign_categories():
    """campaign.json (mismo repo que offers.json, ya actualizado por el 'git pull' de main())
    -- esquema con VARIAS campañas a la vez desde el 25 ago 2026 ({"campaigns": [ {...}, {...}
    ]}), antes un único objeto: "Vuelta al Cole" y "Vuelta al Hogar" tienen fechas que se
    solapan (15 sept / 28 sept), hacía falta poder tener las dos activas de golpe. Devuelve la
    lista de categoryTarget de las campañas activas/dentro de fecha que tengan uno (storeTarget,
    como el de "Vuelta al Hogar" -> Leroy Merlin entero, no necesita refuerzo de keywords aquí:
    ese multi-tienda ya se descarga entero cada ciclo, sin búsqueda por palabra clave de por
    medio). Nunca lanza: sin campañas o con el JSON mal formado, lista vacía, sin boost."""
    for rel_path in CAMPAIGN_PATH_CANDIDATES:
        path = os.path.join(REPO_DIR, rel_path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            campaigns = data.get("campaigns")
            if not isinstance(campaigns, list):
                return []
            now = datetime.now()
            categories = []
            for campaign in campaigns:
                if not isinstance(campaign, dict) or campaign.get("active") is not True:
                    continue
                category = campaign.get("categoryTarget")
                if not category:
                    continue
                start = campaign.get("startDate")
                end = campaign.get("endDate")
                if start and now < datetime.fromisoformat(start):
                    continue
                if end and now > datetime.fromisoformat(end) + timedelta(days=1):
                    continue
                categories.append(category)
            return categories
        except Exception as e:
            log(f"  aviso: no se pudo leer campaign.json: {e}")
            return []
    return []


def _fetch_watched_asins():
    """Devuelve el conjunto de ASINs que alguien tiene AHORA MISMO en favoritos, según
    Firestore (colección watched_offers, cuenta anónima — ver firestore.rules y
    lib/services/favorites_watch_service.dart de la app). Devuelve None (no un conjunto
    vacío) si no se ha podido consultar Firestore en absoluto — así el resto del script sabe
    distinguir "nadie tiene nada vigilado" de "no se ha podido preguntar" y no toca
    watched_prices.json en este segundo caso. Nunca lanza: sin credenciales, sin red o
    cualquier fallo aquí no debe afectar al catálogo público."""
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        cutoff = datetime.now(timezone.utc) - timedelta(days=WATCHED_LAST_REPORTED_MAX_DAYS)
        asins = set()
        query = db.collection("watched_offers").where(filter=FieldFilter("count", ">", 0))
        for doc in query.stream():
            data = doc.to_dict() or {}
            last_reported = data.get("lastReported")
            if last_reported is not None and last_reported < cutoff:
                continue  # nadie lo ha renovado en mucho tiempo, se trata como abandonado
            asins.add(doc.id)
        return asins
    except Exception as e:
        log(f"  aviso: no se pudo consultar Firestore de favoritos vigilados: {e}")
        return None


def _fetch_alert_keywords():
    """Palabras clave que algún usuario tiene guardadas en users/{uid}.keywordAlerts (27 ago
    2026) -- se buscan de verdad cada ciclo (ver run_categories['Alertas'] en main()), no solo
    se comparan contra lo que ya salió por las categorías normales, para que una alerta que no
    coincide con ningún KEYWORDS_BY_CATEGORY existente tenga opción real de aparecer alguna vez.
    Deduplicada sin importar mayúsculas (misma palabra escrita distinto por dos usuarios cuenta
    una vez). Nunca lanza: sin credenciales, sin red o cualquier fallo aquí no debe afectar al
    resto del ciclo."""
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return []
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        seen_lower = set()
        keywords = []
        for doc in db.collection("users").stream():
            for kw in (doc.to_dict() or {}).get("keywordAlerts") or []:
                kw = kw.strip()
                if kw and kw.lower() not in seen_lower:
                    seen_lower.add(kw.lower())
                    keywords.append(kw)
        return keywords
    except Exception as e:
        log(f"  aviso: no se pudo consultar Firestore de alertas de usuarios: {e}")
        return []


def notify_app_push(all_offers):
    """Manda un push de Firebase Cloud Messaging al topic al que se suscribe la app (ver
    lib/services/push_service.dart) — solo se llama tras un commit/push real a git, nunca en
    ciclos sin cambios (11 ago 2026, ver RASPI_REBAJASDIARIAS.md §3.3: el usuario pidió que
    avisara "cuando actualice las ofertas, para que la gente no se las pierda"). Nunca debe
    tumbar el script si falla, igual que notify_telegram()."""
    # Log de entrada incondicional (12 ago) — el ciclo del 12 ago 08:18 no dejó
    # ningún rastro de esta función (ni éxito ni error), pese a que probado a mano
    # en la Pi con el mismo entorno (venv) funcionaba bien — con este log al menos
    # queda claro si la función llegó a entrar en algún momento, para diagnosticar
    # si vuelve a pasar.
    log("Intentando enviar push de catálogo actualizado a la app...")
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        log("  aviso: no se encuentra el archivo de credenciales de Firebase, se omite el push")
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)

        flash_count = sum(1 for o in all_offers if o.get("is_flash"))
        daily_count = sum(1 for o in all_offers if (o.get("discount_percent") or 0) > 50)
        if flash_count > 0:
            body = (
                f"🔥 {flash_count} ofertas flash o con más del 50% de descuento, "
                "recién actualizadas."
            )
        elif daily_count > 0:
            body = f"📦 {daily_count} ofertas con más del 50% de descuento, recién actualizadas."
        else:
            body = f"📦 Catálogo actualizado: {len(all_offers)} ofertas activas."

        message = messaging.Message(
            notification=messaging.Notification(title="¡Catálogo actualizado! 🛍️", body=body),
            topic=CATALOG_UPDATES_TOPIC,
        )
        messaging.send(message)
        log("Push de catálogo actualizado enviado a la app.")
    except Exception as e:
        log(f"aviso: no se pudo enviar el push del catálogo a la app: {e}")


def notify_submitter_push(uid, offer):
    """Aviso personal al autor de una sugerencia en cuanto se publica (26 ago 2026, pedido
    explícito: "que si alguien ha publicado una oferta que salte una notificación") --
    aparte del push genérico de notify_app_push() de arriba, que es para todo el mundo. Mismo
    mecanismo de topics de Firebase Cloud Messaging que ya usa esa función, sin gestionar
    tokens por dispositivo: cada usuario logueado se suscribe solo a "user_<uid>" (ver
    PushService en la app), así que basta con mandar el mensaje a ese topic -- ni siquiera hay
    que saber en qué dispositivo está. Nunca debe tumbar _process_submissions() si falla."""
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)

        message = messaging.Message(
            notification=messaging.Notification(
                title="¡Tu oferta ya está publicada! 🎉",
                body=f"{offer['title'][:80]} — {offer['discount_percent']}% de descuento",
            ),
            topic=f"user_{uid}",
        )
        messaging.send(message)
        log(f"  push personal enviado al autor de la sugerencia ({uid}).")
    except Exception as e:
        log(f"  aviso: no se pudo enviar el push personal al autor: {e}")


def notify_favorite_price_changes(price_changes, merged):
    """Avisa por push a quien tenga la campana de precio activada en un favorito cuyo precio
    ha cambiado este ciclo, si la dirección coincide con lo que pidió ('down', 'up' o 'both')
    -- ver la campana de favoritos_page.dart y el documento users/{uid}.notifications en
    Firestore (favorites_service.dart). Hasta ahora esa preferencia se guardaba pero nada la
    leía nunca (27 ago 2026, pedido explícito del usuario tras revisar mejoras pendientes).
    Mismo mecanismo de topics que notify_submitter_push(): cada usuario logueado está
    suscrito a "user_<uid>", así que basta con mandar el mensaje a ese topic, sin gestionar
    tokens por dispositivo. Nunca debe tumbar main() si falla."""
    if not price_changes:
        return
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, messaging

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()

        sent = 0
        for doc in db.collection("users").stream():
            data = doc.to_dict() or {}
            # Interruptor general (27 ago 2026, ver favorites_service.dart) -- apaga TODOS los
            # avisos de precio en favoritos de este usuario sin tocar las campanas puestas
            # producto a producto, que se quedan tal cual para cuando lo vuelva a activar.
            if data.get("favoriteAlertsEnabled") is False:
                continue
            notifications = data.get("notifications") or {}
            if not notifications:
                continue
            uid = doc.id
            for asin, wanted in notifications.items():
                direction = price_changes.get(asin)
                if direction is None or (wanted != "both" and wanted != direction):
                    continue
                offer = merged.get(asin)
                if offer is None:
                    continue
                emoji, verb = ("📉", "ha bajado") if direction == "down" else ("📈", "ha subido")
                try:
                    messaging.send(messaging.Message(
                        notification=messaging.Notification(
                            title=f"{emoji} Precio {verb} en un favorito",
                            body=f"{offer['title'][:80]} — ahora {offer['price']} €",
                        ),
                        topic=f"user_{uid}",
                    ))
                    sent += 1
                except Exception as e:
                    log(f"  aviso: no se pudo avisar a {uid} de {asin}: {e}")
        if sent:
            log(f"  {sent} avisos de cambio de precio en favoritos enviados.")
    except Exception as e:
        log(f"  aviso: no se pudo comprobar cambios de precio en favoritos: {e}")


def notify_keyword_alerts(brand_new_asins, merged):
    """Avisa por push cuando aparece una oferta NUEVA de verdad (no vista en ningún ciclo
    anterior) cuyo título coincide con alguna palabra clave que un usuario tenga guardada en
    users/{uid}.keywordAlerts (27 ago 2026, pedido explícito: "que puedan ir agregando varias
    palabras... que les pueda avisar de varias ofertas"). Mismo mecanismo de topics que el
    resto de pushes personales -- topic "user_<uid>". Solo mira brand_new_asins (nunca lo
    re-scrapeado) para no repetir el aviso cada ciclo mientras la oferta siga viva -- una
    persona con la alerta "nike" puesta debe enterarse UNA vez por oferta nueva, no cada 3h
    mientras siga en el catálogo. Nunca debe tumbar main() si falla."""
    if not brand_new_asins:
        return
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, messaging

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()

        new_offers = [merged[a] for a in brand_new_asins if a in merged]
        sent = 0
        for doc in db.collection("users").stream():
            data = doc.to_dict() or {}
            # Interruptor general (27 ago 2026, ver alerts_service.dart) -- apaga TODAS las
            # alertas de este usuario sin borrar las palabras guardadas, que se quedan tal cual
            # para cuando lo vuelva a activar.
            if data.get("keywordAlertsEnabled") is False:
                continue
            keywords = [k.strip().lower() for k in data.get("keywordAlerts") or [] if k.strip()]
            if not keywords:
                continue
            uid = doc.id
            matches = [
                offer for offer in new_offers
                if any(kw in offer.get("title", "").lower() for kw in keywords)
            ]
            if not matches:
                continue
            if len(matches) == 1:
                body = f"{matches[0]['title'][:80]} — {matches[0]['price']} €"
            else:
                body = f"{len(matches)} ofertas nuevas coinciden con tus alertas"
            try:
                messaging.send(messaging.Message(
                    notification=messaging.Notification(
                        title="🔔 Nueva oferta de tu alerta",
                        body=body,
                    ),
                    topic=f"user_{uid}",
                ))
                sent += 1
            except Exception as e:
                log(f"  aviso: no se pudo avisar a {uid} de sus alertas: {e}")
        if sent:
            log(f"  {sent} avisos de alertas de palabra clave enviados.")
    except Exception as e:
        log(f"  aviso: no se pudo comprobar alertas de palabra clave: {e}")


def build_driver():
    options = Options()
    options.binary_location = CHROMIUM_PATH
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1024,1400")
    options.add_argument("--lang=es-ES")
    options.add_argument("--blink-settings=imagesEnabled=false")  # no gastar RAM/CPU pintando fotos
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
    )
    # Doble bloqueo de imágenes (flag de arriba + preferencia) — en una Pi 3B con 1GB de RAM,
    # pintar las rejillas de fotos de Amazon es lo que más tardaba y acababa provocando timeouts
    # y hasta el crash del propio chromedriver. Solo necesitamos el texto (precio/título) y la
    # URL de la imagen, que sigue estando en el HTML aunque no se descargue/pinte el píxel real.
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2}
    )
    options.page_load_strategy = "eager"  # no esperar a que carguen todos los subrecursos
    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def parse_price(text):
    if not text:
        return None
    text = text.replace("\xa0", " ").strip()
    m = re.search(r"([\d.,]+)", text)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


# Valoración con estrellas (27 ago 2026, pedido explícito del usuario: "las estrellas de
# valoración"). rating_text es el textContent del contenedor de la valoración tal cual lo
# manda _EXTRACT_CARDS_JS -- algo como "4,84,8 de 5 estrellas (125)" (el número visible +
# el texto de accesibilidad + el nº de reseñas entre paréntesis, todo junto porque se coge el
# texto plano del div contenedor entero, no de un span suelto -- más robusto que depender de
# que un selector concreto siga existiendo tal cual si Amazon cambia el marcado interno).
def parse_rating(text):
    if not text:
        return None
    # OJO: un solo `\d` en cada grupo, no `\d+` (bug real probando esto a mano, 27 ago 2026) --
    # el número visible ("4,8") va PEGADO sin espacio justo delante del texto de accesibilidad
    # ("4,8 de 5 estrellas"), ej. "4,84,8 de 5 estrellas". Con `\d+` (varios dígitos) el primer
    # grupo se comía el "8" del número visible Y el "4" del texto de accesibilidad juntos ("84"),
    # dando una valoración inventada de 84,8 en vez de 4,8. Como la nota siempre es de un dígito
    # entero (0-5) + un decimal, un solo `\d` por grupo es exacto y evita el problema.
    m = re.search(r"(\d),(\d)\s*de\s*5\s*estrellas", text)
    if not m:
        return None
    try:
        return round(float(f"{m.group(1)}.{m.group(2)}"), 1)
    except ValueError:
        return None


def parse_rating_count(text):
    if not text:
        return None
    m = re.search(r"\(([\d.,]+)\)\s*$", text.strip())
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", "").replace(",", ""))
    except ValueError:
        return None


# Extrae todas las tarjetas de resultado de una vez con JS: en una Pi 3B, hacer una consulta
# Selenium por cada campo de cada tarjeta (varios find_element por tarjeta x ~60 tarjetas)
# suponía más de 2 minutos por búsqueda solo en ida-y-vuelta del protocolo WebDriver. Con un
# único execute_script se hace todo dentro del navegador y se recibe ya el resultado final.
_EXTRACT_CARDS_JS = """
return Array.from(document.querySelectorAll(
  'div[data-component-type="s-search-result"][data-asin]'
)).map(function(card) {
  function text(sel) {
    var el = card.querySelector(sel);
    return el ? el.textContent : null;
  }
  // OJO: antes era 'h2 span' (solo el primer span dentro del h2) — cuando el
  // título tiene varios spans anidados (marca en uno, resto en otro) eso
  // capturaba solo la marca ("NIKE" a secas), no el título completo. Detectado
  // el 17 ago 2026: el 56% del catálogo tenía títulos de 1-2 palabras por esto,
  // rompiendo la búsqueda de la web (buscar "zapatillas" solo encontraba 1
  // resultado real). 'h2' a secas junta el texto de todos los spans internos.
  var titleEl = card.querySelector('h2');
  var imgEl = card.querySelector('img.s-image');
  var flashEl = card.querySelector(".s-coupon-highlight-color, [aria-label*='Flash' i]");
  // 26 ago 2026, variante del mismo bug detectada por el usuario ("se van repitiendo las
  // ofertas muchas veces"): en ciertas tarjetas (vistas en búsquedas por marca, ej. "gafas de
  // sol Tommy Hilfiger") el propio <h2> SOLO contiene la marca de verdad, sin truncar nada --
  // "h2 a secas" ya no basta porque el resto del título ni está dentro del h2. El alt del
  // img.s-image sí trae siempre el nombre completo real del producto (Amazon lo usa para
  // accesibilidad, más fiable que el texto visible en estas tarjetas) -- se usa cuando es más
  // largo que el del h2, dejando el h2 como estaba para el resto de tarjetas donde ya es
  // completo.
  var h2Text = titleEl ? titleEl.textContent.trim() : '';
  // El alt de los resultados patrocinados empieza con "Anuncio patrocinado: " (etiqueta de
  // accesibilidad de Amazon, no parte del nombre del producto) -- se quita antes de usarlo,
  // si no se cuela tal cual en el título ("Anuncio patrocinado: Adidas Hombre Run 70S 2.0").
  var imgAlt = imgEl ? (imgEl.getAttribute('alt') || '').replace(/^Anuncio patrocinado:\s*/i, '').trim() : '';
  var title = imgAlt.length > h2Text.length ? imgAlt : h2Text;
  // Valoración (27 ago 2026): 'span.a-icon-alt' trae el texto de accesibilidad ("4,8 de 5
  // estrellas") -- se coge el texto del DIV contenedor entero (closest('div')), no solo ese
  // span, porque ahí al lado vive también el nº de reseñas entre paréntesis ("(125)"). Más
  // robusto que encadenar varios selectores concretos que Amazon podría cambiar.
  var ratingEl = card.querySelector('span.a-icon-alt');
  var ratingContainer = ratingEl ? ratingEl.closest('div') : null;
  return {
    asin: card.getAttribute('data-asin'),
    title: title || null,
    price_text: text('span.a-price span.a-offscreen'),
    original_price_text: text('span.a-price.a-text-price span.a-offscreen'),
    image: imgEl ? imgEl.getAttribute('src') : '',
    is_flash: !!flashEl,
    rating_text: ratingContainer ? ratingContainer.textContent : null
  };
});
"""


def scrape_keyword(driver, keyword, category):
    url = f"https://www.amazon.es/s?k={quote_plus(keyword)}"
    results = []
    try:
        driver.get(url)
    except TimeoutException:
        log(f"  timeout cargando '{keyword}', se salta")
        return results

    time.sleep(2)
    try:
        cards_data = driver.execute_script(_EXTRACT_CARDS_JS)
    except Exception as e:
        log(f"  error extrayendo tarjetas de '{keyword}': {e}")
        return results

    for card in cards_data:
        asin = card.get("asin")
        title = (card.get("title") or "").strip()
        if not asin or not title:
            continue

        price = parse_price(card.get("price_text"))
        if price is None:
            continue

        original_price = parse_price(card.get("original_price_text"))
        if original_price is None or original_price <= price:
            continue  # sin descuento real detectable

        discount = round((original_price - price) / original_price * 100)
        if discount < MIN_DISCOUNT_PERCENT:
            continue
        if price > 0 and discount > MAX_DISCOUNT_PERCENT:
            continue  # precio de referencia poco creíble, ver MAX_DISCOUNT_PERCENT
        # price == 0 (gratis de verdad, p.ej. libro Kindle en promoción) se deja pasar aunque el
        # descuento salga en 100 — ahí no hay un precio de referencia inflado que desconfiar.

        rating_text = card.get("rating_text")
        results.append(
            {
                "id": asin,
                "title": title[:180],
                "category": category,
                "price": round(price, 2),
                "original_price": round(original_price, 2),
                "discount_percent": int(discount),
                "is_flash": bool(card.get("is_flash")),
                "image": card.get("image") or "",
                "url": f"https://www.amazon.es/dp/{asin}?tag={AFFILIATE_TAG}",
                # Valoración real de Amazon (27 ago 2026) -- None si el producto no tiene
                # todavía ninguna reseña o si Amazon no la mostró esta vez, nunca inventada.
                "rating": parse_rating(rating_text),
                "rating_count": parse_rating_count(rating_text),
            }
        )
        if len(results) >= MAX_PRODUCTS_PER_KEYWORD:
            break
    return results


# Extracción para una ficha de producto individual (no una página de resultados) — selectores
# distintos a _EXTRACT_CARDS_JS. Amazon cambia este marcado con cierta frecuencia (A/B tests,
# tipo de producto...), así que se prueban varios selectores alternativos por campo; si ninguno
# encuentra nada, scrape_product_page() simplemente no actualiza ese ASIN esta vez.
_EXTRACT_PRODUCT_PAGE_JS = """
function text(sel) {
  var el = document.querySelector(sel);
  return el ? el.textContent : null;
}
var titleEl = document.querySelector('#productTitle');
var imgEl = document.querySelector('#landingImage, #imgBlkFront, #main-image');
return {
  title: titleEl ? titleEl.textContent : null,
  price_text: text(
    '#corePrice_feature_div span.a-price:not(.a-text-price) span.a-offscreen, ' +
    '#corePriceDisplay_desktop_feature_div span.a-price:not(.a-text-price) span.a-offscreen, ' +
    'span.priceToPay span.a-offscreen, #price_inside_buybox'
  ),
  original_price_text: text(
    '#corePriceDisplay_desktop_feature_div span.a-price.a-text-price span.a-offscreen, ' +
    '.basisPrice span.a-offscreen, span.a-price.a-text-price span.a-offscreen'
  ),
  image: imgEl ? imgEl.getAttribute('src') : '',
  rating_text: (function() {
    var ratingEl = document.querySelector('#acrPopover span.a-icon-alt, span.a-icon-alt');
    var container = ratingEl ? ratingEl.closest('div') : null;
    return container ? container.textContent : null;
  })()
};
"""


def scrape_product_page(driver, asin):
    """Visita la ficha de UN producto en concreto (por ASIN, no por búsqueda por keyword) para
    refrescar su precio aunque ya no salga en ninguna búsqueda por categoría. A diferencia de
    scrape_keyword, NO filtra por descuento mínimo — aquí interesa el precio real tenga o no
    descuento ahora mismo, porque el objetivo es avisar de cualquier cambio a quien lo tenga en
    favoritos. Devuelve None si no se ha podido leer un precio válido (producto retirado,
    página bloqueada, cambio de HTML...); nunca lanza."""
    url = f"https://www.amazon.es/dp/{asin}"
    try:
        driver.get(url)
    except TimeoutException:
        log(f"  timeout cargando ficha de {asin}, se salta")
        return None

    time.sleep(2)
    try:
        data = driver.execute_script(_EXTRACT_PRODUCT_PAGE_JS)
    except Exception as e:
        log(f"  error extrayendo ficha de {asin}: {e}")
        return None

    title = (data.get("title") or "").strip()
    price = parse_price(data.get("price_text"))
    if not title or price is None:
        return None  # producto ya no disponible o página con un formato inesperado

    original_price = parse_price(data.get("original_price_text"))
    if original_price is None or original_price <= price:
        original_price = price  # sin descuento visible ahora mismo, pero el precio sigue interesando
    discount = round((original_price - price) / original_price * 100) if original_price > 0 else 0

    rating_text = data.get("rating_text")
    return {
        "id": asin,
        "title": title[:180],
        "price": round(price, 2),
        "original_price": round(original_price, 2),
        "discount_percent": int(discount),
        "is_flash": False,
        "image": data.get("image") or "",
        "url": f"https://www.amazon.es/dp/{asin}?tag={AFFILIATE_TAG}",
        "rating": parse_rating(rating_text),
        "rating_count": parse_rating_count(rating_text),
    }


def _fetch_pending_submissions(log):
    """Sugerencias de usuarios pendientes de revisar (colección submissions, ver
    firestore.rules en rebajasdiarias-web) -- mismo patrón de credenciales que
    _fetch_watched_asins()/notify_app_push() de abajo, MISMO archivo de credenciales
    (FIREBASE_CREDENTIALS_PATH). Nunca lanza: si Firestore no responde, se devuelve [] y este
    ciclo simplemente no revisa sugerencias, no aborta nada."""
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return []
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()

        query = (
            db.collection("submissions")
            .where(filter=FieldFilter("status", "==", "pending"))
            .limit(SUBMISSIONS_MAX_PER_CYCLE)
        )
        return [(doc.id, doc.to_dict() or {}) for doc in query.stream()]
    except Exception as e:
        log(f"  aviso: no se pudo consultar Firestore de sugerencias: {e}")
        return []


# Kindle Unlimited (26 ago 2026, pedido explícito: "si tienes el unlimited sí te sale gratis,
# o marcar eso... que ponga eso en la descripción"). Caso real distinto de un descuento: el
# selector de formato "Versión Kindle" de la ficha muestra "0,00€" + un icono con clase
# "a-icon-kindle-unlimited" (aria-label "Con Kindle Unlimited") cuando el libro está incluido
# en la suscripción -- NUNCA un precio tachado real para el comprador normal (ver
# #corePrice_feature_div, que sigue mostrando el precio de venta de siempre). Por eso nunca se
# trata como un descuento fabricado: se marca con su propio campo honesto
# (kindle_unlimited=True) para que la web/app lo etiqueten como "Gratis con Kindle Unlimited"
# en vez de "Gratis" a secas. HTML real inspeccionado en la Pi (26 ago) antes de escribir
# esto -- el icono es un <i> aparte, NO texto dentro de .slot-price (intento anterior fallaba
# por buscar la palabra "kindleunlimited" como texto, que no existe en ningún sitio del DOM).
_EXTRACT_KINDLE_SWATCH_JS = """
var swatch = document.querySelector('#tmm-grid-swatch-KINDLE');
if (!swatch) return null;
var priceEl = swatch.querySelector('.slot-price');
var kuIcon = swatch.querySelector('.a-icon-kindle-unlimited, [aria-label*="Kindle Unlimited"]');
var link = swatch.querySelector('a[href*="/dp/"]');
var m = link ? link.getAttribute('href').match(/\\/dp\\/([A-Za-z0-9]{10})/) : null;
return {
  priceText: priceEl ? priceEl.textContent.replace(/\\s+/g, ' ').trim() : null,
  isKindleUnlimited: !!kuIcon,
  kindleAsin: m ? m[1] : null
};
"""


def _check_kindle_unlimited_free(driver):
    """Si la ficha ya cargada en `driver` es un libro incluido gratis con Kindle Unlimited
    (0,00€ + icono de Kindle Unlimited en el selector de formato), devuelve el ASIN de la
    EDICIÓN KINDLE concreta (puede ser distinto del ASIN de la URL sugerida, p.ej. si se
    sugirió la tapa blanda -- el enlace de afiliado final debe llevar a la edición que de
    verdad es gratis, no a la de tapa dura/blanda de pago). None si no aplica. Nunca lanza."""
    try:
        data = driver.execute_script(_EXTRACT_KINDLE_SWATCH_JS)
        if not data or not data.get("priceText"):
            return None
        if data.get("isKindleUnlimited") and "0,00" in data["priceText"]:
            return data.get("kindleAsin")
        return None
    except Exception:
        return None


def _build_kindle_unlimited_offer(driver, asin, kindle_asin):
    """Construye la oferta de un libro Kindle Unlimited cuando scrape_product_page() no ha
    podido sacar título/precio del buybox principal (visto en real, 26 ago 2026: pasa cuando la
    página no trae ningún formato preseleccionado). Reutiliza la misma extracción de
    título/imagen de _EXTRACT_PRODUCT_PAGE_JS -- la página ya está cargada, no hace falta
    volver a navegar. Devuelve None si ni siquiera el título se puede sacar."""
    try:
        data = driver.execute_script(_EXTRACT_PRODUCT_PAGE_JS)
    except Exception:
        return None
    title = (data.get("title") or "").strip() if data else ""
    if not title:
        return None
    offer_id = f"{asin}_ku" if kindle_asin == asin else kindle_asin
    return {
        "id": offer_id,
        "title": title[:180],
        "category": _detect_amazon_category(driver) or SUBMISSION_CATEGORY,
        "price": 0.0,
        "original_price": 0.0,
        "discount_percent": 0,
        "is_flash": False,
        "image": (data.get("image") or ""),
        "url": f"https://www.amazon.es/dp/{kindle_asin}?tag={AFFILIATE_TAG}",
        "kindle_unlimited": True,
        "pinned": offer_id in PINNED_OFFER_IDS,
    }


def _resolve_submission(driver, url):
    """Comprueba si una URL sugerida por un usuario es una oferta real publicable. Reutiliza la
    misma extracción que scrape_product_page() (favoritos vigilados), pero a diferencia de esa
    SÍ exige el mismo umbral de descuento que el resto del catálogo (30-80%) -- aquí el objetivo
    es publicar solo ofertas reales, no vigilar un precio tenga o no descuento. Nunca lanza:
    devuelve (oferta, None) si cualifica, o (None, motivo) si se rechaza -- el motivo se guarda
    en Firestore para que quien la sugirió sepa por qué."""
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except Exception:
        return None, "la URL no es válida"
    host = (parsed.hostname or "").lower()
    if host not in SUBMISSION_ALLOWED_HOSTS:
        return None, "de momento solo se admiten enlaces de productos de amazon.es"

    m = re.search(r"/(?:dp|gp/product)/([A-Za-z0-9]{10})", parsed.path)
    if not m:
        m = re.search(r"[?&]asin=([A-Za-z0-9]{10})", url, re.IGNORECASE)
    if not m:
        return None, ("no se ha podido identificar el producto en la URL -- usa el enlace "
                       "directo del producto (amazon.es/dp/XXXXXXXXXX)")
    asin = m.group(1).upper()

    result = scrape_product_page(driver, asin)
    if result is None:
        # Antes de rechazar del todo: la página SÍ se ha cargado (scrape_product_page navega
        # primero y solo devuelve None si no consigue extraer título+precio del buybox
        # principal después) -- se puede seguir intentando el camino de Kindle Unlimited, que
        # lee un selector totalmente distinto (el swatch de formato, no el buybox) y puede
        # funcionar aunque el buybox no dé nada útil (visto en real, 26 ago 2026: un libro sin
        # ningún formato preseleccionado no rellena el buybox principal, pero el swatch de
        # Kindle sí muestra su precio).
        kindle_asin = _check_kindle_unlimited_free(driver)
        if kindle_asin:
            offer = _build_kindle_unlimited_offer(driver, asin, kindle_asin)
            if offer:
                return offer, None
        return None, ("no se ha podido comprobar el precio ahora mismo (producto no "
                       "disponible, o página con un formato inesperado)")

    price = result["price"]
    original_price = result["original_price"]
    if original_price <= price:
        # Antes de rechazar del todo: ¿es un libro incluido gratis con Kindle Unlimited? Caso
        # real distinto de un descuento (ver _check_kindle_unlimited_free arriba) -- se publica
        # igual, pero SIN fabricar ningún % de descuento, marcado con su propio campo honesto.
        kindle_asin = _check_kindle_unlimited_free(driver)
        if kindle_asin:
            offer = dict(result)
            offer["id"] = f"{asin}_ku" if kindle_asin == asin else kindle_asin
            offer["url"] = f"https://www.amazon.es/dp/{kindle_asin}?tag={AFFILIATE_TAG}"
            offer["price"] = 0.0
            offer["original_price"] = 0.0
            offer["discount_percent"] = 0
            offer["category"] = _detect_amazon_category(driver) or SUBMISSION_CATEGORY
            offer["kindle_unlimited"] = True
            offer["pinned"] = offer["id"] in PINNED_OFFER_IDS
            return offer, None
        return None, "no tiene ningún descuento real ahora mismo"
    discount = round((original_price - price) / original_price * 100)
    if discount < MIN_DISCOUNT_PERCENT:
        return None, (f"el descuento real es de solo un {discount}% (hace falta al menos "
                       f"{MIN_DISCOUNT_PERCENT}%)")
    if price > 0 and discount > MAX_DISCOUNT_PERCENT:
        return None, "el descuento calculado no parece creíble (precio de referencia inflado)"

    offer = dict(result)
    # 26 ago 2026, pedido explícito ("aparte de lo que el usuario proponga que vaya a
    # sugerencias que vaya a su seccion"): categoría real detectada de las migas de pan de
    # Amazon si es posible (ver _detect_amazon_category) -- "Sugerencias" ahora es solo el
    # último recurso, no la categoría de todo lo aprobado por este camino. Lo que SÍ distingue
    # siempre una sugerencia del resto del catálogo es offer['submitted_by'] (ver más abajo),
    # que ya se usa para la sección transversal "Sugerencias de usuarios" en la web/app.
    offer["category"] = _detect_amazon_category(driver) or SUBMISSION_CATEGORY
    offer["discount_percent"] = int(discount)
    offer["pinned"] = offer["id"] in PINNED_OFFER_IDS
    return offer, None


def _load_submission_offers():
    try:
        with open(SUBMISSION_OFFERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_submission_offers(mapping):
    try:
        with open(SUBMISSION_OFFERS_PATH, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # no crítico -- en el peor caso, se reintenta la próxima vez que haya cambios


def _process_submissions(driver, log):
    """Revisa las sugerencias de usuarios pendientes este ciclo y actualiza su estado en
    Firestore (approved/rejected + motivo). Devuelve un dict {id: oferta} con las aprobadas,
    listo para mezclarse en new_or_updated como cualquier otra fuente. Se llama DENTRO del
    mismo try/except que ya protege el resto del scraping de Amazon en main() -- un fallo aquí
    no debe impedir que el ciclo normal termine bien."""
    pending = _fetch_pending_submissions(log)
    if not pending:
        return {}
    log(f"{len(pending)} sugerencia(s) de usuarios pendientes de revisar...")

    from firebase_admin import firestore
    db = firestore.client()
    submission_offers = _load_submission_offers()

    approved = {}
    changed = False
    for doc_id, data in pending:
        url = (data.get("url") or "").strip()
        offer, reason = _resolve_submission(driver, url)
        try:
            if offer:
                # "Que se vea el usuario que lo ha publicado" (26 ago 2026): solo el primer
                # nombre, nunca el correo ni el nombre completo en una tarjeta pública -- el
                # correo/nombre completo solo lo ve el propio usuario (Mis sugerencias) y el
                # administrador (panel), nunca el resto del catálogo.
                display_name = (data.get("submittedByName") or "").strip()
                offer["submitted_by"] = display_name.split()[0] if display_name else None

                # Campos denormalizados (título/precio/imagen) además de status/offerId: para
                # que "mis sugerencias" en el perfil de web/app pueda pintar una tarjeta con
                # información real sin tener que ir a buscarla aparte en offers.json (26 ago
                # 2026, pedido explícito: "que se vea en el perfil de cada uno las ofertas
                # subidas").
                db.collection("submissions").document(doc_id).update({
                    "status": "approved",
                    "offerId": offer["id"],
                    "title": offer["title"],
                    "price": offer["price"],
                    "originalPrice": offer["original_price"],
                    "discountPercent": offer["discount_percent"],
                    "image": offer["image"],
                    "reviewedAt": firestore.SERVER_TIMESTAMP,
                })
                log(f"  sugerencia aprobada: {offer['title'][:60]!r} ({offer['discount_percent']}%)")
                approved[offer["id"]] = offer
                submission_offers[offer["id"]] = doc_id
                changed = True

                # "Que si alguien ha publicado una oferta que salte una notificación" (26 ago
                # 2026): aviso personal al autor, aparte del push genérico de "catálogo
                # actualizado" -- nunca debe poder tumbar la aprobación en sí (ya hecha arriba)
                # si falla.
                submitted_by_uid = data.get("submittedBy")
                if submitted_by_uid:
                    notify_submitter_push(submitted_by_uid, offer)
            else:
                db.collection("submissions").document(doc_id).update({
                    "status": "rejected",
                    "reason": reason,
                    "reviewedAt": firestore.SERVER_TIMESTAMP,
                })
                log(f"  sugerencia rechazada ({url}): {reason}")
        except Exception as e:
            log(f"  aviso: no se pudo actualizar el estado de la sugerencia {doc_id}: {e}")
        time.sleep(random.uniform(*INTER_SEARCH_DELAY_RANGE))  # mismo respeto de ritmo que el resto

    if changed:
        _save_submission_offers(submission_offers)
    return approved


def _reconcile_deleted_submissions(log):
    """'Que ellos las puedan eliminar' (26 ago 2026): el usuario puede borrar su propia
    sugerencia desde la web/app en cualquier momento (firestore.rules ya lo permite
    directamente, sin pasar por la Pi). Si esa sugerencia ya estaba aprobada y viva en el
    catálogo, el borrado del documento de Firestore por sí solo no la retira de offers.json --
    aquí es donde se nota: para cada oferta que sepamos que viene de una sugerencia
    (submission_offers.json), se comprueba si su documento sigue existiendo en Firestore; si ya
    no existe, se devuelve su id para que main() la retire del catálogo en este mismo ciclo.
    Nunca lanza -- si Firestore no responde, no se retira nada esta vez (más seguro que
    arriesgarse a borrar de más por un fallo de red)."""
    submission_offers = _load_submission_offers()
    if not submission_offers:
        return set()
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return set()
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()

        to_remove = set()
        remaining = dict(submission_offers)
        for offer_id, doc_id in submission_offers.items():
            try:
                if not db.collection("submissions").document(doc_id).get().exists:
                    to_remove.add(offer_id)
                    remaining.pop(offer_id, None)
            except Exception as e:
                log(f"  aviso: no se pudo comprobar la sugerencia {doc_id} de la oferta "
                    f"{offer_id}: {e}")

        if to_remove:
            log(f"{len(to_remove)} oferta(s) sugerida(s) retirada(s) por su propio autor: "
                f"{', '.join(sorted(to_remove))}")
            _save_submission_offers(remaining)
        return to_remove
    except Exception as e:
        log(f"  aviso: no se pudo comprobar sugerencias borradas: {e}")
        return set()


def diversify_order(offers):
    """Reordena el catálogo (nunca elimina nada) para que ninguna categoría
    lo domine visualmente en Flash/Ofertas del día — bug real reportado por
    el usuario ("siempre me salen las maletas/el ColaCao"): esos productos
    llevan varios ciclos con el mismo descuento activo, y como la app/web
    simplemente pintan la lista en el orden del JSON, siempre salían primero.

    Se hace aquí (en la Pi) y no solo en la app, para que el arreglo llegue
    a todo el mundo en el próximo ciclo del cron sin depender de que nadie
    actualice la app en Google Play ni de un redeploy de la web.

    Dentro de cada categoría se conserva "más reciente primero" (first_seen
    descendente, mismo criterio que ya usan la app y la web: al volver a
    abrir la app, lo primero que se ve son cosas nuevas — lo viejo ya se
    vio antes, importa menos), y luego se entrelazan las categorías en
    orden aleatorio (round-robin) para que dos ofertas seguidas casi nunca
    sean de la misma categoría.
    """
    by_category = {}
    for offer in offers:
        by_category.setdefault(offer.get("category", "Otros"), []).append(offer)
    for group in by_category.values():
        group.sort(key=lambda o: o.get("first_seen", ""), reverse=True)

    categories = list(by_category.keys())
    random.shuffle(categories)

    result = []
    while categories:
        for category in list(categories):
            group = by_category[category]
            result.append(group.pop(0))
            if not group:
                categories.remove(category)
    return result


def main():
    log("=== Inicio ===")

    jitter = random.uniform(0, JITTER_MAX_MINUTES * 60)
    log(f"Esperando {round(jitter / 60, 1)} min antes de empezar (para no disparar siempre "
        f"justo a la hora en punto del cron)...")
    time.sleep(jitter)

    current_hour = datetime.now().hour
    if not (ACTIVE_HOUR_START <= current_hour < ACTIVE_HOUR_END):
        log(f"Hora actual ({current_hour}h) fuera de la franja activa "
            f"({ACTIVE_HOUR_START}h-{ACTIVE_HOUR_END}h) — probablemente un @reboot tras un corte "
            f"de luz. Se omite esta ejecución sin tocar nada, ya tocará en el próximo cron.")
        return

    if not os.path.isdir(REPO_DIR):
        log(f"ERROR: no existe {REPO_DIR}, aborto")
        sys.exit(1)

    # Candado compartido con check_submissions.py (26 ago 2026, ver REPO_LOCK_PATH): este ciclo
    # puede tardar varios minutos tocando git, así que espera su turno (bloqueante) en vez de
    # arriesgarse a pisar un commit/push a medias de check_submissions.py, que dispara cada
    # pocos minutos. No hace falta soltarlo a mano -- el propio proceso lo libera solo al
    # terminar (aquí main() se llama una única vez por ejecución del script).
    _repo_lock_file = open(REPO_LOCK_PATH, "w")
    fcntl.flock(_repo_lock_file, fcntl.LOCK_EX)

    subprocess.run(["git", "-C", REPO_DIR, "pull", "--quiet"], check=False)

    try:
        with open(OFFERS_PATH, encoding="utf-8") as f:
            existing = json.load(f)
    except Exception as e:
        log(f"ERROR leyendo offers.json existente: {e}. Aborto por seguridad.")
        sys.exit(1)

    existing_offers = {o["id"]: o for o in existing.get("offers", []) if o.get("id")}
    log(f"Catálogo actual: {len(existing_offers)} ofertas")

    # Backfill: ofertas guardadas antes de tener first_seen/last_seen (o la primera vez que se
    # ejecuta esta versión del script). Se les asigna la fecha del propio archivo en vez de "ahora"
    # para no hacer que de golpe parezcan todas recién añadidas.
    backfill_date = existing.get("updated_at") or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    for o in existing_offers.values():
        o.setdefault("first_seen", backfill_date)
        o.setdefault("last_seen", backfill_date)

    group_idx = _next_group_index()
    group_names = CATEGORY_GROUPS[group_idx]
    run_categories = {name: KEYWORDS_BY_CATEGORY[name] for name in group_names}
    log(f"Grupo de categorías de esta ejecución ({group_idx + 1}/{len(CATEGORY_GROUPS)}): "
        f"{', '.join(group_names)}")

    for priority_category in PRIORITY_CATEGORIES:
        already_in_group = priority_category in run_categories
        run_categories.setdefault(priority_category, KEYWORDS_BY_CATEGORY[priority_category])
        if not already_in_group:
            log(f"Categoría prioritaria añadida fuera de su grupo: '{priority_category}'")

    # Alertas de usuarios (27 ago 2026): se buscan de verdad como una categoría más, no solo se
    # comparan contra lo que ya salga de las demás -- ver _fetch_alert_keywords() y el trato
    # especial de "sample = keywords" (sin sorteo) más abajo en el bucle de scraping.
    alert_keywords = _fetch_alert_keywords()
    if alert_keywords:
        if len(alert_keywords) > MAX_ALERT_KEYWORDS_PER_RUN:
            random.shuffle(alert_keywords)
            alert_keywords = alert_keywords[:MAX_ALERT_KEYWORDS_PER_RUN]
        run_categories["Alertas"] = alert_keywords
        log(f"Alertas de usuarios añadidas a la búsqueda de hoy: {len(alert_keywords)} "
            f"palabra(s) -- {', '.join(alert_keywords)}")

    boost_categories = set()
    for boost_category in _active_campaign_categories():
        if boost_category in KEYWORDS_BY_CATEGORY:
            already_in_group = boost_category in run_categories
            run_categories.setdefault(boost_category, KEYWORDS_BY_CATEGORY[boost_category])
            boost_categories.add(boost_category)
            log(f"Campaña activa: refuerzo de búsquedas en '{boost_category}'"
                f"{' (ya estaba en el grupo de hoy)' if already_in_group else ''}")
        else:
            log(f"  aviso: categoryTarget '{boost_category}' de una campaña no coincide con "
                f"ninguna categoría conocida, se ignora el boost")

    driver = None
    scraped_count = 0
    new_or_updated = {}
    watched_asins = None
    watched_results = {}
    try:
        driver = build_driver()
        categories = list(run_categories.items())
        random.shuffle(categories)
        for category, keywords in categories:
            if category == 'Alertas':
                # Todas, no una muestra -- aquí interesa cubrir TODAS las alertas activas de
                # los usuarios, ya topadas a MAX_ALERT_KEYWORDS_PER_RUN antes de llegar aquí.
                sample = keywords
            elif category in boost_categories:
                n = CAMPAIGN_KEYWORDS_MAX
                sample = random.sample(keywords, min(n, len(keywords)))
            else:
                n = random.randint(*KEYWORDS_PER_CATEGORY_RANGE)
                sample = random.sample(keywords, min(n, len(keywords)))
            if category == 'Libros':
                # Siempre, no sujeto al sorteo de arriba -- ver LIBROS_GRATIS_KEYWORDS.
                sample = list(dict.fromkeys(sample + LIBROS_GRATIS_KEYWORDS))
            for kw in sample:
                log(f"Buscando '{kw}' ({category})...")
                try:
                    found = scrape_keyword(driver, kw, category)
                except WebDriverException as e:
                    log(f"  error de navegador con '{kw}': {e}")
                    continue
                for offer in found:
                    new_or_updated[offer["id"]] = offer
                scraped_count += len(found)
                time.sleep(random.uniform(*INTER_SEARCH_DELAY_RANGE))  # no machacar a Amazon

        # Favoritos vigilados: solo si el scraping normal de arriba ha ido bien (si no, mejor
        # no arriesgar más peticiones en una sesión que ya pinta rara). Solo se visita
        # directamente el ASIN de los que NO tengan ya un precio fresco de esta misma
        # ejecución o del catálogo público actual — para esos, es gratis, no hace falta visita.
        if scraped_count > 0:
            watched_asins = _fetch_watched_asins()
            if watched_asins:
                already_fresh = set(new_or_updated) | set(existing_offers)
                to_visit = [a for a in watched_asins if a not in already_fresh]
                random.shuffle(to_visit)
                if len(to_visit) > MAX_WATCHED_DIRECT_VISITS_PER_RUN:
                    log(f"{len(to_visit)} favoritos vigilados sin precio fresco, se visitan "
                        f"solo {MAX_WATCHED_DIRECT_VISITS_PER_RUN} esta vez (el resto, en "
                        f"próximas ejecuciones)")
                    to_visit = to_visit[:MAX_WATCHED_DIRECT_VISITS_PER_RUN]
                for asin in to_visit:
                    log(f"Comprobando favorito vigilado {asin} directamente...")
                    result = scrape_product_page(driver, asin)
                    if result:
                        watched_results[asin] = result
                    time.sleep(random.uniform(*INTER_SEARCH_DELAY_RANGE))
            elif watched_asins is not None:
                log("  ningún favorito vigilado activo ahora mismo")
    except Exception as e:
        log(f"ERROR fatal durante el scraping: {e}. Aborto sin tocar offers.json.")
        sys.exit(1)
    finally:
        if driver is not None:
            driver.quit()

    # Multi-tienda (Leroy Merlin, Stylevana — ver multitienda_feeds.py): independiente del
    # scraping de Amazon de arriba, nunca debe poder tumbar el ciclo entero. Se mezcla en el
    # mismo new_or_updated para que app/web/Telegram lean todo del mismo offers.json (un solo
    # catálogo, no pipelines paralelos) — ver sync_telegram_group_topics() más abajo, que ya
    # publica por categoría sin distinguir tienda.
    try:
        multitienda_offers = fetch_multitienda_offers(log)
    except Exception as e:
        log(f"aviso: fallo en la ingesta multi-tienda, se continúa sin ella este ciclo: {e}")
        multitienda_offers = {}
    new_or_updated.update(multitienda_offers)

    if len(new_or_updated) == 0:
        log(
            "No se ha encontrado NINGUNA oferta nueva/actualizada en esta ejecución, ni de "
            "Amazon ni de multi-tienda (posible bloqueo de Amazon, cambio de HTML, o fallo de "
            "red en los feeds de Awin). Abortando sin tocar offers.json."
        )
        sys.exit(1)

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    # Fusión por ASIN. Ofertas nuevas de verdad → first_seen = ahora (para que la app las pueda
    # mostrar arriba, "más recientes primero"). Ofertas ya existentes que se han vuelto a ver →
    # conservan su first_seen original (no son nuevas) pero se actualiza el precio/descuento a lo
    # último visto y se refresca last_seen (para que no las pode la limpieza de caducadas).
    merged = dict(existing_offers)
    # Precio antes/después por ASIN, solo de lo tocado esta ejecución (new_or_updated) -- para
    # avisar a quien tenga la campana de favoritos activada (ver notify_favorite_price_changes()
    # más abajo, 27 ago 2026). Se calcula ANTES de pisar new_offer en merged, con el precio viejo
    # de existing_offers todavía disponible.
    price_changes = {}
    # Ofertas nuevas de verdad este ciclo (no vistas en ningún ciclo anterior) -- para las
    # alertas de palabra clave de abajo, que solo deben avisar una vez por oferta, el ciclo en
    # que aparece, no cada vez que se vuelve a ver mientras siga viva (27 ago 2026).
    brand_new_asins = set()
    for asin, new_offer in new_or_updated.items():
        prior = existing_offers.get(asin)
        if prior is not None:
            try:
                old_price, new_price = float(prior.get("price")), float(new_offer.get("price"))
                if new_price < old_price:
                    price_changes[asin] = "down"
                elif new_price > old_price:
                    price_changes[asin] = "up"
            except (TypeError, ValueError):
                pass
        else:
            brand_new_asins.add(asin)
        new_offer["first_seen"] = prior["first_seen"] if prior else now_iso
        new_offer["last_seen"] = now_iso
        merged[asin] = new_offer

    # "Que ellos las puedan eliminar" (26 ago 2026): retira del catálogo las ofertas cuyo autor
    # ha borrado su propia sugerencia desde entonces (ver _reconcile_deleted_submissions()). Se
    # hace aquí, ya con `merged` construido, para que gane siempre sobre cualquier otra fuente
    # que la hubiera vuelto a traer este mismo ciclo (aunque no debería pasar -- Amazon ya no la
    # sugiere ninguna búsqueda por keyword porque no tiene keyword asociada).
    try:
        removed_by_author = _reconcile_deleted_submissions(log)
        for offer_id in removed_by_author:
            merged.pop(offer_id, None)
    except Exception as e:
        log(f"aviso: fallo comprobando sugerencias borradas por su autor, se omite esta vez: {e}")

    # Favoritos vigilados: si se ha podido preguntar a Firestore esta vez (watched_asins no es
    # None), se reconstruye watched_prices.json entero a partir de la lista actual — así un
    # ASIN que ya nadie tiene en favoritos desaparece solo del archivo, sin lógica de poda
    # aparte. Mismo esquema de campos que offers.json ("offers"/Offer.fromJson en la app), para
    # poder reutilizar el mismo modelo Dart al leerlo.
    watched_written = False
    if watched_asins is not None:
        try:
            with open(WATCHED_PATH, encoding="utf-8") as f:
                existing_watched = json.load(f).get("prices", {})
        except Exception:
            existing_watched = {}

        watched_final = {}
        for asin in watched_asins:
            fresh = watched_results.get(asin) or merged.get(asin)
            prior = existing_watched.get(asin)
            if fresh is not None:
                watched_final[asin] = {
                    "id": asin,
                    "title": fresh.get("title", ""),
                    "category": fresh.get("category") or (prior or {}).get("category") or "Otros",
                    "price": fresh.get("price"),
                    "original_price": fresh.get("original_price", fresh.get("price")),
                    "discount_percent": fresh.get("discount_percent", 0),
                    "is_flash": bool(fresh.get("is_flash", False)),
                    "image": fresh.get("image", ""),
                    "url": fresh.get("url") or f"https://www.amazon.es/dp/{asin}?tag={AFFILIATE_TAG}",
                    "first_seen": (prior or {}).get("first_seen", now_iso),
                    "checked_at": now_iso,
                    "rating": fresh.get("rating"),
                    "rating_count": fresh.get("rating_count"),
                }
            elif prior is not None:
                watched_final[asin] = prior  # sin novedad esta vez, se conserva tal cual

        removed = set(existing_watched) - set(watched_final)
        if removed or watched_final != existing_watched:
            log(f"watched_prices.json: {len(watched_final)} favoritos vigilados "
                f"({len(watched_results)} comprobados directamente esta vez"
                f"{f', {len(removed)} retirados porque ya nadie los tiene en favoritos' if removed else ''}).")
            with open(WATCHED_PATH, "w", encoding="utf-8") as f:
                json.dump(
                    {"updated_at": now_iso, "prices": watched_final},
                    f, ensure_ascii=False, indent=2,
                )
            watched_written = True

    # Poda: ofertas no vistas en ninguna pasada reciente (probablemente ya no existan o el
    # descuento haya desaparecido). No se aplica si dejaría el catálogo por debajo del mínimo.
    cutoff = datetime.now().astimezone() - timedelta(days=STALE_AFTER_DAYS)

    def _is_fresh(offer):
        try:
            return datetime.fromisoformat(offer["last_seen"]) >= cutoff
        except Exception:
            return True  # si no se puede leer la fecha, no se borra por precaución

    pruned = {k: v for k, v in merged.items() if _is_fresh(v)}
    if len(pruned) >= MIN_TOTAL_OFFERS:
        removed = len(merged) - len(pruned)
        if removed:
            log(f"Se retiran {removed} ofertas no vistas en los últimos {STALE_AFTER_DAYS} días "
                f"(ya no parecen disponibles).")
        merged = pruned
    else:
        log("Se omite la limpieza de ofertas caducadas esta vez: dejaría el catálogo por debajo "
            "del mínimo.")

    if len(merged) < MIN_TOTAL_OFFERS:
        log(
            f"Catálogo resultante ({len(merged)}) por debajo del mínimo "
            f"({MIN_TOTAL_OFFERS}). Aborto sin tocar offers.json."
        )
        sys.exit(1)

    log(f"Ofertas nuevas/actualizadas esta ejecución: {len(new_or_updated)}. "
        f"Catálogo final: {len(merged)}.")

    output = {
        "updated_at": now_iso,
        "affiliate_tag": AFFILIATE_TAG,
        "offers": diversify_order(list(merged.values())),
    }

    with open(OFFERS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Catálogo ampliado: YA NO se genera aquí (26 ago 2026, bug real: descargar los 7 feeds
    # completos de Leroy Merlin dejaba la Pi sin memoria y el proceso entero moría por SIGKILL
    # del kernel -- un try/except normal NO protege contra esto, el OOM killer no da opción a
    # capturar nada, así que se perdía TAMBIÉN el commit/push de offers.json que venía después
    # en el mismo proceso). Movido a generate_extended_catalog_cron.py, proceso aparte con su
    # propio cron (ver crontab en la Pi / RASPI_REBAJASDIARIAS.md) -- si ese proceso muere, este
    # ciclo normal ni se entera.
    git_paths = ["offers.json"]
    if watched_written:
        git_paths.append("watched_prices.json")
    if os.path.isfile(SUBMISSION_OFFERS_PATH):
        git_paths.append("submission_offers.json")
    subprocess.run(["git", "-C", REPO_DIR, "add"] + git_paths, check=True)
    diff = subprocess.run(["git", "-C", REPO_DIR, "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        log("Sin cambios respecto al commit anterior, no se hace commit.")
    else:
        commit_msg = (
            f"Actualiza ofertas ({len(new_or_updated)} nuevas/actualizadas, "
            f"{len(merged)} totales)"
            + (", favoritos vigilados" if watched_written else "")
            + f" — {output['updated_at']}"
        )
        subprocess.run(["git", "-C", REPO_DIR, "commit", "-m", commit_msg], check=True)
        push = subprocess.run(["git", "-C", REPO_DIR, "push"], capture_output=True, text=True)
        if push.returncode != 0:
            log(f"ERROR haciendo push: {push.stderr}")
            sys.exit(1)
        log("Push realizado correctamente.")
        notify_telegram(
            f"📦 RebajasDiarias actualizado: {len(new_or_updated)} ofertas nuevas/actualizadas, "
            f"{len(merged)} en total."
        )
        # Push a la app (11 ago 2026) — solo aquí, en la rama donde hubo un commit/push real;
        # nunca en un ciclo sin cambios (evita avisos vacíos "actualizado" cuando no hay nada
        # nuevo que ver).
        notify_app_push(list(merged.values()))
        # Avisos personales de cambio de precio en favoritos (27 ago 2026) -- misma rama que el
        # push genérico de arriba, con las mismas garantías (solo en ciclos con push real).
        notify_favorite_price_changes(price_changes, merged)
        # Alertas de palabra clave/marca (27 ago 2026) -- misma rama, mismas garantías.
        notify_keyword_alerts(brand_new_asins, merged)

    # Publicación en el grupo público de Telegram: se hace DESPUÉS del push, no antes (cambiado
    # el 11 ago 2026) — así una tanda larga (p.ej. el primer envío del catálogo completo al tema
    # "Todas las Ofertas", ver TELEGRAM_ALL_OFFERS_CATEGORY) nunca retrasa que la web/app vean el
    # catálogo nuevo. Se hace siempre que el catálogo final es válido, haya habido o no cambios
    # que comitear a git.
    sync_telegram_group_topics(list(merged.values()))

    log("=== Fin ===")


if __name__ == "__main__":
    main()
