#!/usr/bin/env python3
"""
search_requests_listener.py — buscador con Amazon en vivo, en TIEMPO REAL (29 ago 2026,
pedido explícito: "en la otra app era super rápida... el buscador primero busca directamente
en amazon para que sea rapido y luego salen las ofertas que tenemos"). Sustituye a
check_search_requests.py, que sondeaba Firestore cada 1 minuto (cron) -- demasiado lento para
lo que el usuario quería. Este proceso se queda corriendo TODO EL RATO y reacciona al instante
en cuanto se crea una búsqueda nueva, usando el listener en tiempo real de Firestore (Admin
SDK) en vez de sondear -- respuesta típica de 1-3 segundos en vez de hasta 60.

Sigue siendo gratis (la propia Pi, no un servicio de pago) -- el usuario explícitamente
descartó pagar Firebase Cloud Functions para tener velocidad, así que la solución fue cambiar
"sondear cada minuto" por "escuchar en tiempo real", no gastar dinero.

IMPORTANTE: esto es un SERVICIO (systemd, ver rebajasdiarias-search.service), NO un cron --
necesita estar siempre vivo, no lanzarse y terminar. systemd lo reinicia solo si se cae.

check_search_requests.py se deja en el repo sin más (sus funciones de disponibilidad se
reutilizan aquí por import) pero ya NO está en el crontab -- este proceso lo sustituye del
todo para las búsquedas. El sondeo periódico de disponibilidad (para el aviso de Telegram
cuando nadie está buscando) se mantiene aquí también, en un hilo aparte.
"""

import os
import smtplib
import sys
import threading
import time
from email.message import EmailMessage
from datetime import timedelta

# --- Forzar IPv4 (14-oct-2026) ----------------------------------------------------------------
# El IPv6 de esta Raspberry esta roto, pero el sistema lo PREFIERE: el DNS (via Tailscale)
# devuelve AAAA primero para Google/Firestore/Amazon, el proceso intenta IPv6, a veces se cuelga
# hasta agotar el tiempo -> Firestore "DEADLINE_EXCEEDED" (el buscador no recibe/escribe
# busquedas) y timeouts a Amazon, de forma INTERMITENTE. Resolver nativo de gRPC + getaddrinfo
# limitado a IPv4 hacen que TODO use la via IPv4, que si funciona (probado: 8/8 lecturas de
# Firestore OK en <0.7s). Debe ir ANTES de importar firebase_admin/grpc.
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _getaddrinfo_ipv4(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, *args, **kwargs)
_socket.getaddrinfo = _getaddrinfo_ipv4
# ----------------------------------------------------------------------------------------------

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

import amazon_paapi as paapi
import keyword_alert_search
from update_offers import FIREBASE_CREDENTIALS_PATH
from check_search_requests import (
    HEALTH_CHECK_INTERVAL_MINUTES,
    HEALTH_CHECK_QUERY,
    _load_availability_state,
    _save_availability_state,
    _note_availability,
)

db = None


def log(msg):
    print(f"[search_requests_listener] {msg}", flush=True)


# 28 sep 2026, bug real encontrado con datos reales de Firestore: varias búsquedas seguidas
# ("impresoras de mano/portatil/con tinta", tecleadas en pocos minutos refinando la misma
# búsqueda) salían en el panel "Qué pide la gente" sin nº de ofertas -- para siempre, no un
# simple retraso. Causa real: hasta ahora solo sync_search_stats.py (cron de la Pi, cada 10
# min) copiaba el resultCount de search_requests a su search_history correspondiente
# (emparejando por término+hora) -- pero AmazonSearchService.deleteSearchRequest() (app/web)
# borra search_requests en cuanto el usuario sale de la pantalla de búsqueda, y si eso pasa
# antes de que le toque el turno al cron (segundos, si se encadenan varias búsquedas, contra
# los 10 min del cron), el documento fuente ya no existe cuando le toca sincronizar. Arreglo
# real: escribirlo aquí mismo, nada más completarse la búsqueda (segundos después de crearse,
# mucho antes de que exista ninguna oportunidad de borrado) -- mismo criterio de emparejamiento
# que _fill_search_history_counts() en sync_search_stats.py (± HISTORY_MATCH_SECONDS), pero
# para un solo término en vez de en lote. Sin tocar la app -- va por delante de todo eso.
HISTORY_MATCH_SECONDS = 120


def _fill_matching_search_history(query_text, requested_at, result_count):
    if requested_at is None:
        return
    term = query_text.strip().lower()
    if not term:
        return
    try:
        since = requested_at - timedelta(seconds=HISTORY_MATCH_SECONDS)
        until = requested_at + timedelta(seconds=HISTORY_MATCH_SECONDS)
        docs = (
            db.collection("search_history")
            .where(filter=FieldFilter("requestedAt", ">=", since))
            .where(filter=FieldFilter("requestedAt", "<=", until))
            .stream()
        )
        for d in docs:
            data = d.to_dict() or {}
            # 8 oct 2026, bug real con datos de Firestore: el panel "Qué pide la gente" enseñaba
            # 9/10 en búsquedas que de verdad habían encontrado 58, 65 o 71 (log de la Pi:
            # 'tommy hombre M': 71 -> search_history guardaba 9). Causa: desde que la búsqueda
            # entrega por tandas (_entregar_parcial más abajo), el documento pasa a "done" ya en
            # la PRIMERA tanda, así que el nº que escribe la app (updateSearchHistoryResultCount)
            # es el de esa tanda -- y este relleno se lo saltaba, porque hasta ahora bastaba con
            # que hubiera cualquier número para no tocarlo (candado del 28 sep, puesto cuando
            # "done" llegaba una sola vez y ese número ya era el bueno). El total definitivo no
            # entraba nunca: quedaba congelado en 10 para siempre. Ahora solo se respeta el que
            # ya hay si es >= al definitivo -- nunca se baja un número, solo se completa.
            existing = data.get("resultCount")
            if isinstance(existing, (int, float)) and existing >= result_count:
                continue
            if (data.get("query") or "").strip().lower() != term:
                continue
            d.reference.set({"resultCount": result_count}, merge=True)
    except Exception as e:
        log(f"aviso: no se pudo rellenar resultCount en search_history para {query_text!r}: {e}")


def _process_request(doc_id, data):
    # 13 oct 2026: TODO el cuerpo va envuelto -> el fallo de UNA busqueda nunca sube como
    # excepcion no capturada del hilo (eso dispararia el excepthook, pensado solo para la muerte
    # del watch). Una busqueda que peta deja su doc en "error" y ya, el listener sigue vivo.
    try:
        query_text = (data.get("query") or "").strip()
        doc_ref = db.collection("search_requests").document(doc_id)
        if not query_text:
            doc_ref.set({"status": "error"}, merge=True)
            return
        # 21 sep 2026, aviso real ("por que me ha tardado mucho"): un usuario esperando delante de
        # la pantalla no debe competir con el repaso de fondo de keyword_alert_cleanup.py --
        # ver mark_live_search_pending()/yield_to_live_search() en keyword_alert_search.py.
        priority_marker = keyword_alert_search.mark_live_search_pending()
        try:
            _process_request_inner(doc_id, doc_ref, query_text, data)
        finally:
            keyword_alert_search.clear_live_search_pending(priority_marker)
    except Exception as e:
        log(f"aviso: fallo procesando la busqueda {doc_id!r}: {e}")
        try:
            db.collection("search_requests").document(doc_id).set({"status": "error"}, merge=True)
        except Exception:
            pass


def _process_request_inner(doc_id, doc_ref, query_text, data):
    # Camino de "alerta de palabra clave" (14 sep 2026, ver AmazonSearchService.
    # createKeywordAlertRequest() en la app -- se dispara al añadir una alerta en Ajustes):
    # usa el motor compartido con keyword_alert_cleanup.py -- scraping real (Selenium/Chrome),
    # guarda en keyword_alert_offers (umbral 1%, no 30%) y avisa por push desde ahí mismo. Este
    # documento de search_requests solo sirve de disparador async, nadie lo escucha en vivo.
    if data.get("notifyPush") and data.get("requestedBy"):
        try:
            new_offers = keyword_alert_search.refresh_keyword_alert(
                db, data["requestedBy"], query_text, first_search=True
            )
            if new_offers is None:
                doc_ref.set({"status": "unavailable"}, merge=True)
                log(f"{query_text!r} (alerta): no se pudo completar el scraping ahora mismo")
            else:
                doc_ref.set({"status": "done", "resultCount": len(new_offers)}, merge=True)
        except Exception as e:
            log(f"ERROR en alerta {query_text!r}: {e}")
            try:
                doc_ref.set({"status": "error"}, merge=True)
            except Exception:
                pass
        return
    # Buscador de la propia pantalla (AmazonSearchService.createSearchRequest()) -- hasta el 20
    # sep 2026 llamaba a paapi.search_amazon() (Amazon Product Advertising API), bloqueada desde
    # semanas antes por el umbral de ventas de Amazon (403 AssociateNotEligible, mismo motivo
    # por el que las alertas se reescribieron el 14 sep) -- nunca se actualizó este camino en su
    # momento, así que llevaba devolviendo "unavailable"/0 resultados en silencio todo este
    # tiempo (hallazgo real: "he buscado anillo luminoso... no ha encontrado nada y en alertas
    # sí"). Ahora usa el MISMO motor real que las alertas (scraping Selenium/Chrome, con
    # candado + pausa del ciclo normal + reintentos ya incluidos en _scrape_keyword_live). Sin
    # umbral propio a partir de aquí (20 sep 2026, pedido explícito: "lo que es en vivo me da
    # igual que tanto por % tenga de descuento") -- se deja caer a los valores por defecto de
    # _scrape_keyword_live(), que son los mismos de las alertas (1%-100%, MIN/MAX_SAVING_
    # PERCENT_KEYWORD_ALERT), no el 30-80% estándar del resto del catálogo. import amazon_paapi
    # se deja arriba sin usar aquí por si hiciera falta más adelante para otra cosa.
    # 8 oct 2026, pedido explícito ("necesito que tarde poco como antes, que vayan apareciendo y
    # luego se le vayan añadiendo abajo y así no se notará que tarda más"): desde que la búsqueda
    # pagina (ver SEARCH_MAX_PAGES en amazon_paapi.py) reunir los ~90 resultados tarda unos
    # segundos, pero no hace falta esperar a tenerlos todos para enseñar algo. Cada tanda que
    # llega se escribe ya en el MISMO documento que la web y la app están escuchando en vivo
    # (onSnapshot/snapshots()): la primera cae en ~1 s, igual de rápido que antes, y el resto se
    # van añadiendo solos debajo sin que nadie espere mirando un círculo. Por eso el estado va
    # como "done" desde la primera tanda -- es el único que los clientes pintan; con un estado
    # nuevo tipo "partial" se quedarían en "Buscando…" y habría que tocar web y app.
    entregadas = 0
    # Una escritura por página serían ~10 por búsqueda. La primera tanda se escribe siempre (es
    # la que hace que la búsqueda se sienta rápida); a partir de ahí se agrupan de
    # TANDA_MINIMA en TANDA_MINIMA para gastar la mitad de escrituras sin que se note el salto.
    # La escritura final de abajo lleva igualmente la lista completa, así que no se pierde nada.
    TANDA_MINIMA = 10

    def _entregar_parcial(offers_hasta_ahora):
        nonlocal entregadas
        if len(offers_hasta_ahora) <= entregadas:
            return  # nada nuevo que escribir, no se gasta una escritura de Firestore de más
        if entregadas and len(offers_hasta_ahora) - entregadas < TANDA_MINIMA:
            return  # todavía son pocas nuevas, se esperan a la siguiente página
        entregadas = len(offers_hasta_ahora)
        try:
            doc_ref.set(
                {"status": "done", "results": offers_hasta_ahora,
                 "resultCount": len(offers_hasta_ahora)},
                merge=True,
            )
        except Exception as e:
            # Una tanda intermedia que no se pueda escribir no debe tumbar la búsqueda: la
            # escritura final de abajo lleva igualmente la lista completa.
            log(f"aviso: no se pudo escribir una tanda parcial de {query_text!r}: {e!r}")

    try:
        offers = keyword_alert_search._scrape_keyword_live(
            query_text, on_partial=_entregar_parcial
        )
        if offers is None:
            doc_ref.set({"status": "unavailable"}, merge=True)
            log(f"{query_text!r}: no se pudo completar el scraping ahora mismo")
            return
        doc_ref.set(
            {"status": "done", "results": offers, "resultCount": len(offers)},
            merge=True,
        )
        log(f"{query_text!r}: {len(offers)} resultado(s) con descuento real")
        # No en el camino de alertas de arriba (notifyPush) a propósito -- ese término vive en
        # keyword_alert_stats, no en search_stats/search_history (ver comentario de
        # _fill_search_history_counts() en sync_search_stats.py, mismo criterio aquí).
        _fill_matching_search_history(query_text, data.get("requestedAt"), len(offers))
    except Exception as e:
        log(f"ERROR buscando {query_text!r}: {e}")
        try:
            doc_ref.set({"status": "error"}, merge=True)
        except Exception:
            pass


def _on_snapshot(col_snapshot, changes, read_time):
    # Cada búsqueda se procesa en su propio hilo -- si llegan dos peticiones a la vez, no se
    # bloquean entre sí esperando la respuesta de Amazon de la otra.
    for change in changes:
        if change.type.name in ("ADDED", "MODIFIED"):
            data = change.document.to_dict() or {}
            if data.get("status") == "pending":
                threading.Thread(
                    target=_process_request,
                    args=(change.document.id, data),
                    daemon=True,
                ).start()


def _health_check_loop():
    """Sondeo periódico de disponibilidad (29 ago 2026, pedido explícito: avisar por Telegram
    en los dos sentidos) -- con el listener en tiempo real ya no hace falta para procesar
    búsquedas de verdad, pero sigue haciendo falta para detectar un cambio de disponibilidad
    cuando NADIE está buscando en ese momento. Corre en un hilo aparte, cada 30 min como
    mucho."""
    while True:
        # Comprobar primero (y de paso calentar el token OAuth cacheado) al arrancar, antes de
        # dormir: asi la 1a busqueda tras un reinicio ya encuentra el token listo.
        try:
            available = paapi.search_amazon(HEALTH_CHECK_QUERY, item_count=1) is not None
            if not available:
                # Confirmar una caida con un unico reintento antes de avisar -> evita falsos
                # positivos por un blip puntual (o por el token frio justo al arrancar).
                time.sleep(5)
                available = paapi.search_amazon(HEALTH_CHECK_QUERY, item_count=1) is not None
            prev = _load_availability_state().get("available")
            _save_availability_state(available)
            # Avisar POR CORREO solo en un cambio real de estado (nunca la 1a vez ni en reinicios
            # sin cambio). notify_telegram ya no se usa aqui: su token esta muerto y el dueño no
            # se enteraba de las caidas (14-oct-2026).
            if prev is not None and prev != available:
                if available:
                    _send_alert_email(
                        "[RebajasDiarias] Buscador RECUPERADO",
                        "El buscador con Amazon en vivo vuelve a funcionar con normalidad.\n\n"
                        f"Hora: {time.ctime()}")
                    log("aviso por correo: buscador recuperado")
                else:
                    _alert_search_down("el sondeo periodico detecta la API de Amazon caida")
        except Exception as e:
            log(f"aviso: fallo en el sondeo periódico de disponibilidad: {e}")
        time.sleep(HEALTH_CHECK_INTERVAL_MINUTES * 60)



# --- Auto-recuperacion + aviso (13 oct 2026) ------------------------------------------------
REJUVENATE_HOURS = 2  # cada cuanto se renueva el watch de Firestore (preventivo anti-GOAWAY)
# Fichero con credenciales de email para avisos (NO en git): 2-3 lineas ->
#   1: remitente (gmail)   2: contraseña de APLICACION de Gmail (16 car.)   3 (opc): destinatario
EMAIL_ALERT_CREDS_PATH = os.path.expanduser("~/.rebajas_email_alert.txt")
OWNER_EMAIL = "rebajasdiarias21@gmail.com"
ALERT_COOLDOWN_MIN = 30  # no repetir el aviso si ya se mando hace menos de esto
ALERT_STATE_PATH = os.path.expanduser("~/.rebajas_search_last_alert")


# EmailJS: mismas claves que el formulario de Contacto de la app (lib/pages/contact_page.dart).
# Envia correo al dueño SIN contraseña de Gmail. El User-Agent de navegador es obligatorio
# (EmailJS/Cloudflare devuelve 403 code 1010 a peticiones no-navegador).
EMAILJS_SERVICE_ID = "rebajasdiariasemail"
EMAILJS_TEMPLATE_ID = "template_jr8dfz7"
EMAILJS_USER_ID = "bAIzFcq-eIz-qB7uK"


def _send_alert_email(subject, body):
    try:
        import json as _json
        import urllib.request as _ur
        payload = {
            "service_id": EMAILJS_SERVICE_ID,
            "template_id": EMAILJS_TEMPLATE_ID,
            "user_id": EMAILJS_USER_ID,
            "template_params": {
                "user_name": subject,
                "user_email": OWNER_EMAIL,
                "user_message": body,
                "fecha": time.ctime(),
                "reply_to": OWNER_EMAIL,
            },
        }
        req = _ur.Request(
            "https://api.emailjs.com/api/v1.0/email/send",
            data=_json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "origin": "http://localhost",
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                "Referer": "http://localhost/",
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=20) as r:
            if r.status == 200:
                log("aviso por email (EmailJS) enviado correctamente")
                return True
        return False
    except Exception as e:
        log(f"aviso: no se pudo enviar email de alerta (EmailJS): {e}")
        return False


def _alert_search_down(reason):
    """Avisa al dueño de que el buscador se cayo (y se reinicia solo). Nunca lanza. Con cooldown:
    si ya se aviso hace < ALERT_COOLDOWN_MIN, NO se repite -> un aviso cuando esta caido, no uno
    por cada reintento/reinicio."""
    try:
        if os.path.isfile(ALERT_STATE_PATH):
            last = float(open(ALERT_STATE_PATH).read().strip() or "0")
            if time.time() - last < ALERT_COOLDOWN_MIN * 60:
                log("aviso de caida omitido (cooldown activo, ya avisado hace poco)")
                return
    except Exception:
        pass
    try:
        with open(ALERT_STATE_PATH, "w") as _fh:
            _fh.write(str(time.time()))
    except Exception:
        pass
    subject = "[RebajasDiarias] BUSCADOR CAIDO - reiniciandose"
    body = ("El listener de busquedas ha fallado y se esta reiniciando automaticamente.\n\n"
            f"Motivo: {reason}\n"
            f"Hora: {time.ctime()}\n\n"
            "El servicio (systemd, Restart=always) deberia volver en ~10s. Si recibes esto varias "
            "veces seguidas, hay algo mas y conviene revisar la Pi.")
    if not _send_alert_email(subject, body):
        try:
            from firebase_admin import auth, messaging
            uid = auth.get_user_by_email(OWNER_EMAIL).uid
            messaging.send(messaging.Message(
                notification=messaging.Notification(
                    title="\u26a0\ufe0f Buscador caido (se reinicia solo)",
                    body=f"Fallo del buscador: {reason}. Reiniciandose."),
                topic=f"user_{uid}"))
            log("aviso por push (dueño) enviado")
        except Exception as e:
            log(f"aviso: no se pudo mandar push de alerta: {e}")


def _thread_excepthook(args):
    """Si CUALQUIER hilo muere por una excepcion no capturada (incluido el hilo interno del watch
    de Firestore), avisamos y matamos el proceso -> systemd lo revive limpio. os._exit porque
    sys.exit dentro de un hilo no mata el proceso entero."""
    try:
        reason = f"{args.exc_type.__name__}: {args.exc_value}"
    except Exception:
        reason = "excepcion no capturada en un hilo"
    try:
        log(f"HILO MUERTO ({getattr(args.thread, 'name', '?')}): {reason} -- reiniciando el buscador")
    except Exception:
        pass
    try:
        _alert_search_down(reason)
    except Exception:
        pass
    os._exit(1)


def main():
    global db
    threading.excepthook = _thread_excepthook
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        log("ERROR: no hay credenciales de Firebase, no se puede arrancar")
        return
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    threading.Thread(target=_health_check_loop, daemon=True).start()

    query = db.collection("search_requests").where(filter=FieldFilter("status", "==", "pending"))

    # El watch se re-crea cada REJUVENATE_HOURS (13 oct 2026): refresca la conexion gRPC para que
    # los GOAWAY "too_many_pings" no se acumulen hasta matar el hilo del watch (lo que dejo el
    # buscador ~8h caido en silencio). El nuevo watch recibe en su snapshot inicial las peticiones
    # "pending" que hubiera, asi que no se pierde ninguna en la renovacion.
    while True:
        watch = query.on_snapshot(_on_snapshot)
        log("escuchando search_requests en tiempo real...")
        time.sleep(REJUVENATE_HOURS * 3600)
        try:
            watch.unsubscribe()
        except Exception:
            pass
        log("watch de Firestore renovado (preventivo anti-GOAWAY)")


if __name__ == "__main__":
    main()
