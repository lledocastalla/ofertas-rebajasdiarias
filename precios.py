#!/usr/bin/env python3
"""
precios.py — comprueba contra Amazon que el precio de una oferta SIGUE SIENDO el que vamos a
anunciar, justo antes de publicarla.

Por qué existe (22 oct 2026, petición del usuario: "si ya no está ese precio se quite").

El catálogo `offers.json` se regenera cada pocas horas, pero entre que se genera y que el bot
publica pueden pasar horas, y Amazon cambia precios continuamente. El resultado era que
podíamos anunciar 78,00 € en una publicación que la gente abre y encuentra a 95,00 €. Para una
app cuyo único argumento es "comprobamos el precio antes de publicar", eso es el peor fallo
posible: no es un error técnico, es que el mensaje de la marca deja de ser verdad.

Se comprueba en UNA sola llamada para hasta 10 ofertas (es el tamaño de lote de la API), así
que revalidar los 10 mejores candidatos cuesta lo mismo que revalidar uno.

DECISIÓN IMPORTANTE: si la API de Amazon no contesta, **no se publica**. Es tentador dejar
pasar la publicación "por si acaso" para no perder el hueco, pero entonces la frase
"comprobamos el precio" sería mentira justo los días en que falla algo. Mejor un hueco vacío
que un precio inventado. Son 6 publicaciones al día: perder una no es nada.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Margen que se tolera entre lo que dice el catálogo y lo que dice Amazon ahora mismo. Los
# céntimos bailan por redondeos y por el IVA de algunas categorías; medio euro o un 1% no es
# un precio distinto, es ruido. Más que eso, sí lo es.
TOLERANCIA_EUR = 0.50
TOLERANCIA_PCT = 0.01


def log(msg):
    print(f"[precios] {msg}", flush=True)


def precios_ahora(asins):
    """{asin: precio} con lo que cuesta cada cosa en este momento.

    Un ASIN que no aparezca en el resultado es que Amazon ya no da precio para él: sin stock,
    sin oferta o retirado. Devuelve None si la API entera no está disponible, que es distinto
    de "no hay precio" y quien llama debe tratarlo distinto."""
    if not asins:
        return {}
    import amazon_paapi as paapi
    res = paapi.get_items(list(asins))
    if res is None:
        return None
    items, fallidos = res
    if fallidos:
        log(f"{len(fallidos)} sin comprobar (lote fallido); se tratan como no vigentes")
    out = {}
    for item in items:
        asin = item.get("asin")
        if not asin:
            continue
        for listing in (item.get("offersV2", {}).get("listings") or []):
            precio = (listing.get("price") or {}).get("money", {}).get("amount")
            if precio is not None:
                out[asin] = round(float(precio), 2)
                break
    return out


def sigue_vigente(oferta, precio_real):
    """¿El precio que íbamos a anunciar es el que la gente va a encontrar?"""
    if precio_real is None:
        return False
    try:
        anunciado = float(oferta.get("price"))
    except (TypeError, ValueError):
        return False
    margen = max(TOLERANCIA_EUR, anunciado * TOLERANCIA_PCT)
    return abs(precio_real - anunciado) <= margen


def filtrar_vigentes(ofertas, limite=10):
    """De una lista de candidatas EN ORDEN DE PREFERENCIA, devuelve las que siguen a su precio.

    Se miran como mucho `limite` porque es lo que cabe en una llamada. Devuelve None si la API
    no está disponible -- quien llama no debe publicar en ese caso."""
    cand = list(ofertas)[:limite]
    reales = precios_ahora([o["id"] for o in cand if o.get("id")])
    if reales is None:
        log("la API de Amazon no está disponible; no se publica nada esta vez")
        return None
    vigentes = []
    for o in cand:
        real = reales.get(o.get("id"))
        if sigue_vigente(o, real):
            vigentes.append(o)
        else:
            motivo = "sin precio en Amazon" if real is None else f"ahora {real} €"
            log(f"descartada: {(o.get('title') or '')[:42]} "
                f"(anunciaba {o.get('price')} €, {motivo})")
    return vigentes
