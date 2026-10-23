#!/usr/bin/env python3
"""
facebook_limpiar.py — borra de la página de Facebook las publicaciones cuyo precio ya no es
el que anunciaban.

Por qué existe (22 oct 2026, petición del usuario: "si ya no está ese precio se quite").

`precios.py` evita publicar un precio caducado, pero no arregla lo que YA está publicado: una
oferta anunciada el lunes a 78 € puede costar 95 € el jueves, y la publicación sigue ahí
diciendo 78 €. En Facebook se puede borrar (el token tiene `pages_manage_posts`); en Instagram
NO -- la API solo permite borrar comentarios, `DELETE /{ig_media_id}` responde
`(#10) Insufficient permissions` y no es un permiso que se pueda pedir.

Por defecto NO BORRA NADA: enseña lo que haría. Hay que pasarle --borrar a propósito, porque
borrar es irreversible y una expresión regular mal puesta se llevaría por delante publicaciones
buenas.

Uso:
    python3 facebook_limpiar.py              # solo mira y cuenta
    python3 facebook_limpiar.py --borrar     # borra de verdad
    python3 facebook_limpiar.py --limite 50  # cuántas publicaciones recientes revisar
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CREDENCIALES = Path.home() / ".rebajas_facebook_credentials.json"
GRAPH = "https://graph.facebook.com/v21.0"
LIMITE_POR_DEFECTO = 100

# El ASIN sale de la URL de afiliado que va en la primera línea del pie.
RE_ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
# El precio, del formato que escribe build_caption(): "… — 43,17€ (antes 60,00€, -28%)".
# Se ancla en "€ (antes" y no en el guion porque los títulos llevan guiones a menudo.
RE_PRECIO = re.compile(r"([\d.]+,\d{2})\s*€\s*\(antes")


def log(msg):
    print(f"[fb_limpiar] {msg}", flush=True)


def _euros(txt):
    return float(txt.replace(".", "").replace(",", "."))


def publicaciones(pid, tok, limite):
    out, url = [], (f"{GRAPH}/{pid}/posts?" + urllib.parse.urlencode(
        {"fields": "id,created_time,message", "limit": 50, "access_token": tok}))
    while url and len(out) < limite:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read())
        out.extend(d.get("data", []))
        url = (d.get("paging") or {}).get("next")
    return out[:limite]


def borrar(post_id, tok):
    q = urllib.parse.urlencode({"access_token": tok})
    req = urllib.request.Request(f"{GRAPH}/{post_id}?{q}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("success", True)
    except urllib.error.HTTPError as e:
        log(f"  no se pudo borrar {post_id}: {e.read().decode()[:160]}")
        return False


def main():
    de_verdad = "--borrar" in sys.argv
    limite = LIMITE_POR_DEFECTO
    if "--limite" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--limite") + 1])

    creds = json.loads(CREDENCIALES.read_text(encoding="utf-8"))
    tok, pid = creds["page_access_token"], creds["page_id"]

    posts = publicaciones(pid, tok, limite)
    log(f"{len(posts)} publicaciones revisadas")

    conAsin, sinAsin = [], 0
    for p in posts:
        msg = p.get("message") or ""
        m, mp = RE_ASIN.search(msg), RE_PRECIO.search(msg)
        if not m or not mp:
            sinAsin += 1
            continue
        conAsin.append({"post": p["id"], "asin": m.group(1),
                        "precio": _euros(mp.group(1)),
                        "cuando": p.get("created_time", "")[:10],
                        "texto": msg.split("\n")[2][:46] if len(msg.split("\n")) > 2 else ""})
    log(f"{len(conAsin)} son de Amazon con precio legible; {sinAsin} no (Awin, manuales o sin texto)")
    if not conAsin:
        return 0

    import precios
    caducadas, vivas = [], 0
    for i in range(0, len(conAsin), 10):
        lote = conAsin[i:i + 10]
        reales = precios.precios_ahora([x["asin"] for x in lote])
        if reales is None:
            log("la API de Amazon no responde; se para para no borrar a ciegas")
            return 1
        for x in lote:
            real = reales.get(x["asin"])
            if real is None:
                x["ahora"] = None
                caducadas.append(x)
            elif real - x["precio"] > max(precios.TOLERANCIA_EUR,
                                          x["precio"] * precios.TOLERANCIA_PCT):
                # Solo se borra cuando ahora cuesta MÁS de lo que anunciamos, que es el caso
                # que engaña a quien entra. Si ha bajado todavía más, la publicación se queda:
                # el visitante se encuentra un precio mejor del prometido, y borrar eso sería
                # tirar alcance a cambio de nada. Si algún día se prefiere exactitud estricta,
                # basta con volver a comparar en valor absoluto.
                x["ahora"] = real
                caducadas.append(x)
            else:
                if real < x["precio"]:
                    log(f"  se queda (ha bajado a {real} €): {x['texto'][:40]}")
                vivas += 1

    log(f"\n{vivas} siguen a su precio, {len(caducadas)} ya no:")
    for x in caducadas:
        ahora = "sin stock / retirado" if x["ahora"] is None else f"ahora {x['ahora']} €"
        log(f"  {x['cuando']}  anunciaba {x['precio']:>7.2f} €  →  {ahora:<22} "
            f"{x['texto']}")

    if not de_verdad:
        log(f"\nEN SECO: no se ha borrado nada. Para borrarlas: --borrar")
        return 0

    hechas = sum(1 for x in caducadas if borrar(x["post"], tok))
    log(f"\nborradas {hechas} de {len(caducadas)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
