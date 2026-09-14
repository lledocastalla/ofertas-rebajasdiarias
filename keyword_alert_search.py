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

Dos disparadores comparten esta misma función, para no duplicar la lógica de guardar/quitar:
  - search_requests_listener.py, al momento de añadir una palabra (ver AmazonSearchService.
    createKeywordAlertRequest() en la app) -- un Chrome headless tarda unos segundos en abrir y
    buscar, no es instantáneo como la API lo hubiera sido, pero es lo que de verdad funciona.
  - keyword_alert_cleanup.py, en un cron propio cada hora -- vuelve a comprobar CADA palabra ya
    guardada de CADA usuario, añade lo nuevo (avisa por push) y quita lo que ya no cumpla.

Candado NO bloqueante compartido con update_offers.py/check_submissions.py (mismo
REPO_LOCK_PATH, mismo perfil de Chrome -- dos Chrome a la vez sobre el mismo user-data-dir
fallan) -- si el ciclo normal de scraping está corriendo ahora mismo, se sale sin más, nunca se
espera bloqueado a que termine un ciclo entero (puede tardar minutos). Se reintenta solo en el
siguiente disparo (cron horario, o la próxima vez que alguien añada/repita la alerta).

Colección `keyword_alert_offers/{uid}_{keyword}_{asin}` -- id determinista para que guardar dos
veces la misma oferta la actualice en vez de duplicarla.

Umbral 1% sin techo (MIN_SAVING_PERCENT_KEYWORD_ALERT/MAX_SAVING_PERCENT_KEYWORD_ALERT), mucho
más bajo que el 30-80% del resto del proyecto A PROPÓSITO (pedido explícito del usuario) --
esto es una palabra muy concreta pedida por una persona, no el catálogo general: mejor un 5%
real que nada. Nunca toca MIN_DISCOUNT_PERCENT/MAX_DISCOUNT_PERCENT de update_offers.py, que
siguen en 30-80% para el ciclo normal del catálogo.
"""

import fcntl

from firebase_admin import firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

import update_offers as uo

MIN_SAVING_PERCENT_KEYWORD_ALERT = 1
MAX_SAVING_PERCENT_KEYWORD_ALERT = 100  # "hasta el máximo" -- sin techo, a diferencia del 80%
CATEGORY_LABEL = "Alerta"


def log(msg):
    print(f"[keyword_alert_search] {msg}", flush=True)


def _scrape_keyword_live(keyword):
    """Abre un Chrome real y busca `keyword` en Amazon.es con el umbral bajo de las alertas.
    Devuelve None si el candado del repo está ocupado (ciclo normal de scraping en curso) o si
    algo falla de verdad al abrir/usar Chrome -- en los dos casos NO se debe tocar nada de lo
    ya guardado (fallo temporal, no que las ofertas hayan desaparecido de verdad). Devuelve una
    lista (puede estar vacía) si el scraping se completó con normalidad."""
    lock_file = open(uo.REPO_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"'{keyword}': el perfil de Chrome está en uso ahora mismo, se reintenta luego")
        return None

    driver = None
    try:
        driver = uo.build_driver()
        return uo.scrape_keyword(
            driver,
            keyword,
            CATEGORY_LABEL,
            min_discount_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT,
            max_discount_percent=MAX_SAVING_PERCENT_KEYWORD_ALERT,
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
