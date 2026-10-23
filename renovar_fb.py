#!/usr/bin/env python3
"""
renovar_fb.py — convierte un token corto del Explorador en un token de página PERMANENTE.

Por qué existe (20 oct 2026): el token de la página llevaba ~26 días invalidado y
facebook_bot.py acumulaba 311 fallos seguidos sin que nadie se enterara. El token que da el
Explorador de la API Graph dura una hora, y el "Extend Access Token" del depurador lo deja en
60 días — o sea, el mismo problema otra vez dentro de dos meses.

Hay una tercera opción que mucha gente no conoce y es la buena: un **token de PÁGINA derivado
de un token de usuario de larga duración NO CADUCA**. Son dos pasos:

  1. token corto de usuario  --(app_id + app_secret)-->  token largo de usuario (60 días)
  2. token largo de usuario  --(GET /me/accounts)-->     token de PÁGINA (sin caducidad)

Este script hace los dos y deja el resultado en el fichero de credenciales que ya usa
facebook_bot.py, conservando app_id, app_secret y page_id.

El token corto se lee de ~/.tok_corto (que escribe el usuario con su portapapeles) y el
fichero se BORRA al terminar: no tiene por qué quedarse ahí.

Uso:
    python3 renovar_fb.py
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
TOKEN_CORTO = HOME / ".tok_corto"
CREDS = HOME / ".rebajas_facebook_credentials.json"
GRAPH = "https://graph.facebook.com/v21.0"


def get(path, params):
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.loads(r.read().decode())


def main():
    if not TOKEN_CORTO.exists():
        print(f"Falta {TOKEN_CORTO}. Escribe ahí el token del Explorador.", file=sys.stderr)
        return 1
    corto = TOKEN_CORTO.read_text().strip()
    if not corto.startswith("EAA"):
        print("Eso no parece un token de Meta (debería empezar por EAA).", file=sys.stderr)
        return 1

    c = json.loads(CREDS.read_text())
    page_id = c["page_id"]

    # Paso 1: corto -> largo (60 días).
    try:
        largo = get("oauth/access_token", {
            "grant_type": "fb_exchange_token",
            "client_id": c["app_id"],
            "client_secret": c["app_secret"],
            "fb_exchange_token": corto,
        })["access_token"]
    except Exception as e:
        print(f"ERROR al alargar el token de usuario: {e}", file=sys.stderr)
        return 1
    print("paso 1: token de usuario alargado a 60 días")

    # Paso 2: largo -> token de PÁGINA (sin caducidad).
    try:
        cuentas = get("me/accounts", {"access_token": largo}).get("data", [])
    except Exception as e:
        print(f"ERROR al pedir las páginas: {e}", file=sys.stderr)
        return 1
    pagina = next((p for p in cuentas if p.get("id") == page_id), None)
    if not pagina or not pagina.get("access_token"):
        print(f"No aparece la página {page_id} entre las que devuelve Meta. "
              f"Devuelve: {[p.get('name') for p in cuentas]}", file=sys.stderr)
        return 1
    print(f"paso 2: token de página obtenido para «{pagina.get('name')}»")

    # Comprobar ANTES de guardar, para no sustituir un token que funciona por uno que no.
    try:
        d = get("debug_token", {
            "input_token": pagina["access_token"],
            "access_token": f"{c['app_id']}|{c['app_secret']}",
        })["data"]
    except Exception as e:
        print(f"ERROR al comprobar el token nuevo: {e}", file=sys.stderr)
        return 1
    if not d.get("is_valid"):
        print(f"El token nuevo NO es válido, no lo guardo: {d}", file=sys.stderr)
        return 1
    caduca = d.get("expires_at") or 0
    print(f"comprobado: válido, {'SIN CADUCIDAD' if not caduca else f'caduca en {caduca}'}")
    print("permisos:", ", ".join(sorted(d.get("scopes", []))))

    c["page_access_token"] = pagina["access_token"]
    CREDS.write_text(json.dumps(c, indent=2), encoding="utf-8")
    CREDS.chmod(0o600)
    TOKEN_CORTO.unlink(missing_ok=True)
    print(f"guardado en {CREDS} y borrado {TOKEN_CORTO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
