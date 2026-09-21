#!/usr/bin/env python3
"""
keyword_alert_search.py — motor compartido de las alertas de palabra clave con búsqueda en
vivo en Amazon (14 sep 2026, pedido explícito: "que busque las ofertas sobre esa palabra...
que tarde lo menos posible, y solo que busque cosas que estén en oferta" + "que se guarden y si
ya no están las ofertas que se eliminen" + "desde 1% de descuento hasta el máximo").

REESCRITO el mismo día (14 sep 2026, pedido explícito): la primera versión llamaba a la Amazon
Creators API (amazon_paapi.py) -- bloqueada por el umbral de ventas de Amazon (403
AssociateNotEligible), el mismo motivo por el que search_requests llevaba semanas sin dar
resultados a nadie. En vez de depender de esa elegibilidad, usa el MISMO scraping real
(scrape_keyword() en update_offers.py, Selenium/Chrome) que ya funciona hoy para el resto del
catálogo -- no hace falta ninguna API con permisos especiales.

**Perfil de Chrome APARTE + pausa del ciclo normal** (14 sep 2026, segunda vuelta, pedido
explícito: "lo más rápido posible y con pocos resultados de alertas" -- uno es "el de las
ofertas de la app", este es otro distinto -- y luego "si hay una alerta en búsqueda pausa el
otro scraping... y en terminar que siga", "así no se satura la Pi"): a diferencia de
check_submissions.py, que SÍ comparte perfil y candado con update_offers.py porque puede
permitirse esperar unos minutos, las alertas no esperan Y además no deben competir por CPU/RAM
con el ciclo normal en una Pi con pocos recursos. `KEYWORD_ALERT_PROFILE_DIR` propio (evita que
los dos Chrome choquen por el mismo user-data-dir) + **SIGSTOP al proceso del ciclo normal
mientras dura la búsqueda, SIGCONT al terminar** (ver UPDATE_OFFERS_PID_PATH en
update_offers.py) -- congela el ciclo normal a nivel de sistema operativo sin tocar su estado
interno (git a medias, progreso por categoría...), se reanuda exactamente donde estaba. Si el
ciclo normal no está corriendo, no hay nada que pausar y se sigue igual.

Dos disparadores comparten esta misma función, para no duplicar la lógica de guardar/quitar:
  - search_requests_listener.py, al momento de añadir una palabra (ver AmazonSearchService.
    createKeywordAlertRequest() en la app) -- un Chrome headless tarda unos segundos en abrir y
    buscar, no es instantáneo como la API lo hubiera sido, pero es lo que de verdad funciona.
  - keyword_alert_cleanup.py, en un cron propio cada hora -- vuelve a comprobar CADA palabra ya
    guardada de CADA usuario, añade lo nuevo (avisa por push) y quita lo que ya no cumpla.

Colección `keyword_alert_offers/{uid}_{keyword}_{asin}` -- id determinista para que guardar dos
veces la misma oferta la actualice en vez de duplicarla.

Umbral 1% sin techo (MIN_SAVING_PERCENT_KEYWORD_ALERT/MAX_SAVING_PERCENT_KEYWORD_ALERT), mucho
más bajo que el 30-80% del resto del proyecto A PROPÓSITO (pedido explícito del usuario) --
esto es una palabra muy concreta pedida por una persona, no el catálogo general: mejor un 5%
real que nada. Nunca toca MIN_DISCOUNT_PERCENT/MAX_DISCOUNT_PERCENT de update_offers.py, que
siguen en 30-80% para el ciclo normal del catálogo.
"""

import fcntl
import os
import re
import signal
import subprocess
import time

from firebase_admin import firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

import update_offers as uo

MIN_SAVING_PERCENT_KEYWORD_ALERT = 1
MAX_SAVING_PERCENT_KEYWORD_ALERT = 100  # "hasta el máximo" -- sin techo, a diferencia del 80%
CATEGORY_LABEL = "Alerta"
MAX_PRODUCTS_KEYWORD_ALERT = 24  # 14 sep 2026, segundo aviso real: "solo encuentra 5 ofertas y
# sé que hay muchas más" -- subido de 5 a 16, y de 16 a 24 el 15 sep (pedido explícito: "estaría
# guay que enviara más ofertas") -- sigue siendo GRATIS en recursos, una sola página de
# resultados de Amazon.es ya trae normalmente hasta 24 tarjetas, no hace falta cargar una
# página más (eso sí tendría coste real de scraping/memoria, aparcado para cuando la Pi tenga
# más margen -- ver README-backup-rebajasdiarias.md / RASPI_REBAJASDIARIAS.md).
KEYWORD_ALERT_PROFILE_DIR = f"{uo.HOME}/.rebajas_chrome_profile_alertas"
# Candado propio de ESTE perfil (14 sep 2026, aviso real: "si pongo varias búsquedas solo me
# sale una" -- confirmado en el log real: `session not created: Chrome instance exited` cuando
# dos alertas se procesan a la vez, dos Chrome sobre el MISMO user-data-dir de alertas chocan
# entre sí igual que chocarían con el del ciclo normal, ver REPO_LOCK_PATH). BLOQUEANTE (LOCK_EX
# sin _NB) a propósito, a diferencia de REPO_LOCK_PATH del ciclo normal -- aquí sí queremos que
# la segunda alerta espere su turno en vez de rendirse, cada búsqueda es rápida (segundos).
KEYWORD_ALERT_LOCK_PATH = f"{uo.HOME}/.rebajas_keyword_alert_lock"
# 14 sep 2026, tercer aviso real: varias alertas seguidas SÍ se procesaban una detrás de otra
# (el candado de arriba ya lo garantizaba), pero sin ninguna pausa entre medias -- en una Pi de
# solo 905MB, el Chrome recién cerrado no siempre suelta su memoria/swap a tiempo antes de que
# el siguiente intente abrir uno nuevo, y las últimas de una tanda larga acababan fallando
# ("session not created", timeouts) al no quedar RAM libre. Pausa real tras cada búsqueda,
# dentro del propio candado -- así la siguiente en la cola no arranca hasta que ha pasado tiempo
# de sobra para que el sistema recupere la memoria.
KEYWORD_ALERT_COOLDOWN_SECONDS = 8
# Reintentos con pausa larga (pedido explícito: "si sale un error que arranque al rato otra vez
# hasta que vaya") -- antes un solo fallo (Chrome no arrancó, timeout...) se rendía del todo y
# el usuario se quedaba sin nada. 3 intentos en total, con tiempo de sobra entre cada uno para
# que la memoria se asiente de verdad (más que el cooldown normal de arriba, porque si ha
# fallado es que la Pi venía más apurada de lo normal).
KEYWORD_ALERT_RETRY_ATTEMPTS = 3
KEYWORD_ALERT_RETRY_DELAY_SECONDS = 30
# 17 sep 2026, pedido explícito tras un fallo real por memoria ("Chandal hombre"/"Chandal
# hombre L", 74-148MB libres, muy por debajo del mínimo): "cuando falla por memoria se puede
# hacer que al poco lo pause todo lo busque de nuevo y luego reanude?" -- los 30s entre los 3
# intentos normales no bastan siempre para que la memoria se asiente de verdad (el ciclo normal
# ya estaba pausado mientras tanto, pero un proceso PAUSADO sigue reteniendo su propia memoria
# en RAM, no la suelta solo por estar parado -- necesita más tiempo real, no solo pausa). Si los
# 3 intentos normales fallan TODOS, antes de rendirse del todo se da una última oportunidad tras
# una espera bastante más larga -- el ciclo normal sigue pausado durante esta espera también
# (ver _scrape_keyword_live), nunca se reanuda a medias.
KEYWORD_ALERT_MEMORY_GRACE_SECONDS = 120


def log(msg):
    print(f"[keyword_alert_search] {msg}", flush=True)


# Conectores sin valor para comparar -- si se dejaran, "Nike DE HOMBRE" "coincidiría" con
# cualquier título que tenga "de" o "hombre" sueltos, dando falsos positivos.
_STOPWORDS_ES = {
    "de", "del", "la", "el", "los", "las", "y", "con", "para", "en", "un", "una",
    "unos", "unas", "por", "mujer", "hombre", "niño", "niña",
}


def _title_matches_keyword(title, keyword):
    """¿Tiene el título encontrado alguna relación real con lo que se buscó? Amazon a veces
    devuelve "resultados relacionados" SIN avisar de nada cuando no hay coincidencia exacta --
    comprobado en vivo el 14 sep 2026: buscar "bimba y lola" devuelve Tommy Hilfiger, Tous,
    Pandora... sin ningún aviso en la propia página de Amazon (pedido explícito del usuario:
    "si no hay productos manda algo similar pero no dice nada"). Se considera coincidencia real
    si el título contiene al menos una palabra significativa (3+ letras, sin conectores) de la
    palabra clave -- no hace falta que coincida entera, "Nike de hombre" encaja con cualquier
    título que solo diga "Nike". Sin ninguna palabra significativa que comparar (rarísimo),
    se da por buena -- mejor no descartar de más por un tecnicismo."""
    title_lower = title.lower()
    words = [
        w for w in re.findall(r"[a-záéíóúñ]+", keyword.lower())
        if len(w) >= 3 and w not in _STOPWORDS_ES
    ]
    if not words:
        return True
    return any(w in title_lower for w in words)


def _pause_main_cycle():
    """Pausa (SIGSTOP) el ciclo normal de scraping mientras dura la búsqueda de la alerta, en
    vez de competir por CPU/RAM con él en una Pi con pocos recursos (pedido explícito: "así no
    se satura la Pi"). SIGSTOP congela el proceso tal cual está, sin tocar su estado interno --
    se reanuda exactamente donde iba. Devuelve el PID pausado (o None si el ciclo normal no
    estaba corriendo, nada que pausar, o el PID guardado ya no existe -- proceso muerto sin que
    le diera tiempo a borrar su propio fichero, p. ej. un SIGKILL por falta de memoria)."""
    pid_path = uo.UPDATE_OFFERS_PID_PATH
    if not os.path.isfile(pid_path):
        return None
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGSTOP)
        log(f"ciclo normal (pid {pid}) pausado mientras dura la búsqueda")
        return pid
    except ProcessLookupError:
        # El PID guardado ya no existe de verdad -- limpia el fichero huérfano de paso.
        try:
            os.remove(pid_path)
        except OSError:
            pass
        return None
    except (OSError, ValueError):
        return None


def _resume_main_cycle(pid):
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGCONT)
        log(f"ciclo normal (pid {pid}) reanudado")
    except OSError:
        pass  # ya había terminado solo mientras tanto (poco probable, pero no pasa nada)


def _kill_orphaned_alert_chrome():
    """15 sep 2026, aviso real del usuario: encontrados procesos de chromium con más de 50
    minutos vivos sobre KEYWORD_ALERT_PROFILE_DIR, tumbando el swap de la Pi al 100% -- "cuando
    algo así pasa debe de matarlo automáticamente". Causa real: si webdriver.Chrome() falla AL
    ARRANCAR (el caso real visto: "session not created: Chrome instance exited"), `driver` se
    queda en None y el finally de _scrape_keyword_live_once() nunca llega a llamar a
    driver.quit() -- pero chromedriver ya puede haber lanzado el chromium real por debajo antes
    de que la sesión terminase de fallar, y ese chromium (más sus procesos hijos: gpu-process,
    zygote, utility...) se queda huérfano sin que nada lo mate.

    En vez de depender de tener una referencia viva al driver/service (frágil, justo el caso
    que falla), mata por perfil: CUALQUIER proceso cuyo cmdline mencione
    KEYWORD_ALERT_PROFILE_DIR se para aquí, éxito o fracaso -- este perfil es EXCLUSIVO de las
    alertas (el ciclo normal usa PROFILE_DIR, sin "_alertas"), así que no hay riesgo de matar
    nada del ciclo normal por error. Se llama siempre desde el finally, incondicionalmente."""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", f"user-data-dir={KEYWORD_ALERT_PROFILE_DIR}"],
            check=False,
        )
    except Exception as e:
        log(f"aviso: no se pudo limpiar chrome huérfano de alertas: {e}")


MIN_AVAILABLE_MB_FOR_CHROME = 150  # ver _wait_for_memory_headroom()
MEMORY_WAIT_MAX_SECONDS = 60
MEMORY_WAIT_POLL_SECONDS = 5


def _available_mb():
    """Lee MemAvailable de /proc/meminfo (estimación real del kernel de cuánta RAM se puede dar
    a un proceso nuevo sin empezar a hacer swap agresivo -- más fiable que restar 'usado' de
    'total' a mano, que no tiene en cuenta la caché reclamable). None si no se puede leer."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB -> MB
    except Exception:
        pass
    return None


def _wait_for_memory_headroom():
    """15 sep 2026, aviso real del usuario tras un colapso de la Pi (swap al 100%, dejó de
    responder por red): 'ves con cuidado con esos picos, intenta que no suba tanto' -- el
    patrón real de ese día fue lanzar OTRO Chrome (el de una alerta) justo cuando ya quedaba
    poca memoria libre, empujando el sistema al límite. En vez de lanzar Chrome a ciegas, espera
    aquí (con tope de MEMORY_WAIT_MAX_SECONDS, nunca bloquea para siempre) a que haya un mínimo
    de margen real. Si nunca se libera memoria a tiempo, sigue igualmente -- mejor intentarlo y
    dejar que el reintento normal de _scrape_keyword_live() se encargue, que quedarse colgado
    aquí sin hacer nada."""
    waited = 0
    while waited < MEMORY_WAIT_MAX_SECONDS:
        available = _available_mb()
        if available is None or available >= MIN_AVAILABLE_MB_FOR_CHROME:
            return
        log(f"memoria justa ({available}MB libres, mínimo {MIN_AVAILABLE_MB_FOR_CHROME}MB) -- "
            f"esperando antes de lanzar Chrome para no empujar la Pi al límite")
        time.sleep(MEMORY_WAIT_POLL_SECONDS)
        waited += MEMORY_WAIT_POLL_SECONDS


def _scrape_keyword_live_once(
    keyword,
    min_discount_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT,
    max_discount_percent=MAX_SAVING_PERCENT_KEYWORD_ALERT,
):
    """Un único intento -- abre un Chrome real (perfil propio, aparte del ciclo normal) y busca
    `keyword` en Amazon.es. Umbral bajo de las alertas por defecto (min/max sin tocar en las
    llamadas de siempre); el buscador principal (20 sep 2026, ver search_requests_listener.py)
    pasa el umbral estándar del resto del catálogo (30-80%) en su lugar -- mismo motor real,
    solo cambia el filtro de descuento. NO gestiona el candado ni la pausa del ciclo normal --
    eso lo hace _scrape_keyword_live(), que envuelve TODA la secuencia de reintentos de una vez
    (ver comentario ahí). Devuelve None si algo falla de verdad al abrir/usar Chrome (fallo
    temporal, NO se debe tocar nada de lo ya guardado). Devuelve una lista (puede estar vacía)
    si se completó bien."""
    _wait_for_memory_headroom()
    driver = None
    try:
        driver = uo.build_driver(profile_dir=KEYWORD_ALERT_PROFILE_DIR)
        return uo.scrape_keyword(
            driver,
            keyword,
            CATEGORY_LABEL,
            min_discount_percent=min_discount_percent,
            max_discount_percent=max_discount_percent,
            max_products=MAX_PRODUCTS_KEYWORD_ALERT,
        )
    except Exception as e:
        log(f"ERROR scrapeando '{keyword}': {e}")
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        _kill_orphaned_alert_chrome()
        # Deja que la memoria de este intento se asiente antes del siguiente (o de reanudar el
        # ciclo normal, ver _scrape_keyword_live) -- mismo motivo de siempre, un Chrome recién
        # cerrado no siempre suelta RAM/swap al instante.
        time.sleep(KEYWORD_ALERT_COOLDOWN_SECONDS)


def _scrape_keyword_live(
    keyword,
    min_discount_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT,
    max_discount_percent=MAX_SAVING_PERCENT_KEYWORD_ALERT,
):
    """Como _scrape_keyword_live_once(), pero con reintentos (14 sep 2026, pedido explícito: "si
    sale un error en la búsqueda que arranque al rato otra vez hasta que vaya") -- un fallo
    puntual (Chrome sin memoria para arrancar, timeout de red...) ya no se rinde a la primera,
    reintenta unas cuantas veces con pausa real entre medias.

    16 sep 2026, segunda vuelta tras un fallo real ("Adidas talla 39" se rindió los 3 intentos
    por falta de memoria, 83-115MB libres): antes el ciclo normal se REANUDABA entre cada
    intento (la pausa vivía dentro de _scrape_keyword_live_once, un intento a la vez), así que
    competía justo por la memoria que acababa de causar el fallo durante los 30s de espera entre
    reintentos -- el peor momento posible para dejarlo correr. Pedido explícito: "debería
    pausar otra vez el scraping para darle prioridad a la alerta hasta que termine el ciclo de
    alerta que es poco tiempo y luego reanudar". Ahora el candado (ver KEYWORD_ALERT_LOCK_PATH,
    evita dos Chrome a la vez sobre el mismo perfil) Y la pausa del ciclo normal envuelven TODA
    la secuencia de reintentos de una sola vez -- el ciclo normal solo se reanuda al final
    (éxito o los 3 intentos agotados), nunca a medias.

    17 sep 2026, tercera vuelta tras otro fallo real por memoria ("Chandal hombre"/"Chandal
    hombre L", 74-148MB libres): "cuando falla por memoria se puede hacer que al poco lo pause
    todo lo busque de nuevo y luego reanude?" -- si los 3 intentos normales fallan TODOS, en vez
    de rendirse ahí mismo se da una última oportunidad de verdad tras
    KEYWORD_ALERT_MEMORY_GRACE_SECONDS (bastante más larga que los 30s de entre intentos, para
    que la memoria tenga tiempo real de asentarse) -- el ciclo normal sigue pausado también
    durante esa espera larga, se reanuda solo al final de todo. Sigue devolviendo None solo si
    ni los 3 intentos normales NI esta última oportunidad consiguen nada."""
    lock_file = open(KEYWORD_ALERT_LOCK_PATH, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)  # bloqueante -- espera su turno, no se rinde
    try:
        paused_pid = _pause_main_cycle()
        try:
            for attempt in range(1, KEYWORD_ALERT_RETRY_ATTEMPTS + 1):
                result = _scrape_keyword_live_once(
                    keyword, min_discount_percent, max_discount_percent
                )
                if result is not None:
                    return result
                if attempt < KEYWORD_ALERT_RETRY_ATTEMPTS:
                    log(f"'{keyword}': intento {attempt} fallido, reintentando en "
                        f"{KEYWORD_ALERT_RETRY_DELAY_SECONDS}s...")
                    time.sleep(KEYWORD_ALERT_RETRY_DELAY_SECONDS)
            log(f"'{keyword}': {KEYWORD_ALERT_RETRY_ATTEMPTS} intentos fallidos -- última "
                f"oportunidad tras {KEYWORD_ALERT_MEMORY_GRACE_SECONDS}s de margen real para "
                f"que la memoria se asiente")
            time.sleep(KEYWORD_ALERT_MEMORY_GRACE_SECONDS)
            result = _scrape_keyword_live_once(
                keyword, min_discount_percent, max_discount_percent
            )
            if result is not None:
                return result
            log(f"'{keyword}': también falló la última oportunidad, se rinde por ahora")
            return None
        finally:
            # Incondicional (éxito, fallo total, o incluso una excepción inesperada) -- nunca se
            # debe dejar el ciclo normal pausado para siempre por un error aquí dentro.
            _resume_main_cycle(paused_pid)
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_file.close()


def _send_keyword_alert_push(uid, keyword, new_offers):
    """Mismo formato/topic que notify_keyword_alerts() en update_offers.py -- pero aquí se
    avisa con lo que acaba de traer la propia búsqueda en vivo, no con lo que ya hubiera en el
    catálogo normal. Nunca debe tumbar refresh_keyword_alert() si falla."""
    try:
        if len(new_offers) == 1:
            o = new_offers[0]
            body = f"{o['title'][:80]} — {o['price']} €"
        else:
            body = f'{len(new_offers)} ofertas nuevas para "{keyword}"'
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title="🔍 Encontramos ofertas de tu alerta",
                body=body,
            ),
            data={
                "type": "keyword_alert_offers",
                "keyword": keyword,
                "title": f'Ofertas de "{keyword}"',
            },
            topic=f"user_{uid}",
        ))
        log(f"  push enviado a {uid} por '{keyword}' ({len(new_offers)} oferta(s) nueva(s))")
    except Exception as e:
        log(f"  aviso: no se pudo mandar push de '{keyword}' a {uid}: {e}")


def _send_keyword_alert_empty_push(uid, keyword):
    """14 sep 2026, aviso real: "he puesto una alerta y tarda mucho en aparecer algo, sigue sin
    aparecer" -- la búsqueda en realidad SÍ había terminado bien (resultCount: 0 en Firestore),
    solo que Amazon no tenía ningún descuento real para esa palabra en ese momento. Como el push
    de "encontramos ofertas" de arriba solo se manda si hay algo nuevo, quien busca se queda
    esperando para siempre sin enterarse de que ya se ha terminado de buscar. Solo se llama
    desde la búsqueda EN VIVO al añadir la alerta (ver first_search más abajo) -- nunca desde el
    cron de repaso periódico (keyword_alert_cleanup.py), que sí volvería a encontrar 0 en la
    mayoría de sus pasadas para la mayoría de palabras: mandar este aviso ahí sería spam, no
    información útil."""
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title="🔍 Búsqueda terminada",
                body=f'No encontramos ningún descuento real para "{keyword}" ahora mismo -- '
                     'seguimos vigilando y te avisamos en cuanto aparezca uno.',
            ),
            data={"type": "keyword_alert_empty", "keyword": keyword},
            topic=f"user_{uid}",
        ))
        log(f"  push de '0 resultados' enviado a {uid} por '{keyword}'")
    except Exception as e:
        log(f"  aviso: no se pudo mandar push de '0 resultados' de '{keyword}' a {uid}: {e}")


def refresh_keyword_alert(db, uid, keyword, notify_new=True, first_search=False):
    """Busca `keyword` en vivo (scraping real, umbral 1% sin techo), guarda lo que encuentre en
    keyword_alert_offers y borra lo que ya no aparezca -- pedido explícito "si ya no están las
    ofertas que se eliminen". Devuelve la lista de ofertas NUEVAS (no vistas antes para este
    uid+keyword, puede estar vacía) y manda el push por ellas si notify_new=True. Devuelve None
    si el scraping no se pudo completar (candado ocupado, Chrome falló...) -- en ese caso NO
    toca nada de lo ya guardado.

    `first_search` (14 sep 2026): True solo cuando esto es la búsqueda inmediata al añadir la
    alerta (ver search_requests_listener.py) -- si no encuentra nada, avisa de todos modos (ver
    _send_keyword_alert_empty_push) para que quien la añadió no se quede esperando sin saber si
    ya ha terminado o sigue en marcha. False en el cron de repaso periódico
    (keyword_alert_cleanup.py), donde 0 resultados nuevos es lo normal la mayoría de las veces
    y avisar cada vez sería spam."""
    offers = _scrape_keyword_live(keyword)
    if offers is None:
        return None

    coll = db.collection("keyword_alert_offers")
    existing = list(
        coll.where(filter=FieldFilter("uid", "==", uid))
        .where(filter=FieldFilter("keyword", "==", keyword))
        .stream()
    )
    existing_by_asin = {(d.to_dict() or {}).get("asin"): d for d in existing}
    fresh_asins = {o["id"] for o in offers}

    removed = 0
    for asin, doc in existing_by_asin.items():
        if asin not in fresh_asins:
            doc.reference.delete()
            removed += 1

    new_offers = []
    for o in offers:
        is_new = o["id"] not in existing_by_asin
        doc_id = f"{uid}_{keyword}_{o['id']}"
        coll.document(doc_id).set({
            "uid": uid,
            "keyword": keyword,
            "asin": o["id"],
            "title": o["title"],
            "image": o["image"],
            "url": o["url"],
            "price": o["price"],
            "originalPrice": o["original_price"],
            "discountPercent": o["discount_percent"],
            # 14 sep 2026, aviso real: "las estrellas no salen en las cards de tus alertas" --
            # scrape_keyword() ya las traía (parse_rating()/parse_rating_count() en
            # update_offers.py), solo no se estaban guardando aquí.
            "rating": o.get("rating"),
            "ratingCount": o.get("rating_count"),
            # 14 sep 2026, pedido explícito: "si no hay productos manda algo similar pero no
            # dice nada, podríamos poner un pequeño cartel que diga 'o similares'" -- ver
            # _title_matches_keyword() arriba. false = probablemente un sustituto de Amazon,
            # no lo que se buscó de verdad; la app lo marca con un aviso en vez de nada.
            "exactMatch": _title_matches_keyword(o["title"], keyword),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        if is_new:
            new_offers.append(o)

    if removed:
        log(f"'{keyword}' ({uid}): {removed} oferta(s) quitada(s), ya no vigente(s)")
    if new_offers:
        log(f"'{keyword}' ({uid}): {len(new_offers)} oferta(s) nueva(s) de {len(offers)} total")
        if notify_new:
            _send_keyword_alert_push(uid, keyword, new_offers)
    elif first_search:
        log(f"'{keyword}' ({uid}): 0 resultados en la búsqueda inicial")
        _send_keyword_alert_empty_push(uid, keyword)
    return new_offers
