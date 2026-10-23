#!/usr/bin/env python3
"""
fb_quitar_awin.py — borra de la pagina de Facebook las publicaciones automaticas que NO son
de Amazon (22 oct 2026, el usuario: "quitalas").

Hasta el 22 oct `facebook_bot.py` no filtraba por tienda y publico sobre todo deporteoutlet.
Ademas el texto legal que anade dice "Como afiliados de Amazon...", que en esas era falso.

REGLA DE SEGURIDAD: solo se borra lo que cumple LAS DOS condiciones a la vez:
  1. Tiene la forma exacta del pie que genera el bot: "<precio>,XX€ (antes <precio>,XX€, -N%)".
  2. NO enlaza a amazon.es.
Asi las publicaciones hechas a mano (anuncios de la app, videos, felicitaciones) no pueden
caer por error aunque mencionen una tienda: no tienen ese formato de precio.

Por defecto NO borra. Hay que pasar --borrar.
"""
import json, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

CRED = Path.home() / ".rebajas_facebook_credentials.json"
GRAPH = "https://graph.facebook.com/v21.0"
RE_PRECIO = re.compile(r"[\d.]+,\d{2}\s*€\s*\(antes\s*[\d.]+,\d{2}\s*€")
RE_AMAZON = re.compile(r"amazon\.(?:es|com)", re.I)
RE_URL = re.compile(r"https?://([^\s/]+)")


def log(m):
    print(f"[awin] {m}", flush=True)


def posts(pid, tok, limite):
    out, url = [], f"{GRAPH}/{pid}/posts?" + urllib.parse.urlencode(
        {"fields": "id,created_time,message", "limit": 50, "access_token": tok})
    while url and len(out) < limite:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read())
        out.extend(d.get("data", []))
        url = (d.get("paging") or {}).get("next")
    return out[:limite]


def main():
    de_verdad = "--borrar" in sys.argv
    limite = int(sys.argv[sys.argv.index("--limite") + 1]) if "--limite" in sys.argv else 400
    c = json.loads(CRED.read_text(encoding="utf-8"))
    tok, pid = c["page_access_token"], c["page_id"]

    todas = posts(pid, tok, limite)
    log(f"{len(todas)} publicaciones leidas")

    objetivo, amazon, manuales = [], 0, 0
    for p in todas:
        msg = p.get("message") or ""
        if not RE_PRECIO.search(msg):
            manuales += 1
            continue
        if RE_AMAZON.search(msg):
            amazon += 1
            continue
        dom = RE_URL.search(msg)
        objetivo.append({"id": p["id"], "cuando": p.get("created_time", "")[:10],
                         "dominio": dom.group(1) if dom else "?",
                         "texto": (msg.split("\n")[2] if len(msg.split("\n")) > 2 else msg)[:50]})

    log(f"  {manuales} hechas a mano (sin formato de precio) -> NO se tocan")
    log(f"  {amazon} automaticas de Amazon -> se quedan")
    log(f"  {len(objetivo)} automaticas de otras tiendas -> a borrar")

    from collections import Counter
    log(f"  por dominio: {Counter(o['dominio'] for o in objetivo).most_common()}")
    log("\n  muestra de las 8 primeras:")
    for o in objetivo[:8]:
        log(f"    {o['cuando']}  {o['dominio']:<28} {o['texto']}")

    if not objetivo:
        return 0
    if not de_verdad:
        log("\nEN SECO. Para borrar de verdad: --borrar")
        return 0

    ok = 0
    for o in objetivo:
        q = urllib.parse.urlencode({"access_token": tok})
        req = urllib.request.Request(f"{GRAPH}/{o['id']}?{q}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30):
                ok += 1
        except urllib.error.HTTPError as e:
            log(f"  fallo {o['id']}: {e.read().decode()[:120]}")
    log(f"\nborradas {ok} de {len(objetivo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
