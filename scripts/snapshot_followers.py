#!/usr/bin/env python3
"""Captura de seguidores: guarda una medición por cuenta en followers_history.

Instagram no expone el histórico de seguidores, así que la tendencia mensual
del dashboard se construye con estas mediciones. Corre cada semana (workflow
"Seguidores (captura semanal)") y es barata: solo pide el perfil, sin posts.

Uso:
    python scripts/snapshot_followers.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.extract.apify_client import ApifyClient
from pipeline.load.supabase_client import Supabase
from pipeline.util import periods as P
from pipeline.util.config import load_client, require_env

CAP_USD = 0.10  # por cuenta; una consulta de perfil cuesta centavos


def history_row(client_id: str, key: str, followers, following=None, posts=None) -> dict:
    return {
        "client_id":   client_id,
        "platform":    key,
        "captured_on": datetime.now(P.BOGOTA).date().isoformat(),
        "followers":   int(followers),
        "following_n": int(following) if following not in (None, "") else None,
        "posts_total": int(posts) if posts not in (None, "") else None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_instagram(apify: ApifyClient, ch: dict) -> dict:
    """Perfil de Instagram con el mismo actor de la ingesta, en modo 'details'."""
    items = apify.run_actor_sync(
        ch["apify"]["actor_id"],
        {"directUrls": [ch["url"]], "resultsType": "details", "resultsLimit": 1},
        max_total_charge_usd=CAP_USD,
    )
    prof = next((i for i in items if isinstance(i, dict) and i.get("followersCount") is not None), None)
    if not prof:
        raise RuntimeError("el actor no devolvió followersCount")
    return {"followers": prof["followersCount"], "following": prof.get("followsCount"),
            "posts": prof.get("postsCount")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="sendwave")
    args = parser.parse_args()

    cfg = load_client(args.client)
    client_id = cfg["client"]["id"]
    apify = ApifyClient(token=require_env("APIFY_TOKEN"))
    sb = Supabase()

    rows, failed = [], []
    for ch in cfg["channels"]:
        if ch["platform"] != "instagram":
            print(f"  · {ch['label']}: captura de seguidores aún no implementada para {ch['platform']}")
            continue
        try:
            p = fetch_instagram(apify, ch)
            rows.append(history_row(client_id, ch["key"], p["followers"], p["following"], p["posts"]))
            print(f"  ✓ {ch['label']} (@{ch['handle']}): {p['followers']:,} seguidores".replace(",", "."))
        except Exception as exc:
            failed.append(ch["label"])
            print(f"  ✗ {ch['label']}: {exc}")

    if rows:
        sb.upsert("followers_history", rows, on_conflict="client_id,platform,captured_on")
    if failed:
        print(f"✗ Falló la captura de: {', '.join(failed)}")
        return 1
    print("✅ Captura de seguidores guardada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
