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
import signal

from firebase_admin import firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

import update_offers as uo

MIN_SAVING_PERCENT_KEYWORD_ALERT = 1
MAX_SAVING_PERCENT_KEYWORD_ALERT = 100  # "hasta el máximo" -- sin techo, a diferencia del 80%
CATEGORY_LABEL = "Alerta"
MAX_PRODUCTS_KEYWORD_ALERT = 16  # 14 sep 2026, segundo aviso real: "solo encuentra 5 ofertas y
# sé que hay muchas más" -- subido de 5 a 16 (una sola página de resultados de Amazon.es trae
# normalmente entre 16 y 24 tarjetas, no hace falta paginar más para una alerta concreta).
KEYWORD_ALERT_PROFILE_DIR = f"{uo.HOME}/.rebajas_chrome_profile_alertas"
# Candado propio de ESTE perfil (14 sep 2026, aviso real: "si pongo varias búsquedas solo me
# sale una" -- confirmado en el log real: `session not created: Chrome instance exited` cuando
# dos alertas se procesan a la vez, dos Chrome sobre el MISMO user-data-dir de alertas chocan
# entre sí igual que chocarían con el del ciclo normal, ver REPO_LOCK_PATH). BLOQUEANTE (LOCK_EX
# sin _NB) a propósito, a diferencia de REPO_LOCK_PATH del ciclo normal -- aquí sí queremos que
# la segunda alerta espere su turno en vez de rendirse, cada búsqueda es rápida (segundos).
KEYWORD_ALERT_LOCK_PATH = f"{uo.HOME}/.rebajas_keyword_alert_lock"


def log(msg):
    print(f"[keyword_alert_search] {msg}", flush=True)


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


def _scrape_keyword_live(keyword):
    """Abre un Chrome real (perfil propio, aparte del ciclo normal) y busca `keyword` en
    Amazon.es con el umbral bajo de las alertas -- pausando el ciclo normal mientras dura, si
    estaba corriendo. Candado BLOQUEANTE propio del perfil de alertas primero (ver
    KEYWORD_ALERT_LOCK_PATH) -- si dos alertas se añaden seguidas, la segunda espera a que
    termine la primera en vez de abrir un segundo Chrome sobre el mismo user-data-dir a la vez
    (eso es lo que crasheaba antes: "session not created: Chrome instance exited"). Devuelve
    None si algo falla de verdad al abrir/usar Chrome (fallo temporal, NO se debe tocar nada de
    lo ya guardado). Devuelve una lista (puede estar vacía) si el scraping se completó bien."""
    lock_file = open(KEYWORD_ALERT_LOCK_PATH, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)  # bloqueante -- espera su turno, no se rinde
    try:
        paused_pid = _pause_main_cycle()
        driver = None
        try:
            driver = uo.build_driver(profile_dir=KEYWORD_ALERT_PROFILE_DIR)
            return uo.scrape_keyword(
                driver,
                keyword,
                CATEGORY_LABEL,
                min_discount_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT,
                max_discount_percent=MAX_SAVING_PERCENT_KEYWORD_ALERT,
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


def refresh_keyword_alert(db, uid, keyword, notify_new=True):
    """Busca `keyword` en vivo (scraping real, umbral 1% sin techo), guarda lo que encuentre en
    keyword_alert_offers y borra lo que ya no aparezca -- pedido explícito "si ya no están las
    ofertas que se eliminen". Devuelve la lista de ofertas NUEVAS (no vistas antes para este
    uid+keyword, puede estar vacía) y manda el push por ellas si notify_new=True. Devuelve None
    si el scraping no se pudo completar (candado ocupado, Chrome falló...) -- en ese caso NO
    toca nada de lo ya guardado."""
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
    return new_offers
