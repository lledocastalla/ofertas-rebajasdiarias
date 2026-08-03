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
- **Mínimo 30% de descuento real para incluir un producto, sin excepciones** (ni
  siquiera los flash). Si un producto tiene menos del 30%, no se incluye.
- `is_flash: true` únicamente si Amazon marca el producto como oferta con cuenta atrás
  ("Finaliza en HH:MM:SS") o etiqueta "Oferta flash" explícita — no para "Oferta Prime
  limitada" genérica. Prioriza encontrar varias (al menos 5-8 si es posible) en cada
  actualización: revisa la pestaña "Ofertas flash" de amazon.es/deals y busca también
  la etiqueta "Oferta flash" en resultados de búsqueda de marcas.
- Busca activamente ropa/calzado de marcas reconocidas con descuento real ≥30%
  (Tommy Hilfiger, Adidas, Nike, Levi's, Lacoste, Calvin Klein, The North Face, Puma,
  New Balance, etc.) vía `amazon.es/s?k=<marca>` — suelen tener varias ofertas
  genuinas de 30-65% y son las que más interesan al usuario. Repártelas entre
  "Moda Hombre", "Moda Mujer" y "Deporte" según corresponda.
- `category` debe ser una de: Bebés, Moda Hombre, Moda Mujer, Hogar, Juguetes,
  Tecnología, Mascotas, Deporte, Gafas de Sol, Gaming, Música, Libros, Belleza,
  Alimentación, Jardín, Oficina, Salud, Viajes, Automóviles, Relojes.
- Incluir tantos productos reales como sea razonable encontrar que cumplan el 30%
  mínimo, cubriendo el mayor número posible de categorías distintas. Es normal que
  algunas categorías (Hogar, Salud, Oficina...) tengan pocos o ningún producto si
  ahora mismo no hay descuentos reales ≥30% ahí — no rebajar el umbral para rellenar.
- `url` siempre debe incluir `?tag=rebajasdiaria-21` (o `&tag=rebajasdiaria-21` si el
  enlace ya tiene query params).
- Sobrescribir el array `offers` completo en cada ejecución (no acumular duplicados
  de ejecuciones anteriores).
