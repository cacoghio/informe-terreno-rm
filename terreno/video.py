"""render_video: video de 30-60 s del visor 3D (spec sec. 6, Task 12).

Captura determinista: ``viewer/tour.js`` expone ``window.__tour.seek(t)`` (todo el estado del visor es funcion de t) y
``viewer/tools/capture.mjs`` (Node + @playwright/test, ya instalados en ``viewer/node_modules``) toma una foto PNG por
fotograma y las pasa por stdin a ffmpeg (libx264, yuv420p). WebGL va por Chromium headless con SwiftShader (software);
``TERRENO_GL=d3d11`` usa la GPU si existe. Escribe ``<out>.render.json`` con los tiempos reales (insumo del criterio 9).
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from terreno.errors import RejectRequest

VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"
CAPTURE_SCRIPT = VIEWER_DIR / "tools" / "capture.mjs"
MIN_SECONDS, MAX_SECONDS = 30, 60
DEFAULT_FPS = 12
SIZE = (1440, 900)


@contextlib.contextmanager
def serve_viewer(root: Path = VIEWER_DIR):
    """Sirve ``root`` (por defecto poc/viewer) en 127.0.0.1 con un puerto libre; devuelve la URL base."""

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):  # sin ruido en la salida de pytest
            pass

    handler = functools.partial(Quiet, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def viewer_url(base: str, site: str, **params: str) -> str:
    """URL del visor para una carpeta de salida (ruta relativa al servidor). Sin datos personales."""
    q = "&".join([f"site={site}", *[f"{k}={v}" for k, v in params.items()]])
    return f"{base.rstrip('/')}/index.html?{q}"


def render_video(viewer_url: str, out: Path, seconds: int = 45, *, fps: int = DEFAULT_FPS, timeout_s: int = 1800) -> Path:
    """Graba el recorrido del visor en ``viewer_url`` y lo guarda como MP4 (H.264) de ``seconds`` s (30-60)."""
    if not (MIN_SECONDS <= seconds <= MAX_SECONDS):
        raise RejectRequest(f"el video debe durar entre {MIN_SECONDS} y {MAX_SECONDS} s (pedido: {seconds})")
    if not viewer_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise RejectRequest("el visor para el video debe servirse en localhost (file:// esta bloqueado)")
    node, ffmpeg = shutil.which("node"), shutil.which("ffmpeg")
    if not node or not ffmpeg:
        raise RuntimeError(f"faltan herramientas: node={node} ffmpeg={ffmpeg}")
    if not (VIEWER_DIR / "node_modules" / "@playwright").exists():
        raise RuntimeError("falta viewer/node_modules: ejecuta `npm install` y `npx playwright install chromium` en poc/viewer")
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [node, str(CAPTURE_SCRIPT), viewer_url, str(out), str(seconds), str(fps), str(SIZE[0]), str(SIZE[1])],
        capture_output=True, text=True, timeout=timeout_s, cwd=str(VIEWER_DIR), env={**os.environ},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"captura de video fallo ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    if stats.get("problems"):
        raise RuntimeError(f"el visor reporto errores durante la captura: {stats['problems']}")
    out.with_suffix(".render.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    return out
