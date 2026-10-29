"""Multi-provider LLM abstraction.

Cada provider implementa la misma interfaz `call(system, user, **kwargs) -> str`.
Permite cambiar de Anthropic a Gemini a OpenAI con una sola línea del YAML
sin tocar `insights_llm.py`.
"""
from __future__ import annotations

from typing import Any

from pipeline.util.log import get_logger

log = get_logger(__name__)


def get_provider(name: str):
    """Factory: retorna la función call() del provider indicado."""
    name = (name or "gemini").lower()
    if name == "gemini":
        from pipeline.transform.llm_providers import gemini
        return gemini.call
    if name == "anthropic":
        from pipeline.transform.llm_providers import anthropic
        return anthropic.call
    raise ValueError(f"Provider LLM desconocido: {name}")
