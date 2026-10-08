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

25 sep 2026, aviso real: "no se puede mezclar con las que tengan activas en el app vieja" --
el contador `active_keyword_alerts` que ve el admin en "Qué pide la gente" lo suman/restan
alerts_service.dart al añadir/quitar (_bumpActiveAlert()/_dropActiveAlert()), pero eso solo lo
hace quien YA tiene el código de hoy; quien no ha actualizado sigue guardando su alerta en
users/{uid}.keywordAlerts de siempre (nunca cambió), pero no toca ese contador aparte, así que
se quedaba invisible. Como este script YA recorre a TODOS los usuarios cada hora, aprovecha esa
misma pasada para recalcular active_keyword_alerts desde la fuente real (quién tiene qué
guardado ahora mismo) -- fuente de verdad ajena a qué versión de la app tiene cada uno, y de
paso corrige sola cualquier desajuste que la cuenta en vivo del cliente pudiera acumular por
una carrera. No pisa firstAddedAt si el documento ya existía.
"""

import firebase_admin
from firebase_admin import credentials, firestore

import keyword_alert_search as kas
from update_offers import FIREBASE_CREDENTIALS_PATH


def log(msg):
    print(f"[keyword_alert_cleanup] {msg}", flush=True)


def _sync_active_keyword_alerts(db, counts, display):
    existing = {d.id: (d.to_dict() or {}) for d in db.collection("active_keyword_alerts").stream()}
    batch = db.batch()
    pending = 0
    written = 0
    for normalized, count in counts.items():
        data = {"keyword": display[normalized], "count": count}
        if normalized not in existing or "firstAddedAt" not in existing[normalized]:
            data["firstAddedAt"] = firestore.SERVER_TIMESTAMP
        batch.set(db.collection("active_keyword_alerts").document(normalized), data, merge=True)
        pending += 1
        written += 1
        if pending >= 400:
            batch.commit()
            batch = db.batch()
            pending = 0
    deleted = 0
    for normalized in existing:
        if normalized not in counts:
            batch.delete(db.collection("active_keyword_alerts").document(normalized))
            pending += 1
            deleted += 1
            if pending >= 400:
                batch.commit()
                batch = db.batch()
                pending = 0
    if pending:
        batch.commit()
    if written or deleted:
        log(f"active_keyword_alerts: {written} palabra(s) sincronizadas, {deleted} borrada(s)")


def main():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    checked = 0
    unavailable = 0
    active_counts = {}
    active_display = {}
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        keywords = [k.strip() for k in data.get("keywordAlerts") or [] if k.strip()]
        for kw in keywords:
            normalized = kw.lower()
            active_counts[normalized] = active_counts.get(normalized, 0) + 1
            active_display.setdefault(normalized, kw)
        # Interruptor general (ver alerts_service.dart) -- si el usuario apagó los avisos, no
        # tiene sentido seguir gastando peticiones a Amazon revisando sus palabras (pero sí
        # cuenta arriba como "activa", que es un concepto aparte de si avisa o no).
        if data.get("keywordAlertsEnabled") is False:
            continue
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

    try:
        _sync_active_keyword_alerts(db, active_counts, active_display)
    except Exception as e:
        log(f"aviso: fallo sincronizando active_keyword_alerts: {e}")

    if unavailable and unavailable == checked and checked > 0:
        log(f"{checked} alerta(s) revisada(s) -- Amazon no disponible ahora mismo, nada tocado.")
    else:
        log(f"{checked} alerta(s) revisada(s).")


if __name__ == "__main__":
    main()
