# Dashboard de redes · SendWave

Dashboard orgánico de Instagram para **SendWave**: [@sendwaveapp_latam](https://www.instagram.com/sendwaveapp_latam/) y [@sendwaveappbrasil](https://www.instagram.com/sendwaveappbrasil/).
Ejecutor: Datalítica. Publicado en `https://sendwave.datalitica.com.co` (GitHub Pages).

Se construyó sobre el pipeline del dashboard de Comapan. La diferencia es que **cada cuenta es un canal** definido en [`config/clients/sendwave.yaml`](config/clients/sendwave.yaml). Para sumar una cuenta o una red basta con agregar un canal; las pestañas, el ranking y el PDF se generan solos.

## Períodos

| Período | Rango | URL |
|---|---|---|
| Diagnóstico (línea base) | 1 jun – 31 oct 2026 | `/diagnostico/` |
| Mensual | desde nov 2026 | `/2026-11/`, `/2026-12/`, … |

Cada cuenta es un reporte independiente: un selector arriba cambia entre LATAM y Brasil (`?cuenta=ig_latam`), sin cifras combinadas. La raíz `/` redirige al período más reciente. Cada período tiene un PDF por cuenta (`reporte-ig_latam.pdf`, `reporte-ig_brasil.pdf`) que baja el botón **PDF**.

## Flujo

```
Apify (instagram-scraper, 1 corrida por cuenta)
   │  scripts/ingest.py            ← workflow "Ingesta"
   ▼
Supabase (clients, periods, accounts, posts)
   │  scripts/build_dashboard.py   ← workflow "Build dashboard + PDF" (en cadena)
   │    · métricas por cuenta, atípicos, deltas vs mes anterior
   │    · categorías de contenido con Gemini (se guardan, se etiqueta una sola vez)
   │    · hallazgos y recomendaciones con Gemini
   ▼
HTML estático por período (templates/dashboard.html + DATA embebida)
   │  scripts/render_pdf.py        ← Playwright, A4 horizontal
   ▼
GitHub Pages → sendwave.datalitica.com.co
```

## Estructura

```
config/clients/sendwave.yaml   canales, períodos, branding, reglas
scripts/ingest.py              Apify → Supabase
scripts/build_dashboard.py     Supabase → /<período>/index.html
scripts/render_pdf.py          HTML → /<período>/reporte.pdf
templates/dashboard.html       plantilla única (todo el front)
pipeline/                      clientes Apify/Supabase, normalizadores, LLM
supabase/schema.sql            esquema de la base
docs/RUNBOOK.md                operación paso a paso
```

## Reglas de negocio (iguales a Comapan)

- **Engagement** = likes + comentarios (Instagram no expone los compartidos ni los guardados públicamente).
- **Atípico** = engagement > 5× la mediana de la cuenta en el período (piso 5). Las cifras de cabecera, las gráficas, el ranking y la tabla comparada usan solo publicaciones típicas. Las atípicas se listan aparte.
- **Tasa de engagement** = engagement promedio por publicación ÷ seguidores. Permite comparar LATAM y Brasil, que tienen audiencias de distinto tamaño.
- **Deltas**: en % en los KPIs y en valor absoluto en el ranking, siempre contra el mes anterior. El diagnóstico no tiene deltas.
- Todas las fechas y horas se expresan en **America/Bogota**.

La operación completa está en [docs/RUNBOOK.md](docs/RUNBOOK.md).
