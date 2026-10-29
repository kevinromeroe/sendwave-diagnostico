# RUNBOOK · SendWave

## 1. Puesta en marcha (una sola vez)

1. **Supabase**: crear un proyecto nuevo (p. ej. `sendwave-dashboard`) y ejecutar [`supabase/schema.sql`](../supabase/schema.sql) en el SQL Editor.
   Va en un proyecto aparte del de Comapan porque en aquel `periods.id` es la clave única global, y el `2026-11` de un cliente chocaría con el del otro.
2. **GitHub**: publicar esta carpeta como repo **privado** `kevinromeroe/sendwave-diagnostico` desde GitHub Desktop.
3. **Secrets** (Settings → Secrets and variables → Actions):
   - `APIFY_TOKEN`: el mismo de Datalítica.
   - `SUPABASE_URL` y `SUPABASE_SERVICE_KEY`: los del proyecto **nuevo** (Settings → API → service_role).
   - `GEMINI_API_KEY`: la misma de Comapan.
4. **GitHub Pages**: Settings → Pages → Deploy from branch → `main` / root. El archivo `CNAME` ya trae `sendwave.datalitica.com.co`.
   Con un repo privado, Pages requiere un plan de pago de GitHub. Si no se tiene, el repo tiene que ser público. Por eso `robots.txt` y la etiqueta `noindex` bloquean a los buscadores.
5. **DNS** (proveedor de datalitica.com.co): crear un registro `CNAME` con nombre `sendwave` que apunte a `kevinromeroe.github.io`. Cuando propague, activar **Enforce HTTPS** en Pages.
6. **Diagnóstico**:
   1. Actions → **Ingesta** → Run workflow con período `diagnostico`.
   2. El **Build dashboard + PDF** corre solo al terminar y genera `/diagnostico/` y su PDF.

## 2. Operación mensual (desde noviembre de 2026)

Es automática:
- La **ingesta** corre cada martes (mes en curso) y el día 2 de cada mes (cierre del mes anterior).
- El **build** corre en cadena y regenera todos los períodos y el PDF del mes.
- Antes de noviembre de 2026 la ingesta programada no hace nada.

Para correrla a mano:
1. Actions → **Ingesta** → período `2026-11` (o `auto`).
2. Verificar `https://sendwave.datalitica.com.co/2026-11/` con Cmd/Ctrl+Shift+R.

Para regenerar un PDF puntual, usar Actions → **Render PDF (manual)** → `diagnostico 2026-11`.
Si se reingesta el diagnóstico, su PDF no se rehace solo: hay que correr Build con período `diagnostico` o este render manual.

## 3. Seguidores (captura semanal)

Instagram no publica el histórico de seguidores, así que la serie la construimos nosotros:

- Workflow **Seguidores (captura semanal)**: todos los lunes 07:00 Bogotá guarda una medición por cuenta en la tabla `followers_history`. Cuesta centavos (solo pide el perfil).
- Cada **ingesta** también suma un punto.
- El build toma la **última medición de cada mes** para la tarjeta de tendencia de seguidores.
- Para meses sin captura propia se pueden cargar mediciones de fuentes públicas (archive.org) en `config/clients/sendwave_seguidores_extra.json`.

**Regla para clientes nuevos:** activar la captura de seguidores el mismo día del onboarding. Lo que no se captura no se puede recuperar después.

## 4. Agregar una cuenta o una red

1. En `config/clients/sendwave.yaml`, agregar un elemento a `channels` con `key` única, `platform`, `label`, `short`, `handle`, `url`, `color` y su bloque `apify`.
   - Para TikTok, Facebook o LinkedIn hay que copiar los actores y los inputs de `comapan.yaml` en el repo de Comapan.
2. Correr la ingesta del período. La pestaña nueva aparece sola.

## 5. Verificación tras la ingesta

En el log del workflow, cada cuenta reporta `✓ N publicaciones en el período / M traídas`.
- Si `M` llega al tope (`resultsLimit: 200`) y el período es largo, subir el tope en el YAML.
- Si falla una cuenta, **no se escribe nada** en Supabase y el build no corre. El dashboard conserva los datos anteriores.

## 6. Problemas conocidos (heredados de Comapan)

| Síntoma | Causa | Qué hacer |
|---|---|---|
| Miniatura rota | La URL del CDN de Instagram expiró antes de descargarla | Volver a correr la ingesta del período (descarga las que falten) |
| Likes en 0 en un post | La cuenta oculta los likes (Apify devuelve -1) | Se normaliza a 0. El engagement queda solo con los comentarios |
| Categorías raras | Sin `GEMINI_API_KEY` se usa el clasificador por palabras clave | Revisar el secret. Para reetiquetar, poner `tag_primary = null` en Supabase y correr el build |
| Seguidores "—" en un mes | No hay snapshot de cuentas en ese período | El build usa el snapshot del diagnóstico. Correr la ingesta del mes |

## 7. Datos de entorno

| Item | Valor |
|---|---|
| Repo | `kevinromeroe/sendwave-diagnostico` |
| Dominio | `sendwave.datalitica.com.co` |
| CLIENT_ID | `sendwave` |
| Zona horaria | America/Bogota (UTC-5) |
| Costo Apify estimado | diagnóstico: ~0,5–1 USD · mes: ~0,2 USD (tope de 0,60 USD por cuenta y corrida) |
