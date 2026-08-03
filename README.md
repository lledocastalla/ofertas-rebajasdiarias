# ofertas-rebajasdiarias

Repo público que solo aloja `offers.json`, el catálogo de ofertas que consume la app
Flutter **RebajasDiarias** (repo separado, privado) vía:

```
https://raw.githubusercontent.com/lledocastalla/ofertas-rebajasdiarias/main/offers.json
```

## Actualización automática

Un agente programado (Claude, vía RemoteTrigger) corre 3 veces al día y reescribe
`offers.json` con ofertas reales encontradas en Amazon.es. Nunca debe inventar
precios ni porcentajes de descuento — todo dato debe venir de una página real de
Amazon.es en el momento de la ejecución.

## Esquema de `offers.json`

```json
{
  "updated_at": "ISO8601 con offset, momento de la última actualización",
  "affiliate_tag": "rebajasdiaria-21",
  "offers": [
    {
      "id": "ASIN real de Amazon (10 caracteres)",
      "title": "Título corto y legible del producto (no el título SEO completo)",
      "category": "Una de las categorías de abajo",
      "price": 37.90,
      "original_price": 54.90,
      "discount_percent": 31,
      "is_flash": true,
      "image": "URL real de imagen m.media-amazon.com del producto",
      "url": "https://www.amazon.es/dp/{ASIN}?tag=rebajasdiaria-21"
    }
  ]
}
```

Reglas:
- `discount_percent` = el % que Amazon muestra en la oferta, redondeado. Nunca fabricado.
- `is_flash: true` únicamente si Amazon marca el producto como oferta con cuenta atrás
  ("Finaliza en HH:MM:SS") — no para "Oferta Prime limitada" genérica.
- `category` debe ser una de: Bebés, Moda Hombre, Moda Mujer, Hogar, Juguetes,
  Tecnología, Mascotas, Deporte, Gafas de Sol, Gaming, Música, Libros, Belleza,
  Alimentación, Jardín, Oficina, Salud, Viajes, Automóviles, Relojes.
- Incluir tantos productos reales como sea razonable encontrar (idealmente 40-80),
  cubriendo el mayor número posible de categorías distintas, no solo unas pocas.
- `url` siempre debe incluir `?tag=rebajasdiaria-21` (o `&tag=rebajasdiaria-21` si el
  enlace ya tiene query params).
- Sobrescribir el array `offers` completo en cada ejecución (no acumular duplicados
  de ejecuciones anteriores).
