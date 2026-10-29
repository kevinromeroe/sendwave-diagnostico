"""Cliente Supabase liviano — usa REST API con urllib (sin dependencia del SDK).

Operaciones soportadas:
- upsert(table, rows, on_conflict): inserta o actualiza por columnas key
- select(table, filter, columns): lee filas
- delete(table, filter): borra filas

Auth con SUPABASE_URL + SUPABASE_SERVICE_KEY del env. Si no están, lanza.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from pipeline.util.log import get_logger

log = get_logger(__name__)


class SupabaseError(RuntimeError):
    pass


def _env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SupabaseError(f"Env var {name} no está definida.")
    return v


class Supabase:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or _env_or_fail("SUPABASE_URL")).rstrip("/")
        self.key = key or _env_or_fail("SUPABASE_SERVICE_KEY")
        self.headers_base = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _request(self, method: str, path: str, body: Any = None,
                 extra_headers: dict[str, str] | None = None) -> Any:
        url = f"{self.url}/rest/v1{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {**self.headers_base, **(extra_headers or {})}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SupabaseError(f"{method} {path} → HTTP {exc.code}: {detail[:400]}")
        except urllib.error.URLError as exc:
            raise SupabaseError(f"{method} {path} → network error: {exc}")

    # ----- Operaciones de tabla -----
    def upsert(self, table: str, rows: list[dict[str, Any]],
               on_conflict: str | None = None) -> list[dict[str, Any]]:
        """INSERT con ON CONFLICT DO UPDATE en las columnas indicadas en on_conflict."""
        if not rows:
            return []
        path = f"/{table}"
        if on_conflict:
            path += f"?on_conflict={on_conflict}"
        headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
        result = self._request("POST", path, rows, extra_headers=headers)
        log.info("supabase_upsert", extra={"table": table, "rows": len(rows)})
        return result or []

    def select(self, table: str, *, filter: str = "", columns: str = "*",
               limit: int | None = None) -> list[dict[str, Any]]:
        params = {"select": columns}
        if limit:
            params["limit"] = limit
        path = f"/{table}?{urlencode(params)}"
        if filter:
            path += f"&{filter}"
        return self._request("GET", path) or []

    def update(self, table: str, filter: str, patch: dict[str, Any]) -> Any:
        """PATCH parcial sobre filas que matchean filter (no requiere todas las columnas NOT NULL).

        Args:
            table: nombre de la tabla
            filter: query string para identificar filas (ej "id=eq.abc")
            patch: dict con solo las columnas a actualizar
        """
        path = f"/{table}?{filter}"
        return self._request("PATCH", path, patch)


    def delete(self, table: str, filter: str) -> None:
        """filter es la query string ya armada, ej: 'period_id=eq.2026-06'."""
        self._request("DELETE", f"/{table}?{filter}", extra_headers={"Prefer": "return=minimal"})
        log.info("supabase_delete", extra={"table": table, "filter": filter})
