#!/usr/bin/env python3
"""
sync_search_stats.py — nº de ofertas encontradas por término, para el panel de admin
"Qué pide la gente" (24 sep 2026, aviso real: "hay cosas que pone sin dato").

Hasta ahora ese número solo vivía en el documento de cada búsqueda (search_requests), que la
app borraba al salir del buscador y que el panel solo cruzaba con las 300 últimas -- la
mayoría de términos acababan en "sin dato todavía". Este cron copia el resultCount de la
búsqueda más reciente de cada término a su propio search_stats/{término} (lastResultCount),
donde ya no se pierde.

A propósito es un proceso APARTE y no un cambio en search_requests_listener.py -- pedido
explícito del usuario: "no cambies nada del buscador que ahora va muy bien". Solo lee
búsquedas ya terminadas; nunca toca una pendiente ni el resultado que ve el usuario.

Además poda las búsquedas de más de 30 días (una vez al día): desde el 24 sep ni la app ni la
web pueden borrarlas (firestore.rules), así que sin esto la colección crecería sin fin.

Y lo mismo para Alertas (24 sep 2026, aviso real: "en el buscador sí veo las ofertas
encontradas pero en alertas no salen"): cuántas ofertas tiene encontradas ahora mismo cada
palabra (keyword_alert_offers, contado en total SIN mirar de quién es) -> keyword_alert_stats
(lastResultCount). El panel de admin no puede leer keyword_alert_offers (privadas de cada
usuario, firestore.rules), así que el total anónimo lo calcula aquí la Pi. Con count() de
Firestore (1 lectura por cada 1000 documentos) y como mucho una vez por hora.

Uso: cron cada 10 min, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from update_offers import FIREBASE_CREDENTIALS_PATH

HOME = os.path.expanduser("~")
STATE_PATH = f"{HOME}/sync_search_stats_state.json"
RETENTION_DAYS = 30
# Primera ejecución: se recorre todo lo que haya (relleno de lo buscado antes de hoy).
FIRST_RUN_LOOKBACK_DAYS = 60


def log(msg):
    print(f"[sync_search_stats] {datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"aviso: no se pudo guardar el estado: {e}")


def main():
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return
    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_CREDENTIALS_PATH))
    db = firestore.client()

    state = _load_state()
    now = datetime.now(timezone.utc)
    since = (
        datetime.fromisoformat(state["since"])
        if state.get("since")
        else now - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    )
    # Pequeño solape (15 min) por si una búsqueda creada justo antes del último corte terminó
    # después -- reescribir el mismo número dos veces no hace daño.
    since -= timedelta(minutes=15)

    latest = {}  # término normalizado -> (requestedAt, resultCount)
    docs = (
        db.collection("search_requests")
        .where(filter=FieldFilter("requestedAt", ">=", since))
        .stream()
    )
    for d in docs:
        data = d.to_dict() or {}
        # Las alertas de palabra clave (notifyPush) no son búsquedas del buscador: su término
        # vive en keyword_alert_stats, no en search_stats.
        if data.get("notifyPush") or data.get("status") != "done":
            continue
        rc = data.get("resultCount")
        at = data.get("requestedAt")
        # Misma normalización que bumpSearchStat() (web) y SearchStatsService._normalize()
        # (app): trim + minúsculas.
        term = (data.get("query") or "").strip().lower()
        if not isinstance(rc, (int, float)) or at is None or not term or "/" in term:
            continue
        if term not in latest or at > latest[term][0]:
            latest[term] = (at, int(rc))

    written = 0
    for term, (at, rc) in latest.items():
        try:
            db.collection("search_stats").document(term).set(
                {"lastResultCount": rc, "lastResultAt": at}, merge=True
            )
            written += 1
        except Exception as e:
            log(f"aviso: no se pudo guardar {term!r}: {e}")
    if written:
        log(f"{written} término(s) actualizados con su nº de ofertas")

    state["since"] = now.isoformat()

    # Poda diaria.
    if time.time() - state.get("last_prune", 0) >= 24 * 3600:
        cutoff = now - timedelta(days=RETENTION_DAYS)
        deleted = 0
        try:
            while True:
                old = list(
                    db.collection("search_requests")
                    .where(filter=FieldFilter("requestedAt", "<", cutoff))
                    .limit(200)
                    .stream()
                )
                if not old:
                    break
                batch = db.batch()
                for d in old:
                    batch.delete(d.reference)
                batch.commit()
                deleted += len(old)
            state["last_prune"] = time.time()
        except Exception as e:
            log(f"aviso: fallo podando búsquedas antiguas: {e}")
        if deleted:
            log(f"podadas {deleted} búsqueda(s) de más de {RETENTION_DAYS} días")

    # Alertas: nº de ofertas encontradas por palabra, como mucho una vez por hora.
    if time.time() - state.get("last_alerts", 0) >= 3600:
        try:
            alerts_written = 0
            for d in db.collection("keyword_alert_stats").stream():
                data = d.to_dict() or {}
                raw = (data.get("keyword") or d.id).strip()
                variants = {raw, raw.lower(), d.id}
                total = 0
                for kw in variants:
                    agg = (
                        db.collection("keyword_alert_offers")
                        .where(filter=FieldFilter("keyword", "==", kw))
                        .count()
                        .get()
                    )
                    total += int(agg[0][0].value)
                d.reference.set(
                    {"lastResultCount": total, "lastResultAt": firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
                alerts_written += 1
            state["last_alerts"] = time.time()
            if alerts_written:
                log(f"{alerts_written} alerta(s) actualizadas con su nº de ofertas")
        except Exception as e:
            log(f"aviso: fallo contando ofertas de alertas: {e}")

    _save_state(state)


if __name__ == "__main__":
    main()
