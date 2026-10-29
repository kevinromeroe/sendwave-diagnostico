#!/usr/bin/env python3
"""render_pdf.py — Renderiza los informes HTML a PDF limpio con Chromium.

Genera un PDF por cuenta del período indicado (ej. 2026-11/reporte-ig_latam.pdf) con los gráficos
ya dibujados, todas las pestañas desplegadas, en A4 horizontal y con colores.
Resuelve el "PDF deforme" que produce el diálogo de impresión del navegador.

Por defecto renderiza el mes actual (zona Bogotá) y, en los primeros 7 días
del mes, también el anterior (cierre de mes). Se pueden pasar períodos
explícitos para regenerar meses viejos puntualmente.

Requiere un servidor HTTP sirviendo la raíz del repo en localhost:8000
(el workflow lo levanta con `python -m http.server 8000`).

Uso:
    python scripts/render_pdf.py            # AUTO: mes actual (+ anterior los días 1–7)
    python scripts/render_pdf.py 2026-07    # un mes específico
    python scripts/render_pdf.py 2026-06 2026-07 diagnostico   # varios (backfill)
"""
import os
import sys
import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PDF_BASE_URL", "http://localhost:8000")


def periodos_auto():
    """Mes actual (Bogotá) + el anterior si estamos en los primeros 7 días.

    Mismo criterio que la ingesta (cierre de mes), con la fecha de hace 12 h
    para absorber los retrasos del cron de GitHub. Los meses sin carpeta
    (p. ej. antes del primer reporte mensual) se saltan en render().
    """
    tz = datetime.timezone(datetime.timedelta(hours=-5))  # America/Bogota, sin DST
    ref = datetime.datetime.now(tz) - datetime.timedelta(hours=12)
    periods = [ref.strftime("%Y-%m")]
    if ref.day <= 7:
        prev = ref.replace(day=1) - datetime.timedelta(days=1)
        periods.insert(0, prev.strftime("%Y-%m"))
    return periods


def _open(page, url):
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as exc:
        print(f"  ⚠ networkidle timeout ({exc}); continúo igual")
    page.wait_for_timeout(3000)  # dejar que Chart.js dibuje la pestaña inicial


def render(periods):
    """Un PDF por período y por cuenta: <período>/reporte-<cuenta>.pdf"""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        for per in periods:
            if not (Path(per) / "index.html").exists():
                print(f"  ⚠ {per}/index.html no existe — lo salto")
                continue
            _open(page, f"{BASE}/{per}/")
            accounts = page.evaluate("() => CLIENT.channels.map(c => c.key)")
            for acc in accounts:
                url = f"{BASE}/{per}/?cuenta={acc}"
                print(f"→ Renderizando {url}")
                _open(page, url)
                # 1) Clic en cada pestaña visible para que Chart.js inicialice sus gráficos
                tabs = page.eval_on_selector_all(
                    "nav.tabs button[data-tab]:not([hidden])", "els => els.map(e => e.dataset.tab)")
                for tab in tabs:
                    try:
                        page.click(f'nav.tabs button[data-tab="{tab}"]', timeout=4000)
                        page.wait_for_timeout(900)
                    except Exception as exc:
                        print(f"  ⚠ no pude activar pestaña '{tab}': {exc}")
                # 2) Mostrar todas las pestañas y redibujar los gráficos al tamaño de impresión
                page.evaluate(
                    """() => {
                        document.querySelectorAll('.tab').forEach(t => t.style.display = 'block');
                        document.querySelectorAll('canvas').forEach(cv => {
                            try {
                                const ch = (window.Chart && Chart.getChart) ? Chart.getChart(cv) : null;
                                if (ch) { ch.resize(); ch.update('none'); }
                            } catch (e) {}
                        });
                    }"""
                )
                # Esperar a que carguen las miniaturas de las publicaciones (máx. 20 s)
                page.evaluate(
                    """() => Promise.race([
                        Promise.all([...document.images].map(i => i.complete ? null
                            : new Promise(r => { i.onload = i.onerror = r; }))),
                        new Promise(r => setTimeout(r, 20000)),
                    ])"""
                )
                page.wait_for_timeout(2500)
                out = Path(per) / f"reporte-{acc}.pdf"
                page.pdf(
                    path=str(out),
                    format="A4",
                    landscape=True,
                    print_background=True,
                    margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
                    prefer_css_page_size=True,
                )
                print(f"  ✓ {out} ({out.stat().st_size // 1024} KB)")
        browser.close()


def main():
    periods = sys.argv[1:] or periodos_auto()
    print(f"Períodos a renderizar: {periods}")
    render(periods)
    print("\n[DONE] PDF(s) generado(s).")


if __name__ == "__main__":
    main()
