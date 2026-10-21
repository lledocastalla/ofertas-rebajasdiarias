#!/usr/bin/env python3
"""
exportar_stats.py — deja las estadísticas de redes en el repo privado, para el agente semanal.

Por qué existe (21 oct 2026): el agente que revisa el rendimiento corre en la nube de
Anthropic y **no puede entrar en la Pi** — está en una IP de Tailscale, accesible solo desde
la red privada del usuario, y meterle la clave SSH al agente sería una mala idea. Pero sí
puede clonar un repositorio de GitHub. Así que la Pi, que es la única que tiene las
credenciales, deja aquí los números y el agente los lee de ahí.

Va al repo **privado** (`rebajasdiarias-web`), no al público, por decisión del usuario:
alcance y guardados son datos de negocio y no hay ninguna razón para enseñárselos a la
competencia. Los seguidores ya son públicos de todas formas.

Qué exporta a `datos/redes_stats.json`:
  - Instagram: perfil y, por publicación, alcance / guardados / visitas al perfil / likes /
    comentarios, más qué se publicó y POR QUÉ lo eligió el bot (del log), que es lo que
    permite cruzar rendimiento con criterio.
  - Facebook: publicaciones recientes con sus interacciones.
No interpreta nada: eso es trabajo del agente. Aquí solo se recogen los datos.

Si falta el permiso `instagram_manage_insights` el fichero se escribe igual, con el error
dentro, para que el agente pueda decir "no hay datos y este es el motivo" en vez de callar.

Uso:
    python3 exportar_stats.py            # exporta y hace commit+push si hay cambios
    python3 exportar_stats.py --sin-push # solo escribe el fichero (para probar)
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
REPO_WEB = HOME / "rebajasdiarias-web"
SALIDA = REPO_WEB / "datos" / "redes_stats.json"
CRED_IG = HOME / ".rebajas_instagram_credentials.json"
CRED_FB = HOME / ".rebajas_facebook_credentials.json"
LOG_BOT = HOME / "instagram_bot.log"
GRAPH = "https://graph.facebook.com/v21.0"
CUANTAS = 60


def log(m):
    print(f"[exportar_stats] {m}", flush=True)


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


def motivos_del_log():
    """Por qué eligió el bot cada oferta. Sin esto los números no sirven de mucho: saber que
    una publicación tuvo 400 de alcance no dice nada; saber que las que venían de una búsqueda
    de usuarios rinden el doble, sí."""
    motivos = []
    try:
        lineas = LOG_BOT.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return motivos
    actual = None
    for ln in lineas:
        m = re.search(r"elegida: (.+)$", ln)
        if m:
            actual = {"titulo": m.group(1).strip()}
            continue
        m = re.search(r"por qué: (.+)$", ln)
        if m and actual:
            actual["motivo"] = m.group(1).strip()
            continue
        m = re.search(r"imagen: ig/(\d{4}-\d{2}-\d{2})-([A-Z0-9]+)\.jpg", ln)
        if m and actual:
            actual.update({"fecha": m.group(1), "asin": m.group(2)})
            motivos.append(actual)
            actual = None
    return motivos[-CUANTAS:]


def instagram():
    if not CRED_IG.exists():
        return {"error": "sin credenciales"}
    c = json.loads(CRED_IG.read_text())
    tok, ig = c["access_token"], c["ig_user_id"]
    perfil = get(ig, {"fields": "username,followers_count,follows_count,media_count",
                      "access_token": tok})
    if perfil.get("_err"):
        return {"error": perfil["_err"]}

    medios = get(f"{ig}/media", {"fields": "id,caption,timestamp,permalink,media_type",
                                 "limit": CUANTAS, "access_token": tok}).get("data", [])
    publicaciones, fallo = [], None
    for m in medios:
        fila = {"id": m.get("id"), "fecha": m.get("timestamp"), "enlace": m.get("permalink"),
                "tipo": m.get("media_type"),
                "titulo": (m.get("caption") or "").split("\n")[0][:90]}
        # El agente tiene que poder distinguir "nadie lo ve" de "no podemos mirarlo", así que
        # un fallo de permiso se declara en el fichero en vez de exportarse como ceros.
        datos, sin_permiso = insights_de(m["id"], tok)
        fallo = fallo or sin_permiso
        fila.update(datos)
        publicaciones.append(fila)

    return {"perfil": {k: v for k, v in perfil.items() if k != "id"},
            "publicaciones": publicaciones,
            "error_insights": fallo,
            "limite_publicacion": get(f"{ig}/content_publishing_limit",
                                      {"access_token": tok}).get("data")}


def facebook():
    if not CRED_FB.exists():
        return {"error": "sin credenciales"}
    c = json.loads(CRED_FB.read_text())
    tok, pid = c["page_access_token"], c["page_id"]
    # Probado el 21 oct: `likes.summary(true)` y `comments.summary(true)` devuelven
    # "(#10) requires pages_read_engagement / pages_read_user_content" y tumban la consulta
    # ENTERA, dejándola en cero publicaciones. Así que se piden solo los campos que el token
    # puede leer hoy; los que faltan se declaran abajo en vez de exportarse como ceros, que
    # el agente leería como "nadie interactúa" cuando es "no podemos verlo".
    r = get(f"{pid}/posts", {"fields": "id,message,created_time,permalink_url,shares",
                             "limit": 40, "access_token": tok})
    if r.get("_err"):
        return {"error": r["_err"]}
    posts = []
    for p in r.get("data", []):
        posts.append({
            "fecha": p.get("created_time"), "enlace": p.get("permalink_url"),
            "titulo": (p.get("message") or "").split("\n")[0][:90],
            "compartidos": (p.get("shares") or {}).get("count", 0),
        })
    return {"publicaciones": posts,
            "no_disponible": "me gusta y comentarios: el token necesita "
                             "pages_read_engagement y pages_read_user_content"}


def main():
    datos = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "instagram": instagram(),
        "facebook": facebook(),
        "por_que_se_eligio": motivos_del_log(),
        "contexto": (
            "Lo genera la Pi porque es la única que tiene las credenciales; el agente que lo "
            "lee corre en la nube y no puede entrar en ella. 'por_que_se_eligio' viene del log "
            "del bot y permite cruzar el rendimiento con el criterio de selección. Desde el 21 "
            "oct 2026 se publican 6 veces al día en Instagram (antes 1): al comparar periodos, "
            "tenerlo en cuenta."
        ),
    }
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    ig = datos["instagram"]
    log(f"{SALIDA} — {len(ig.get('publicaciones', []))} publicaciones de Instagram, "
        f"{len(datos['facebook'].get('publicaciones', []))} de Facebook")
    if ig.get("error_insights"):
        log(f"aviso: sin insights ({ig['error_insights'][:70]})")

    if "--sin-push" in sys.argv:
        return 0
    try:
        # add -f porque datos/ podría estar ignorado; el fichero no lleva nada secreto.
        subprocess.run(["git", "add", "-f", str(SALIDA)], cwd=REPO_WEB, check=True,
                       capture_output=True)
        hay = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_WEB).returncode
        if hay == 0:
            log("sin cambios, no se hace commit")
            return 0
        subprocess.run(["git", "commit", "-q", "-m",
                        "Estadisticas de redes (automatico, para el agente semanal)"],
                       cwd=REPO_WEB, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=REPO_WEB, check=True, capture_output=True)
        log("subido al repo privado")
    except subprocess.CalledProcessError as e:
        log(f"ERROR al subir: {(e.stderr or b'').decode()[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
