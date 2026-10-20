#!/usr/bin/env python3
"""
salud.py — comprueba que los canales de la Pi siguen vivos y avisa cuando uno se muere.

Por qué existe (20 oct 2026). Ese día se descubrió, por casualidad y a la vez, que había TRES
canales caídos sin que nadie se hubiera enterado:

  - Facebook: el token de la página estaba invalidado ("the session has been invalidated
    because the user changed their password"). 311 intentos fallidos seguidos, ~26 días.
    En toda su vida solo publicó 7 veces, en agosto.
  - Telegram: faltaba el fichero del token. 513 avisos con HTTP 404 y ni una sola notificación
    con éxito en ~22.000 líneas de log.
  - Instagram: sin montar.

Los tres fallaban igual: escribiendo una línea en un log que nadie mira. Un bot que falla solo
contra un log es un bot que se muere en silencio, y cuanto más tiempo pasa menos se nota,
porque la ausencia de publicaciones no duele como duele un error en pantalla.

Esto no arregla nada por sí solo: comprueba y avisa. A propósito NO intenta renovar tokens ni
tocar credenciales — si algo se rompe hay que mirarlo, no que un script lo parchee a ciegas y
siga escondiendo el problema, que es justo como llegamos aquí.

Dónde avisa, por orden:
  1. Telegram privado al usuario, si el token está (es el canal bueno: lo lees en el móvil).
  2. `~/salud.txt`, siempre. Es lo que hay que mirar al empezar una sesión.
Y devuelve código de salida 1 si algo está roto, para que se note desde el cron.

Uso:
    python3 salud.py            # comprueba y avisa solo si algo está mal
    python3 salud.py --siempre  # avisa aunque esté todo bien (para probarlo)
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
INFORME = HOME / "salud.txt"
REPO_OFERTAS = HOME / "ofertas-rebajasdiarias"
TELEGRAM_TOKEN_PATH = HOME / ".rebajas_telegram_bot_token"
TELEGRAM_CHAT_ID = "1338347086"          # privado del usuario, igual que en update_offers.py
FACEBOOK_CREDS = HOME / ".rebajas_facebook_credentials.json"
INSTAGRAM_CREDS = HOME / ".rebajas_instagram_credentials.json"
# Si el catálogo lleva más de esto sin tocarse, es que el scraping ha dejado de funcionar.
# El cron va cada 3 horas entre las 08:00 y las 23:00, así que 10 h cubre la noche sin falsos
# positivos (último ciclo a las 22:00 → siguiente a las 08:00).
HORAS_SIN_CATALOGO = 10


def _get(url, params=None, timeout=25):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


# --- Comprobaciones -------------------------------------------------------------------------
# Cada una devuelve (nombre, ok, detalle). `ok` None = no aplica / no configurado a propósito.

def mirar_telegram():
    if not TELEGRAM_TOKEN_PATH.exists():
        return ("Telegram", False,
                f"falta {TELEGRAM_TOKEN_PATH}. Sácalo de BotFather (/mybots → tu bot → API "
                f"Token) y escríbelo ahí con chmod 600.")
    tok = TELEGRAM_TOKEN_PATH.read_text().strip()
    if not tok:
        return ("Telegram", False, f"{TELEGRAM_TOKEN_PATH} está vacío")
    try:
        r = _get(f"https://api.telegram.org/bot{tok}/getMe")
    except Exception as e:
        return ("Telegram", False, f"la API no responde o el token no vale: {e}")
    if not r.get("ok"):
        return ("Telegram", False, f"token rechazado: {r}")
    return ("Telegram", True, f"bot @{(r.get('result') or {}).get('username', '?')}")


def mirar_facebook():
    if not FACEBOOK_CREDS.exists():
        return ("Facebook", False, f"falta {FACEBOOK_CREDS}")
    c = json.loads(FACEBOOK_CREDS.read_text())
    try:
        d = _get("https://graph.facebook.com/v21.0/debug_token", {
            "input_token": c["page_access_token"],
            "access_token": f"{c['app_id']}|{c['app_secret']}",
        }).get("data", {})
    except Exception as e:
        return ("Facebook", False, f"no se pudo comprobar el token: {e}")
    if not d.get("is_valid"):
        return ("Facebook", False,
                f"token NO válido ({d.get('error', {}).get('message', 'sin motivo')}). "
                f"Hay que regenerarlo en el Explorador de la API Graph.")
    cad = d.get("expires_at") or 0
    if cad:
        quedan = (cad - time.time()) / 86400
        if quedan < 7:
            return ("Facebook", False, f"el token caduca en {quedan:.0f} días: renuévalo ya")
        return ("Facebook", True, f"válido, caduca en {quedan:.0f} días")
    return ("Facebook", True, "válido y sin fecha de caducidad")


def mirar_instagram():
    if not INSTAGRAM_CREDS.exists():
        return ("Instagram", None, "sin configurar todavía (pendiente de vincular la cuenta)")
    c = json.loads(INSTAGRAM_CREDS.read_text())
    try:
        r = _get(f"https://graph.facebook.com/v21.0/{c['ig_user_id']}",
                 {"fields": "username", "access_token": c["access_token"]})
    except Exception as e:
        return ("Instagram", False, f"el token no vale o la cuenta no responde: {e}")
    return ("Instagram", True, f"@{r.get('username', '?')}")


def mirar_catalogo():
    f = REPO_OFERTAS / "offers.json"
    if not f.exists():
        return ("Catálogo", False, f"no existe {f}")
    horas = (time.time() - f.stat().st_mtime) / 3600
    try:
        # offers.json es un dict {updated_at, affiliate_tag, offers}, no una lista.
        n = len((json.loads(f.read_text(encoding="utf-8")) or {}).get("offers") or [])
    except Exception as e:
        return ("Catálogo", False, f"offers.json no se puede leer: {e}")
    if horas > HORAS_SIN_CATALOGO:
        return ("Catálogo", False,
                f"sin actualizar desde hace {horas:.0f} h ({n} ofertas). El scraping ha dejado "
                f"de funcionar: mira ~/update_offers.log")
    return ("Catálogo", True, f"{n} ofertas, actualizado hace {horas:.1f} h")


def mirar_logs_con_errores():
    """Un log que solo escupe errores es un proceso muerto que nadie ha enterrado. Se mira la
    cola, no el fichero entero: lo de hace un mes ya no importa, lo que importa es si AHORA
    mismo todo lo que escribe son fallos."""
    malos = []
    for log in sorted(HOME.glob("*.log")):
        try:
            cola = subprocess.run(["tail", "-40", str(log)], capture_output=True, text=True,
                                  timeout=20).stdout.splitlines()
        except Exception:
            continue
        cola = [l for l in cola if l.strip()]
        if len(cola) < 10:
            continue
        fallos = sum(1 for l in cola
                     if any(p in l.lower() for p in
                            ("error", "traceback", "no se pudo", "failed", "exception")))
        if fallos >= len(cola) * 0.8:
            malos.append(f"{log.name} ({fallos}/{len(cola)} líneas son errores)")
    if malos:
        return ("Logs", False, "procesos que solo escupen errores: " + "; ".join(malos))
    return ("Logs", True, "ninguno escupiendo errores en cadena")


def avisar_por_telegram(texto):
    if not TELEGRAM_TOKEN_PATH.exists():
        return False
    tok = TELEGRAM_TOKEN_PATH.read_text().strip()
    try:
        datos = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": texto}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=datos),
            timeout=25).read()
        return True
    except Exception:
        return False


def main():
    ahora = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    resultados = [f() for f in (mirar_catalogo, mirar_telegram, mirar_facebook,
                                mirar_instagram, mirar_logs_con_errores)]
    rotos = [r for r in resultados if r[1] is False]

    lineas = [f"SALUD DE LA PI — {ahora}", ""]
    for nombre, ok, detalle in resultados:
        icono = "OK  " if ok else ("ROTO" if ok is False else "--  ")
        lineas.append(f"[{icono}] {nombre}: {detalle}")
    if rotos:
        lineas += ["", f"{len(rotos)} roto(s). Mientras no se arregle, ese canal no publica "
                       f"nada y NO avisa por su cuenta."]
    texto = "\n".join(lineas)

    INFORME.write_text(texto + "\n", encoding="utf-8")
    print(texto)

    if rotos or "--siempre" in sys.argv:
        avisar_por_telegram(texto)
    return 1 if rotos else 0


if __name__ == "__main__":
    sys.exit(main())
