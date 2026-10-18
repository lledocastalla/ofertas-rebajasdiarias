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

Tamaño: ~300 productos x 1 registro/día x ~40 bytes = unos 12 KB al mes. Se podan los registros
de más de PODA_DIAS. No crece.

Coste de API: lotes de 10 ASIN, o sea ~30 llamadas al día. Para comparar, el ciclo normal de
update_offers.py hace ~1.200 revalidaciones seis veces al día, así que esto es ruido.

Uso:
    python3 price_watch.py              # registra el precio de hoy
    python3 price_watch.py --informe    # saca el análisis de lo acumulado
    python3 price_watch.py --init       # crea la lista de productos a vigilar (solo una vez)
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LISTA_PATH = os.path.expanduser("~/price_watch_lista.json")
HIST_PATH = os.path.expanduser("~/price_watch_historico.json")
PODA_DIAS = 400
OBJETIVO = 300


def log(msg):
    print(f"[price_watch] {msg}", flush=True)


def _cargar(path, por_defecto):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return por_defecto


def crear_lista():
    """Elige los productos a vigilar: los más valorados del catálogo actual, que son los que la
    gente de verdad compra y los que tendrán oferta en Black Friday. La lista se congela a
    propósito -- si cambiara cada día no habría serie que comparar."""
    import update_offers  # noqa: F401  (aplica el parche IPv4 al importarse)
    catalogo = _cargar(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "offers.json"), {})
    amazon = [o for o in catalogo.get("offers", [])
              if (o.get("store") or "amazon") == "amazon" and (o.get("rating_count") or 0) > 0]
    amazon.sort(key=lambda o: -(o.get("rating_count") or 0))
    vistos, lista = set(), []
    for o in amazon:
        if o["id"] in vistos:
            continue
        vistos.add(o["id"])
        lista.append({"asin": o["id"], "titulo": o.get("title", "")[:90],
                      "categoria": o.get("category", "")})
        if len(lista) >= OBJETIVO:
            break
    with open(LISTA_PATH, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False, indent=1)
    log(f"lista creada con {len(lista)} productos en {LISTA_PATH}")
    return lista


def registrar():
    import amazon_paapi as paapi
    lista = _cargar(LISTA_PATH, None)
    if not lista:
        lista = crear_lista()
    asins = [x["asin"] for x in lista]

    res = paapi.get_items(asins)
    if res is None:
        log("la API de Amazon no está disponible ahora mismo; no se registra nada")
        return 1
    items, fallidos = res

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
    return 0


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
        precios = [serie[d][0] for d in fechas]
        hoy_p, hoy_ref = serie[fechas[-1]]
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
        crear_lista()
    elif "--informe" in sys.argv:
        sys.exit(informe())
    else:
        sys.exit(registrar())
