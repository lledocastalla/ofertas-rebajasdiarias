#!/usr/bin/env python3
"""
check_community_replies.py — manda el push real de "tienes un mensaje sin leer" cuando alguien
responde a un hilo tuyo de la Comunidad (31 ago 2026, pedido explícito: "si me contestan a algún
mensaje debe mandar notificación push y decirme que tienes un mensaje sin leer").

El cliente (CommunityService.notifyReply, ver rebajasdiarias-app/lib/services/
community_service.dart) ya escribe el documento en Firestore `community_notifications` al
momento, para el badge/lista "🔔 Mensajes" del perfil -- eso pasa en tiempo real, sin depender de
este script. Lo que el cliente NO puede hacer es mandar el push de verdad (hace falta el Admin
SDK, que solo vive aquí). Mismo espíritu que check_community_reports.py: si no hay nada nuevo,
termina al momento; guarda en un fichero de estado los ids ya avisados para no repetir el mismo
push en cada ciclo.

Uso: cron cada 3 min (misma cadencia que check_community_reports.py/check_submissions.py), ver
crontab en RASPI_REBAJASDIARIAS.md.
"""

import json
import os

from update_offers import FIREBASE_CREDENTIALS_PATH

HOME = os.path.expanduser("~")
STATE_PATH = f"{HOME}/community_replies_notified.json"
MAX_PER_CYCLE = 30  # de sobra para uso normal


def log(msg):
    print(f"[check_community_replies] {msg}", flush=True)


def _load_notified_ids() -> set:
    if not os.path.isfile(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_notified_ids(ids: set):
    try:
        trimmed = list(ids)[-2000:]
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
    except Exception as e:
        log(f"aviso: no se pudo guardar el estado: {e}")


def _send_push(for_uid: str, title: str, body: str):
    """Nunca debe tumbar el script si falla -- mismo criterio que notify_telegram()."""
    try:
        from firebase_admin import messaging

        messaging.send(
            messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                topic=f"user_{for_uid}",
            )
        )
    except Exception as e:
        log(f"  aviso: no se pudo enviar el push a {for_uid}: {e}")


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

    notified = _load_notified_ids()

    try:
        pending = list(
            db.collection("community_notifications")
            .limit(MAX_PER_CYCLE * 5)
            .stream()
        )
    except Exception as e:
        log(f"aviso: no se pudo consultar Firestore de mensajes: {e}")
        return

    new_items = [n for n in pending if n.id not in notified]
    if not new_items:
        return

    new_items = new_items[:MAX_PER_CYCLE]
    log(f"{len(new_items)} mensaje(s) nuevo(s)")

    for item in new_items:
        data = item.to_dict() or {}
        for_uid = data.get("forUid")
        from_name = data.get("fromName") or "Alguien"
        post_title = data.get("postTitle") or "tu hilo"
        snippet = (data.get("snippet") or "").strip()
        if for_uid:
            _send_push(
                for_uid,
                "💬 Tienes un mensaje sin leer",
                f"{from_name} te respondió en \"{post_title}\""
                + (f": {snippet}" if snippet else ""),
            )
            log(f"  avisado: {item.id} (para {for_uid})")
        notified.add(item.id)

    _save_notified_ids(notified)


if __name__ == "__main__":
    main()
