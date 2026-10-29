#!/usr/bin/env python3
"""Ingesta: corre los actores de Apify de cada canal del cliente, filtra al
período y persiste cuentas + publicaciones en Supabase.

Si falla cualquier canal, NO se toca Supabase y el script sale con error: el
dashboard conserva los datos anteriores y el build en cadena no se dispara.

Uso:
    python scripts/ingest.py --period diagnostico     # línea base (rango del YAML)
    python scripts/ingest.py --period 2026-11         # un mes
    python scripts/ingest.py                          # auto: mes actual + cierre del anterior (días 1-7)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.extract.apify_client import ApifyClient
from pipeline.load.supabase_client import Supabase
from pipeline.transform import normalize
from pipeline.util import periods as P
from pipeline.util.config import load_client, require_env
from pipeline.util.log import get_logger

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

log = get_logger("ingest")
ROOT = Path(__file__).resolve().parent.parent
THUMBS = ROOT / "assets" / "thumbs"


# ─────────────────────────────────────────────────────────────
# Extracción por plataforma
# ─────────────────────────────────────────────────────────────
def _actor_input(ch: dict, start: datetime) -> dict:
    inp = dict(ch["apify"]["input"])
    if ch["platform"] == "instagram":
        # Evita pagar por publicaciones viejas que igual se descartan
        inp["onlyPostsNewerThan"] = start.date().isoformat()
    return inp


def extract_channel(apify: ApifyClient, ch: dict, start: datetime) -> dict:
    """Devuelve {account, posts} normalizados para un canal."""
    platform, ax = ch["platform"], ch["apify"]
    cap = float(ax.get("cap_usd", 0.50))
    if platform == "instagram":
        items = apify.run_actor_sync(ax["actor_id"], _actor_input(ch, start), max_total_charge_usd=cap)
        return normalize.normalize_instagram(items)
    if platform == "tiktok":
        items = apify.run_actor_sync(ax["actor_id"], ax["input"], max_total_charge_usd=cap)
        return normalize.normalize_tiktok(items)
    if platform == "linkedin":
        items = apify.run_actor_sync(ax["actor_id"], ax["input"], max_total_charge_usd=cap)
        return normalize.normalize_linkedin(items)
    if platform == "facebook":
        page = apify.run_actor_sync(ax["page"]["actor_id"], ax["page"]["input"], max_total_charge_usd=cap)
        posts = apify.run_actor_sync(ax["posts"]["actor_id"], ax["posts"]["input"], max_total_charge_usd=cap)
        return {"account": normalize.normalize_facebook_page(page),
                "posts": normalize.normalize_facebook_posts(posts)}
    raise ValueError(f"Plataforma no soportada: {platform}")


def filter_window(posts: list[dict], start: datetime, end: datetime) -> list[dict]:
    return [p for p in posts
            if isinstance(p.get("timestamp"), datetime) and start <= p["timestamp"] <= end]


# ─────────────────────────────────────────────────────────────
# Filas Supabase
# ─────────────────────────────────────────────────────────────
def _int(x):
    try:
        return int(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(x):
    if x in (True, "True", "true"):
        return True
    if x in (False, "False", "false"):
        return False
    return None


def account_row(client_id: str, period_id: str, key: str, acc: dict) -> dict:
    return {
        "client_id":    client_id,
        "period_id":    period_id,
        "platform":     key,
        "username":     acc.get("username") or None,
        "display_name": acc.get("nombre") or None,
        "bio":          acc.get("bio") or None,
        "followers":    _int(acc.get("seguidores")),
        "following_n":  _int(acc.get("siguiendo")),
        "posts_total":  _int(acc.get("posts_totales")),
        "verified":     _bool(acc.get("verified")),
        "is_business":  _bool(acc.get("business_account")),
        "category":     acc.get("categoria") or None,
        "external_url": acc.get("url_externa") or None,
        "raw":          acc,
        "snapshot_at":  datetime.now(timezone.utc).isoformat(),
    }


def post_rows(client_id: str, key: str, posts: list[dict]) -> list[dict]:
    rows = []
    for p in posts:
        extra = p.get("extra") or {}
        rows.append({
            "id":         p["id"],
            "client_id":  client_id,
            "platform":   key,
            "url":        p.get("url") or "",
            "type":       p.get("type"),
            "caption":    p.get("caption"),
            "hashtags":   p.get("hashtags") or [],
            "posted_at":  p["timestamp"].isoformat(),
            "likes":      p.get("likes", 0),
            "comments":   p.get("comments", 0),
            "shares":     p.get("shares", 0),
            "views":      _int(extra.get("playCount")),
            "engagement": p.get("engagement", 0),
            "media_url":  p.get("media_url") or None,
            "is_pinned":  bool(extra.get("isPinned", False)),
            "raw":        {k: v for k, v in p.items() if k != "timestamp"},
        })
    return rows


def download_thumb(url: str | None, key: str, post_id: str) -> str | None:
    """Baja y comprime la miniatura (las URLs del CDN expiran en horas)."""
    if not url or not _PIL_OK:
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    fname = f"{key}-{hashlib.sha1(str(post_id).encode()).hexdigest()[:12]}.jpg"
    dest = THUMBS / fname
    if dest.exists():
        return f"/assets/thumbs/{fname}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; DataliticaBot/1.0)",
            "Referer": "https://www.google.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if len(data) < 200:
            return None
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        if img.width > 400:
            img = img.resize((400, int(img.height * 400 / img.width)), Image.LANCZOS)
        img.save(dest, "JPEG", quality=60, optimize=True)
        return f"/assets/thumbs/{fname}"
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
def auto_periods(cfg: dict) -> list[str]:
    """Mes actual (Bogotá) y, del día 1 al 7, también el anterior para cerrarlo
    con sus últimas publicaciones. Se omiten los meses previos al primer reporte."""
    ref = datetime.now(P.BOGOTA) - timedelta(hours=12)  # absorbe retrasos del cron
    months = [ref.strftime("%Y-%m")]
    if ref.day <= 7:
        months.insert(0, (ref.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    return [m for m in months if m >= cfg["periods"]["monthly_start"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="sendwave")
    parser.add_argument("--period", default="auto",
                        help="'diagnostico', YYYY-MM o 'auto' (mes actual + cierre del anterior).")
    args = parser.parse_args()

    cfg = load_client(args.client)
    if args.period == "auto":
        todo = auto_periods(cfg)
        if not todo:
            print(f"Nada que ingestar: los reportes mensuales empiezan en {cfg['periods']['monthly_start']}.")
            return 0
    else:
        todo = [args.period]
    apify = ApifyClient(token=require_env("APIFY_TOKEN"))
    sb = Supabase()
    rc = 0
    for period in todo:
        rc |= ingest_period(cfg, apify, sb, period)
    return rc


def ingest_period(cfg: dict, apify: ApifyClient, sb: Supabase, period: str) -> int:
    client_id = cfg["client"]["id"]
    try:
        P.validate_monthly(cfg, period)
        start, end = P.window(cfg, period)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1

    print("═" * 55)
    print(f"  INGESTA {client_id} · {period} ({start.date()} a {end.date()})")
    print("═" * 55)

    # 1) Extraer TODOS los canales antes de escribir nada
    results: dict[str, dict] = {}
    failed: list[str] = []
    for ch in cfg["channels"]:
        print(f"\n── {ch['label']} (@{ch['handle']}) ──")
        try:
            block = extract_channel(apify, ch, start)
            total = len(block.get("posts") or [])
            block["posts"] = filter_window(block.get("posts") or [], start, end)
            results[ch["key"]] = block
            print(f"  ✓ {len(block['posts'])} publicaciones en el período / {total} traídas")
        except Exception as exc:
            log.error("channel_failed", extra={"channel": ch["key"], "err": str(exc)})
            print(f"  ✗ FALLÓ: {exc}")
            failed.append(ch["label"])

    if failed:
        print(f"\n✗ Ingesta {period} INCOMPLETA — fallaron: {', '.join(failed)}")
        print("  No se modificó Supabase: el dashboard conserva los datos anteriores.")
        return 1

    # 2) Persistir
    print("\n→ Persistiendo en Supabase…")
    sb.upsert("clients", [{"id": client_id, "name": cfg["client"]["name"]}], on_conflict="id")
    sb.upsert("periods", [{
        "client_id":   client_id,
        "id":          period,
        "label":       P.label(cfg, period),
        "starts_on":   start.date().isoformat(),
        "ends_on":     end.date().isoformat(),
        "is_baseline": period == cfg["periods"]["diagnostico"]["id"],
    }], on_conflict="client_id,id")

    now_iso = datetime.now(timezone.utc).isoformat()
    for ch in cfg["channels"]:
        key, block = ch["key"], results[ch["key"]]
        if block.get("account"):
            sb.upsert("accounts", [account_row(client_id, period, key, block["account"])],
                      on_conflict="client_id,period_id,platform")
        posts = block["posts"]
        if posts:
            sb.upsert("posts", post_rows(client_id, key, posts), on_conflict="id")
        ok = ko = 0
        for p in posts:
            local = download_thumb(p.get("media_url"), key, p["id"])
            if local:
                sb.update("posts", f"id=eq.{p['id']}",
                          {"media_url_local": local, "thumbnail_downloaded_at": now_iso})
                ok += 1
            else:
                ko += 1
        print(f"  ✓ {ch['label']}: cuenta + {len(posts)} publicaciones · miniaturas ✓{ok} ✗{ko}")

    print(f"\n✅ Ingesta {period} terminada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
