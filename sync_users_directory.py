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

import json
import os
import time

from update_offers import FIREBASE_CREDENTIALS_PATH

# Copia local del directorio de usuarios, para que new_user_watcher.py no lea Firestore.
INSTANTANEA_PATH = os.path.expanduser("~/users_directory_local.json")
ESTADO_PATH = os.path.expanduser("~/sync_users_directory_lectura.json")
LECTURA_COMPLETA_CADA = 3600  # segundos


def _leer_estado():
    try:
        with open(ESTADO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _guardar_estado(d):
    try:
        with open(ESTADO_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def _leer_instantanea():
    """La copia local de la última pasada. Si no existe (primera vez tras el cambio), se
    devuelve vacío y entonces todos los usuarios parecen nuevos: se reescribirían los 65 una
    vez. Es una escritura de más, puntual, y a cambio nunca se deja de escribir algo que sí
    había cambiado."""
    try:
        with open(INSTANTANEA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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
    # LEER FIRESTORE SOLO UNA VEZ POR HORA (22 oct 2026) ---------------------------------
    # Este cron va cada 10 min y leía el directorio entero en CADA pasada: 65 usuarios x 144
    # pasadas = ~9.400 lecturas al día, el mayor consumidor de la Pi. Con el tope de 50.000
    # del plan gratuito eso deja poco margen, y cuando la cuota se agota no se cae solo la
    # Pi: se cae la app de los usuarios (favoritos, alertas, comunidad dejan de responder).
    #
    # Pero el directorio casi nunca cambia, así que releerlo cada 10 minutos no aporta nada.
    # Ahora se compara contra la copia local y solo se lee Firestore de verdad una vez por
    # hora. Eso baja de ~9.400 a ~1.600 lecturas al día sin tocar la frecuencia del cron,
    # que la pidió el usuario expresamente el 21 sep.
    #
    # La lectura horaria no es opcional: es la que detecta cambios hechos fuera de aquí y la
    # que permite limpiar usuarios borrados. Si solo nos fiáramos de la copia local, un
    # desajuste se quedaría para siempre. Con la pasada horaria se corrige solo.
    ahora = time.time()
    estado = _leer_estado()
    toca_leer = (ahora - estado.get("ultima_lectura", 0)) >= LECTURA_COMPLETA_CADA
    existing_docs = None
    if toca_leer:
        existing_docs = list(db.collection("users_directory").stream())
        existing = {d.id: (d.to_dict() or {}) for d in existing_docs}
        estado["ultima_lectura"] = ahora
        _guardar_estado(estado)
    else:
        # Copia local: lo que se escribió la última vez. Solo lleva displayName y email, así
        # que photoURL no se puede comparar y se omite del cotejo en las pasadas rápidas; si
        # cambia una foto, la pasada horaria lo pilla.
        existing = _leer_instantanea()

    seen_uids = set()
    # Copia local del directorio para que new_user_watcher.py NO tenga que leer Firestore
    # (20 oct 2026). Ese cron corría cada 5 minutos y hacía un .stream() del directorio
    # entero solo para mirar si había algún UID nuevo: 288 pasadas x 65 usuarios = 18.720
    # lecturas al día, más de un tercio del tope diario del plan gratuito, gastadas en releer
    # lo mismo. Y al agotarse la cuota se cae TODO lo que necesita base de datos, incluida la
    # app (en los logs del 20 oct: "429 Quota exceeded" en tres crons a la vez).
    # Aquí la lectura ya está hecha de todas formas, así que la copia sale gratis.
    instantanea = {}
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
        instantanea[user.uid] = {"displayName": datos["displayName"], "email": datos["email"]}
        anterior = existing.get(user.uid)
        campos = datos if toca_leer else {k: datos[k] for k in ("displayName", "email")}
        if anterior is not None and all(anterior.get(k) == v for k, v in campos.items()):
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
    # Solo en la pasada completa: borrar requiere saber qué hay de verdad en Firestore, y la
    # copia local no sirve para eso. En las pasadas rápidas se salta y ya se limpiará dentro
    # de la hora -- un usuario borrado que siga una hora de más en el directorio no molesta
    # a nadie, y es mucho mejor que arriesgarse a borrar por un desajuste de la copia.
    # existing_docs ya se leyó arriba -- no se vuelve a leer.
    del_batch = db.batch() if existing_docs is not None else None
    if existing_docs is None:
        existing_docs = []
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
    if del_count > 0 and del_batch is not None:
        del_batch.commit()

    # Se escribe al final y de golpe: si algo peta antes, se queda la copia anterior, que es
    # vieja pero coherente -- mejor que media copia.
    try:
        tmp = f"{INSTANTANEA_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(instantanea, f)
        os.replace(tmp, INSTANTANEA_PATH)
    except Exception as e:
        log(f"aviso: no se pudo guardar la copia local del directorio: {e}")

    # Que se vea en el log en qué modo fue cada pasada: sin esto, al depurar un consumo
    # raro de cuota no hay forma de saber cuántas pasadas leyeron Firestore de verdad.
    log("lectura COMPLETA de Firestore" if toca_leer
        else "pasada rápida (copia local, 0 lecturas de Firestore)")
    log(f"{synced} usuario(s) escrito(s), {sin_cambios} sin cambios, "
        f"{skipped_anonymous} anónimo(s) omitido(s), {removed} entrada(s) vieja(s) borrada(s)")


if __name__ == "__main__":
    main()
