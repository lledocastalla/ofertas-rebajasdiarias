#!/usr/bin/env python3
"""
instagram_formatos.py — publica REELS e HISTORIAS, además de la foto normal del bot.

Por qué existe (21 oct 2026). Publicar una foto al día es la base, pero no hace crecer una
cuenta: la foto la ve sobre todo quien ya te sigue. Lo que llega a gente nueva son los reels,
que Instagram reparte fuera de tus seguidores. Y las historias mantienen viva a la gente que
ya está.

QUÉ PUEDE Y QUÉ NO PUEDE HACER ESTO — léelo antes de esperar milagros:
  - Un reel publicado por API **no puede llevar música del catálogo de Instagram**. Eso es una
    limitación de Meta, no del código: la música solo se añade desde la app a mano. Un reel sin
    audio rinde peor que uno con un audio de moda. Se publica con una pista de silencio porque
    Instagram exige que el vídeo tenga audio.
  - **No se puede comentar ni seguir a otras cuentas** por API. Eso, que es lo que más mueve
    seguidores, sigue siendo trabajo manual. No hay forma legítima de automatizarlo.
Dicho eso, un reel diario de ofertas llega a bastante más gente que una foto, y eso sí se
puede hacer solo.

CÓMO SE PUBLICA UN REEL (es distinto de una foto): el contenedor tarda en procesarse, así que
hay que preguntar por su estado hasta que diga FINISHED antes de publicarlo. Si se publica
antes, Meta devuelve error.

EL CARRUSEL ES EL SUSTITUTO DEL VÍDEO (21 oct 2026). El usuario pidió "algún vídeo a la
semana en el muro" y no se puede: Meta responde "El tipo de contenido multimedia VÍDEO no es
compatible" (subcode 2207067) porque ya no existen los vídeos de muro -- todo vídeo tiene que
ser un reel, y los reels fallan al publicar. Un carrusel cubre la misma intención: una
publicación semanal más rica, que ocupa más tiempo de pantalla porque hay que deslizar, y que
en una cuenta de ofertas funciona bien porque deja ver cinco chollos de un tirón.

Uso:
    python3 instagram_formatos.py --reel [--dry-run]       # escrito, pero Meta lo rechaza
    python3 instagram_formatos.py --historia [--dry-run]
    python3 instagram_formatos.py --carrusel [--dry-run]
    python3 instagram_formatos.py --video     # genera el vídeo y lo deja accesible, sin publicar
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HOME = Path.home()
CREDENCIALES = HOME / ".rebajas_instagram_credentials.json"
ESTADO = HOME / ".rebajas_instagram_publicadas.json"
REPO_WEB = HOME / "rebajasdiarias-web"
CARPETA_IG = REPO_WEB / "ig"
BASE_PUBLICA = "https://rebajasdiarias.es/ig"
GRAPH = "https://graph.facebook.com/v21.0"

ANCHO, ALTO = 1080, 1920      # vertical, el formato de reels e historias
CUANTAS_REEL = 5              # ofertas por reel
CUANTAS_CARRUSEL = 6          # 1 portada + 5 ofertas; Instagram admite hasta 10
# Ritmo pensado para TikTok, no para Instagram (21 oct 2026). TikTok decide en el primer
# segundo si te sigue enseñando, así que el vídeo abre con un rótulo gancho y va más rápido:
# 2,4 s por oferta en vez de 3, y nada de empezar directamente con un producto.
SEGUNDOS_GANCHO = 1.6
SEGUNDOS_POR_OFERTA = 2.4
SEGUNDOS_CIERRE = 1.8

sys.path.insert(0, str(REPO_WEB / "scripts"))


def log(m):
    print(f"[instagram_formatos] {m}", flush=True)


def _leer(ruta):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except Exception:
        return []


def _post(url, datos):
    """Si Meta rechaza algo, el motivo viene en el CUERPO de la respuesta, no en el código
    HTTP. Sin leerlo solo se ve "HTTP Error 400: Bad Request", que no dice nada y obliga a
    adivinar. Se relanza con el mensaje de Meta dentro."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(datos).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            raise RuntimeError(f"{err.get('message','?')} "
                               f"(code {err.get('code')}, subcode {err.get('error_subcode')})")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f"HTTP {e.code} sin cuerpo legible")


def _get(url, params):
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=60) as r:
        return json.loads(r.read().decode())


# --- Elegir qué sale ------------------------------------------------------------------------
def elegir(cuantas):
    """Las mejores del día con el mismo criterio que la publicación normal, para que todo
    hable con la misma voz. Variedad por categoría: cinco ofertas de Moda Hombre seguidas en
    un reel de 15 segundos se nota todavía más que en el muro.

    **Arreglo del 22 oct 2026** (aviso del usuario: "siempre estás subiendo el mismo producto
    a la historia de Instagram"). Y era verdad: esto cogía el número 1 del ranking y no
    guardaba memoria de nada, así que las tres historias del día —11:00, 16:00 y 21:00— salían
    con el mismo producto, y al día siguiente otra vez el mismo mientras el catálogo no
    cambiara. En el log se ve: el adidas Adizero SL2 el 21 y el 22.

    La solución no es inventar otra memoria, sino usar LA MISMA que ya tiene el bot del muro
    (`~/.rebajas_instagram_publicadas.json`, las últimas 400 ofertas publicadas). Así se
    consiguen dos cosas de una vez: una historia nunca repite producto, y además nunca repite
    lo que ya salió en el muro, que era el otro duplicado posible y nadie había mirado.

    Si la memoria llegara a comerse el catálogo entero, se permite repetir antes que no
    publicar: más vale una repetición que una historia vacía.
    """
    from generate_category_pages import fetch_offers
    import instagram_tendencias as it
    import instagram_bot as ib

    candidatas = [o for o in fetch_offers() if (o.get("store") or "amazon") == "amazon"]
    ya = set(ib._leer(ib.ESTADO))
    ya_tit = set(ib._leer(ib.ESTADO_TITULOS))
    frescas = [o for o in candidatas
               if o.get("id") not in ya
               and ib.clave_titulo(o.get("title")) not in ya_tit]
    if len(frescas) >= cuantas:
        candidatas = frescas
    else:
        log(f"solo quedan {len(frescas)} ofertas sin publicar de {len(candidatas)}; "
            f"se permite repetir para no quedarse sin publicar")

    # temas_recientes hace que no caigan tres historias seguidas de la misma categoría, que
    # es lo mismo que ya hace el muro. Antes no se le pasaba y por eso no penalizaba nada.
    orden = it.ordenar(candidatas, ib.busquedas_de_la_gente(),
                       temas_recientes=list(reversed(ib._leer(ib.ESTADO_TEMAS))))
    # Variedad DENTRO de la misma tanda. La categoría no basta: el carrusel del 21 oct metió
    # el adidas Adizero y el adidas Runfalcon porque Amazon los clasifica en categorías
    # distintas, pero para quien lo ve son dos zapatillas adidas seguidas.
    elegidas, cats, claves = [], [], set()
    for o, _, _ in orden:
        c = o.get("category") or "?"
        k = ib.clave_titulo(o.get("title"))
        if c in cats or k in claves:
            continue
        elegidas.append(o)
        cats.append(c)
        claves.add(k)
        # Se cogen el doble de las necesarias porque la revalidación de precio va a tirar
        # algunas, y quedarse corto obligaría a una segunda llamada a la API.
        if len(elegidas) >= cuantas * 2:
            break

    import precios
    vigentes = precios.filtrar_vigentes(elegidas, limite=10)
    if vigentes is None:
        return []
    if len(vigentes) < cuantas:
        log(f"solo {len(vigentes)} de {len(elegidas)} siguen a su precio; "
            f"se publica con lo que hay")
    return vigentes[:cuantas]


# --- Componer los fotogramas ----------------------------------------------------------------
def _fuente(tam, negrita=True):
    from PIL import ImageFont
    for ruta in (("/System/Library/Fonts/Avenir Next.ttc", 0 if negrita else 7),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrita
                  else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None)):
        try:
            return (ImageFont.truetype(ruta[0], tam, index=ruta[1]) if ruta[1] is not None
                    else ImageFont.truetype(ruta[0], tam))
        except Exception:
            continue
    return ImageFont.load_default()


def componer_vertical(oferta, pie_extra=None):
    """Tarjeta 1080x1920 para reel o historia. Fondo oscuro a propósito: en el muro compite
    con fotos de producto sobre blanco, y en vertical a pantalla completa el contraste se
    agradece. La foto va arriba sobre un recuadro blanco porque las imágenes de Amazon vienen
    recortadas sobre blanco y sobre fondo oscuro quedarían con un marco feo."""
    from PIL import Image, ImageDraw
    import instagram_imagen as ii

    FONDO, BLANCO, GRIS, ROJO = (17, 24, 36), (255, 255, 255), (150, 160, 175), (214, 45, 45)
    lienzo = Image.new("RGB", (ANCHO, ALTO), FONDO)
    d = ImageDraw.Draw(lienzo)

    url = (oferta.get("image") or "").replace("_AC_UL320_", "_AC_UL1000_")
    try:
        foto = ii._descargar(url)
    except Exception:
        try:
            foto = ii._descargar(oferta.get("image"))
        except Exception:
            return None
    caja = foto.getbbox()
    if caja:
        foto = foto.crop(caja)
    zona = 860
    p = min(zona / foto.width, zona / foto.height)
    foto = foto.resize((max(1, int(foto.width * p)), max(1, int(foto.height * p))),
                       Image.LANCZOS)
    marco = Image.new("RGB", (zona + 60, zona + 60), BLANCO)
    marco.paste(foto.convert("RGB"), ((marco.width - foto.width) // 2,
                                      (marco.height - foto.height) // 2))
    lienzo.paste(marco, ((ANCHO - marco.width) // 2, 230))

    dto = oferta.get("discount_percent") or 0
    if dto:
        f = _fuente(104)
        t = f"-{dto}%"
        iz, ar, de, ab = d.textbbox((0, 0), t, font=f)
        d.rounded_rectangle([60, 70, 60 + (de - iz) + 70, 70 + (ab - ar) + 54],
                            radius=22, fill=ROJO)
        d.text((60 + 35 - iz, 70 + 27 - ar), t, font=f, fill=BLANCO)

    y = 230 + marco.height + 70
    f_t = _fuente(52, negrita=False)
    lineas, actual = [], ""
    for w in (oferta.get("title") or "").split():
        if d.textlength((actual + " " + w).strip(), font=f_t) > ANCHO - 140:
            lineas.append(actual)
            actual = w
            if len(lineas) == 2:
                break
        else:
            actual = (actual + " " + w).strip()
    if len(lineas) < 2 and actual:
        lineas.append(actual)
    for ln in lineas[:2]:
        iz, ar, de, ab = d.textbbox((0, 0), ln, font=f_t)
        d.text(((ANCHO - (de - iz)) / 2 - iz, y), ln, font=f_t, fill=BLANCO)
        y += 66

    f_p, f_a = _fuente(140), _fuente(60, negrita=False)
    ahora, antes = ii.precio_txt(oferta.get("price") or 0), ii.precio_txt(
        oferta.get("original_price") or 0)
    an = d.textlength(ahora, font=f_p)
    d.text(((ANCHO - an) / 2, y + 40), ahora, font=f_p, fill=BLANCO)
    if (oferta.get("original_price") or 0) > (oferta.get("price") or 0):
        aa = d.textlength(antes, font=f_a)
        xa, ya = (ANCHO - aa) / 2, y + 210
        d.text((xa, ya), antes, font=f_a, fill=GRIS)
        d.line([xa - 6, ya + 38, xa + aa + 6, ya + 38], fill=GRIS, width=4)

    pie = pie_extra or "descuento real verificado · rebajasdiarias.es"
    f_c = _fuente(40, negrita=False)
    d.text(((ANCHO - d.textlength(pie, font=f_c)) / 2, ALTO - 110), pie, font=f_c, fill=GRIS)
    return lienzo


def portada_carrusel(cuantas):
    """La primera imagen decide si alguien desliza o pasa de largo, así que no es una oferta:
    es un titular que dice qué va a encontrar y cuántas cosas hay."""
    from PIL import Image, ImageDraw
    import instagram_imagen as ii

    LADO = 1080
    FONDO, BLANCO, GRIS, ROJO = (17, 24, 36), (255, 255, 255), (150, 160, 175), (214, 45, 45)
    lienzo = Image.new("RGB", (LADO, LADO), FONDO)
    d = ImageDraw.Draw(lienzo)
    # El tamaño se ajusta hasta que quepa. Con un tamaño fijo "MEJORES CHOLLOS" se salía por
    # los dos lados: DejaVu (la fuente de la Pi) es bastante más ancha que Avenir (la del
    # Mac), así que lo que cuadra al probar en el Mac se desborda en producción.
    MARGEN = 70
    for txt, tam, y, color in ((f"LOS {cuantas}", 150, 210, BLANCO),
                               ("MEJORES CHOLLOS", 104, 380, BLANCO),
                               ("DE LA SEMANA", 104, 500, ROJO)):
        f = _fuente(tam)
        while tam > 40 and d.textlength(txt, font=f) > LADO - 2 * MARGEN:
            tam -= 4
            f = _fuente(tam)
        d.text(((LADO - d.textlength(txt, font=f)) / 2, y), txt, font=f, fill=color)
    for txt, y in (("Descuentos comprobados contra el precio anterior.", 700),
                   ("Si está inflado, no entra.", 752)):
        tam = 40
        f = _fuente(tam, negrita=False)
        while tam > 24 and d.textlength(txt, font=f) > LADO - 2 * MARGEN:
            tam -= 2
            f = _fuente(tam, negrita=False)
        d.text(((LADO - d.textlength(txt, font=f)) / 2, y), txt, font=f, fill=GRIS)
    f = _fuente(44)
    d.text(((LADO - d.textlength("Desliza →", font=f)) / 2, 860), "Desliza →", font=f,
           fill=BLANCO)
    try:
        logo = Image.open(ii.LOGO).convert("RGBA")
        c = logo.getbbox()
        if c:
            logo = logo.crop(c)
        logo = logo.resize((int(logo.width * 70 / logo.height), 70), Image.LANCZOS)
        lienzo.paste(logo, ((LADO - logo.width) // 2, LADO - 150), logo)
    except Exception:
        pass
    fc = _fuente(32, negrita=False)
    d.text(((LADO - d.textlength("rebajasdiarias.es", font=fc)) / 2, LADO - 62),
           "rebajasdiarias.es", font=fc, fill=GRIS)
    return lienzo


def pie_carrusel(ofertas):
    import instagram_imagen as ii
    l = [ii_cabecera() + "\n",
         f"Los {len(ofertas)} mejores chollos de la semana 🔥 Desliza para verlos todos.\n"]
    for o in ofertas:
        l.append(f"• {(o.get('title') or '')[:55]} — {ii.precio_txt(o.get('price'))} "
                 f"(antes {ii.precio_txt(o.get('original_price'))}, -{o.get('discount_percent')}%)")
    l.append("\n🔎 ¿Buscabas otra cosa? En la app escribes lo que quieras —una marca, un "
             "modelo, tu talla— y te salen los chollos que hay de eso ahora mismo.")
    l.append("\nComprobamos el precio que tenía antes: si el descuento está sobre un precio "
             "inflado, no lo publicamos.")
    l.append("\n#chollos #ofertas #descuentos #amazon #ahorro #rebajas #chollazo #ofertasamazon")
    return "\n".join(l)


def _anotar_publicadas(ofertas, tema):
    """Apunta lo que acaba de salir en la MISMA memoria que usa instagram_bot.py.

    Sin esto el filtro de elegir() no sirve de nada: si nadie escribe, nunca hay nada que
    excluir. Se llama solo después de que Meta confirme la publicación -- anotar algo que al
    final no se publicó sería peor que no anotar, porque esa oferta quedaría quemada sin
    haberse usado."""
    import instagram_bot as ib
    for o in ofertas:
        if o.get("id"):
            ib._recordar(ib.ESTADO, o["id"])
            ib._recordar(ib.ESTADO_TITULOS, ib.clave_titulo(o.get("title")), tope=120)
    ib._recordar(ib.ESTADO_TEMAS, f"{tema}|{ofertas[0].get('category') or ''}", tope=40)


def publicar_carrusel(ofertas, tok, ig, seco):
    """Son TRES pasos: una llamada por imagen con is_carousel_item, otra para el contenedor
    padre con la lista de hijos, y la publicación. Todas las imágenes deben tener la misma
    proporción, por eso se usa la tarjeta cuadrada de siempre."""
    import instagram_imagen as ii

    CARPETA_IG.mkdir(exist_ok=True)
    hoy = date.today().isoformat()
    ficheros = []
    portada = portada_carrusel(len(ofertas))
    f0 = CARPETA_IG / f"carrusel-{hoy}-00.jpg"
    portada.save(f0, "JPEG", quality=92, optimize=True)
    ficheros.append(f0)
    for i, o in enumerate(ofertas, 1):
        img = ii.componer(o)
        if img is None:
            log(f"  sin imagen: {(o.get('title') or '')[:40]}")
            continue
        f = CARPETA_IG / f"carrusel-{hoy}-{i:02d}.jpg"
        img.save(f, "JPEG", quality=92, optimize=True)
        ficheros.append(f)
    if len(ficheros) < 3:
        log("no hay imágenes suficientes para un carrusel")
        return 1
    log(f"{len(ficheros)} imágenes (portada + {len(ficheros)-1} ofertas)")

    pie = pie_carrusel(ofertas)
    if seco:
        log("en seco, no se publica. Pie de foto:")
        print("\n" + pie + "\n")
        return 0

    if not desplegar():
        return 1
    try:
        hijos = []
        for f in ficheros:
            r = _post(f"{GRAPH}/{ig}/media", {
                "image_url": f"{BASE_PUBLICA}/{f.name}",
                "is_carousel_item": "true", "access_token": tok})
            hijos.append(r["id"])
        padre = _post(f"{GRAPH}/{ig}/media", {
            "media_type": "CAROUSEL", "children": ",".join(hijos),
            "caption": pie, "access_token": tok})
        pub = _post(f"{GRAPH}/{ig}/media_publish",
                    {"creation_id": padre["id"], "access_token": tok})
    except Exception as e:
        log(f"ERROR publicando el carrusel: {e}")
        return 1
    log(f"publicado: {pub}")
    _anotar_publicadas(ofertas, "carrusel")
    return 0


def _rotulo(lineas, sub=None):
    """Una tarjeta de solo texto, para el gancho del principio y el cierre del final."""
    from PIL import Image, ImageDraw
    FONDO, BLANCO, ROJO, GRIS = (17, 24, 36), (255, 255, 255), (214, 45, 45), (150, 160, 175)
    img = Image.new("RGB", (ANCHO, ALTO), FONDO)
    d = ImageDraw.Draw(img)
    y = (ALTO - len(lineas) * 190) // 2 - 60
    for txt, color in lineas:
        tam = 150
        f = _fuente(tam)
        while tam > 50 and d.textlength(txt, font=f) > ANCHO - 120:
            tam -= 6
            f = _fuente(tam)
        d.text(((ANCHO - d.textlength(txt, font=f)) / 2, y), txt, font=f, fill=color)
        y += int(tam * 1.25)
    if sub:
        tam = 54
        f = _fuente(tam, negrita=False)
        while tam > 30 and d.textlength(sub, font=f) > ANCHO - 140:
            tam -= 3
            f = _fuente(tam, negrita=False)
        d.text(((ANCHO - d.textlength(sub, font=f)) / 2, y + 60), sub, font=f, fill=GRIS)
    return img


def montar_reel(ofertas, destino):
    """Fotogramas + ffmpeg. Lleva un zoom lento: un pase de imágenes quietas parece lo que es,
    y Instagram reparte peor lo que no tiene movimiento. Y una pista de silencio porque
    Instagram rechaza los vídeos sin audio."""
    tmp = Path("/tmp/reel_frames")
    subprocess.run(["rm", "-rf", str(tmp)], check=False)
    tmp.mkdir(parents=True, exist_ok=True)

    BLANCO, ROJO = (255, 255, 255), (214, 45, 45)
    # El gancho va el primero y es solo texto: TikTok decide en el primer segundo, y abrir
    # con la foto de un producto se lee como anuncio y se pasa de largo.
    _rotulo([(f"{len(ofertas)} CHOLLOS", BLANCO), ("DE HOY", ROJO)],
            "con el precio de antes a la vista").save(tmp / "00_gancho.jpg", "JPEG", quality=92)
    duraciones = [SEGUNDOS_GANCHO]
    for i, o in enumerate(ofertas, 1):
        img = componer_vertical(o)
        if img is None:
            log(f"  sin imagen descargable: {o.get('title','')[:40]}")
            continue
        img.save(tmp / f"{i:02d}.jpg", "JPEG", quality=92)
        duraciones.append(SEGUNDOS_POR_OFERTA)
    # Y el cierre dice qué hacer. Sin esto el vídeo se acaba y no pasa nada.
    _rotulo([("BUSCA", BLANCO), ("LO TUYO", ROJO)],
            "en la app Rebajas Diarias").save(tmp / "99_cierre.jpg", "JPEG", quality=92)
    duraciones.append(SEGUNDOS_CIERRE)

    marcos = sorted(tmp.glob("*.jpg"))
    if len(marcos) < 4:
        log("no hay suficientes imágenes para el vídeo")
        return False

    lista = tmp / "lista.txt"
    lista.write_text("".join(f"file '{m}'\nduration {d}\n"
                             for m, d in zip(marcos, duraciones))
                     + f"file '{marcos[-1]}'\n", encoding="utf-8")
    total = round(sum(duraciones), 2)
    r = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(lista),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        # zoompan da el zoom lento; fps 30 porque a 25 Instagram recomprime y se nota.
        # Zoom algo más vivo que antes (0.0014 frente a 0.0008): en TikTok el movimiento
        # lento se lee como imagen fija y la gente desliza.
        "-vf", (f"scale={ANCHO*2}:-2,zoompan=z='min(zoom+0.0014,1.15)':"
                f"d={int(SEGUNDOS_POR_OFERTA*30)}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={ANCHO}x{ALTO}:fps=30,format=yuv420p"),
        # Ajustes pegados a lo que pide Meta para reels. El primer intento (crf 23, aac 96k,
        # GOP por defecto) daba "Fatal, subcode 2207085" al publicar AUNQUE el contenedor
        # decía FINISHED: Meta acepta el vídeo, lo procesa y lo rechaza al final. Por eso:
        #   - perfil high y nivel 4.0, que es lo que documentan;
        #   - GOP cerrado cada 2 s (-g 60 -sc_threshold 0): lo exigen explícitamente;
        #   - audio a 48 kHz estéreo 128 kbps, su recomendación exacta;
        #   - -r 30 fijo, para que el concat no deje una base de tiempos rara.
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-preset", "medium", "-crf", "21", "-r", "30",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-shortest", "-t", str(total), "-movflags", "+faststart", str(destino),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR de ffmpeg: {(r.stderr or '')[-300:]}")
        return False
    log(f"vídeo listo: {destino.name} ({destino.stat().st_size // 1024} KB, {total}s)")
    return True


# --- Publicar -------------------------------------------------------------------------------
def desplegar():
    r = subprocess.run([sys.executable, str(REPO_WEB / "scripts" / "refresh_and_deploy.py")],
                       capture_output=True, text=True, cwd=str(REPO_WEB))
    if r.returncode != 0:
        log(f"ERROR desplegando: {(r.stderr or r.stdout or '')[-300:]}")
        return False
    return True


def esperar_contenedor(cid, tok, intentos=30):
    """Un reel no está listo nada más crear el contenedor: Meta lo procesa. Publicar antes de
    tiempo devuelve error, así que se pregunta por el estado hasta que diga FINISHED."""
    for i in range(intentos):
        time.sleep(8)
        try:
            est = _get(f"{GRAPH}/{cid}", {"fields": "status_code", "access_token": tok})
        except Exception as e:
            log(f"  aviso consultando estado: {e}")
            continue
        c = est.get("status_code")
        if c == "FINISHED":
            return True
        if c == "ERROR":
            log(f"  Meta no pudo procesar el vídeo: {est}")
            return False
        log(f"  procesando… ({c}, intento {i+1})")
    log("  se agotó la espera procesando el vídeo")
    return False


def ii_cabecera():
    import instagram_imagen as ii
    return ii._cabecera()


def pie_reel(ofertas):
    # Enlace en la primera línea: Instagram corta el pie a las ~125 letras y lo de abajo no
    # se lee. Lo pidió el usuario el 21 oct y tiene razón.
    # Sin dirección mientras dure la restricción de Instagram (ver SIN_ENLACES en
    # instagram_imagen.py). La frase no es un enlace; la URL en texto sí lo parecía.
    lineas = [ii_cabecera() + "\n",
              f"🔥 {len(ofertas)} chollos de hoy con descuento real\n"]
    for o in ofertas:
        import instagram_imagen as ii
        lineas.append(f"• {(o.get('title') or '')[:52]} — {ii.precio_txt(o.get('price'))} "
                      f"(-{o.get('discount_percent')}%)")
    lineas.append("\n🔎 ¿Buscabas otra cosa? En la app escribes lo que quieras —una marca, un "
                  "modelo, tu talla— y te salen los chollos que hay de eso ahora mismo.")
    lineas.append("\nComprobamos el precio que tenía antes: si el descuento está sobre un "
                  "precio inflado, no lo publicamos.")
    lineas.append("\n#chollos #ofertas #descuentos #amazon #ahorro #rebajas #chollazo #ofertasamazon")
    return "\n".join(lineas)


def main():
    seco = "--dry-run" in sys.argv
    es_video = "--video" in sys.argv
    es_reel = "--reel" in sys.argv or es_video
    es_carrusel = "--carrusel" in sys.argv
    if not es_reel and not es_carrusel and "--historia" not in sys.argv:
        print(__doc__)
        return 1

    cuantas = CUANTAS_CARRUSEL if es_carrusel else (CUANTAS_REEL if es_reel else 1)
    ofertas = elegir(cuantas)
    if not ofertas:
        log("no hay ofertas que cumplan los criterios hoy")
        return 0
    for o in ofertas:
        log(f"  {o.get('category')}: {(o.get('title') or '')[:46]} "
            f"-{o.get('discount_percent')}%")

    if es_carrusel:
        if not CREDENCIALES.exists() and not seco:
            log(f"Falta {CREDENCIALES}")
            return 1
        creds = json.loads(CREDENCIALES.read_text()) if CREDENCIALES.exists() else {}
        return publicar_carrusel(ofertas, creds.get("access_token"),
                                 creds.get("ig_user_id"), seco)

    CARPETA_IG.mkdir(exist_ok=True)
    hoy = date.today().isoformat()
    if es_reel:
        fichero = CARPETA_IG / f"reel-{hoy}.mp4"
        if not montar_reel(ofertas, fichero):
            return 1
        pie = pie_reel(ofertas)
    else:
        img = componer_vertical(ofertas[0], pie_extra="Enlace en la biografía 👆")
        if img is None:
            log("no se pudo componer la historia")
            return 1
        fichero = CARPETA_IG / f"historia-{hoy}.jpg"
        img.save(fichero, "JPEG", quality=92, optimize=True)
        pie = None
        log(f"imagen de historia lista: {fichero.name}")

    if es_video:
        # Solo generar y dejarlo accesible. Para TikTok (21 oct 2026): Instagram rechaza
        # publicar vídeo por API, y la API de TikTok sin auditar tampoco publica sola. Pero
        # el vídeo SÍ se genera bien, así que se deja en una dirección fija y el usuario lo
        # sube desde el móvil en medio minuto. De paso gana lo que ninguna API puede darle:
        # añadir un audio de moda, que en TikTok es la mitad del alcance.
        if not desplegar():
            return 1
        log(f"listo para descargar: {BASE_PUBLICA}/{fichero.name}")
        print("\n--- TEXTO PARA TIKTOK ---\n" + pie + "\n")
        return 0

    if seco:
        log("en seco, no se publica.")
        if pie:
            print("\n" + pie + "\n")
        return 0

    if not CREDENCIALES.exists():
        log(f"Falta {CREDENCIALES}")
        return 1
    creds = json.loads(CREDENCIALES.read_text())
    tok, ig = creds["access_token"], creds["ig_user_id"]

    if not desplegar():
        return 1
    url = f"{BASE_PUBLICA}/{fichero.name}"

    try:
        if es_reel:
            cont = _post(f"{GRAPH}/{ig}/media", {
                "media_type": "REELS", "video_url": url, "caption": pie,
                "share_to_feed": "true", "access_token": tok})
        else:
            cont = _post(f"{GRAPH}/{ig}/media", {
                "media_type": "STORIES", "image_url": url, "access_token": tok})
        cid = cont.get("id")
        if not cid:
            log(f"Meta no devolvió contenedor: {cont}")
            return 1
        if es_reel and not esperar_contenedor(cid, tok):
            return 1
        pub = _post(f"{GRAPH}/{ig}/media_publish",
                    {"creation_id": cid, "access_token": tok})
    except Exception as e:
        log(f"ERROR publicando: {e}")
        return 1

    log(f"publicado: {pub}")
    _anotar_publicadas(ofertas, "reel" if es_reel else "historia")
    return 0


if __name__ == "__main__":
    sys.exit(main())
