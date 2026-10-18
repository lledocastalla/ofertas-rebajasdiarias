#!/usr/bin/env python3
"""
sync_users_directory.py — vuelca la lista de usuarios de Firebase Auth a Firestore
(`users_directory/{uid}`) para que el panel de admin (app/web) pueda ofrecer un selector con
nombre + foto en vez de pedir el UID a mano (30 ago 2026, pedido explícito: "todos los usuarios
registrados yo poder elegir").

Por qué hace falta este paso aparte: el cliente (Flutter/JS) nunca puede listar usuarios de
Firebase Auth directamente -- eso solo lo permite el Admin SDK, en servidor. La Pi ya tiene
acceso vía `firebase-service-account.json` (mismo patrón que el resto de scripts), así que es
el sitio natural para mantener este directorio actualizado.

`users_directory` es una colección NUEVA, separada de `users/{uid}` (que ya existe y guarda
datos funcionales -- favoritos, alertas, etc.) -- se mantiene aparte a propósito para no tocar
las reglas de esa colección ni mezclar datos sensibles (email) con datos funcionales.
firestore.rules debe restringir su lectura a admin (contiene emails).

Sincronización completa cada vez (barato -- la base de usuarios es pequeña): upsert de cada
usuario real de Auth, y borra del directorio cualquier entrada vieja que ya no exista en Auth
(cuenta borrada) O que ya no cuente como real (ver más abajo).

21 sep 2026, aviso real del usuario ("tengo visibles los que no se han registrado y también
los cuenta como usuarios"): desde AuthService.ensureAnonymousSession() (main.dart, sesión
anónima automática al abrir la app sin cuenta real) cada apertura de la app sin login crea una
cuenta de verdad en Firebase Auth, sin nombre/email -- este script las volcaba igual que a
cualquier cuenta real, así que el panel de admin las mostraba y contaba como "usuarios
registrados" sin serlo. Un usuario anónimo se reconoce de forma fiable con el Admin SDK por
`provider_data` vacío (no tiene ningún proveedor de login vinculado, a diferencia de Google/
email) -- se salta al listar Y se borra del directorio si ya estuviera ahí de una sincronización
vieja (misma limpieza de abajo, sin necesitar código aparte: basta con no añadirla a
seen_uids).

También restaurado en la Pi 5 (21 sep 2026): este archivo vivía SOLO en la Pi vieja, nunca
llegó a git -- la migración de Pi 3B a Pi 5 lo dejó fuera sin que nadie se diera cuenta, y el
cron de cada hora llevaba fallando en silencio desde entonces ("No such file or directory" en
sync_users_directory.log). Ahora versionado para que esto no vuelva a pasar.

Uso: cron cada hora, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import os

from update_offers import FIREBASE_CREDENTIALS_PATH


def log(msg):
    print(f"[sync_users_directory] {msg}", flush=True)


def main():
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        return

    import firebase_admin
    from firebase_admin import auth, credentials, firestore

    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    # ESCRIBIR SOLO LO QUE HA CAMBIADO (18 oct 2026) -------------------------------------
    # Antes se reescribían los 64 usuarios ENTEROS en cada pasada, cambiara algo o no: como
    # siempre se ponía updatedAt con la hora del momento, para Firestore todo era un dato
    # nuevo. Con el cron cada 10 minutos eso son 64 x 6 x 24 = 9.216 escrituras al día, casi
    # la mitad del tope diario del plan gratuito (20.000) gastada en copiar datos idénticos.
    # Y al agotarse la cuota se caía TODO lo que necesita base de datos: favoritos, alertas,
    # comunidad y el buscador en vivo (se veía en los logs: miles de "429 Quota exceeded").
    #
    # Ahora se lee primero el directorio y solo se escribe el usuario cuyo nombre, correo o
    # foto hayan cambiado de verdad. Un día normal eso son cero escrituras. La lectura es la
    # MISMA que ya hacía falta abajo para la limpieza, así que no se lee dos veces.
    #
    # updatedAt deja de refrescarse en cada pasada, a propósito: no lo usa nadie (comprobado
    # en la app y en la web, solo se lee la lista para el selector de admin). Si algún día
    # hiciera falta "cuándo se vio por última vez", habría que guardarlo en otro sitio que no
    # cueste una escritura por usuario y pasada.
    existing_docs = list(db.collection("users_directory").stream())
    existing = {d.id: (d.to_dict() or {}) for d in existing_docs}

    seen_uids = set()
    batch = db.batch()
    batch_count = 0
    synced = 0
    sin_cambios = 0
    skipped_anonymous = 0

    for user in auth.list_users().iterate_all():
        # Sesión anónima de fondo (ensureAnonymousSession()), no una cuenta real -- no tiene
        # ningún proveedor de login vinculado. No debe aparecer ni contar en el directorio.
        if not user.provider_data:
            skipped_anonymous += 1
            continue
        seen_uids.add(user.uid)
        datos = {
            "displayName": user.display_name or "",
            "email": user.email or "",
            "photoURL": user.photo_url or "",
        }
        anterior = existing.get(user.uid)
        if anterior is not None and all(anterior.get(k) == v for k, v in datos.items()):
            sin_cambios += 1
            continue
        doc_ref = db.collection("users_directory").document(user.uid)
        batch.set(doc_ref, {**datos, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
        batch_count += 1
        synced += 1
        if batch_count >= 400:  # tope de 500 escrituras por batch de Firestore, con margen
            batch.commit()
            batch = db.batch()
            batch_count = 0

    if batch_count > 0:
        batch.commit()

    # Limpieza: quitar del directorio a quien ya no exista en Auth (cuenta borrada) o que ahora
    # se considere anónima (nunca se añadió a seen_uids arriba) -- así una sincronización vieja
    # de antes de este cambio también se limpia sola, sin paso manual aparte.
    removed = 0
    # existing_docs ya se leyó arriba -- no se vuelve a leer (ahorra 64 lecturas por pasada).
    del_batch = db.batch()
    del_count = 0
    for doc in existing_docs:
        if doc.id not in seen_uids:
            del_batch.delete(doc.reference)
            del_count += 1
            removed += 1
            if del_count >= 400:
                del_batch.commit()
                del_batch = db.batch()
                del_count = 0
    if del_count > 0:
        del_batch.commit()

    log(f"{synced} usuario(s) escrito(s), {sin_cambios} sin cambios, "
        f"{skipped_anonymous} anónimo(s) omitido(s), {removed} entrada(s) vieja(s) borrada(s)")


if __name__ == "__main__":
    main()
