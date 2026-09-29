#!/usr/bin/env python3
"""
sync_analytics_stats.py — tres datos de Google Analytics 4 para el panel privado "InfoRebajas"
(29 sep 2026, app aparte, nunca publicada, solo para el propio dueño), todos SIN tocar ni una
línea de `rebajasdiarias-app` ni de `rebajasdiarias-web`: `firebase_analytics` ya está activo en
la app (ver push_service.dart) y GA4 ya recoge automáticamente, con cada evento, tanto la sesión
del usuario como la versión de la app instalada -- no hace falta instrumentar nada nuevo, solo
LEER lo que Firebase Analytics ya manda.

1. Origen del tráfico -- sesiones por país, últimos 7 días (`runReport`, dimensión `country`).
2. "Quién está online ahora" -- usuarios activos en los últimos ~30 min, el propio informe en
   tiempo real de GA4 (`runRealtimeReport`) -- da exactamente esto de fábrica, sin necesitar
   ningún heartbeat propio.
3. Desglose por versión de app instalada -- GA4 recoge `appVersion` como dimensión automática
   de cada evento (parte de la información básica del dispositivo que manda el SDK, no algo que
   haya que loguear a mano) -- basta con pedir un `runReport` agrupado por esa dimensión.

Necesita una credencial nueva que solo Vicente puede crear (cuenta de servicio con rol "Viewer"
en la propiedad GA4 -- Analytics > Administrar > Acceso a la propiedad -- y "Google Analytics
Data API" habilitada en el proyecto de Google Cloud, que puede ser el mismo `rebajasdiarias-8958a`
que ya usa Firebase): `~/ga4-data-api-credentials.json` (JSON de la cuenta de servicio) +
`~/ga4-stats-config.json` (`{"propertyId": "..."}`, el ID de propiedad GA4 se ve en Analytics >
Administrar > Detalles de la propiedad).

Nunca debe tumbar el cron si falta la credencial. Las cifras de "sesiones"/"activeUsers" de GA4
(fuera del informe en tiempo real) van con su propio retraso de procesamiento -- por eso el cron
recomendado es cada 4-6h, no más seguido, salvo el informe en tiempo real, que sí refleja el
momento actual.

Uso: cron cada 4-6h, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import json
import os
import time

from update_offers import FIREBASE_CREDENTIALS_PATH

HOME = os.path.expanduser("~")
GA4_CREDENTIALS_PATH = f"{HOME}/ga4-data-api-credentials.json"
GA4_CONFIG_PATH = f"{HOME}/ga4-stats-config.json"


def log(msg):
    print(f"[sync_analytics_stats] {msg}", flush=True)


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


def _get_ga4_client_and_property():
    if not os.path.isfile(GA4_CREDENTIALS_PATH) or not os.path.isfile(GA4_CONFIG_PATH):
        return None, None
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(GA4_CREDENTIALS_PATH)
        client = BetaAnalyticsDataClient(credentials=creds)
        with open(GA4_CONFIG_PATH, encoding="utf-8") as f:
            property_id = json.load(f)["propertyId"]
        return client, f"properties/{property_id}"
    except Exception as e:
        log(f"aviso: no se pudo inicializar el cliente de GA4: {e}")
        return None, None


def _sync_traffic_geo(db, client, property_path):
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    try:
        response = client.run_report(
            RunReportRequest(
                property=property_path,
                dimensions=[Dimension(name="country")],
                metrics=[Metric(name="activeUsers"), Metric(name="sessions")],
                date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
                limit=50,
            )
        )
        rows = [
            {
                "country": row.dimension_values[0].value,
                "activeUsers": int(row.metric_values[0].value),
                "sessions": int(row.metric_values[1].value),
            }
            for row in response.rows
        ]
    except Exception as e:
        log(f"tráfico: fallo al consultar GA4: {e}")
        return

    db.collection("traffic_geo").document("last_7_days").set(
        {"rows": rows, "updatedAt": time.time()}
    )
    log(f"tráfico: {len(rows)} país(es)")


def _sync_online_now(db, client, property_path):
    from google.analytics.data_v1beta.types import Metric, RunRealtimeReportRequest

    try:
        response = client.run_realtime_report(
            RunRealtimeReportRequest(
                property=property_path,
                metrics=[Metric(name="activeUsers")],
            )
        )
        active_now = (
            int(response.rows[0].metric_values[0].value) if response.rows else 0
        )
    except Exception as e:
        log(f"online ahora: fallo al consultar GA4 en tiempo real: {e}")
        return

    db.collection("analytics_live").document("summary").set(
        {"activeUsersNow": active_now, "updatedAt": time.time()}
    )
    log(f"online ahora: {active_now} usuario(s) activo(s)")


def _sync_app_versions(db, client, property_path):
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    try:
        response = client.run_report(
            RunReportRequest(
                property=property_path,
                dimensions=[Dimension(name="appVersion")],
                metrics=[Metric(name="activeUsers")],
                date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
                limit=50,
            )
        )
        rows = [
            {
                "version": row.dimension_values[0].value,
                "activeUsers": int(row.metric_values[0].value),
            }
            for row in response.rows
        ]
    except Exception as e:
        log(f"versiones: fallo al consultar GA4: {e}")
        return

    db.collection("app_version_stats").document("last_7_days").set(
        {"rows": rows, "updatedAt": time.time()}
    )
    log(f"versiones: {len(rows)} versión(es) distinta(s)")


def main():
    db = _get_firestore_db()
    if db is None:
        return
    client, property_path = _get_ga4_client_and_property()
    if client is None:
        log("sin credenciales de GA4 configuradas todavía, se salta")
        return
    _sync_traffic_geo(db, client, property_path)
    _sync_online_now(db, client, property_path)
    _sync_app_versions(db, client, property_path)


if __name__ == "__main__":
    main()
