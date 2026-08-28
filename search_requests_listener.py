#!/usr/bin/env python3
"""
search_requests_listener.py — buscador con Amazon en vivo, en TIEMPO REAL (29 ago 2026,
pedido explícito: "en la otra app era super rápida... el buscador primero busca directamente
en amazon para que sea rapido y luego salen las ofertas que tenemos"). Sustituye a
check_search_requests.py, que sondeaba Firestore cada 1 minuto (cron) -- demasiado lento para
lo que el usuario quería. Este proceso se queda corriendo TODO EL RATO y reacciona al instante
en cuanto se crea una búsqueda nueva, usando el listener en tiempo real de Firestore (Admin
SDK) en vez de sondear -- respuesta típica de 1-3 segundos en vez de hasta 60.

Sigue siendo gratis (la propia Pi, no un servicio de pago) -- el usuario explícitamente
descartó pagar Firebase Cloud Functions para tener velocidad, así que la solución fue cambiar
"sondear cada minuto" por "escuchar en tiempo real", no gastar dinero.

IMPORTANTE: esto es un SERVICIO (systemd, ver rebajasdiarias-search.service), NO un cron --
necesita estar siempre vivo, no lanzarse y terminar. systemd lo reinicia solo si se cae.

check_search_requests.py se deja en el repo sin más (sus funciones de disponibilidad se
reutilizan aquí por import) pero ya NO está en el crontab -- este proceso lo sustituye del
todo para las búsquedas. El sondeo periódico de disponibilidad (para el aviso de Telegram
cuando nadie está buscando) se mantiene aquí también, en un hilo aparte.
"""

import os
import threading
import time

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

import amazon_paapi as paapi
from update_offers import FIREBASE_CREDENTIALS_PATH
from check_search_requests import (
    HEALTH_CHECK_INTERVAL_MINUTES,
    HEALTH_CHECK_QUERY,
    _load_availability_state,
    _note_availability,
)

db = None


def log(msg):
    print(f"[search_requests_listener] {msg}", flush=True)


def _process_request(doc_id, data):
    query_text = (data.get("query") or "").strip()
    doc_ref = db.collection("search_requests").document(doc_id)
    if not query_text:
        doc_ref.set({"status": "error"}, merge=True)
        return
    try:
        items = paapi.search_amazon(query_text)
        _note_availability(items is not None, _load_availability_state())
        if items is None:
            doc_ref.set({"status": "unavailable"}, merge=True)
            log(f"{query_text!r}: Amazon no disponible ahora mismo")
            return
        offers = paapi.offers_from_items(items)
        doc_ref.set(
            {"status": "done", "results": offers, "resultCount": len(offers)},
            merge=True,
        )
        log(f"{query_text!r}: {len(offers)} resultado(s) con descuento real")
    except Exception as e:
        log(f"ERROR buscando {query_text!r}: {e}")
        try:
            doc_ref.set({"status": "error"}, merge=True)
        except Exception:
            pass


def _on_snapshot(col_snapshot, changes, read_time):
    # Cada búsqueda se procesa en su propio hilo -- si llegan dos peticiones a la vez, no se
    # bloquean entre sí esperando la respuesta de Amazon de la otra.
    for change in changes:
        if change.type.name in ("ADDED", "MODIFIED"):
            data = change.document.to_dict() or {}
            if data.get("status") == "pending":
                threading.Thread(
                    target=_process_request,
                    args=(change.document.id, data),
                    daemon=True,
                ).start()


def _health_check_loop():
    """Sondeo periódico de disponibilidad (29 ago 2026, pedido explícito: avisar por Telegram
    en los dos sentidos) -- con el listener en tiempo real ya no hace falta para procesar
    búsquedas de verdad, pero sigue haciendo falta para detectar un cambio de disponibilidad
    cuando NADIE está buscando en ese momento. Corre en un hilo aparte, cada 30 min como
    mucho."""
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL_MINUTES * 60)
        try:
            items = paapi.search_amazon(HEALTH_CHECK_QUERY, item_count=1)
            _note_availability(items is not None, _load_availability_state())
        except Exception as e:
            log(f"aviso: fallo en el sondeo periódico de disponibilidad: {e}")


def main():
    global db
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        log("ERROR: no hay credenciales de Firebase, no se puede arrancar")
        return
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    threading.Thread(target=_health_check_loop, daemon=True).start()

    query = db.collection("search_requests").where(filter=FieldFilter("status", "==", "pending"))
    query.on_snapshot(_on_snapshot)
    log("escuchando search_requests en tiempo real...")

    # on_snapshot corre en un hilo propio de la librería -- este hilo principal solo se queda
    # vivo para que el proceso no termine (systemd lo reinicia si de verdad se cae).
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
