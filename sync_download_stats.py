#!/usr/bin/env python3
"""
sync_download_stats.py — descargas de Android (Google Play) y iOS (App Store) en un único
documento diario por plataforma, para el panel privado "InfoRebajas" (29 sep 2026, app aparte,
nunca publicada, solo para el propio dueño: pedido explícito "cuantas descargar tenemos de
apple cuantas en Android").

Ninguna de las dos tiendas tiene un endpoint REST simple de "descargas de hoy" -- las dos vías
reales son más indirectas, y las dos necesitan una credencial nueva que SOLO Vicente puede crear
(requiere entrar con su cuenta a Play Console/App Store Connect, no algo que se pueda automatizar
desde aquí):

1. GOOGLE PLAY -- no existe una API de "installs" directa. La vía real y documentada es activar
   la exportación a Cloud Storage de los informes de estadísticas (Play Console > su app >
   Estadísticas > "Exportación a Cloud Storage" > activar). Eso crea un bucket privado
   `pubsite_prod_rev_<id-de-cuenta>` con un CSV nuevo cada día
   (`stats/installs/installs_<package_name>_<AAAAMM>_overview.csv`, un mes por fichero, una fila
   por día). Hace falta además dar acceso a ese bucket a una cuenta de servicio nueva desde
   Play Console (Usuarios y permisos > invitar, con permiso "Ver información de la app" como
   mínimo) -- el acceso lo controla Play Console, no un rol de Cloud IAM aparte.
   Config: `~/google-play-developer-credentials.json` (JSON de la cuenta de servicio) +
   `~/google-play-stats-config.json` (`{"bucket": "pubsite_prod_rev_...", "package_name":
   "com.rebajasdiarias.app"}`, el nombre del bucket lo enseña la propia pantalla de activación).

2. APP STORE -- App Store Connect API, informe de ventas (`GET /v1/salesReports`,
   reportType=SALES, reportSubType=SUMMARY, frequency=DAILY). Hace falta una clave API nueva en
   App Store Connect (Usuarios y accesos > Claves > rol "Ventas e informes" o superior) -- da
   tres valores: el archivo `.p8`, el Key ID y el Issuer ID, más el "Número de proveedor"
   (vendor number, visible en la pestaña Pagos y datos financieros).
   Config: `~/appstoreconnect_private_key.p8` + `~/appstoreconnect_credentials.json`
   (`{"keyId": "...", "issuerId": "...", "vendorNumber": "...", "appleId": "6743941711"}`).

Los dos informes van con retraso real (normalmente el de ayer, a veces el de anteayer) -- por
eso se guarda `reportDate` (la fecha real que cubre el dato) además de `updatedAt` (cuándo se
ejecutó este script), para no confundir "cuándo se ejecutó" con "de qué día son las cifras".

Nunca debe tumbar el cron si falta alguna credencial -- si falta una de las dos, se salta esa
plataforma en silencio y sigue con la otra (mismo criterio defensivo que el resto de la Pi).

IMPORTANTE (primera ejecución real): ni el formato exacto de columnas del CSV de Play ni los
códigos de "Product Type Identifier" del informe de Apple se han podido verificar contra datos
reales todavía (no hay credenciales en este entorno) -- ambas funciones registran en el log las
cabeceras/columnas que encuentran, para poder confirmar a ojo que el número que sacan es el
correcto la primera vez que corran de verdad, y ajustar aquí si hiciera falta.

Uso: cron 1×/día (las cifras de descargas van con retraso, no tiene sentido más a menudo), ver
crontab en RASPI_REBAJASDIARIAS.md.
"""

import csv
import gzip
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

from update_offers import FIREBASE_CREDENTIALS_PATH

HOME = os.path.expanduser("~")

GOOGLE_PLAY_CREDENTIALS_PATH = f"{HOME}/google-play-developer-credentials.json"
GOOGLE_PLAY_STATS_CONFIG_PATH = f"{HOME}/google-play-stats-config.json"

APPSTORECONNECT_KEY_PATH = f"{HOME}/appstoreconnect_private_key.p8"
APPSTORECONNECT_CONFIG_PATH = f"{HOME}/appstoreconnect_credentials.json"

# Códigos documentados de "Product Type Identifier" para descargas de app (no compras dentro de
# la app ni suscripciones) -- ver aviso en el docstring sobre verificar esto la primera vez.
APPSTORE_DOWNLOAD_PRODUCT_TYPES = {"1", "1-B", "1F", "F1"}


def log(msg):
    print(f"[sync_download_stats] {msg}", flush=True)


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


def _sync_android(db):
    if not os.path.isfile(GOOGLE_PLAY_CREDENTIALS_PATH) or not os.path.isfile(
        GOOGLE_PLAY_STATS_CONFIG_PATH
    ):
        log("Android: sin credenciales configuradas todavía, se salta")
        return

    try:
        with open(GOOGLE_PLAY_STATS_CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        bucket_name = config["bucket"]
        package_name = config["package_name"]
    except Exception as e:
        log(f"Android: config inválida en {GOOGLE_PLAY_STATS_CONFIG_PATH}: {e}")
        return

    try:
        from google.cloud import storage
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_PLAY_CREDENTIALS_PATH
        )
        client = storage.Client(credentials=creds, project=creds.project_id)
        bucket = client.bucket(bucket_name)

        now = datetime.now(timezone.utc)
        blob_path = (
            f"stats/installs/installs_{package_name}_{now:%Y%m}_overview.csv"
        )
        blob = bucket.blob(blob_path)
        if not blob.exists():
            log(f"Android: no existe todavía {blob_path} (¿mes recién empezado?)")
            return
        raw = blob.download_as_bytes()
    except Exception as e:
        log(f"Android: fallo al leer el informe de Play: {e}")
        return

    # El formato de estos CSV ha sido históricamente UTF-16 con BOM -- se prueban ambas
    # codificaciones para no romper si Google ya lo cambió a UTF-8.
    text = None
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        log("Android: no se pudo decodificar el CSV con ninguna codificación esperada")
        return

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        log("Android: CSV vacío")
        return
    log(f"Android: columnas encontradas: {list(rows[0].keys())}")

    # La fila del último día disponible en el fichero -- suele ser ayer, a veces anteayer.
    last_row = rows[-1]
    date_col = next((c for c in last_row if c.strip().lower() == "date"), None)
    installs_col = next(
        (c for c in last_row if "daily user installs" in c.strip().lower()), None
    ) or next((c for c in last_row if "daily device installs" in c.strip().lower()), None)

    if date_col is None or installs_col is None:
        log("Android: no se encontraron las columnas esperadas, revisar cabeceras arriba")
        return

    try:
        report_date = last_row[date_col].strip()
        installs_today = int(float(last_row[installs_col].strip() or 0))
    except Exception as e:
        log(f"Android: fila con formato inesperado: {e}")
        return

    db.collection("download_stats").document("android").set(
        {
            "installsLast30d": sum(
                int(float(r.get(installs_col, "0") or 0)) for r in rows[-30:]
            ),
            "installsToday": installs_today,
            "reportDate": report_date,
            "updatedAt": time.time(),
        }
    )
    log(f"Android: {installs_today} instalación(es) el {report_date}")


def _appstoreconnect_jwt(key_id: str, issuer_id: str, private_key_pem: bytes) -> str:
    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1100,  # máximo permitido por Apple: 20 min (1200s), con margen
        "aud": "appstoreconnect-v1",
    }
    return pyjwt.encode(
        payload, private_key_pem, algorithm="ES256", headers={"kid": key_id}
    )


def _sync_ios(db):
    if not os.path.isfile(APPSTORECONNECT_KEY_PATH) or not os.path.isfile(
        APPSTORECONNECT_CONFIG_PATH
    ):
        log("iOS: sin credenciales configuradas todavía, se salta")
        return

    try:
        with open(APPSTORECONNECT_CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        with open(APPSTORECONNECT_KEY_PATH, "rb") as f:
            private_key_pem = f.read()
    except Exception as e:
        log(f"iOS: no se pudieron leer las credenciales: {e}")
        return

    try:
        import requests

        token = _appstoreconnect_jwt(
            config["keyId"], config["issuerId"], private_key_pem
        )
        # El informe de hoy casi nunca está listo -- se pide el de ayer, que suele ser el más
        # reciente ya consolidado (ver docstring: puede tener otro día más de retraso).
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        resp = requests.get(
            "https://api.appstoreconnect.apple.com/v1/salesReports",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "filter[frequency]": "DAILY",
                "filter[reportDate]": report_date,
                "filter[reportType]": "SALES",
                "filter[reportSubType]": "SUMMARY",
                "filter[vendorNumber]": config["vendorNumber"],
            },
            timeout=30,
        )
        if resp.status_code == 404:
            log(f"iOS: informe del {report_date} todavía no disponible")
            return
        resp.raise_for_status()
        tsv_text = gzip.decompress(resp.content).decode("utf-8")
    except Exception as e:
        log(f"iOS: fallo al pedir el informe de ventas: {e}")
        return

    rows = list(csv.DictReader(io.StringIO(tsv_text), delimiter="\t"))
    if not rows:
        log("iOS: informe vacío")
        return
    log(f"iOS: columnas encontradas: {list(rows[0].keys())}")

    apple_id = str(config.get("appleId", ""))
    downloads_today = 0
    unexpected_types = set()
    for row in rows:
        if str(row.get("Apple Identifier", "")).strip() != apple_id:
            continue
        product_type = str(row.get("Product Type Identifier", "")).strip()
        units = int(float(row.get("Units", "0") or 0))
        if product_type in APPSTORE_DOWNLOAD_PRODUCT_TYPES:
            downloads_today += units
        else:
            unexpected_types.add(product_type)

    if unexpected_types:
        log(
            "iOS: aviso -- tipos de producto no reconocidos para esta app "
            f"(no sumados, revisar si alguno debería contar): {unexpected_types}"
        )

    db.collection("download_stats").document("ios").set(
        {
            "downloadsToday": downloads_today,
            "reportDate": report_date,
            "updatedAt": time.time(),
        }
    )
    log(f"iOS: {downloads_today} descarga(s) el {report_date}")


def main():
    db = _get_firestore_db()
    if db is None:
        return
    _sync_android(db)
    _sync_ios(db)


if __name__ == "__main__":
    main()
