#!/usr/bin/env python3
"""
instagram_insights.py — qué está funcionando de verdad en Instagram, con datos.

Por qué existe (21 oct 2026): hasta ahora el bot publicaba a ciegas. Elegía la oferta con un
criterio razonable (lo que busca la gente + temporada), pero nadie comprobaba si eso que
parecía razonable le interesaba a alguien. Con 1.051 seguidores ya hay volumen suficiente para
que los números digan algo.

Lee los insights reales de cada publicación y los ordena. No inventa recomendaciones: enseña
los datos y deja que se vean. Las métricas que importan en una cuenta de chollos:

  - ALCANCE: cuánta gente distinta lo vio. Es lo que decide el algoritmo.
  - GUARDADOS: la señal más fuerte en contenido de ofertas. Guardar = "esto me interesa de
    verdad, vuelvo luego". Pesa más que un me gusta para que Instagram reparta el contenido.
  - VISITAS AL PERFIL: quien va al perfil es quien puede acabar pulsando el enlace de la bio,
    que es el único camino hacia la web y la app.
  - ME GUSTA y COMENTARIOS: los que más se miran y los que menos dicen.

Uso:
    python3 instagram_insights.py            # últimas 15 publicaciones
    python3 instagram_insights.py --todas    # todo lo que devuelva la API
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CREDENCIALES = Path.home() / ".rebajas_instagram_credentials.json"
GRAPH = "https://graph.facebook.com/v21.0"


def get(path, params):
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        try:
            return {"_err": json.loads(e.read().decode()).get("error", {}).get("message", "?")}
        except Exception:
            return {"_err": str(e)}


# Métricas por publicación. Medido el 21 oct: todas existen, pero NO todas valen para todos
# los formatos -- pedir profile_visits sobre un carrusel o un vídeo antiguo tumba la consulta
# entera con "does not support ... for this media product type". Así que se piden las básicas
# (válidas en todo) y aparte las extra, que si fallan se dejan fuera sin perder el resto.
METRICAS = ["reach", "saved", "likes", "comments", "shares", "views", "total_interactions"]
METRICAS_EXTRA = ["profile_visits", "follows"]


def insights_de(media_id, tok):
    """Devuelve (datos, error_de_permiso). Distingue los dos fallos a propósito:
    - falta de permiso -> hay que arreglarlo, se avisa y se para;
    - métrica no soportada en ese formato -> se sigue con las que sí valen.
    Confundirlos lleva a tablas de ceros que se leen como 'no lo ve nadie'."""
    datos = {}
    for grupo in (METRICAS, METRICAS_EXTRA):
        r = get(f"{media_id}/insights", {"metric": ",".join(grupo), "access_token": tok})
        if r.get("_err"):
            if "permission" in r["_err"].lower():
                return datos, r["_err"]
            continue  # formato que no admite esas métricas: no es un fallo que arreglar
        for d in r.get("data", []):
            datos[d["name"]] = (d.get("values") or [{}])[0].get("value", 0)
    return datos, None


def main():
    if not CREDENCIALES.exists():
        print(f"Falta {CREDENCIALES}", file=sys.stderr)
        return 1
    c = json.loads(CREDENCIALES.read_text())
    tok, ig = c["access_token"], c["ig_user_id"]
    tope = 100 if "--todas" in sys.argv else 15

    perfil = get(ig, {"fields": "username,followers_count,media_count", "access_token": tok})
    if perfil.get("_err"):
        print(f"ERROR: {perfil['_err']}", file=sys.stderr)
        return 1
    print(f"@{perfil['username']} — {perfil['followers_count']} seguidores, "
          f"{perfil['media_count']} publicaciones\n")

    medios = get(f"{ig}/media", {"fields": "id,caption,timestamp,permalink,media_type",
                                 "limit": tope, "access_token": tok}).get("data", [])
    if not medios:
        print("Sin publicaciones todavía.")
        return 0

    filas = []
    for m in medios:
        # Los insights de una publicación solo existen a partir de cierta antigüedad y no todas
        # las métricas están en todos los formatos; si falta una, se deja a cero en vez de
        # romper la tabla entera.
        datos, sin_permiso = insights_de(m["id"], tok)
        if sin_permiso:
            print(f"ERROR: {sin_permiso}\nFalta instagram_manage_insights en el token: "
                  f"añadirlo a la app y regenerarlo con renovar_fb.py.", file=sys.stderr)
            return 1
        titulo = (m.get("caption") or "").split("\n")[0][:42]
        filas.append({"fecha": (m.get("timestamp") or "")[:10], "titulo": titulo,
                      "enlace": m.get("permalink"),
                      **{k: datos.get(k, 0) for k in METRICAS + METRICAS_EXTRA}})

    cab = (f"{'fecha':<11}{'vistas':>7}{'alcance':>8}{'guard.':>7}{'likes':>6}"
           f"{'coment':>7}  título")
    print(cab)
    print("-" * len(cab))
    for f in sorted(filas, key=lambda x: -(x["views"] or x["reach"])):
        print(f"{f['fecha']:<11}{f['views']:>7}{f['reach']:>8}{f['saved']:>7}"
              f"{f['likes']:>6}{f['comments']:>7}  {f['titulo']}")

    vistas = [f for f in filas if f["views"] or f["reach"]]
    if vistas:
        n = len(vistas)
        print(f"\nMedia de {n} publicación(es) con datos: "
              f"vistas {sum(f['views'] for f in vistas)//n}, "
              f"alcance {sum(f['reach'] for f in vistas)//n}, "
              f"guardados {sum(f['saved'] for f in vistas)/n:.1f}")
        print("\nGuardados es la señal que más importa aquí: guardar una oferta significa "
              "'me interesa de verdad', y a Instagram le pesa más que un me gusta.")
    else:
        print("\nTodavía sin datos de alcance: los insights tardan un rato en aparecer "
              "después de publicar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
