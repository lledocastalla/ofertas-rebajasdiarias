#!/usr/bin/env python3
"""
price_watch.py — registra UNA VEZ AL DÍA el precio de una lista fija de productos de Amazon,
estén rebajados o no.

Por qué existe (18 oct 2026):

El catálogo de offers.json solo guarda productos que YA están rebajados entre un 30% y un 80%.
Eso significa que nunca observamos un producto a su precio normal, y por tanto no podemos
demostrar si el "antes" que anuncia Amazon es real o está inflado. Se intentó sacar del
histórico de git (413 commits desde el 3 de agosto) y el resultado fue "el 100% de los precios
de referencia no se han visto nunca", que no es un hallazgo: es la definición del catálogo.
Sesgo de selección puro.

Este script arregla la raíz: sigue una lista FIJA de productos y apunta su precio cada día,
pase lo que pase con el descuento. En unas semanas tendremos la serie completa de cada uno y
entonces sí se puede decir, con datos: "este producto costó X todos los días de noviembre y el
día de Black Friday lo anuncian rebajado desde Y".

AMPLIADO el 22 oct 2026. La primera versión congelaba una foto de los 300 productos más
valorados del catálogo. Eso tenía dos fallos: (1) en offers.json solo hay ~1.086 productos de
Amazon a la vez, así que "300" no era una decisión sino casi todo lo que había con
valoraciones, y (2) el catálogo ROTA -- cada día entran y salen productos según su descuento --
así que una foto fija desperdicia todo lo que pasa por delante. Ahora la lista es ACUMULATIVA:
cada ejecución añade los ASIN de Amazon nuevos que vea y no quita ninguno.

Tamaño: cada registro son ~25 bytes. Con MAXIMO productos y PODA_DIAS de ventana el techo son
unos pocos MB, y en la práctica mucho menos porque un producto que sale del catálogo deja de
registrar. Si algún día molesta, lo que hay que bajar es PODA_DIAS, no el número de productos.

Coste de API: lotes de 10 ASIN. Con ~1.100 productos son ~110 llamadas al día. Para comparar,
el ciclo normal de update_offers.py hace ~1.200 revalidaciones seis veces al día (~7.200), así
que esto sigue siendo ruido.

Uso:
    python3 price_watch.py              # registra el precio de hoy
    python3 price_watch.py --informe    # saca el análisis de lo acumulado
    python3 price_watch.py --init       # crea la lista de productos a vigilar (solo una vez)
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LISTA_PATH = os.path.expanduser("~/price_watch_lista.json")
HIST_PATH = os.path.expanduser("~/price_watch_historico.json")
# 6 meses: cubre de sobra cualquier afirmación estacional (Black Friday, rebajas de enero,
# Prime Day) y mantiene el fichero en un tamaño que la Pi lee y escribe sin esfuerzo. Eran 400
# días, que no servían para nada y multiplicaban el fichero.
PODA_DIAS = 180
# Techo de seguridad para que la lista acumulativa no pueda desbocarse nunca.
MAXIMO = 3000


def log(msg):
    print(f"[price_watch] {msg}", flush=True)


def _cargar(path, por_defecto):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return por_defecto


def ampliar_lista():
    """Añade a la lista de vigilancia los productos de Amazon del catálogo que aún no estaban,
    SIN tocar ni reordenar los que ya había.

    La regla de oro: de aquí nunca se borra nada. Si un producto sale del catálogo seguirá en la
    lista; simplemente la API dejará de devolver precio para él y su serie se quedará como
    estaba. Quitarlo rompería el único activo que tiene este script, que es el historial.

    Dos filtros que se han quitado a propósito el 22 oct 2026:
    - **Número de valoraciones.** Para comprobar si un "antes" anunciado es real da exactamente
      igual que el producto sea popular, y filtrar por eso sesgaba la muestra hacia los más
      vendidos, que son justo los que menos necesitan trucos de precio.
    - **El tope de 300.** Era arbitrario y además nos dejaba fuera la mitad del Amazon que hay
      en el catálogo.
    """
    import update_offers  # noqa: F401  (aplica el parche IPv4 al importarse)
    catalogo = _cargar(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "offers.json"), {})
    lista = _cargar(LISTA_PATH, [])
    if not isinstance(lista, list):
        lista = []
    vistos = {x.get("asin") for x in lista}

    nuevos = 0
    for o in catalogo.get("offers", []):
        if (o.get("store") or "amazon") != "amazon":
            continue
        asin = o.get("id")
        if not asin or asin in vistos:
            continue
        if len(lista) >= MAXIMO:
            log(f"alcanzado el techo de {MAXIMO} productos; no se añaden más")
            break
        vistos.add(asin)
        lista.append({"asin": asin, "titulo": (o.get("title") or "")[:90],
                      "categoria": o.get("category", ""),
                      "desde": date.today().isoformat()})
        nuevos += 1

    if nuevos:
        with open(LISTA_PATH, "w", encoding="utf-8") as f:
            json.dump(lista, f, ensure_ascii=False, indent=1)
        log(f"+{nuevos} productos nuevos a vigilar, {len(lista)} en total")
    else:
        log(f"sin productos nuevos; {len(lista)} en vigilancia")
    return lista


def registrar():
    import amazon_paapi as paapi
    # La lista se amplía ANTES de registrar, así un producto que acaba de entrar en el catálogo
    # empieza su serie hoy mismo en vez de esperar al día siguiente.
    lista = ampliar_lista()
    asins = [x["asin"] for x in lista]

    res = paapi.get_items(asins)
    if res is None:
        log("la API de Amazon no está disponible ahora mismo; no se registra nada")
        return 1
    items, fallidos = res

    # Reintentos. Al ampliar la lista a ~1.100 productos (22 oct 2026) son ~115 llamadas
    # seguidas y la API empezó a devolver 429 a más de la mitad: 633 de 1.143 quedaron sin
    # comprobar en la primera ejecución. Un hueco en la serie es justo lo que este script no
    # se puede permitir, así que los lotes caídos se repiten con una pausa creciente.
    # Se espera ANTES de reintentar, no después, porque el 429 ya se ha producido.
    for espera in (30, 60, 120):
        if not fallidos:
            break
        log(f"{len(fallidos)} sin comprobar; reintento en {espera}s")
        time.sleep(espera)
        res2 = paapi.get_items(sorted(fallidos))
        if res2 is None:
            break
        items2, fallidos2 = res2
        items.extend(items2)
        if len(fallidos2) >= len(fallidos):
            # No mejora: la API está limitando de verdad, insistir solo empeora las cosas.
            fallidos = fallidos2
            break
        fallidos = fallidos2

    hoy = date.today().isoformat()
    hist = _cargar(HIST_PATH, {})
    nuevos = 0
    for item in items:
        asin = item.get("asin") or item.get("asin".upper())
        if not asin:
            continue
        for listing in (item.get("offersV2", {}).get("listings") or []):
            precio = (listing.get("price") or {}).get("money", {}).get("amount")
            if precio is None:
                continue
            base = (listing.get("price") or {}).get("savingBasis", {}).get("money", {}).get("amount")
            # [precio, precio_de_referencia_que_anuncia_amazon] -- el segundo puede faltar
            hist.setdefault(asin, {})[hoy] = [round(float(precio), 2),
                                              round(float(base), 2) if base else None]
            nuevos += 1
            break

    # Poda: los registros viejos no aportan y el fichero no debe crecer sin fin.
    limite = (date.today() - timedelta(days=PODA_DIAS)).isoformat()
    for asin in list(hist):
        hist[asin] = {d: v for d, v in hist[asin].items() if d >= limite}
        if not hist[asin]:
            del hist[asin]

    with open(HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    kb = os.path.getsize(HIST_PATH) / 1024
    log(f"{nuevos} precios registrados ({len(fallidos)} sin comprobar), "
        f"{len(hist)} productos en el histórico, {kb:.0f} KB")
    # Un día con la mitad de la lista sin comprobar no es un fallo que deba pasar callado:
    # las series quedan con huecos y eso se nota semanas después, cuando ya no hay arreglo.
    if asins and len(fallidos) > len(asins) * 0.2:
        log(f"AVISO: {100*len(fallidos)//len(asins)}% de la lista sin comprobar hoy")
    return 0


def _precio(reg):
    """El precio pagado de un registro. Acepta [precio, referencia] y también un número suelto,
    por si algún día se compacta el formato."""
    return float(reg[0] if isinstance(reg, (list, tuple)) else reg)


def _referencia(reg):
    """El "antes" que anunciaba Amazon ese día, o None si no anunciaba ninguno."""
    if isinstance(reg, (list, tuple)) and len(reg) > 1 and reg[1] is not None:
        return float(reg[1])
    return None


def informe():
    """Lo que de verdad se puede afirmar con los datos acumulados, sin pasarse ni un pelo."""
    hist = _cargar(HIST_PATH, {})
    lista = {x["asin"]: x for x in _cargar(LISTA_PATH, [])}
    if not hist:
        log("todavía no hay histórico; ejecuta sin argumentos unos días primero")
        return 1

    dias = sorted({d for v in hist.values() for d in v})
    log(f"histórico de {len(hist)} productos, del {dias[0]} al {dias[-1]} ({len(dias)} días)")

    inflados, analizados = [], 0
    for asin, serie in hist.items():
        if len(serie) < 14:       # mínimo dos semanas de observación continua
            continue
        fechas = sorted(serie)
        precios = [_precio(serie[d]) for d in fechas]
        hoy_p, hoy_ref = _precio(serie[fechas[-1]]), _referencia(serie[fechas[-1]])
        if not hoy_ref or hoy_ref <= hoy_p:
            continue
        analizados += 1
        # El precio de referencia que anuncian, ¿se ha pagado alguna vez mientras mirábamos?
        if max(precios) < hoy_ref * 0.95:
            inflados.append({
                "asin": asin,
                "titulo": lista.get(asin, {}).get("titulo", ""),
                "precio_hoy": hoy_p,
                "referencia_anunciada": hoy_ref,
                "maximo_real_observado": max(precios),
                "dias_observado": len(serie),
            })

    if not analizados:
        log("aún no hay productos con 14+ días de datos. Vuelve a ejecutar más adelante.")
        return 0
    pct = 100 * len(inflados) / analizados
    log(f"de {analizados} productos rebajados con 14+ días de seguimiento, "
        f"{len(inflados)} ({pct:.1f}%) anuncian un precio de referencia que NUNCA hemos visto")
    for x in sorted(inflados, key=lambda x: -(x["referencia_anunciada"] - x["maximo_real_observado"]))[:10]:
        log(f"   {x['titulo'][:50]:<52} hoy {x['precio_hoy']:>7.2f} | "
            f"dice {x['referencia_anunciada']:>8.2f} | máx real {x['maximo_real_observado']:>7.2f}")
    return 0


if __name__ == "__main__":
    if "--init" in sys.argv:
        ampliar_lista()
    elif "--informe" in sys.argv:
        sys.exit(informe())
    else:
        sys.exit(registrar())
