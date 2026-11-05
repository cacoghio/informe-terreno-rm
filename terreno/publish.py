"""publish_viewer: copia el visor estatico y los archivos de datos a una carpeta lista para Cloudflare Pages.

Solo copia una lista blanca de archivos (visor + terreno.glb, predio.geojson, manifest.json saneado, layers/*): nunca la
carpeta de trabajo, la cache, los DEM, el GeoPackage ni rejection.json. El manifiesto publicado conserva ``result_hash`` (el
hash excluye lo volatil) pero pierde rutas locales, ejecutable de Python y plataforma. Sin datos del cliente en la URL.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from terreno.errors import RejectRequest
from terreno.glb import GLB_MAX_BYTES, GLB_NAME, LAYERS_INDEX, PREDIO_NAME
from terreno.manifest import MANIFEST_NAME, hash_manifest

VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"
PUBLISH_DIR = "publicado"
DATA_SUBDIR = "datos"
STATIC_FILES = ("index.html", "app.js", "style.css", "sun.js", "texts.js", "tour.js", "legend.json")
STATIC_DIRS = ("vendor",)
LAYER_FILES = ("pendiente.png", "orientacion.png", "curvas.png", "sombreado.png", LAYERS_INDEX)
KEEP_VOLATILE = ("generated_at", "acquired")


def sanitized_manifest(manifest: dict) -> dict:
    m = json.loads(json.dumps(manifest))
    vol = m.get("volatile", {})
    m["volatile"] = {k: vol[k] for k in KEEP_VOLATILE if k in vol}
    return m


def publish_viewer(manifest: dict, out_dir: Path, *, viewer_dir: Path = VIEWER_DIR) -> Path:
    """Escribe ``out_dir/publicado/`` (visor en la raiz, datos en ``datos/``); devuelve la carpeta. Abrir con
    ``index.html?site=datos``. Rechaza si falta algo del contrato (GLB, predio, capas), si el manifiesto fue alterado o si
    algun archivo supera 25 MiB."""
    out_dir, viewer_dir = Path(out_dir), Path(viewer_dir)
    if (out_dir / "rejection.json").exists():
        raise RejectRequest("hay un rejection.json en la carpeta de salida: no se publica un pedido rechazado")
    if manifest.get("result_hash") != hash_manifest(manifest):
        raise RejectRequest("manifiesto alterado: result_hash no coincide con su contenido")
    need = [out_dir / GLB_NAME, out_dir / PREDIO_NAME] + [out_dir / "layers" / f for f in LAYER_FILES]
    missing = [str(p.relative_to(out_dir)) for p in need if not p.exists()]
    if missing:
        raise RejectRequest(f"faltan archivos para publicar (ejecutar build_glb antes): {missing}")
    for f in STATIC_FILES:
        if not (viewer_dir / f).exists():
            raise RejectRequest(f"falta el archivo del visor: {f}")
    for d in STATIC_DIRS:
        if not (viewer_dir / d).is_dir():
            raise RejectRequest(f"falta la carpeta del visor: {d}")

    pub = out_dir / PUBLISH_DIR
    if pub.exists():
        shutil.rmtree(pub)  # carpeta generada por esta misma funcion
    data = pub / DATA_SUBDIR
    (data / "layers").mkdir(parents=True)
    for f in STATIC_FILES:
        shutil.copy2(viewer_dir / f, pub / f)
    for d in STATIC_DIRS:
        shutil.copytree(viewer_dir / d, pub / d)
    shutil.copy2(out_dir / GLB_NAME, data / GLB_NAME)
    shutil.copy2(out_dir / PREDIO_NAME, data / PREDIO_NAME)
    for f in LAYER_FILES:
        shutil.copy2(out_dir / "layers" / f, data / "layers" / f)
    (data / MANIFEST_NAME).write_text(
        json.dumps(sanitized_manifest(manifest), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    too_big = [str(p.relative_to(pub)) for p in pub.rglob("*") if p.is_file() and p.stat().st_size > GLB_MAX_BYTES]
    if too_big:
        shutil.rmtree(pub)
        raise RejectRequest(f"archivos sobre 25 MiB (limite de Cloudflare Pages): {too_big}")
    return pub
