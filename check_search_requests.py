#!/usr/bin/env python3
"""
check_search_requests.py — buscador con Amazon en vivo (28 ago 2026, pedido explícito del
usuario, ver amazon_paapi.py para el porqué de la arquitectura).

Cron dedicado, cada 1 minuto (el mínimo que permite cron) -- mismo espíritu que
check_submissions.py: si no hay ninguna búsqueda pendiente en Firestore, termina al momento sin
llamar a Amazon. A diferencia de check_submissions.py, esto NUNCA toca git ni offers.json --
los resultados son por usuario y efímeros, viven solo en el propio documento de Firestore de la
búsqueda, así que no hace falta ningún candado compartido con update_offers.py.

Uso: cron cada 1 min, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import os

import amazon_paapi as paapi
from update_offers import FIREBASE_CREDENTIALS_PATH

SEARCH_REQUESTS_MAX_PER_CYCLE = 10  # de sobra para uso normal; evita un cliente que abuse


def log(msg):
    print(f"[check_search_requests] {msg}", flush=True)


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
        return

    log(f"{len(pending)} búsqueda(s) pendiente(s)")
    for request_id, data in pending:
        query_text = (data.get("query") or "").strip()
        doc_ref = db.collection("search_requests").document(request_id)
        if not query_text:
            doc_ref.set({"status": "error"}, merge=True)
            continue
        try:
            items = paapi.search_amazon(query_text)
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
