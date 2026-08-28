#!/usr/bin/env python3
"""
check_search_requests.py — buscador con Amazon en vivo (28 ago 2026, pedido explícito del
usuario, ver amazon_paapi.py para el porqué de la arquitectura).

Cron dedicado, cada 1 minuto (el mínimo que permite cron) -- mismo espíritu que
check_submissions.py: si no hay ninguna búsqueda pendiente en Firestore, termina al momento sin
llamar a Amazon. A diferencia de check_submissions.py, esto NUNCA toca git ni offers.json --
los resultados son por usuario y efímeros, viven solo en el propio documento de Firestore de la
búsqueda, así que no hace falta ningún candado compartido con update_offers.py.

Además hace de "vigía" de la disponibilidad de la API (pedido explícito, 28 ago 2026: "cuando
no estén activas que me avise al telegram de la pi y cuando se activen también") -- Amazon
exige un mínimo de 10 ventas válidas en los últimos 30 días para el acceso a PA-API a través de
la Creators API, así que el acceso entra y sale solo según las ventas del mes. Un cambio de
estado (disponible→no disponible o al revés) manda un aviso por el mismo Telegram que ya usa el
resto de la Pi; NO se avisa en cada ciclo, solo cuando cambia de verdad (ver
_load_availability_state()/_save_availability_state()).

Uso: cron cada 1 min, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import json
import os
import time

import amazon_paapi as paapi
from update_offers import FIREBASE_CREDENTIALS_PATH, notify_telegram

SEARCH_REQUESTS_MAX_PER_CYCLE = 10  # de sobra para uso normal; evita un cliente que abuse

HOME = os.path.expanduser("~")
AVAILABILITY_STATE_PATH = f"{HOME}/amazon_api_availability.json"
# Si no hay ninguna búsqueda real de un usuario, se hace como mucho una petición de sondeo
# cada 30 min -- suficiente para avisar con margen razonable sin gastar cupo de la API de
# Amazon en comprobar algo que probablemente no ha cambiado desde hace un minuto.
HEALTH_CHECK_INTERVAL_MINUTES = 30
HEALTH_CHECK_QUERY = "ofertas"  # búsqueda genérica, solo para comprobar si la API responde


def log(msg):
    print(f"[check_search_requests] {msg}", flush=True)


def _load_availability_state():
    if not os.path.isfile(AVAILABILITY_STATE_PATH):
        return {"available": None, "last_checked": 0}
    try:
        with open(AVAILABILITY_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"available": None, "last_checked": 0}


def _save_availability_state(available: bool):
    try:
        with open(AVAILABILITY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"available": available, "last_checked": time.time()}, f)
    except Exception as e:
        log(f"aviso: no se pudo guardar el estado de disponibilidad: {e}")


def _note_availability(available: bool, state: dict):
    """Avisa por Telegram SOLO si el estado cambia respecto a la última vez conocida --
    state['available'] puede ser None la primera vez que corre esto (nunca avisa en ese primer
    caso, solo guarda el punto de partida)."""
    previous = state.get("available")
    _save_availability_state(available)
    if previous is None or previous == available:
        return
    if available:
        notify_telegram(
            "✅ El buscador con Amazon en vivo ha vuelto a estar disponible "
            "(ya se cumplen los requisitos de elegibilidad de nuevo)."
        )
        log("aviso Telegram: API vuelve a estar disponible")
    else:
        notify_telegram(
            "⚠️ El buscador con Amazon en vivo ha dejado de estar disponible ahora mismo "
            "(revisa ventas de los últimos 30 días / elegibilidad en afiliados.amazon.es)."
        )
        log("aviso Telegram: API ha dejado de estar disponible")


def _get_firestore_db():
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        log(f"aviso: no se pudo inicializar Firestore: {e}")
        return None


def main():
    db = _get_firestore_db()
    if db is None:
        return

    from google.cloud.firestore_v1.base_query import FieldFilter

    try:
        query = (
            db.collection("search_requests")
            .where(filter=FieldFilter("status", "==", "pending"))
            .limit(SEARCH_REQUESTS_MAX_PER_CYCLE)
        )
        pending = [(doc.id, doc.to_dict() or {}) for doc in query.stream()]
    except Exception as e:
        log(f"aviso: no se pudo consultar Firestore de búsquedas: {e}")
        return

    if not pending:
        # Nadie está buscando ahora mismo -- de vez en cuando se sondea igualmente la API para
        # detectar un cambio de disponibilidad sin depender de que alguien busque justo
        # entonces (petición explícita: avisar en los dos sentidos, sin que dependa del azar).
        state = _load_availability_state()
        elapsed_minutes = (time.time() - state.get("last_checked", 0)) / 60
        if elapsed_minutes < HEALTH_CHECK_INTERVAL_MINUTES:
            return
        items = paapi.search_amazon(HEALTH_CHECK_QUERY, item_count=1)
        _note_availability(items is not None, state)
        return

    log(f"{len(pending)} búsqueda(s) pendiente(s)")
    state = _load_availability_state()
    for request_id, data in pending:
        query_text = (data.get("query") or "").strip()
        doc_ref = db.collection("search_requests").document(request_id)
        if not query_text:
            doc_ref.set({"status": "error"}, merge=True)
            continue
        try:
            items = paapi.search_amazon(query_text)
            _note_availability(items is not None, state)
            state = _load_availability_state()  # recoger el estado recién guardado
            if items is None:
                # "no disponible ahora" (ver amazon_paapi.py) -- el cliente ya sabe seguir con
                # otras cosas cuando ve este estado (petición explícita del usuario: "como las
                # API no siempre hay acceso pues no pasa nada el buscador sigue con otras
                # cosas").
                doc_ref.set({"status": "unavailable"}, merge=True)
                log(f"  {query_text!r}: Amazon no disponible ahora mismo")
                continue
            offers = paapi.offers_from_items(items)
            doc_ref.set(
                {"status": "done", "results": offers, "resultCount": len(offers)},
                merge=True,
            )
            log(f"  {query_text!r}: {len(offers)} resultado(s) con descuento real")
        except Exception as e:
            log(f"  ERROR buscando {query_text!r}: {e}")
            try:
                doc_ref.set({"status": "error"}, merge=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
