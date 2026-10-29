"""Normalize: convierte items raw de cada plataforma al shape canónico interno.

Cada función `normalize_<plataforma>(items)` retorna:
  {
    "account": {...},         # snapshot del perfil
    "posts": [                # lista normalizada de posts
      {
        "id": str,
        "url": str,
        "type": str,           # "Image" | "Video" | "Sidecar" | "Album" | ...
        "caption": str,
        "hashtags": [str],
        "timestamp": datetime, # tz-aware UTC
        "likes": int,
        "comments": int,
        "shares": int,         # 0 si no aplica
        "engagement": int,     # sum de los tres anteriores (con regla por plataforma)
        "extra": dict,         # campos específicos (playCount, etc.)
        "media_url": str | None,  # URL del thumbnail
      },
      ...
    ]
  }

Mantenerlas independientes hace que un cambio en el shape de un actor solo afecte
a su normalizador correspondiente.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pipeline.util.log import get_logger

log = get_logger(__name__)


@dataclass
class NormalizedPost:
    id: str
    url: str
    type: str
    caption: str
    hashtags: list[str]
    timestamp: datetime | None
    likes: int
    comments: int
    shares: int
    engagement: int
    extra: dict[str, Any]
    media_url: str | None


def _to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # heurística: si es muy grande, está en ms
        if value > 1e12:
            value /= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _hashtags_from_caption(caption: str) -> list[str]:
    return re.findall(r"#([A-Za-zÁÉÍÓÚáéíóúÑñ0-9_]+)", caption or "")


# =============================================================
# INSTAGRAM (apify/instagram-scraper)
# =============================================================
def normalize_instagram(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"account": {}, "posts": []}

    first = items[0]
    meta = first.get("metaData") or {}
    account = {
        "plataforma": "Instagram",
        "username": meta.get("username") or first.get("ownerUsername", ""),
        "nombre": meta.get("fullName", ""),
        "bio": meta.get("biography", ""),
        "categoria": meta.get("businessCategoryName", ""),
        "seguidores": str(meta.get("followersCount") or ""),
        "siguiendo": str(meta.get("followsCount") or ""),
        "posts_totales": str(meta.get("postsCount") or ""),
        "business_account": str(meta.get("isBusinessAccount", "")),
        "verified": str(meta.get("verified", "")),
        "url_externa": meta.get("externalUrl", ""),
        "direccion": (meta.get("businessAddress") or {}).get("street_address", ""),
        "telefono": "",
        "rating": "",
        "page_likes": "",
        "website": "",
        "views_totales_90d": "",
        "snapshot_fecha": datetime.now(timezone.utc).date().isoformat(),
    }

    posts: list[NormalizedPost] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        caption = item.get("caption") or ""
        hashtags = item.get("hashtags") or _hashtags_from_caption(caption)
        # Instagram devuelve likesCount = -1 cuando la cuenta oculta los likes
        likes = max(int(item.get("likesCount") or 0), 0)
        comments = max(int(item.get("commentsCount") or 0), 0)
        posts.append(
            NormalizedPost(
                id=str(item["id"]),
                url=item.get("url", ""),
                type=item.get("type") or "Unknown",
                caption=caption,
                hashtags=list(hashtags),
                timestamp=_to_dt(item.get("timestamp")),
                likes=likes,
                comments=comments,
                shares=0,
                engagement=likes + comments,
                extra={
                    "shortCode": item.get("shortCode"),
                    # Reproducciones de reels: se guardan aparte, no suman al engagement
                    "playCount": item.get("videoPlayCount") or item.get("videoViewCount"),
                    "productType": item.get("productType"),
                    "isPinned": bool(item.get("isPinned", False)),
                },
                media_url=item.get("displayUrl"),
            )
        )
    return {"account": account, "posts": [p.__dict__ for p in posts]}


# =============================================================
# FACEBOOK POSTS (apify/facebook-posts-scraper)
# =============================================================
def normalize_facebook_posts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Solo posts. El perfil viene del actor de Pages aparte (`normalize_facebook_page`)."""
    posts: list[NormalizedPost] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("postId"):
            continue
        media = item.get("media") or [{}]
        first_media = media[0] if media else {}
        media_type = first_media.get("__typename") or "Text"
        likes = int(item.get("likes") or 0)
        shares = int(item.get("shares") or 0)
        # FB Posts scraper a veces no devuelve comments. Asumimos 0 si falta.
        comments = int(item.get("commentsCount") or 0)
        caption = item.get("text") or ""
        posts.append(
            NormalizedPost(
                id=str(item["postId"]),
                url=item.get("topLevelUrl") or item.get("url", ""),
                type=media_type,
                caption=caption,
                hashtags=_hashtags_from_caption(caption),
                timestamp=_to_dt(item.get("time")),
                likes=likes,
                comments=comments,
                shares=shares,
                engagement=likes + comments + shares,
                extra={"pageName": item.get("pageName")},
                media_url=first_media.get("thumbnail"),
            )
        )
    return [p.__dict__ for p in posts]


def normalize_facebook_page(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    p = items[0]
    return {
        "plataforma": "Facebook",
        "username": p.get("pageName", ""),
        "nombre": p.get("title", ""),
        "bio": p.get("intro", ""),
        "categoria": ", ".join(p.get("categories") or []),
        "seguidores": str(p.get("followers") or ""),
        "siguiendo": str(p.get("followings") or ""),
        "posts_totales": "",
        "business_account": "",
        "verified": "",
        "url_externa": p.get("website") or "",
        "direccion": p.get("address", ""),
        "telefono": p.get("phone", ""),
        "rating": p.get("rating", ""),
        "page_likes": str(p.get("likes") or ""),
        "website": p.get("website") or "",
        "views_totales_90d": "",
        "snapshot_fecha": datetime.now(timezone.utc).date().isoformat(),
    }


# =============================================================
# TIKTOK (clockworks/tiktok-scraper)
# =============================================================
def normalize_tiktok(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"account": {}, "posts": []}

    first = items[0]
    author = first.get("authorMeta") or {}
    account = {
        "plataforma": "TikTok",
        "username": author.get("name", ""),
        "nombre": author.get("nickName", ""),
        "bio": author.get("signature", ""),
        "categoria": (author.get("commerceUserInfo") or {}).get("category", ""),
        "seguidores": str(author.get("fans") or ""),
        "siguiendo": str(author.get("following") or ""),
        "posts_totales": str(author.get("video") or ""),
        "business_account": str((author.get("commerceUserInfo") or {}).get("commerceUser", "")),
        "verified": str(author.get("verified", "")),
        "url_externa": author.get("bioLink", "") or "",
        "direccion": "",
        "telefono": "",
        "rating": "",
        "page_likes": "",
        "website": "",
        "views_totales_90d": str(sum(int(it.get("playCount") or 0) for it in items)),
        "snapshot_fecha": datetime.now(timezone.utc).date().isoformat(),
    }

    posts: list[NormalizedPost] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        # filtrar ads/sponsored para engagement orgánico
        if item.get("isAd") or item.get("isSponsored"):
            continue
        caption = item.get("text") or ""
        hashtags_raw = item.get("hashtags") or []
        hashtags = [h.get("name", "") for h in hashtags_raw if h.get("name")]
        likes = int(item.get("diggCount") or 0)
        comments = int(item.get("commentCount") or 0)
        shares = int(item.get("shareCount") or 0)
        video_meta = item.get("videoMeta") or {}
        posts.append(
            NormalizedPost(
                id=str(item["id"]),
                url=item.get("webVideoUrl", ""),
                type="Slideshow" if item.get("isSlideshow") else "Video",
                caption=caption,
                hashtags=hashtags,
                timestamp=_to_dt(item.get("createTimeISO") or item.get("createTime")),
                likes=likes,
                comments=comments,
                shares=shares,
                engagement=likes + comments + shares,
                extra={
                    "playCount": int(item.get("playCount") or 0),
                    "collectCount": int(item.get("collectCount") or 0),
                    "duration": video_meta.get("duration"),
                    "isPinned": bool(item.get("isPinned")),
                },
                media_url=video_meta.get("coverUrl"),
            )
        )
    return {"account": account, "posts": [p.__dict__ for p in posts]}


# =============================================================
# LINKEDIN (harvestapi/linkedin-company-posts)
# =============================================================
def normalize_linkedin(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"account": {}, "posts": []}

    first = items[0]
    author = first.get("author") or {}
    # parsear "5,020 followers" → 5020
    info_text = author.get("info") or ""
    m = re.match(r"([\d,]+)\s+followers", info_text)
    followers = int(m.group(1).replace(",", "")) if m else None

    account = {
        "plataforma": "LinkedIn",
        "username": author.get("universalName", ""),
        "nombre": author.get("name", ""),
        "bio": "",
        "categoria": "",
        "seguidores": str(followers) if followers else "",
        "siguiendo": "",
        "posts_totales": "",
        "business_account": "",
        "verified": "",
        "url_externa": author.get("website") or "",
        "direccion": "",
        "telefono": "",
        "rating": "",
        "page_likes": "",
        "website": author.get("website") or "",
        "views_totales_90d": "",
        "snapshot_fecha": datetime.now(timezone.utc).date().isoformat(),
    }

    posts: list[NormalizedPost] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        engagement = item.get("engagement") or {}
        likes = int(engagement.get("likes") or 0)
        comments = int(engagement.get("comments") or 0)
        shares = int(engagement.get("shares") or 0)
        content = item.get("content") or ""
        # tipo inferido del contenido
        if item.get("postVideo"):
            tipo = "Video"
        elif item.get("postImages"):
            tipo = "Image"
        else:
            tipo = "post"
        media_url = None
        if item.get("postVideo"):
            media_url = (item.get("postVideo") or {}).get("thumbnailUrl")
        elif item.get("postImages"):
            first_img = (item.get("postImages") or [{}])[0]
            media_url = (first_img.get("url") if isinstance(first_img, dict)
                         else first_img) if first_img else None

        posts.append(
            NormalizedPost(
                id=str(item["id"]),
                url=item.get("linkedinUrl", ""),
                type=tipo,
                caption=content,
                hashtags=_hashtags_from_caption(content),
                timestamp=_to_dt((item.get("postedAt") or {}).get("date") or
                                 (item.get("postedAt") or {}).get("timestamp")),
                likes=likes,
                comments=comments,
                shares=shares,
                engagement=likes + comments + shares,
                extra={"reactions_breakdown": engagement.get("reactions", [])},
                media_url=media_url,
            )
        )
    return {"account": account, "posts": [p.__dict__ for p in posts]}
