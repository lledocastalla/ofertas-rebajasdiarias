# ofertas-rebajasdiarias

Repo público que solo aloja `offers.json`, el catálogo de ofertas que consume la app
Flutter **RebajasDiarias** (repo separado, privado) vía:

```
https://raw.githubusercontent.com/lledocastalla/ofertas-rebajasdiarias/main/offers.json
```

## Actualización automática

`update_offers.py` corre por `cron` en una Raspberry Pi, cada 3 horas dentro de la
franja 08:00-23:00 (nunca de madrugada). Cada ejecución:

- Con Selenium + Chromium headless (sesión ya logueada en Amazon.es, perfil
  persistente), busca un subconjunto aleatorio de palabras clave por categoría —
  nunca las 20+ categorías de golpe, para no tardar horas en una Pi 3B ni parecer
  un bot golpeando Amazon a ritmo constante (retrasos aleatorios, orden variable).
- En paralelo, `multitienda_feeds.py` descarga 1 feed rotado de los 7 de Leroy
  Merlin (afiliación Awin) + el feed completo de Stylevana, y calcula descuento
  real sobre esos catálogos.
- Fusiona los resultados nuevos con los ya existentes en `offers.json` por ID
  (ASIN de Amazon o SKU de tienda) — nunca sustituye el catálogo entero de golpe,
  así que categorías/tiendas no tocadas en una ejecución concreta no pierden sus
  ofertas ya publicadas.
- Filtra descuento mínimo 30% (máximo 80%, ver `MAX_DISCOUNT_PERCENT` — un
  descuento calculado por encima de eso casi seguro viene de un "precio anterior"
  inflado, no de una rebaja real).
- Si `campaign.json` tiene una campaña activa con `categoryTarget` (p.ej. "Vuelta
  al Cole"), esa categoría se refuerza con más búsquedas ese ciclo.
- Regla de seguridad crítica: si el scraping falla (Amazon bloquea, cambia el
  HTML, sin red...) o el catálogo resultante tiene menos del mínimo aceptable de
  productos, el script ABORTA sin tocar `offers.json` ni hacer commit/push. Nunca
  se deja la app/web sin ofertas por un fallo puntual.
- De paso (mismo proceso, no un paso aparte): publica destacados en un grupo de
  Telegram, manda un push de "catálogo actualizado" a la app (Firebase Cloud
  Messaging) y comprueba precios de favoritos vigilados por usuarios de la app.

## Esquema de `offers.json`

```json
{
  "updated_at": "ISO8601 con offset, momento de la última actualización",
  "affiliate_tag": "rebajasdiaria-21",
  "offers": [
    {
      "id": "ASIN de Amazon (10 caracteres), o id propio de tienda (ej. \"lm_44738882308\")",
      "title": "Título corto y legible del producto (no el título SEO completo)",
      "category": "Una de las categorías de abajo",
      "price": 37.90,
      "original_price": 54.90,
      "discount_percent": 31,
      "is_flash": true,
      "image": "URL real de imagen del producto",
      "url": "enlace de afiliado real (Amazon Associates o Awin, según la tienda)",
      "store": "amazon | leroymerlin | stylevana (por defecto amazon si se omite)",
      "store_label": "Amazon | Leroy Merlin | Stylevana",
      "first_seen": "ISO8601, primera vez que se vio esta oferta",
      "last_seen": "ISO8601, última vez que se vio (usado para retirar ofertas caducadas)"
    }
  ]
}
```

Multi-tienda desde el 24 ago 2026 (ver `multitienda_feeds.py`): además de Amazon, el
catálogo incluye Leroy Merlin ES y Stylevana vía feeds de afiliación de Awin. `is_flash`
solo aplica a Amazon (Leroy Merlin/Stylevana no marcan ofertas flash en su feed, siempre
`false` ahí).

Reglas:
- `discount_percent` = el % de descuento real, redondeado. Nunca fabricado — siempre
  calculado a partir de `price`/`original_price` reales de la tienda de origen en el
  momento de la ejecución.
- **Mínimo 30% de descuento real para incluir un producto, sin excepciones** (ni
  siquiera los flash), y **máximo 80%** — un descuento calculado por encima de eso casi
  siempre viene de un "precio anterior" inflado por el vendedor (nunca vendido de
  verdad), no de una rebaja genuina, así que se descarta la oferta entera (no se aplica
  a productos realmente gratis, precio 0€).
- `is_flash: true` únicamente si Amazon marca el producto como oferta con cuenta atrás
  ("Finaliza en HH:MM:SS") o etiqueta "Oferta flash" explícita — no para "Oferta Prime
  limitada" genérica.
- Búsqueda por categoría vía marcas reconocidas (ver `KEYWORDS_BY_CATEGORY` en
  `update_offers.py` para la lista completa y actualizada por categoría) — cada
  ejecución sondea un subconjunto aleatorio, no todas de golpe.
- `category` debe ser una de: Bebés, Moda Hombre, Moda Mujer, Hogar, Juguetes,
  Tecnología, Mascotas, Deporte, Gafas de Sol, Gaming, Música, Libros, Belleza,
  Alimentación, Jardín, Oficina, Salud, Viajes, Automóviles, Relojes, Bricolaje,
  Decoración, Cocinas y Baños, Muebles (estas últimas 4, multi-tienda vía Leroy
  Merlin, ver `multitienda_feeds.py`), y Vuelta al Cole (25 ago 2026, categoría
  **estacional**: solo se busca activamente mientras haya una campaña activa en
  `campaign.json` con `categoryTarget: "Vuelta al Cole"` — ver `KEYWORDS_BY_CATEGORY`
  en `update_offers.py`; fuera de esa ventana no se busca y las ofertas ya
  encontradas se retiran solas por antigüedad).
- Es normal que algunas categorías (Hogar, Salud, Oficina...) tengan pocos o ningún
  producto si ahora mismo no hay descuentos reales ≥30% ahí — no se rebaja el umbral
  para rellenar.
- `url` de Amazon siempre incluye `?tag=rebajasdiaria-21` (o `&tag=` si ya tiene query
  params); las de Leroy Merlin/Stylevana son enlaces de tracking de Awin
  (`awin1.com/pclick.php?...`), ya con el afiliado correcto.
- Los resultados nuevos de cada ejecución se **fusionan** con los ya existentes por
  `id` (no se sobrescribe el array entero de golpe) — una oferta se retira sola cuando
  `last_seen` lleva más de `STALE_AFTER_DAYS` (2 días) sin actualizarse, no por edad de
  `first_seen`.

## `catalog_extended.json` (buscador ampliado, 26 ago 2026)

Archivo APARTE de `offers.json`, mismo repo/patrón (subido a GitHub, sin servidor nuevo). Lo
consume la web/app SOLO cuando una búsqueda no encuentra nada en el catálogo curado normal —
da acceso a mucho más catálogo real de Leroy Merlin y Perfumería Comas sin publicarlo todo en
la portada. Mismo esquema exacto que `offers.json` (mismos campos, mismo enlace de afiliado
real).

- Generado por `generate_extended_catalog()` en `multitienda_feeds.py`, llamado desde
  `update_offers.py` con throttle propio (`EXTENDED_CATALOG_MIN_HOURS = 20` — no se regenera en
  cada ciclo de 3h, descargar los 7 feeds completos de Leroy tarda ~2 min).
- **No se fusiona ni se poda con el tiempo** como `offers.json`: cada vez que toca regenerarse,
  se sustituye entero (volcado fresco del feed de origen, filtrado por descuento 30-80% igual
  que siempre).
- Leroy Merlin: top 3.000 por descuento de cada uno de los 7 feeds (no el 100% del catálogo —
  comprobado el 26 ago 2026 que sin tope son **682.332 productos** cualificando, inviable como
  archivo único; el recorte sigue siendo "lo más rebajado de verdad" de cada categoría).
  Perfumería Comas: catálogo entero, sin recorte (cabe de sobra, unos pocos miles de productos).
  Total real medido: ~24.700 productos, ~10.8 MB JSON / ~1.5 MB gzip.
- Se comitea junto a `offers.json` en el mismo commit cuando toca regenerarse (no genera un
  commit aparte).
