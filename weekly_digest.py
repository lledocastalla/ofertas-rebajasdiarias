#!/usr/bin/env python3
"""
weekly_digest.py — resumen semanal por push con chollos de las categorías que le interesan a
cada usuario (28 ago 2026, idea "10" de la ronda de mejoras inspirada en otras apps de chollos:
"menos invasivo que un aviso por cada cosa suelta, y le da un motivo para volver aunque no
tenga alertas puestas"). Proceso APARTE de update_offers.py (mismo motivo que
generate_extended_catalog_cron.py: si algo falla aquí, no se lleva por delante el ciclo normal
de scraping).

No depende de que exista un selector de "categorías favoritas" en la app (esa sería una función
aparte, no construida esta ronda) -- usa directamente las categorías de los favoritos que el
usuario YA tiene guardados en Firestore (users/{uid}.favorites). Si no tiene favoritos, no hay
nada que resumir y se salta en silencio.

Throttle propio (WEEKLY_DIGEST_MIN_HOURS, mismo patrón de archivo de estado que
_should_regenerate_extended_catalog en update_offers.py) -- así el cron puede llamarse a diario
sin miedo a mandar el resumen más de una vez por semana.

Uso: cron aparte, una vez a la semana en hora tranquila (ver crontab en la Pi).
"""

import json
import os
from datetime import datetime

import update_offers as uo

WEEKLY_DIGEST_STATE_PATH = f"{uo.HOME}/.rebajas_weekly_digest_state.json"
# Margen de seguridad por debajo de una semana completa, por si el cron se dispara más de una
# vez el mismo día o el reloj de la Pi se adelanta un poco -- igual que EXTENDED_CATALOG_MIN_HOURS.
WEEKLY_DIGEST_MIN_HOURS = 24 * 6
OFFERS_PER_DIGEST = 4  # cuántas ofertas como máximo se consideran para el cuerpo del push


def log(msg):
    print(f"[weekly_digest] {msg}", flush=True)


def _should_send():
    try:
        with open(WEEKLY_DIGEST_STATE_PATH) as f:
            last = datetime.fromisoformat(json.load(f)["last_sent"])
    except Exception:
        return True
    return (datetime.now().astimezone() - last).total_seconds() >= WEEKLY_DIGEST_MIN_HOURS * 3600


def _mark_sent():
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(WEEKLY_DIGEST_STATE_PATH, "w") as f:
        json.dump({"last_sent": now_iso}, f)


def _load_catalog():
    try:
        with open(uo.OFFERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"no se pudo leer el catálogo: {e}")
        return {}
    offers = data.get("offers", []) if isinstance(data, dict) else data
    return {o["id"]: o for o in offers if o.get("id")}


def _best_offers_for_categories(catalog, categories, exclude_ids, limit):
    """Ofertas con más descuento de esas categorías, sin repetir las que el usuario ya tiene
    en favoritos -- mismo criterio de "lo mejor primero" que el resto de la app."""
    candidates = [
        o for o in catalog.values()
        if o.get("category") in categories and o.get("id") not in exclude_ids
    ]
    candidates.sort(key=lambda o: o.get("discount_percent", 0), reverse=True)
    return candidates[:limit]


def main():
    if not _should_send():
        return
    if not os.path.isfile(uo.FIREBASE_CREDENTIALS_PATH):
        return

    catalog = _load_catalog()
    if not catalog:
        return

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, messaging

        if not firebase_admin._apps:
            cred = credentials.Certificate(uo.FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
        db = firestore.client()

        sent = 0
        for doc in db.collection("users").stream():
            data = doc.to_dict() or {}
            # Reutiliza el interruptor que ya existe para avisos de favoritos (Ajustes ->
            # "Precio en favoritos") -- no se añade uno específico para el resumen semanal
            # solo por esto, para no obligar a compilar la app de nuevo; conceptualmente es el
            # mismo "quiero que me avisen de cosas relacionadas con mis favoritos".
            if data.get("favoriteAlertsEnabled") is False:
                continue
            favorite_ids = data.get("favorites") or []
            if not favorite_ids:
                continue
            favorite_offers = [catalog[a] for a in favorite_ids if a in catalog]
            categories = {o.get("category") for o in favorite_offers if o.get("category")}
            if not categories:
                continue

            picks = _best_offers_for_categories(
                catalog, categories, exclude_ids=set(favorite_ids), limit=OFFERS_PER_DIGEST
            )
            if not picks:
                continue

            uid = doc.id
            cat_list = ", ".join(sorted(categories)[:3])
            if len(picks) == 1:
                body = f"{picks[0]['title'][:80]} — {picks[0]['price']} €"
            else:
                body = f"{len(picks)} chollos nuevos en {cat_list} que te pueden interesar"
            try:
                messaging.send(messaging.Message(
                    notification=messaging.Notification(
                        title="📬 Tu resumen semanal de Rebajas Diarias",
                        body=body,
                    ),
                    topic=f"user_{uid}",
                ))
                sent += 1
            except Exception as e:
                log(f"  aviso: no se pudo enviar el resumen a {uid}: {e}")

        log(f"{sent} resúmenes semanales enviados.")
        _mark_sent()
    except Exception as e:
        log(f"aviso: no se pudo generar el resumen semanal: {e}")


if __name__ == "__main__":
    main()
