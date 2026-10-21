#!/usr/bin/env python3
"""
instagram_bot.py — lleva el Instagram solo: publica una oferta al día y responde comentarios.

Por qué existe (20 oct 2026, pedido del usuario: "llevar el instagram automatizado", "que
trabaje publicando y comentando según tendencias"). Hermano de facebook_bot.py: misma Graph
API de Meta, mismo patrón de credenciales fuera del repo, mismo fichero de estado.

CÓMO SE PUBLICA EN INSTAGRAM (no es como en Facebook, son DOS llamadas):
  1. POST /{ig_user_id}/media          con image_url + caption  -> devuelve un creation_id
  2. POST /{ig_user_id}/media_publish  con ese creation_id       -> publica de verdad
La imagen NO se sube como fichero: Instagram la descarga de una URL pública. Por eso el script
deja el JPG dentro del repo de la web y lo despliega antes de llamar a la API -- si la imagen
no es accesible desde fuera, Meta devuelve error y no publica.

QUÉ SE PUBLICA: lo decide instagram_tendencias.py cruzando el catálogo con lo que buscan
nuestros propios usuarios (search_stats) y con la temporada del año. Ver allí el porqué.

SOBRE COMENTAR (importante, limita lo que se puede automatizar): la API de Instagram solo
permite comentar y responder en TUS PROPIAS publicaciones. No existe ningún endpoint para
comentar en posts de otras cuentas; eso solo se consigue simulando un navegador, que va contra
los términos de Meta y termina con la cuenta restringida. Así que aquí se responde a quien nos
comenta, que además es lo que de verdad retiene seguidores.

Y no responde a todo: solo a lo que puede contestar con la verdad (precio, disponibilidad,
dónde está el enlace, agradecer un elogio). Cualquier queja, duda rara o cosa que no entienda
la deja anotada para que la contestes tú. Un bot que responde a todo con la misma frase se nota
a la legua y hace más daño que no responder.

REQUISITOS QUE NO DEPENDEN DEL CÓDIGO (20 oct 2026: NINGUNO está puesto todavía):
  - Cuenta de Instagram de EMPRESA o CREADOR (una personal no sirve).
  - Vinculada a la página de Facebook que ya existe.
  - Token con permisos instagram_basic, instagram_content_publish y
    instagram_manage_comments (este último para responder).
  - El ig_user_id.
Mientras falten, el script sale avisando y no rompe nada. Pasos en
~/Desktop/INSTAGRAM_PUESTA_EN_MARCHA.md (en el Mac).

Credenciales (fichero a mano, permisos 600, NUNCA en git):
    ~/.rebajas_instagram_credentials.json
    {"ig_user_id": "...", "access_token": "..."}

Uso:
    python3 instagram_bot.py                # publica la oferta del día
    python3 instagram_bot.py --dry-run      # enseña qué publicaría y por qué, sin publicar
    python3 instagram_bot.py --responder    # responde los comentarios nuevos
    python3 instagram_bot.py --responder --dry-run
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

HOME = Path.home()
CREDENCIALES = HOME / ".rebajas_instagram_credentials.json"
ESTADO = HOME / ".rebajas_instagram_publicadas.json"
ESTADO_COM = HOME / ".rebajas_instagram_comentarios.json"
ESTADO_TEMAS = HOME / ".rebajas_instagram_temas.json"
PARA_TI = HOME / "instagram_para_contestar.txt"
REPO_WEB = HOME / "rebajasdiarias-web"
CARPETA_IG = REPO_WEB / "ig"
BASE_PUBLICA = "https://rebajasdiarias.es/ig"
GRAPH = "https://graph.facebook.com/v21.0"
MAX_RECORDADAS = 400
# Cuántos términos de búsqueda se traen de Firestore. Son lecturas, y la cuota del plan Spark
# es de 50.000 al día compartidas con toda la app, así que ni uno de más.
TOP_BUSQUEDAS = 300

sys.path.insert(0, str(REPO_WEB / "scripts"))


def log(msg):
    print(f"[instagram_bot] {msg}", flush=True)


def _leer(ruta):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except Exception:
        return []


def _recordar(ruta, valor, tope=MAX_RECORDADAS):
    hechas = list(_leer(ruta))
    hechas.append(valor)
    ruta.write_text(json.dumps(hechas[-tope:]), encoding="utf-8")


def _creds():
    if not CREDENCIALES.exists():
        return None
    return json.loads(CREDENCIALES.read_text(encoding="utf-8"))


def _get(url, params):
    u = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(u, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, datos):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(datos).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


# --- Publicar -------------------------------------------------------------------------------
def busquedas_de_la_gente():
    """Lo que escriben nuestros usuarios en el buscador. Si Firestore falla, se sigue sin ello:
    es un empuje para elegir mejor, no un requisito para publicar."""
    try:
        from update_offers import FIREBASE_CREDENTIALS_PATH
        import firebase_admin
        from firebase_admin import credentials, firestore
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(credentials.Certificate(FIREBASE_CREDENTIALS_PATH))
        db = firestore.client()
        docs = db.collection("search_stats").limit(TOP_BUSQUEDAS).stream()
        return {d.id: (d.to_dict() or {}).get("count") or 0 for d in docs}
    except Exception as e:
        log(f"aviso: sin datos de búsquedas ({e}); se elige solo por ahorro y temporada")
        return {}


def elegir_oferta():
    """La mejor del día según tendencias. Filtros de credibilidad antes de puntuar: sin ellos
    salen cosas de 3 € al -80% que parecen cebo."""
    from generate_category_pages import fetch_offers
    import instagram_tendencias as it

    # Los filtros de credibilidad los aplica it.ordenar(): tienen que ir DESPUÉS de cruzar con
    # las tendencias, no antes. Ver el comentario en instagram_tendencias.py.
    ya = set(_leer(ESTADO))
    candidatas = [o for o in fetch_offers()
                  if (o.get("store") or "amazon") == "amazon" and o.get("id") not in ya]
    if not candidatas:
        return None, ""

    nombre_temp, _ = it.temporada()
    if nombre_temp:
        log(f"temporada en curso: {nombre_temp}")
    # Los temas de los últimos días se pasan para que no salgan tres adidas seguidas: el
    # término más buscado gana siempre si no se le pone freno.
    orden = it.ordenar(candidatas, busquedas_de_la_gente(),
                       temas_recientes=list(reversed(_leer(ESTADO_TEMAS))))
    if not orden:
        return None, ""
    oferta, _, motivo = orden[0]
    return oferta, motivo


def generar(oferta, motivo=""):
    """Compone la imagen con el mismo módulo que usa todo lo demás, para que no haya dos
    estilos distintos conviviendo."""
    import instagram_imagen as ii
    import instagram_angulos as ia

    # El ángulo decide de QUÉ va esta publicación ("te ahorras 52 €", "lo más buscado"...).
    # Sin esto las 6 del día tienen la misma forma y el seguidor las reconoce y pasa de largo.
    # La semilla es la hora, para que dos publicaciones del mismo día no caigan en el mismo.
    _, rotulo, linea = ia.elegir_angulo(
        oferta,
        viene_de_busqueda="buscado" in (motivo or ""),
        semilla=datetime.now().strftime("%H"))

    img = ii.componer(oferta, rotulo=rotulo or None)
    if img is None:
        return None, None
    CARPETA_IG.mkdir(exist_ok=True)
    nombre = f"{date.today().isoformat()}-{oferta['id']}.jpg"
    img.save(CARPETA_IG / nombre, "JPEG", quality=90, optimize=True)
    return nombre, ii.pie_de_foto(oferta, primera_linea=linea or None)


def regenerar_landing():
    """Rehace /ig/, la página a la que apunta el enlace de la biografía (21 oct 2026, lo
    detectó el usuario: "¿no has creado enlace del producto?"). Instagram no permite enlaces
    en el pie, así que el único camino desde una publicación hasta el producto es esa página.
    Si no se regenera aquí, enseña la oferta de ayer y el recién llegado no encuentra lo que
    acaba de ver."""
    r = subprocess.run([sys.executable, str(REPO_WEB / "scripts" / "generate_ig_landing.py")],
                       capture_output=True, text=True, cwd=str(REPO_WEB))
    if r.returncode != 0:
        log("aviso: no se pudo regenerar /ig/; se publica igual")
        log((r.stderr or r.stdout or "")[-300:])
    else:
        log((r.stdout or "").strip()[:120])


def desplegar():
    """Instagram descarga la imagen de una URL pública, así que hay que publicarla antes. Se
    reutiliza el despliegue que ya existe para la web."""
    r = subprocess.run([sys.executable, str(REPO_WEB / "scripts" / "refresh_and_deploy.py")],
                       capture_output=True, text=True, cwd=str(REPO_WEB))
    if r.returncode != 0:
        log("ERROR desplegando la imagen; no se publica")
        log((r.stderr or r.stdout or "")[-400:])
        return False
    return True


def it_tema(motivo):
    import instagram_tendencias as it
    return it.tema_de(motivo)


def publicar(seco):
    oferta, motivo = elegir_oferta()
    if not oferta:
        log("no hay ninguna oferta nueva que cumpla los criterios hoy")
        return 0

    nombre, pie = generar(oferta, motivo)
    if not nombre:
        log(f"no se pudo componer la imagen de {oferta['id']}")
        return 1
    log(f"elegida: {oferta['title'][:60]}")
    log(f"  por qué: {motivo or 'la que más euros ahorra hoy'}")
    log(f"  imagen: ig/{nombre}")

    if seco:
        log("en seco, no se publica. Pie de foto:")
        print("\n" + pie + "\n")
        return 0

    creds = _creds()
    if not creds:
        log(f"Falta {CREDENCIALES}. La imagen queda en {CARPETA_IG / nombre} para subirla a "
            f"mano. Pasos para automatizarlo: ~/Desktop/INSTAGRAM_PUESTA_EN_MARCHA.md")
        return 1

    # Se apunta como publicada ANTES de desplegar para que la página /ig/ la enseñe la
    # primera, y la página se genera antes del despliegue para que suba junto con la imagen.
    _recordar(ESTADO, oferta["id"])
    regenerar_landing()
    if not desplegar():
        return 1

    try:
        cont = _post(f"{GRAPH}/{creds['ig_user_id']}/media", {
            "image_url": f"{BASE_PUBLICA}/{nombre}",
            "caption": pie,
            "access_token": creds["access_token"],
        })
        creation_id = cont.get("id")
        if not creation_id:
            log(f"Meta no devolvió creation_id: {cont}")
            return 1
        pub = _post(f"{GRAPH}/{creds['ig_user_id']}/media_publish", {
            "creation_id": creation_id,
            "access_token": creds["access_token"],
        })
    except Exception as e:
        log(f"ERROR publicando: {e}")
        return 1

    # Se apunta "tema|categoría": desde que se publica 6 veces al día, la variedad hay que
    # cuidarla también por categoría (seis ofertas de Moda Hombre el mismo día cansan igual
    # que seis adidas, aunque ninguna repita término de búsqueda).
    _recordar(ESTADO_TEMAS, f"{it_tema(motivo)}|{oferta.get('category') or ''}", tope=40)
    log(f"publicado: {pub}")
    return 0


# --- Responder comentarios ------------------------------------------------------------------
# Solo se responde a lo que se puede contestar con la verdad. El resto se anota para el usuario.
PRECIO = re.compile(r"\b(precio|cuanto|cuánto|cuesta|vale|euros?|€)\b", re.I)
ENLACE = re.compile(r"\b(enlace|link|donde|dónde|url|comprar|compro|web|app)\b", re.I)
DISPONIBLE = re.compile(r"\b(disponible|queda|quedan|stock|caducad|sigue|activa|vigente)\b", re.I)
ELOGIO = re.compile(r"\b(gracias|genial|buen[oa]|crack|top|brutal|chulo|guapo|vaya|gran)\b|"
                    r"[\U0001F300-\U0001FAFF]", re.I)
# Nada de responder a esto en automático: una queja contestada por un bot enfada más.
QUEJA = re.compile(r"\b(estaf|enga|fake|falso|mentira|timo|spam|basura|subid|inflad|denunci|"
                   r"pesim|malo|peor|no funciona|no me|reclam|devoluc)\w*", re.I)


def redactar_respuesta(texto, n):
    """Devuelve la respuesta, o None si hay que dejarlo para el usuario. `n` es el índice del
    comentario, solo para ir alternando la frase: repetir la misma palabra por palabra en todos
    los comentarios es lo que delata a un bot."""
    if QUEJA.search(texto):
        return None
    if DISPONIBLE.search(texto):
        return ["Comprueba el precio en el enlace antes de comprar: cambian a lo largo del día "
                "y lo actualizamos varias veces, pero Amazon va a su aire 🙂",
                "Los precios se mueven durante el día. Míralo en el enlace, que es el que está "
                "al día 🙂"][n % 2]
    if PRECIO.search(texto) or ENLACE.search(texto):
        return ["Lo tienes en el enlace de la biografía, con el precio de ahora mismo 👆",
                "En el enlace de la bio está, con el precio actualizado 👆"][n % 2]
    if ELOGIO.search(texto):
        return ["¡Gracias! 🙌", "Gracias a ti 🙌", "¡Me alegro! 🙌"][n % 3]
    return None


def responder(seco):
    creds = _creds()
    if not creds:
        log(f"Falta {CREDENCIALES}; no se pueden leer comentarios. Pasos: "
            f"~/Desktop/INSTAGRAM_PUESTA_EN_MARCHA.md")
        return 1

    tok = creds["access_token"]
    try:
        medios = _get(f"{GRAPH}/{creds['ig_user_id']}/media",
                      {"fields": "id,caption", "limit": 10, "access_token": tok}).get("data", [])
    except Exception as e:
        log(f"ERROR leyendo publicaciones: {e}")
        return 1

    hechos = set(_leer(ESTADO_COM))
    respondidos, pendientes = 0, []

    for m in medios:
        try:
            coms = _get(f"{GRAPH}/{m['id']}/comments",
                        {"fields": "id,text,username", "limit": 50,
                         "access_token": tok}).get("data", [])
        except Exception as e:
            log(f"aviso: no se pudieron leer comentarios de {m['id']} ({e})")
            continue

        for i, c in enumerate(coms):
            if c["id"] in hechos:
                continue
            texto = c.get("text", "")
            resp = redactar_respuesta(texto, i)
            if not resp:
                pendientes.append(f"@{c.get('username','?')}: {texto}")
                continue
            if seco:
                log(f"  respondería a @{c.get('username','?')}: {texto[:50]!r} -> {resp!r}")
            else:
                try:
                    _post(f"{GRAPH}/{c['id']}/replies", {"message": resp, "access_token": tok})
                except Exception as e:
                    log(f"ERROR respondiendo a {c['id']}: {e}")
                    continue
                _recordar(ESTADO_COM, c["id"], tope=2000)
            respondidos += 1

    if pendientes:
        PARA_TI.write_text(
            "Comentarios que NO he contestado porque hay que mirarlos (quejas, dudas raras o "
            "cosas que no entiendo). Contéstalos tú desde la app de Instagram:\n\n"
            + "\n".join(f"  - {p}" for p in pendientes) + "\n", encoding="utf-8")
        log(f"{len(pendientes)} comentarios para ti en {PARA_TI}")
    log(f"{respondidos} respondidos{' (en seco)' if seco else ''}")
    return 0


def main():
    seco = "--dry-run" in sys.argv
    if "--responder" in sys.argv:
        return responder(seco)
    return publicar(seco)


if __name__ == "__main__":
    sys.exit(main())
