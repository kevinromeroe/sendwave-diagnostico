"""Períodos del cliente: ventanas en hora Bogotá, etiquetas y orden.

Dos tipos de período:
  - "diagnostico": línea base con rango fijo definido en el YAML del cliente.
  - "YYYY-MM": un mes calendario, válido desde `periods.monthly_start`.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

BOGOTA = timezone(timedelta(hours=-5))  # America/Bogota, sin DST

MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def is_month(period: str) -> bool:
    return len(period) == 7 and period[4] == "-" and period[:4].isdigit() and period[5:].isdigit()


def window(cfg: dict, period: str) -> tuple[datetime, datetime]:
    """(inicio, fin) del período, ambos inclusive, en hora Bogotá."""
    diag = cfg["periods"]["diagnostico"]
    if period == diag["id"]:
        d0 = datetime.fromisoformat(diag["starts_on"])
        d1 = datetime.fromisoformat(diag["ends_on"])
    elif is_month(period):
        y, m = int(period[:4]), int(period[5:])
        d0 = datetime(y, m, 1)
        d1 = datetime(y, m, calendar.monthrange(y, m)[1])
    else:
        raise ValueError(f"Período inválido: '{period}'. Usar '{diag['id']}' o YYYY-MM.")
    return (d0.replace(tzinfo=BOGOTA),
            d1.replace(hour=23, minute=59, second=59, tzinfo=BOGOTA))


def label(cfg: dict, period: str) -> str:
    diag = cfg["periods"]["diagnostico"]
    if period == diag["id"]:
        return diag["label"]
    return f"{MESES[int(period[5:])]} {period[:4]}"


def short_label(cfg: dict, period: str) -> str:
    """Etiqueta corta para textos corridos ('Jun a Oct 2026', 'Noviembre 2026')."""
    diag = cfg["periods"]["diagnostico"]
    if period == diag["id"]:
        return diag.get("short_label") or diag["label"]
    return label(cfg, period)


def previous(cfg: dict, period: str) -> str | None:
    """Mes anterior de un período mensual. None para el diagnóstico."""
    if not is_month(period):
        return None
    y, m = int(period[:4]), int(period[5:])
    return f"{y - 1}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def validate_monthly(cfg: dict, period: str) -> None:
    """Los reportes mensuales arrancan en `monthly_start`; antes de eso todo es diagnóstico."""
    start = cfg["periods"]["monthly_start"]
    if is_month(period) and period < start:
        raise ValueError(
            f"El período {period} es anterior al primer mes de reporte ({start}); "
            f"ese rango ya está cubierto por el diagnóstico."
        )


def sort_key(cfg: dict, period: str) -> tuple:
    return (0, "") if period == cfg["periods"]["diagnostico"]["id"] else (1, period)


def to_bogota(value) -> datetime | None:
    """Convierte un timestamp ISO (str) o datetime a hora Bogotá."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BOGOTA)
