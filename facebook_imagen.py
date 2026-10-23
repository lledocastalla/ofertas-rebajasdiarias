#!/usr/bin/env python3
"""
facebook_imagen.py — tarjeta de producto para Facebook, en vertical 4:5.

Por qué existe (23 oct 2026, el usuario: "en facebook puedes hacer un diseño chulo de imagen,
en insta está bien pero facebook no vale nada"). Y tenía razón: `facebook_bot.py` publicaba la
**foto cruda de Amazon** tal cual, sin marca, sin precio y sin descuento, mientras Instagram
lleva tarjeta compuesta desde el principio.

Formato 1080x1350 (4:5) y no 1080x1080 a propósito: en el muro de Facebook en móvil, el 4:5 es
la proporción que más alto ocupa sin que la red la recorte, o sea más pantalla robada al
scroll con el mismo contenido.

NO se toca `instagram_imagen.py`: se le importan las piezas que ya están probadas (la fuente
con respaldo DejaVu para la Pi, el formato de precios, la descarga). Dos copias del mismo
código acabarían divergiendo y el fallo saldría meses después en una sola de las dos redes.

Las dos cosas que hacían que mi primer intento quedara pobre, y que vienen de leer `componer`:
  1. Pedir la foto GRANDE: Amazon sirve `_AC_UL320_` (320 px) y basta cambiar el sufijo a
     `_AC_UL1000_` para tener una decente.
  2. Recortar con `getbbox()` el margen blanco que traen las fotos de Amazon. Sin eso el
     producto sale diminuto en medio de un vacío.
"""

import os
import sys

sys.path.insert(0, "/home/rebajasdiarias/rebajasdiarias-web/scripts")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "rebajasdiarias-web", "scripts"))

from PIL import Image, ImageDraw

import instagram_imagen as ii
from instagram_imagen import BLANCO, GRIS, ROJO, TINTA, NEGRITA, MEDIA, NORMAL, f, precio_txt

ANCHO, ALTO = 1080, 1350
NOCHE = (15, 22, 34)
VERDE = (34, 178, 120)
OSCURO = True          # el claro queda a un cambio de aquí, por si se prefiere


def _foto(oferta, caja_ancho, caja_alto):
    url = (oferta.get("image") or "").replace("_AC_UL320_", "_AC_UL1000_")
    try:
        im = ii._descargar(url)
    except Exception:
        try:
            im = ii._descargar(oferta.get("image"))
        except Exception:
            return None
    c = im.getbbox()
    if c:
        im = im.crop(c)
    p = min(caja_ancho / im.width, caja_alto / im.height)
    im = im.resize((max(1, int(im.width * p)), max(1, int(im.height * p))), Image.LANCZOS)
    fondo = Image.new("RGBA", im.size, (255, 255, 255, 255))
    fondo.alpha_composite(im)
    return fondo.convert("RGB")


def _lineas(d, texto, fuente, ancho, maximo=2):
    out, actual = [], ""
    for w in (texto or "").split():
        prueba = (actual + " " + w).strip()
        if d.textlength(prueba, font=fuente) > ancho:
            out.append(actual)
            actual = w
            if len(out) == maximo:
                break
        else:
            actual = prueba
    if len(out) < maximo and actual:
        out.append(actual)
    if len(out) == maximo and len(" ".join(out)) < len(texto or ""):
        out[-1] = out[-1][:44].rstrip() + "…"
    return [ln for ln in out[:maximo] if ln]


def componer(oferta, rotulo=None):
    """Devuelve la imagen 1080x1350 lista para publicar, o None si no se pudo bajar la foto."""
    fondo_color = NOCHE if OSCURO else BLANCO
    tinta = BLANCO if OSCURO else TINTA
    lienzo = Image.new("RGB", (ANCHO, ALTO), fondo_color)
    d = ImageDraw.Draw(lienzo)

    # El reparto va de abajo arriba, igual que en la tarjeta de Instagram: así da igual que el
    # producto sea apaisado o alargado, nunca queda un hueco muerto.
    ALTO_MARCA, ALTO_LINEA, HUECO, ALTO_PRECIOS, MARGEN_SUP = 210, 60, 26, 196, 60
    fu_t = f(46, MEDIA)
    lineas = _lineas(d, oferta.get("title"), fu_t, ANCHO - 130)
    alto_texto = len(lineas) * ALTO_LINEA + HUECO + ALTO_PRECIOS
    y_texto = ALTO - ALTO_MARCA - alto_texto

    zona_alto = y_texto - MARGEN_SUP - 40
    foto = _foto(oferta, ANCHO - 170, zona_alto - 60)
    if foto is None:
        return None

    if OSCURO:
        # Tarjeta blanca ajustada A LA FOTO, no un panel fijo: las fotos de Amazon vienen
        # recortadas sobre blanco y sobre fondo oscuro necesitan base, pero un panel grande con
        # el producto pequeño dentro es justo lo que hacía que esto no valiera nada.
        pad = 34
        x0 = (ANCHO - foto.width) // 2 - pad
        y0 = MARGEN_SUP + (zona_alto - foto.height) // 2 - pad
        d.rounded_rectangle([x0, y0, x0 + foto.width + pad * 2, y0 + foto.height + pad * 2],
                            radius=30, fill=BLANCO)
        lienzo.paste(foto, (x0 + pad, y0 + pad))
    else:
        lienzo.paste(foto, ((ANCHO - foto.width) // 2,
                            MARGEN_SUP + (zona_alto - foto.height) // 2))

    # --- Chapa del descuento -----------------------------------------------------------------
    dto = oferta.get("discount_percent") or 0
    txt = rotulo or (f"-{dto}%" if dto else "")
    if txt:
        tam = 92
        fu = f(tam, NEGRITA)
        while tam > 36 and d.textlength(txt, font=fu) > ANCHO - 200:
            tam -= 4
            fu = f(tam, NEGRITA)
        iz, ar, de, ab = d.textbbox((0, 0), txt, font=fu)
        pw, ph = (de - iz) + 60, (ab - ar) + 48
        d.rounded_rectangle([46, 42, 46 + pw, 42 + ph], radius=20, fill=ROJO)
        d.text((46 + 30 - iz, 42 + 24 - ar), txt, font=fu, fill=BLANCO)

    # --- Título ------------------------------------------------------------------------------
    y = y_texto
    for ln in lineas:
        iz, _, de, _ = d.textbbox((0, 0), ln, font=fu_t)
        d.text(((ANCHO - (de - iz)) / 2 - iz, y), ln, font=fu_t, fill=tinta)
        y += ALTO_LINEA

    # --- Precios -----------------------------------------------------------------------------
    nuevo = float(oferta.get("price") or 0)
    antes = float(oferta.get("original_price") or 0)
    fu_p, fu_a = f(112, NEGRITA), f(48, NORMAL)
    t_n, t_a = precio_txt(nuevo), precio_txt(antes)
    an_n = d.textlength(t_n, font=fu_p)
    an_a = d.textlength(t_a, font=fu_a) if antes > nuevo else 0
    x = (ANCHO - (an_n + (an_a + 28 if an_a else 0))) / 2
    yp = y + HUECO
    d.text((x, yp), t_n, font=fu_p, fill=tinta if OSCURO else ROJO)
    if an_a:
        xa, ya = x + an_n + 28, yp + 52
        d.text((xa, ya), t_a, font=fu_a, fill=GRIS)
        d.line([xa - 4, ya + 32, xa + an_a + 4, ya + 32], fill=GRIS, width=4)

    # El ahorro en euros dice más que el porcentaje: "-60%" es abstracto, "ahorras 84 €" no.
    if antes > nuevo:
        fu_s = f(42, NEGRITA)
        s = f"AHORRAS {precio_txt(antes - nuevo)}"
        d.text(((ANCHO - d.textlength(s, font=fu_s)) / 2, yp + 152), s, font=fu_s, fill=VERDE)

    # --- Marca -------------------------------------------------------------------------------
    try:
        logo = Image.open(ii.LOGO).convert("RGBA")
        c = logo.getbbox()
        if c:
            logo = logo.crop(c)
        logo = logo.resize((int(logo.width * (92 / logo.height)), 92), Image.LANCZOS)
        fu_m = f(44, NEGRITA)
        nombre = "Rebajas Diarias"
        an_m = d.textlength(nombre, font=fu_m)
        xt = (ANCHO - (logo.width + 20 + an_m)) / 2
        if OSCURO:
            r = 12
            d.rounded_rectangle([xt - r, ALTO - 160 - r,
                                 xt + logo.width + r, ALTO - 160 + 92 + r],
                                radius=18, fill=BLANCO)
        lienzo.paste(logo, (int(xt), ALTO - 160), logo)
        d.text((xt + logo.width + 20, ALTO - 136), nombre, font=fu_m, fill=tinta)
    except Exception:
        pass
    fu_c = f(30, NORMAL)
    claim = "descuento real verificado · rebajasdiarias.es"
    d.text(((ANCHO - d.textlength(claim, font=fu_c)) / 2, ALTO - 52), claim, font=fu_c, fill=GRIS)
    return lienzo
