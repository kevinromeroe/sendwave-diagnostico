#!/usr/bin/env python3
"""Build del dashboard: lee Supabase, calcula las métricas de cada canal y
escribe un HTML estático por período (DATA embebida, sin llamadas en el front).

Salidas:
  /diagnostico/index.html      línea base
  /YYYY-MM/index.html          un reporte por mes
  /index.html                  redirige al período más reciente

Cada cuenta (canal del YAML) es un reporte independiente: el dashboard tiene
un selector de cuenta y nada se suma ni se compara entre cuentas.

Reglas de negocio (iguales al dashboard de Comapan):
  - Atípico = engagement > Q3 + 3×IQR de la cuenta en el período (ver split_outliers).
    Las métricas de cabecera, gráficas, ranking y tabla comparada usan SOLO
    publicaciones típicas (orgánico); las atípicas se listan aparte.
  - Deltas vs mes anterior: % en los KPIs, cambio absoluto en el ranking.
  - Zona horaria: America/Bogota.

Uso:
    python scripts/build_dashboard.py                  # todos los períodos
    python scripts/build_dashboard.py --period 2026-11
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.load.supabase_client import Supabase
from pipeline.transform import hallazgos_llm
from pipeline.util import periods as P
from pipeline.util.config import load_client
from pipeline.util.log import get_logger

log = get_logger("build")
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "dashboard.html"
DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

# ─────────────────────────────────────────────────────────────
# Taxonomía de contenido (12 categorías) para una app de remesas
# ─────────────────────────────────────────────────────────────
TAG_TAXONOMY = {
    "promocion":    "tasa de cambio, descuento, envío gratis, código o bono",
    "producto":     "funcionalidad de la app, cómo funciona, seguridad, rapidez, destinos",
    "tutorial":     "paso a paso para enviar, registrarse o usar la app",
    "educacion":    "educación financiera, ahorro, tips de dinero, datos útiles",
    "testimonio":   "historia o reseña de un usuario real, UGC, caso de éxito",
    "comunidad":    "migración, identidad, familia a la distancia, cultura del país de origen",
    "estacional":   "fecha especial: día de la madre, navidad, fiestas patrias, black friday",
    "tendencia":    "meme, trend, audio viral, humor o cultura pop",
    "concurso":     "sorteo, giveaway, dinámica con premio",
    "interaccion":  "pregunta o encuesta a la audiencia sin premio",
    "marca":        "institucional: misión, valores, logros, equipo",
    "alianza":      "colaboración con influencer, creador u otra marca",
}

_KEYWORDS = [  # fallback cuando Gemini falla; español y portugués
    ("concurso",    ["sorteo", "giveaway", "sorteio", "concurso", "gana ", "ganhe", "premio", "prêmio"]),
    ("promocion",   ["tasa", "taxa", "descuento", "desconto", "gratis", "grátis", "código", "codigo",
                     "cupón", "cupom", "bono", "bônus", "promo", "oferta", "% "]),
    ("tutorial",    ["paso a paso", "passo a passo", "cómo enviar", "como enviar", "aprende a", "aprenda a",
                     "tutorial", "así de fácil", "simples assim"]),
    ("estacional",  ["día de la madre", "dia das mães", "navidad", "natal", "año nuevo", "ano novo",
                     "black friday", "independencia", "independência", "día del padre", "dia dos pais"]),
    ("testimonio",  ["testimonio", "depoimento", "historia de", "história de", "nos cuenta", "conta pra gente"]),
    ("educacion",   ["tip", "dica", "ahorr", "econom", "finanzas", "finanças", "sabías", "você sabia"]),
    ("interaccion", ["cuéntanos", "cuentanos", "comenta", "comente", "¿cuál", "qual ", "responde", "responda"]),
    ("comunidad",   ["familia", "família", "migrante", "imigrante", "distancia", "distância", "hogar", "lar"]),
    ("alianza",     ["colab", "parceria", "alianza", "junto a @", "junto com @"]),
    ("producto",    ["app", "envía", "envia", "transfer", "seguro", "rápido", "rapido"]),
]


def _guess_tag(caption: str) -> str:
    c = (caption or "").lower()
    for tag, words in _KEYWORDS:
        if any(w in c for w in words):
            return tag
    return "marca"


def tag_posts(posts: list[dict], cfg: dict, sb: Supabase | None, batch_size: int = 15) -> None:
    """Etiqueta con Gemini las publicaciones que aún no tienen categoría y la
    persiste en Supabase (cada publicación se clasifica una sola vez).

    Si Gemini falla, el lote usa el clasificador por palabras clave SOLO para este
    build (no se persiste), así el siguiente build vuelve a intentar con Gemini."""
    pending = [p for p in posts if not p.get("tag_primary")]
    print(f"  Categorías: {len(posts) - len(pending)} ya etiquetadas, {len(pending)} por etiquetar")
    if not pending:
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    call = None
    if api_key:
        from pipeline.transform.llm_providers import gemini
        call = gemini.call
    else:
        print("  ⚠ Sin GEMINI_API_KEY: se usa el clasificador por palabras clave")

    labels = {ch["key"]: ch["label"] for ch in cfg["channels"]}
    taxonomy = "\n".join(f"- {k}: {v}" for k, v in TAG_TAXONOMY.items())
    system = (
        f"Clasificas publicaciones de redes sociales de {cfg['client']['descripcion_llm']}. "
        "Los captions pueden estar en español o portugués. "
        "Asigna a cada publicación UNA categoría primaria y entre 0 y 2 secundarias, "
        "usando SOLO esta taxonomía cerrada:\n" + taxonomy + "\n"
        "Usa 'marca' solo para contenido institucional; si habla de la app o de enviar dinero, "
        "prefiere 'producto', 'tutorial' o 'promocion'. "
        'Devuelve SOLO JSON: {"items":[{"i":0,"primary":"promocion","secondary":["tutorial"]}]}'
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    fallos_seguidos = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        parsed = None
        if call and fallos_seguidos >= 2:
            print("  ⚠ Gemini no responde: el resto de lotes usa palabras clave en este build")
            call = None
        if call:
            lines = [f"#{i} [{labels.get(p['platform'], p['platform'])} · {p['type']}] "
                     f"{(p.get('caption') or '')[:400]}" for i, p in enumerate(batch)]
            try:
                parsed = json.loads(call(system, "Publicaciones:\n" + "\n".join(lines),
                                         max_tokens=8000, temperature=0.1))
                fallos_seguidos = 0
            except Exception as exc:
                fallos_seguidos += 1
                print(f"  ⚠ Lote {start // batch_size + 1}: Gemini falló ({str(exc)[:120]}); uso palabras clave")
        answers = {it.get("i"): it for it in (parsed or {}).get("items", []) if isinstance(it, dict)}

        rows = []
        for i, p in enumerate(batch):
            it = answers.get(i) or {}
            primary = str(it.get("primary") or "").lower().strip()
            de_gemini = primary in TAG_TAXONOMY
            if not de_gemini:
                primary = _guess_tag(p.get("caption"))
            secondary = [str(t).lower().strip() for t in (it.get("secondary") or [])]
            secondary = [t for t in secondary if t in TAG_TAXONOMY and t != primary][:2]
            p["tags"], p["tag_primary"] = [primary] + secondary, primary
            if de_gemini:
                rows.append({"id": p["id"], "tags": p["tags"], "tag_primary": primary, "tagged_at": now_iso})
        if sb is not None and rows:
            for r in rows:
                sb.update("posts", f"id=eq.{r['id']}",
                          {"tags": r["tags"], "tag_primary": r["tag_primary"], "tagged_at": r["tagged_at"]})
        print(f"  ✓ Lote {start // batch_size + 1}: {len(rows)}/{len(batch)} con Gemini (guardadas)")


# ─────────────────────────────────────────────────────────────
# Lectura
# ─────────────────────────────────────────────────────────────
def load_posts(sb: Supabase, client_id: str) -> list[dict]:
    # Supabase devuelve máximo 1000 filas por consulta: se pagina
    rows, page = [], 1000
    while True:
        chunk = sb.select("posts", filter=f"client_id=eq.{client_id}&order=id.asc&offset={len(rows)}",
                          columns="id,platform,url,type,caption,hashtags,likes,comments,shares,views,"
                                  "engagement,media_url,media_url_local,tags,tag_primary,posted_at",
                          limit=page)
        rows += chunk
        if len(chunk) < page:
            break
    out = []
    for r in rows:
        ts = P.to_bogota(r.get("posted_at"))
        if not ts:
            continue
        out.append({
            "id": r["id"], "platform": r.get("platform"), "url": r.get("url") or "",
            "type": r.get("type") or "post", "caption": r.get("caption") or "",
            "hashtags": r.get("hashtags") or [], "likes": r.get("likes") or 0,
            "comments": r.get("comments") or 0, "shares": r.get("shares") or 0,
            "views": r.get("views"), "engagement": r.get("engagement") or 0,
            "media_url": r.get("media_url_local") or r.get("media_url") or "",
            "tags": r.get("tags"), "tag_primary": r.get("tag_primary"), "ts": ts,
        })
    return out


def in_window(posts: list[dict], start, end) -> list[dict]:
    return [p for p in posts if start <= p["ts"] <= end]


def load_accounts(sb: Supabase, client_id: str, period: str, fallback: str) -> dict:
    rows = sb.select("accounts", filter=f"client_id=eq.{client_id}&period_id=eq.{period}")
    if not rows and period != fallback:
        rows = sb.select("accounts", filter=f"client_id=eq.{client_id}&period_id=eq.{fallback}")
    return {
        a["platform"]: {
            "username": a.get("username") or "",
            "nombre": a.get("display_name") or "",
            "bio": a.get("bio") or "",
            "seguidores": a.get("followers"),
            "siguiendo": a.get("following_n"),
            "posts_totales": a.get("posts_total"),
            "snapshot_fecha": (a.get("snapshot_at") or "")[:10],
        }
        for a in rows
    }


def followers_by_month(sb: Supabase, client_id: str) -> dict[str, dict[str, int]]:
    """{canal: {YYYY-MM: seguidores}} con todas las capturas de perfil guardadas.

    Instagram no expone el histórico de seguidores: la serie existe desde la
    primera ingesta y suma un punto por cada reporte mensual.
    """
    out: dict[str, dict[str, int]] = defaultdict(dict)
    rows = sb.select("accounts", filter=f"client_id=eq.{client_id}&order=snapshot_at.asc",
                     columns="platform,period_id,followers,snapshot_at")
    for a in rows:
        if a.get("followers") is None:
            continue
        month = a["period_id"] if P.is_month(a["period_id"]) else (a.get("snapshot_at") or "")[:7]
        if month:
            out[a["platform"]][month] = a["followers"]
    return out


def monthly_trend(cfg: dict, key: str, all_posts: list[dict], followers: dict[str, int],
                  until: datetime, an: dict, max_months: int = 12) -> dict:
    """Serie mensual de una cuenta desde el inicio del diagnóstico hasta el fin del período."""
    d0 = datetime.fromisoformat(cfg["periods"]["diagnostico"]["starts_on"])
    months, y, m = [], d0.year, d0.month
    while (y, m) <= (until.year, until.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    months = months[-max_months:]

    now = datetime.now(P.BOGOTA)
    serie = {"labels": [], "parcial": [], "n_posts": [], "engagement_total": [],
             "engagement_promedio": [], "seguidores": [], "tasa": [], "tasa_aprox": []}
    known = sorted(followers.items())
    for month in months:
        ms, me = P.window(cfg, month)
        b = channel_block([p for p in in_window(all_posts, ms, me) if p["platform"] == key], an, ms, me)
        seg = followers.get(month)
        # Tasa del mes: con los seguidores de ese mes o, si no hay captura, con la más cercana
        ref = seg if seg is not None else (min(known, key=lambda kv: abs(_month_diff(kv[0], month)))[1]
                                           if known else None)
        serie["labels"].append(f"{MES_CORTO[int(month[5:])]} {month[2:4]}")
        serie["parcial"].append(ms <= now < me)
        serie["n_posts"].append(b["n_posts"])
        serie["engagement_total"].append(b["engagement_total"])
        serie["engagement_promedio"].append(b["engagement_promedio"])
        serie["seguidores"].append(seg)
        serie["tasa"].append(round(b["engagement_promedio"] / ref * 100, 3) if ref and b["n_posts"] else None)
        serie["tasa_aprox"].append(seg is None and ref is not None)
    return serie


def _month_diff(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:]) - int(b[5:])


# ─────────────────────────────────────────────────────────────
# Métricas por canal
# ─────────────────────────────────────────────────────────────
def _post_dict(p: dict) -> dict:
    return {
        "id": p["id"], "url": p["url"], "tipo": p["type"], "fecha": p["ts"].strftime("%Y-%m-%d"),
        "likes": p["likes"], "comentarios": p["comments"], "engagement": p["engagement"],
        "views": p.get("views"), "caption": p["caption"], "media_url": p["media_url"],
        "tags": p.get("tags"), "tag_primary": p.get("tag_primary"),
    }


def split_outliers(ps: list[dict], an: dict) -> tuple[list[dict], list[dict], float]:
    """(típicas, atípicas, umbral). Atípica = engagement > Q3 + k × IQR (k = 3 por defecto).

    Se usa el rango intercuartílico y no un múltiplo de la mediana porque el
    engagement de estas cuentas es muy disperso: con "5× mediana" LATAM marcaba
    el 25 % de sus piezas como atípicas. Con menos de 4 publicaciones no hay
    cuartiles fiables y no se marca ninguna.
    """
    if len(ps) < 4:
        return list(ps), [], 0
    e = sorted(p["engagement"] for p in ps)
    q1, q3 = e[int(0.25 * (len(e) - 1))], e[int(0.75 * (len(e) - 1))]
    threshold = max(q3 + an.get("outlier_iqr_k", 3) * (q3 - q1), an.get("outlier_floor", 5))
    atip = sorted([p for p in ps if p["engagement"] > threshold], key=lambda p: -p["engagement"])
    ids = {p["id"] for p in atip}
    return [p for p in ps if p["id"] not in ids], atip, threshold


def channel_block(ps: list[dict], an: dict, start, end) -> dict:
    typical, atip, threshold = split_outliers(ps, an)
    n = len(typical)
    engs = [p["engagement"] for p in typical]
    total = sum(engs)

    type_c, type_e = Counter(), Counter()
    day_c, day_e = [0] * 7, [0] * 7
    hour_c, hour_e = Counter(), Counter()
    week_c, week_e = Counter(), Counter()
    hm_c = [[0] * 24 for _ in range(7)]
    hm_e = [[0] * 24 for _ in range(7)]
    tag_c, tag_e = Counter(), Counter()
    hashtags = Counter()
    for p in typical:
        ts, e = p["ts"], p["engagement"]
        type_c[p["type"]] += 1; type_e[p["type"]] += e
        d, h = ts.weekday(), ts.hour
        day_c[d] += 1; day_e[d] += e
        hour_c[h] += 1; hour_e[h] += e
        hm_c[d][h] += 1; hm_e[d][h] += e
        wk = ts.date() - timedelta(days=ts.weekday())  # lunes de la semana
        week_c[wk] += 1; week_e[wk] += e
        if p.get("tag_primary"):
            tag_c[p["tag_primary"]] += 1; tag_e[p["tag_primary"]] += e
        for t in p.get("hashtags") or []:
            if t:
                hashtags[t.lower()] += 1

    types = sorted(type_c, key=lambda t: -type_c[t])
    hours = sorted(hour_c)
    # Todas las semanas del período, también las que no tuvieron publicaciones
    weeks, wk = [], start.date() - timedelta(days=start.weekday())
    while wk <= end.date():
        weeks.append(wk)
        wk += timedelta(days=7)
    views = [p["views"] for p in typical if p.get("views")]

    ranked = sorted(typical, key=lambda p: -p["engagement"])
    top_n = an.get("top_posts_n", 5)
    top = ranked[:top_n]
    rest = [p for p in ranked[top_n:] if p["engagement"] > 0]
    worst = sorted(rest, key=lambda p: p["engagement"])[:top_n]

    return {
        "n_posts": n,
        "n_atipicos": len(atip),
        "engagement_total": total,
        "engagement_promedio": round(total / n, 1) if n else 0,
        "engagement_mediana": sorted(engs)[n // 2] if n else 0,
        "likes_total": sum(p["likes"] for p in typical),
        "comentarios_total": sum(p["comments"] for p in typical),
        "views_total": sum(views) if views else None,
        "n_con_views": len(views),
        "by_type": {
            "labels": types,
            "counts": [type_c[t] for t in types],
            "engagement_promedio": [round(type_e[t] / type_c[t], 1) for t in types],
        },
        "by_day": {
            "labels": DOW, "counts": day_c,
            "engagement_promedio": [round(day_e[i] / day_c[i], 1) if day_c[i] else 0 for i in range(7)],
        },
        "by_hour": {
            "labels": [str(h) for h in hours],
            "counts": [hour_c[h] for h in hours],
            "engagement_promedio": [round(hour_e[h] / hour_c[h], 1) for h in hours],
        },
        "by_week": {"labels": week_labels(weeks, start.date()), "desde": [w.strftime("%d/%m") for w in weeks],
                    "counts": [week_c[w] for w in weeks], "engagement": [week_e[w] for w in weeks]},
        "by_day_hour": {
            "labels_dow": DOW,
            "matrix": [[round(hm_e[d][h] / hm_c[d][h], 1) if hm_c[d][h] else 0 for h in range(24)]
                       for d in range(7)],
            "counts": hm_c,
        },
        "tag_summary": dict(tag_c),
        "tag_engagement": {k: round(tag_e[k] / tag_c[k], 1) for k in tag_c},
        "top_hashtags": hashtags.most_common(an.get("top_hashtags_n", 10)),
        "captions_avg_len": round(sum(len(p["caption"]) for p in typical) / n, 1) if n else 0,
        "top5": [_post_dict(p) for p in top],
        "worst5": [_post_dict(p) for p in worst],
        # Solo las 5 atípicas con más engagement; el total va en n_atipicos
        "atipicos": [_post_dict(p) for p in atip[:top_n]],
        "outlier_threshold": threshold,
    }


MES_CORTO = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def week_labels(mondays: list, period_start) -> list[str]:
    """'Jun S1', 'Jun S2', …: mes y número de semana dentro del mes (lunes a domingo)."""
    out, seen = [], Counter()
    for w in mondays:
        ref = max(w, period_start)
        seen[(ref.year, ref.month)] += 1
        out.append(f"{MES_CORTO[ref.month]} S{seen[(ref.year, ref.month)]}")
    return out


def category_analysis(ps: list[dict], an: dict) -> dict:
    """Mix y desempeño por categoría sobre las publicaciones típicas de UNA cuenta."""
    typical, _, _ = split_outliers(ps, an)
    rows = [{"cat": p["tag_primary"], "type": p["type"], "eng": p["engagement"]}
            for p in typical if p.get("tag_primary")]
    if not rows:
        return {}
    total = len(rows)
    global_mean = sum(r["eng"] for r in rows) / total
    counts = Counter(r["cat"] for r in rows)
    mix = [{"category": c, "count": k, "pct": round(k / total * 100, 1)} for c, k in counts.most_common()]
    cats = [m["category"] for m in mix]

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["cat"]].append(r["eng"])
    perf = []
    for c, e in by_cat.items():
        perf.append({"category": c, "n": len(e), "mean": round(sum(e) / len(e), 1),
                     "median": statistics.median(e)})
    perf.sort(key=lambda x: -x["mean"])

    types = sorted({r["type"] for r in rows})
    cell = defaultdict(list)
    for r in rows:
        cell[(r["cat"], r["type"])].append(r["eng"])
    by_type = [[round(sum(cell[(c, t)]) / len(cell[(c, t)]), 1) if cell[(c, t)] else 0 for t in types]
               for c in cats]

    perf_by = {x["category"]: x["mean"] for x in perf}
    gaps = []
    for m in sorted([m for m in mix if m["pct"] < 10], key=lambda m: -perf_by[m["category"]])[:5]:
        avg = perf_by[m["category"]]
        if avg > global_mean:
            gaps.append({"category": m["category"], "pct_of_mix": m["pct"], "count": m["count"],
                         "avg_engagement": avg, "vs_global": round(avg / global_mean, 2)})
    return {
        "total_posts": total,
        "global_mean": round(global_mean, 1),
        "mix_global": mix,
        "performance": perf,
        "by_type": {"categories": cats, "cols": types, "matrix": by_type},
        "gaps": gaps,
    }


def _pct(cur, prev):
    if cur is None or prev is None:
        return None
    if prev == 0:
        return None if cur == 0 else 100.0
    return round((cur - prev) / prev * 100, 1)


def compute_deltas(b: dict, pb: dict, acc: dict, prev_acc: dict) -> dict:
    """Variación de UNA cuenta frente al mes anterior."""
    cf, pf = (acc or {}).get("seguidores"), (prev_acc or {}).get("seguidores")
    return {
        "n_posts_pct": _pct(b["n_posts"], pb["n_posts"]),
        "engagement_total_pct": _pct(b["engagement_total"], pb["engagement_total"]),
        "engagement_promedio_pct": _pct(b["engagement_promedio"], pb["engagement_promedio"]),
        "audience_pct": _pct(cf, pf),
        "audience_abs": (cf - pf) if (cf is not None and pf is not None) else None,
        "n_posts_abs": b["n_posts"] - pb["n_posts"],
        "engagement_total_abs": b["engagement_total"] - pb["engagement_total"],
        "engagement_promedio_abs": round(b["engagement_promedio"] - pb["engagement_promedio"], 1),
    }


def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", ".")


def resumen_html(ch: dict, b: dict, acc: dict, periodo: str) -> str:
    """Párrafo del resumen ejecutivo de UNA cuenta, solo con datos del período."""
    if not b["n_posts"]:
        return f"Sin publicaciones típicas de <strong>@{ch['handle']}</strong> en {periodo}."
    txt = (f"<strong>En {periodo}, @{ch['handle']} publicó {b['n_posts']} piezas orgánicas "
           f"({_fmt(b['engagement_total'])} interacciones, {_fmt(b['engagement_promedio'])} por publicación).</strong>")
    seg = (acc or {}).get("seguidores")
    if seg:
        er = f"{b['engagement_promedio'] / seg * 100:.2f}".replace(".", ",")
        txt += f" Con {_fmt(seg)} seguidores, su tasa de engagement es {er} %."
    tp = b["by_type"]
    if len(tp["labels"]) > 1:
        best = tp["labels"][tp["engagement_promedio"].index(max(tp["engagement_promedio"]))]
        txt += f" El formato que más conecta es {TYPE_ES.get(best, best).lower()}."
    if b["n_atipicos"]:
        n = b["n_atipicos"]
        txt += (f" {n} {'publicaciones atípicas' if n != 1 else 'publicación atípica'} "
                f"(posible pauta) se analiza{'n' if n != 1 else ''} aparte.")
    return txt


TYPE_ES = {"Image": "la foto", "Sidecar": "el carrusel", "Video": "el reel"}


# ─────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────
def _js(obj) -> str:
    # "</" escapado para que ningún caption cierre el <script>
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def client_meta(cfg: dict) -> dict:
    return {
        "name": cfg["client"]["name"],
        "brand": cfg["branding"],
        "diagnostico_id": cfg["periods"]["diagnostico"]["id"],
        "channels": [{k: ch[k] for k in ("key", "platform", "label", "short", "handle", "url", "color")}
                     for ch in cfg["channels"]],
    }


def render(cfg: dict, data: dict, meta: dict, out: Path) -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in (("__CLIENT__", client_meta(cfg)), ("__REPORT_META__", meta), ("__DATA__", data)):
        if marker not in html:
            raise RuntimeError(f"Falta el marcador {marker} en {TEMPLATE.name}")
        html = html.replace(marker, _js(value), 1)
    html = (html.replace("{{CLIENT_NAME}}", cfg["client"]["name"])
                .replace("{{BRAND_PRIMARY}}", cfg["branding"]["primary"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  ✓ {out.relative_to(ROOT)} ({len(html) // 1024} KB)")


def build_period(cfg, sb, all_posts, period, available) -> bool:
    client_id = cfg["client"]["id"]
    diag_id = cfg["periods"]["diagnostico"]["id"]
    an = cfg.get("analytics", {})
    start, end = P.window(cfg, period)
    print(f"\n═══ {P.label(cfg, period)} ({start.date()} → {end.date()}) ═══")

    posts = in_window(all_posts, start, end)
    by_ch = {ch["key"]: [p for p in posts if p["platform"] == ch["key"]] for ch in cfg["channels"]}
    for ch in cfg["channels"]:
        print(f"  {ch['label']}: {len(by_ch[ch['key']])} publicaciones en el período")

    tag_posts(posts, cfg, sb)

    accounts = load_accounts(sb, client_id, period, diag_id)
    # En un mes en curso la cadencia se mide sobre los días transcurridos
    now = datetime.now(P.BOGOTA)
    corte = min(end, now) if now >= start else end
    days = (corte.date() - start.date()).days + 1
    semanas = round(days / 7, 1)

    prev = P.previous(cfg, period)
    if prev:
        ps, pe = P.window(cfg, prev)
        prev_posts = in_window(all_posts, ps, pe)
        prev_acc = load_accounts(sb, client_id, prev, diag_id)

    followers_hist = followers_by_month(sb, client_id)

    # Cada cuenta es un reporte independiente: nada se suma ni se mezcla entre cuentas
    channels = {}
    for ch in cfg["channels"]:
        key = ch["key"]
        b = channel_block(by_ch[key], an, start, end)
        b["account"] = accounts.get(key) or {}
        b["tendencia"] = monthly_trend(cfg, key, all_posts, followers_hist.get(key, {}), end, an)
        b["category_analysis"] = category_analysis(by_ch[key], an)
        b["deltas"] = None
        if prev:
            pb = channel_block([p for p in prev_posts if p["platform"] == key], an, ps, pe)
            b["deltas"] = compute_deltas(b, pb, b["account"], prev_acc.get(key))
        b["resumen_html"] = resumen_html(ch, b, b["account"], P.short_label(cfg, period))
        channels[key] = b

    data = {
        "generated_at": datetime.now(P.BOGOTA).strftime("%d/%m/%Y"),
        "periodo_id": period,
        "periodo_label": P.label(cfg, period),
        "periodo_short": P.short_label(cfg, period),
        "is_baseline": period == diag_id,
        "prev_label": P.label(cfg, prev) if prev else None,
        "ventana": {"desde": start.date().isoformat(), "hasta": end.date().isoformat()},
        "semanas": semanas,
        "channels": channels,
    }

    for ch in cfg["channels"]:
        try:
            channels[ch["key"]]["hallazgos_llm"] = hallazgos_llm.generate(
                data, ch, cfg, os.environ.get("GEMINI_API_KEY"))
        except Exception as exc:
            print(f"  ⚠ hallazgos_llm {ch['key']} falló: {exc}")
            channels[ch["key"]]["hallazgos_llm"] = None

    meta = {"current": period, "available": available}
    folder = "diagnostico" if period == diag_id else period
    render(cfg, data, meta, ROOT / folder / "index.html")
    return True


def write_landing(latest_url: str) -> None:
    (ROOT / "index.html").write_text(
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0; url={latest_url}">'
        '<meta name="robots" content="noindex"><title>Dashboard</title></head>'
        f'<body><a href="{latest_url}">Ir al reporte más reciente</a></body></html>\n',
        encoding="utf-8")
    print(f"  ✓ index.html → {latest_url}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="sendwave")
    parser.add_argument("--period", default="all")
    args = parser.parse_args()

    cfg = load_client(args.client)
    client_id = cfg["client"]["id"]
    diag_id = cfg["periods"]["diagnostico"]["id"]
    sb = Supabase()

    period_ids = sorted({r["id"] for r in sb.select("periods", filter=f"client_id=eq.{client_id}")},
                        key=lambda p: P.sort_key(cfg, p))
    if not period_ids:
        print("✗ No hay períodos en Supabase. Corre primero la ingesta del diagnóstico.")
        return 1
    available = [{"id": p, "label": P.label(cfg, p),
                  "url": "/diagnostico/" if p == diag_id else f"/{p}/"} for p in period_ids]
    targets = period_ids if args.period == "all" else [args.period]

    all_posts = load_posts(sb, client_id)
    print(f"Publicaciones en Supabase: {len(all_posts)} · períodos: {period_ids}")

    ok = 0
    for period in targets:
        try:
            ok += build_period(cfg, sb, all_posts, period, available)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  ✗ {period} falló: {exc}")
    write_landing(available[-1]["url"])
    print(f"\n═══ {ok}/{len(targets)} períodos generados ═══")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
