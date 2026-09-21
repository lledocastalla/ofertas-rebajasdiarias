#!/usr/bin/env python3
"""
keyword_alert_cleanup.py — repasa cada hora TODAS las alertas de palabra clave de TODOS los
usuarios (users/{uid}.keywordAlerts) y llama a keyword_alert_search.refresh_keyword_alert() por
cada una: añade ofertas nuevas (avisa por push) y quita las que ya no sigan vivas (pedido
explícito del usuario, "si ya no están las ofertas que se eliminen").

Cron propio, NO enganchado al ciclo del catálogo normal (pedido explícito del usuario, "proceso
aparte, más frecuente" -- con pocos usuarios todavía el volumen de peticiones a Amazon es bajo,
no hace falta compartir ciclo con update_offers.py). Cada palabra guardada dispara una búsqueda
en vivo real -- mismo motor que search_requests_listener.py usa al añadir una alerta.
"""

import firebase_admin
from firebase_admin import credentials, firestore

import keyword_alert_search as kas
from update_offers import FIREBASE_CREDENTIALS_PATH


def log(msg):
    print(f"[keyword_alert_cleanup] {msg}", flush=True)


def main():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    checked = 0
    unavailable = 0
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        # Interruptor general (ver alerts_service.dart) -- si el usuario apagó los avisos, no
        # tiene sentido seguir gastando peticiones a Amazon revisando sus palabras.
        if data.get("keywordAlertsEnabled") is False:
            continue
        keywords = [k.strip() for k in data.get("keywordAlerts") or [] if k.strip()]
        for kw in keywords:
            checked += 1
            # 21 sep 2026, ver mark_live_search_pending()/yield_to_live_search() en
            # keyword_alert_search.py -- este repaso es de fondo y puede esperar; una búsqueda
            # real de un usuario delante de la pantalla no debería hacer cola detrás de él.
            kas.yield_to_live_search()
            try:
                result = kas.refresh_keyword_alert(db, doc.id, kw, notify_new=True)
            except Exception as e:
                log(f"ERROR revisando '{kw}' ({doc.id}): {e}")
                continue
            if result is None:
                unavailable += 1

    if unavailable and unavailable == checked and checked > 0:
        log(f"{checked} alerta(s) revisada(s) -- Amazon no disponible ahora mismo, nada tocado.")
    else:
        log(f"{checked} alerta(s) revisada(s).")


if __name__ == "__main__":
    main()
