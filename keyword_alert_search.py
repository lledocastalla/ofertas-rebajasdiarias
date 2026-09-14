#!/usr/bin/env python3
"""
keyword_alert_search.py — motor compartido de las alertas de palabra clave con búsqueda en
vivo en Amazon (14 sep 2026, pedido explícito: "que busque las ofertas sobre esa palabra...
que tarde lo menos posible, y solo que busque cosas que estén en oferta" + "que se guarden y si
ya no están las ofertas que se eliminen" + "desde 1% de descuento hasta el máximo").

Dos disparadores comparten esta misma función, para no duplicar la lógica de guardar/quitar:
  - search_requests_listener.py, al momento de añadir una palabra (ver AmazonSearchService.
    createKeywordAlertRequest() en la app) -- responde en 1-3s, el usuario lo ve casi al
    instante con el aviso push.
  - keyword_alert_cleanup.py, en un cron propio cada hora -- vuelve a comprobar CADA palabra ya
    guardada de CADA usuario, añade lo nuevo (avisa por push) y quita lo que ya no cumpla
    (agotado, ya no rebajado). Reutiliza la MISMA búsqueda de siempre -- la Creators API no
    ofrece un "consultar un ASIN suelto" en este flujo, así que "repetir la búsqueda de la
    palabra" es también la forma de comprobar si una oferta guardada sigue viva.

Colección `keyword_alert_offers/{uid}_{keyword}_{asin}` -- id determinista para que guardar dos
veces la misma oferta la actualice en vez de duplicarla (SetOptions(merge) del lado Firestore
ya lo resuelve solo con .set(..., merge=True)).

Umbral 1% (MIN_SAVING_PERCENT_KEYWORD_ALERT), mucho más bajo que el 30% del resto del proyecto
A PROPÓSITO (pedido explícito del usuario) -- esto es una palabra muy concreta pedida por una
persona, no el catálogo general: mejor un 5% real que nada. Nunca toca MIN_DISCOUNT_PERCENT de
amazon_paapi.py, que sigue en 30% para el buscador normal de la app.
"""

from firebase_admin import firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

import amazon_paapi as paapi

MIN_SAVING_PERCENT_KEYWORD_ALERT = 1


def log(msg):
    print(f"[keyword_alert_search] {msg}", flush=True)


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
    """Busca `keyword` en vivo (umbral 1%), guarda lo que encuentre en keyword_alert_offers y
    borra lo que ya no aparezca -- pedido explícito "si ya no están las ofertas que se
    eliminen". Devuelve la lista de ofertas NUEVAS (no vistas antes para este uid+keyword,
    puede estar vacía) y manda el push por ellas si notify_new=True. Devuelve None si Amazon no
    está disponible ahora mismo -- en ese caso NO toca nada de lo ya guardado, para no borrar
    por un fallo temporal de la API en vez de porque la oferta de verdad haya desaparecido."""
    items = paapi.search_amazon(keyword, min_saving_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT)
    if items is None:
        return None
    offers = paapi.offers_from_items(
        items, min_discount_percent=MIN_SAVING_PERCENT_KEYWORD_ALERT
    )

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
