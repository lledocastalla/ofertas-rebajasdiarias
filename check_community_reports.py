#!/usr/bin/env python3
"""
check_community_reports.py — avisa por Telegram cuando alguien denuncia una publicación de la
Comunidad (30 ago 2026, pedido explícito: "que me llegue a mí" -- hasta ahora una denuncia solo
quedaba anotada en Firestore, `community_post_reports` + `reportCount` en el post, sin ningún
aviso proactivo, había que entrar al panel de admin a mirar).

Mismo espíritu que check_search_requests.py/check_submissions.py: si no hay nada nuevo, termina
al momento. Guarda en un fichero de estado (community_reports_notified.json) los ids de denuncia
ya avisados, para no repetir el mismo aviso en cada ciclo (una denuncia nunca cambia de estado
sola, así que basta con "ya avisado sí/no", a diferencia del vigía de disponibilidad de Amazon
que sí necesita saber el último valor conocido).

Además de Telegram, manda un push a la propia app/web (30 ago 2026, pedido explícito: "que a mí
como admin me mande una notificación push") -- mismo mecanismo de topics que ya usa
notify_submitter_push()/notify_favorite_price_changes() en update_offers.py: cada usuario
logueado está suscrito solo a "user_<uid>", así que basta con mandar el mensaje a ese topic de
cada admin, sin gestionar tokens de dispositivo. Los UID de los dos correos admin fijos
(ver SubmissionsService._adminEmails en la app) están hardcodeados abajo -- lledocastalla@gmail.com
ya tenía sesión iniciada alguna vez (UID real, resuelto con auth.get_user_by_email() el 30 ago);
rebajasdiarias21@gmail.com todavía no tiene ningún registro en Firebase Auth (nunca inició
sesión con esa cuenta en la app/web) -- si algún día la usa, hay que añadir su UID aquí también.

Uso: cron cada 3 min (mismo cadencia que check_submissions.py), ver crontab en
RASPI_REBAJASDIARIAS.md.
"""

import json
import os

from update_offers import FIREBASE_CREDENTIALS_PATH, notify_telegram

HOME = os.path.expanduser("~")
STATE_PATH = f"{HOME}/community_reports_notified.json"
MAX_PER_CYCLE = 20  # de sobra para uso normal

# UIDs de los admins con sesión real conocida -- ver docstring de arriba.
ADMIN_UIDS = [
    "Ji0kzNTKwbO1tKpLBVLroCZf5HJ3",  # lledocastalla@gmail.com
]


def log(msg):
    print(f"[check_community_reports] {msg}", flush=True)


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
        # Tope de tamaño simple -- no hace falta guardar el historial entero para siempre,
        # solo lo suficiente para no repetir avisos recientes.
        trimmed = list(ids)[-2000:]
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
    except Exception as e:
        log(f"aviso: no se pudo guardar el estado: {e}")


def _notify_admin_push(title: str, body: str):
    """Nunca debe tumbar el script si falla -- mismo criterio que notify_telegram()."""
    try:
        from firebase_admin import messaging

        for uid in ADMIN_UIDS:
            try:
                messaging.send(
                    messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        topic=f"user_{uid}",
                    )
                )
            except Exception as e:
                log(f"  aviso: no se pudo enviar el push al admin {uid}: {e}")
    except Exception as e:
        log(f"aviso: no se pudo enviar el push de denuncia: {e}")


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
        reports = list(
            db.collection("community_post_reports").limit(MAX_PER_CYCLE * 5).stream()
        )
    except Exception as e:
        log(f"aviso: no se pudo consultar Firestore de denuncias: {e}")
        return

    new_reports = [r for r in reports if r.id not in notified]
    if not new_reports:
        return

    new_reports = new_reports[:MAX_PER_CYCLE]
    log(f"{len(new_reports)} denuncia(s) nueva(s)")

    for report in new_reports:
        data = report.to_dict() or {}
        post_id = data.get("postId")
        post_title = "(publicación no encontrada, puede que ya se borrara)"
        post_snippet = ""
        if post_id:
            try:
                post_doc = db.collection("community_posts").document(post_id).get()
                if post_doc.exists:
                    post = post_doc.to_dict() or {}
                    post_title = post.get("title") or "(sin título)"
                    text = (post.get("text") or "").strip()
                    post_snippet = text[:200] + ("…" if len(text) > 200 else "")
            except Exception as e:
                log(f"  aviso: no se pudo leer el post {post_id}: {e}")
        notify_telegram(
            "🚩 Denuncia nueva en la Comunidad\n"
            f"Hilo: {post_title}\n"
            + (f"Texto: {post_snippet}\n" if post_snippet else "")
            + "Revísalo en el panel de admin (Mi cuenta → Comunidad) para borrarlo si toca."
        )
        _notify_admin_push(
            "🚩 Denuncia nueva en la Comunidad",
            post_title,
        )
        notified.add(report.id)
        log(f"  avisado: {report.id} (post {post_id})")

    _save_notified_ids(notified)


if __name__ == "__main__":
    main()
