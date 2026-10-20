#!/usr/bin/env python3
"""
new_user_watcher.py — avisa por Telegram y push cuando se registra un usuario nuevo de verdad
(29 sep 2026, panel privado "InfoRebajas" -- app aparte, nunca publicada, solo para el propio
dueño: pedido explícito "cuando se registra un usuario nuevo que me avise").

No hace falta ninguna colección nueva para detectar altas: reutiliza `users_directory`, que
sync_users_directory.py ya mantiene sincronizada cada hora contra Firebase Auth y que YA excluye
las sesiones anónimas (ensureAnonymousSession() en la app) -- aquí "usuario nuevo" significa
cuenta real (Google/Apple), no cada apertura de la app sin login.

Mismo patrón que check_community_reports.py: estado local en JSON con los UIDs ya vistos, para
no repetir el aviso en cada ciclo -- un usuario nunca "deja de estar visto", así que basta con
un conjunto sin más metadatos. Primera ejecución: siembra el estado con los UIDs que ya existan
en ese momento SIN avisar de ninguno -- si no, el primer ciclo dispararía un aviso por cada
usuario histórico de golpe.

Uso: cron cada 5-10 min, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import json
import os

from update_offers import FIREBASE_CREDENTIALS_PATH, notify_telegram

HOME = os.path.expanduser("~")
STATE_PATH = f"{HOME}/new_user_watcher_seen.json"
# Copia local que deja sync_users_directory.py en cada pasada (cada 10 min). Se lee de aquí
# en vez de Firestore -- ver el comentario de abajo en main().
INSTANTANEA_PATH = f"{HOME}/users_directory_local.json"
MAX_PER_CYCLE = 20  # de sobra para uso normal

# UIDs de los admins con sesión real conocida -- mismo criterio/lista que
# check_community_reports.py (ver ese docstring: rebajasdiarias21@gmail.com todavía no tiene
# UID de Firebase Auth porque nunca inició sesión con esa cuenta).
ADMIN_UIDS = [
    "Ji0kzNTKwbO1tKpLBVLroCZf5HJ3",  # lledocastalla@gmail.com
]


def log(msg):
    print(f"[new_user_watcher] {msg}", flush=True)


def _load_seen_uids() -> set:
    if not os.path.isfile(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen_uids(uids: set):
    try:
        trimmed = list(uids)[-5000:]
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
        log(f"aviso: no se pudo enviar el push de usuario nuevo: {e}")


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


class _UsuarioLocal:
    """Imita lo justo de un DocumentSnapshot de Firestore (.id y .to_dict()) para que el
    bucle de abajo siga igual que cuando los datos venían de Firestore."""

    def __init__(self, uid, datos):
        self.id = uid
        self._datos = datos

    def to_dict(self):
        return self._datos


def main():
    # NO SE LEE FIRESTORE (20 oct 2026) ---------------------------------------------------
    # Antes esto hacía un .stream() del directorio entero en cada pasada. Con el cron cada 5
    # minutos son 288 x 65 usuarios = 18.720 lecturas al día, más de un tercio del tope del
    # plan gratuito (50.000), gastadas en releer lo mismo para preguntar "¿hay alguien
    # nuevo?". Y cuando la cuota se agota se cae TODO lo que necesita base de datos -- la
    # app incluida, no solo los crons. El 20 oct ya estaba pasando: tres crons devolviendo
    # "429 Quota exceeded" cada mañana hasta que la cuota se reiniciaba.
    #
    # sync_users_directory.py ya lee el directorio cada 10 minutos para lo suyo, así que
    # ahora deja una copia local y aquí se lee de ahí. Coste en Firestore: cero.
    #
    # Lo que se pierde: hasta 10 minutos de retraso en avisar de un usuario nuevo. Para un
    # aviso de "se ha registrado alguien" no cambia nada.
    seen = _load_seen_uids()
    first_run = len(seen) == 0

    try:
        with open(INSTANTANEA_PATH, encoding="utf-8") as f:
            directorio = json.load(f)
    except FileNotFoundError:
        # Todavía no ha corrido sync_users_directory desde el cambio. En 10 minutos estará.
        return
    except Exception as e:
        log(f"aviso: no se pudo leer la copia local del directorio: {e}")
        return

    users = [_UsuarioLocal(uid, datos) for uid, datos in directorio.items()]
    all_uids = set(directorio)

    if first_run:
        # Sembrar sin avisar -- ver docstring de arriba.
        _save_seen_uids(all_uids)
        log(f"primera ejecución: {len(all_uids)} usuario(s) sembrado(s), sin avisar")
        return

    new_users = [u for u in users if u.id not in seen][:MAX_PER_CYCLE]
    if not new_users:
        return

    log(f"{len(new_users)} usuario(s) nuevo(s)")

    for user in new_users:
        data = user.to_dict() or {}
        name = data.get("displayName") or data.get("email") or "(sin nombre)"
        notify_telegram(f"🆕 Usuario nuevo registrado\n{name}")
        _notify_admin_push("🆕 Usuario nuevo registrado", name)
        seen.add(user.id)
        log(f"  avisado: {user.id} ({name})")

    _save_seen_uids(seen | all_uids)


if __name__ == "__main__":
    main()
