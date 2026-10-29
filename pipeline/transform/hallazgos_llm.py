"""Genera hallazgos + recomendaciones del período con Gemini, a partir de la
data ya computada del build (KPIs, top posts, categorías, deltas, heatmap).

Se llama una vez por cuenta y devuelve la forma EXACTA que consume el template:

    {
      "_meta": {"model": ..., "attempts": 1, "generated_at": "YYYY-MM-DD..."},
      "resumen_ejecutivo": "…",
      "hallazgos_top": [
        {
          "categoria": "patron|anomalia|tendencia|brecha|correlacion|punto_ciego",
          "dato": "…", "comparativo": "…", "interpretacion": "…", "implicacion": "…",
          "recomendacion": {"accion": "…", "prioridad": "alta|media|baja",
                            "esfuerzo": "alto|medio|bajo"}
        }, …
      ],
      "recomendaciones": [
        {"title": "…", "body": "…", "tags": ["Esfuerzo bajo", "Impacto alto", "30 días"]}, …
      ]
    }

Filosofía: si algo falla (sin API key, error de red, JSON inválido), devuelve
None. El template oculta la sección — NUNCA se muestran
datos inventados ni de otro período.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

CATEGORIAS_VALIDAS = {"patron", "anomalia", "tendencia", "brecha", "correlacion", "punto_ciego"}
PRIORIDADES = {"alta", "media", "baja"}
ESFUERZOS = {"alto", "medio", "bajo"}


def _digest_plataforma(block: dict) -> dict:
    """Resumen compacto de una plataforma para el prompt (pocos tokens)."""
    if not block or not block.get("n_posts"):
        return {}
    mejor_dia = None
    by_day = block.get("by_day") or {}
    if by_day.get("labels") and by_day.get("engagement_promedio"):
        pares = list(zip(by_day["labels"], by_day["engagement_promedio"]))
        if pares:
            mejor_dia = max(pares, key=lambda p: p[1] or 0)[0]
    mejor_hora = None
    by_hour = block.get("by_hour") or {}
    if by_hour.get("labels") and by_hour.get("engagement_promedio"):
        pares = list(zip(by_hour["labels"], by_hour["engagement_promedio"]))
        if pares:
            mejor_hora = max(pares, key=lambda p: p[1] or 0)[0]
    top = (block.get("top5") or [{}])[0]
    return {
        "n_posts": block.get("n_posts"),
        "engagement_total": block.get("engagement_total"),
        "engagement_promedio": block.get("engagement_promedio"),
        "engagement_mediana": block.get("engagement_mediana"),
        "mejor_dia": mejor_dia,
        "mejor_hora": mejor_hora,
        "mejor_post": {
            "fecha": top.get("fecha"),
            "engagement": top.get("engagement"),
            "categoria": top.get("tag_primary"),
            "caption": (top.get("caption") or "")[:160],
        },
        "categorias": block.get("tag_summary") or {},
    }


def _build_digest(data: dict, ch: dict) -> dict:
    b = (data.get("channels") or {}).get(ch["key"]) or {}
    d = _digest_plataforma(b)
    ca = b.get("category_analysis") or {}
    return {
        "periodo": data.get("periodo_label"),
        "es_diagnostico": data.get("is_baseline"),
        "semanas": data.get("semanas"),
        "cuenta": f"{ch['label']} (@{ch['handle']}, idioma {ch.get('idioma', '')})",
        "seguidores": (b.get("account") or {}).get("seguidores"),
        "metricas": d,
        "publicaciones_atipicas": b.get("n_atipicos"),
        "deltas_vs_mes_anterior": b.get("deltas"),
        "mix_categorias": ca.get("mix_global"),
        "gaps_categorias": ca.get("gaps"),
    }


SYSTEM_PROMPT = """Eres un analista senior de social media marketing. Analizas el desempeño orgánico de UNA cuenta de {marca}: {cuenta}. Es un reporte independiente: no la compares con otras cuentas de la marca.

Tu tarea: a partir de los datos REALES del período que te doy, generar hallazgos y recomendaciones ESPECÍFICOS Y ACCIONABLES. Nunca inventes números; usa solo los del input. Si un dato no está, no lo menciones. Si es_diagnostico es true, es la línea base de varios meses: no hables de "este mes" ni de variaciones mensuales.

Responde SOLO con un JSON válido (sin markdown, sin explicaciones) con esta forma EXACTA:

{
  "resumen_ejecutivo": "2-3 frases con la síntesis del período, en español, mencionando cifras reales.",
  "hallazgos_top": [
    {
      "categoria": "patron|anomalia|tendencia|brecha|correlacion|punto_ciego",
      "dato": "El dato objetivo con su número real.",
      "comparativo": "Contra qué se compara (vs mes anterior, vs el promedio de la cuenta, entre formatos o categorías).",
      "interpretacion": "Qué significa en lenguaje claro.",
      "implicacion": "Por qué le importa a la marca.",
      "recomendacion": {"accion": "Acción concreta.", "prioridad": "alta|media|baja", "esfuerzo": "alto|medio|bajo"}
    }
  ],
  "recomendaciones": [
    {"title": "Título corto de la recomendación", "body": "1-2 frases explicando qué hacer y por qué, con base en los datos del período.", "tags": ["Esfuerzo bajo|medio|alto", "Impacto bajo|medio|alto", "30 días|30-60 días|60-90 días"]}
  ]
}

Reglas:
- Entre 4 y 6 hallazgos en "hallazgos_top", ordenados de mayor a menor relevancia.
- Exactamente 3 recomendaciones priorizadas en "recomendaciones".
- Usa las categorías y valores enumerados TAL CUAL (minúsculas).
- Todo en español neutro-colombiano, tono profesional y directo.
- Las recomendaciones deben derivarse de los datos de ESTE período, no ser genéricas."""


def generate(data: dict, ch: dict, cfg: dict, api_key) -> dict | None:
    """Hallazgos de UNA cuenta. Devuelve None ante cualquier fallo (la sección se oculta)."""
    if not api_key:
        print(f"  ⚠ hallazgos_llm {ch['key']}: sin GEMINI_API_KEY, sección se omite")
        return None
    b = (data.get("channels") or {}).get(ch["key"]) or {}
    if not b.get("n_posts"):
        return None

    from pipeline.transform.llm_providers import gemini

    system = (SYSTEM_PROMPT
              .replace("{marca}", cfg["client"]["descripcion_llm"])
              .replace("{cuenta}", f"{ch['label']} (@{ch['handle']})"))
    user_prompt = "Datos del período:\n" + json.dumps(_build_digest(data, ch), ensure_ascii=False, indent=1)

    raw, attempts = None, 0
    for attempt in range(1, 2):  # los reintentos ya los hace gemini.call
        attempts = attempt
        try:
            raw = gemini.call(system, user_prompt, max_tokens=6000, temperature=0.3)
            break
        except Exception as exc:
            print(f"  ⚠ hallazgos_llm {ch['key']} intento {attempt} falló: {exc}")
    if not raw:
        return None

    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(txt)
    except Exception as exc:
        print(f"  ⚠ hallazgos_llm {ch['key']}: JSON inválido ({exc}), sección se omite")
        return None

    clean = []
    for h in parsed.get("hallazgos_top") or []:
        if not isinstance(h, dict):
            continue
        cat = str(h.get("categoria", "")).lower()
        reco = h.get("recomendacion") or {}
        prio = str(reco.get("prioridad", "")).lower()
        esf = str(reco.get("esfuerzo", "")).lower()
        clean.append({
            "categoria": cat if cat in CATEGORIAS_VALIDAS else "patron",
            "dato": str(h.get("dato", "")),
            "comparativo": str(h.get("comparativo", "")),
            "interpretacion": str(h.get("interpretacion", "")),
            "implicacion": str(h.get("implicacion", "")),
            "recomendacion": {
                "accion": str(reco.get("accion", "")),
                "prioridad": prio if prio in PRIORIDADES else "media",
                "esfuerzo": esf if esf in ESFUERZOS else "medio",
            },
        })
    if not clean:
        print(f"  ⚠ hallazgos_llm {ch['key']}: sin hallazgos válidos, sección se omite")
        return None

    recos = []
    for r in (parsed.get("recomendaciones") or [])[:3]:
        if isinstance(r, dict):
            tags = r.get("tags") if isinstance(r.get("tags"), list) else []
            recos.append({"title": str(r.get("title", "")), "body": str(r.get("body", "")),
                          "tags": [str(t) for t in tags][:3]})

    print(f"  ✓ hallazgos_llm {ch['key']}: {len(clean)} hallazgos + {len(recos)} recomendaciones")
    return {
        "_meta": {"model": gemini.DEFAULT_MODEL, "attempts": attempts,
                  "generated_at": datetime.now(timezone.utc).isoformat()},
        "resumen_ejecutivo": str(parsed.get("resumen_ejecutivo", "")),
        "hallazgos_top": clean,
        "recomendaciones": recos,
    }
