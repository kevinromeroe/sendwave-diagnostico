"""Provider Gemini (Google AI Studio) — usa la REST API directamente con urllib.

Razones de no usar el SDK oficial `google-generativeai`:
- Una dependencia más en requirements.txt
- El SDK trae mucho overhead que no necesitamos para una llamada simple
- urllib (stdlib) es suficiente y mantiene la imagen liviana

Plan free de Gemini cubre nuestro uso con sobra:
- 15 req/min, 1500 req/día (necesitamos 1 req/mes)
- Gratis sin tarjeta de crédito
- Modelos disponibles: gemini-2.5-flash (default), gemini-2.5-pro, gemini-3.1-*
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from pipeline.util.log import get_logger

log = get_logger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def call(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4000,
    temperature: float = 0.0,
    timeout: int = 60,
) -> str:
    """Llama a Gemini API, retorna el texto de la respuesta.

    Lanza RuntimeError si falla auth o API.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY no definido en env vars.")

    url = f"{BASE_URL}/{model}:generateContent?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            # FIX thinkingBudget: gemini-2.5-flash gasta el presupuesto de tokens
            # "pensando" y devuelve vacio. Con budget 0 responde directo.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "datalitica-pipeline/1.0"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Gemini network error: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Respuesta de Gemini no es JSON: {exc}")

    # Extraer texto del primer candidate
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini no devolvió candidates. Respuesta: {raw[:300]}")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    if not text:
        raise RuntimeError(f"Gemini devolvió texto vacío. Respuesta: {raw[:300]}")

    # Log de uso (no fatal si falta)
    usage = data.get("usageMetadata") or {}
    log.info(
        "gemini_call_ok",
        extra={
            "model": model,
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        },
    )

    return text
