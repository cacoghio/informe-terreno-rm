"""Manifiesto JSON y hash de resultados (spec sec. 7: "todo numero sale de un manifiesto generado por codigo").

Estructura: todo lo que va fuera de la clave ``volatile`` forma parte del hash de resultados; ``volatile`` agrupa lo que
cambia entre corridas identicas (marcas de tiempo, fecha de descarga, rutas absolutas, tiempos por etapa).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import platform
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

import numpy as np
import rasterio

from terreno.config import RECIPE_VERSION, run_gdal
from terreno.confidence import RULES_PATH

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = "1"
VOLATILE_KEY = "volatile"
EXCLUDED_FROM_HASH = (VOLATILE_KEY, "result_hash")

DATASET_NAMES = {
    "glo30": "Copernicus DEM GLO-30 (DSM, 30 m)",
    "aw3d30": "ALOS World 3D 30 m (AW3D30, JAXA)",
}
# Epoca en que se ADQUIRIO el dato (no la fecha de descarga, que va en ``volatile``). Texto fijo y rastreable: sale del
# manifiesto, no del informe.
ACQUISITION_EPOCH = {
    "glo30": "TanDEM-X 2011-2015",
    "aw3d30": "ALOS PRISM 2006-2011",
}
ACQUISITION_EPOCH_NOTE = {
    "glo30": "DLR/Airbus; segun el Copernicus DEM Product Handbook. La licencia cita 2010-2014 (DLR) y 2014-2018 (Airbus): son anos de produccion.",
    "aw3d30": "JAXA, AW3D30 Product Description.",
}
_GDAL_CLI_VERSION: str | None = None


def _clean(o):
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        if math.isnan(f):
            return "NaN"
        if math.isinf(f):
            return "Infinity" if f > 0 else "-Infinity"
        return round(f, 6)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, Path):
        return str(o)
    if is_dataclass(o) and not isinstance(o, type):
        return _clean(asdict(o))
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    raise TypeError(f"no serializable en el manifiesto: {type(o).__name__}")


def canonical_json(obj) -> str:
    """JSON canonico: claves ordenadas, flotantes a 6 decimales, sin espacios."""
    return json.dumps(_clean(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def raster_digest(path: Path) -> str:
    """SHA-256 de los valores y la georreferencia de un raster (no de los bytes del archivo, que llevan fechas)."""
    with rasterio.open(path) as src:
        arr = src.read(1)
        meta = {"shape": list(arr.shape), "dtype": str(arr.dtype), "crs": src.crs.to_string() if src.crs else None,
                "transform": [round(v, 9) for v in tuple(src.transform)[:6]]}
    h = hashlib.sha256()
    h.update(canonical_json(meta).encode("utf-8"))
    h.update(np.ascontiguousarray(np.nan_to_num(arr.astype("float64"), nan=-1.0e300)).tobytes())
    return h.hexdigest()


def tool_versions() -> dict:
    global _GDAL_CLI_VERSION
    if _GDAL_CLI_VERSION is None:
        try:
            out = run_gdal("gdalinfo", "--version").strip().splitlines()
            _GDAL_CLI_VERSION = out[0] if out else "desconocida"
        except Exception as exc:  # no se adivina: se declara
            _GDAL_CLI_VERSION = f"no disponible ({exc})"
    import pyproj
    import shapely

    return {
        "gdal_cli": _GDAL_CLI_VERSION,
        "gdal_rasterio_wheel": getattr(rasterio, "__gdal_version__", "desconocida"),
        "rasterio": rasterio.__version__,
        "numpy": np.__version__,
        "pyproj": pyproj.__version__,
        "shapely": shapely.__version__,
        "python": platform.python_version(),
    }


def recipe_block(*, hypotheses: list[str]) -> dict:
    rules_text = Path(RULES_PATH).read_text(encoding="utf-8")
    block = {
        "version": RECIPE_VERSION,
        "tools": tool_versions(),
        "confidence_rules_hash": sha256_text(rules_text),
        "hypotheses": sorted(hypotheses),
    }
    block["hash"] = sha256_text(canonical_json({k: v for k, v in block.items()}))
    return block


def dataset_entry(role: str, dem, *, array_sha256: str) -> dict:
    """Ficha de trazabilidad de un dataset de entrada. ``acquired`` va en ``volatile`` (ver build_manifest)."""
    return {
        "role": role,
        "source": dem.source,
        "name": DATASET_NAMES.get(dem.source, dem.source),
        "version": "COG_10 (nombre de tile del bucket copernicus-dem-30m; el tile no declara release)"
        if dem.source == "glo30" else "v3.2 (enero de 2021; JAXA, DOI 10.5069/G94M92HB; servido por OpenTopography globaldem)",
        "acquisition_epoch": ACQUISITION_EPOCH.get(dem.source, "no registrada"),
        "acquisition_epoch_note": ACQUISITION_EPOCH_NOTE.get(dem.source, ""),
        "url": dem.source_url,
        "tiles": list(dem.tiles),
        "tile_urls": list(dem.tile_urls),
        "native_crs": "EPSG:4326",
        "vertical_datum": dem.vertical_datum,
        "nodata_fraction": float(dem.nodata_fraction),
        "array_sha256": array_sha256,
    }


def build_manifest(
    *,
    poly_geojson: dict,
    polygon_area_m2: float,
    centroid: tuple[float, float],
    epsg: int,
    pixel_m: float,
    grid_shape: tuple[int, int],
    datasets: list[dict],
    acquired: dict[str, str],
    terrain_stats: dict,
    comparison: dict,
    aux: dict | None,
    validation: dict | None,
    confidence: dict,
    hypotheses: list[str],
    generated_at: str | None = None,
    paths: dict | None = None,
    elapsed_s: dict | None = None,
    solar: dict | None = None,
    context: dict | None = None,
    worldcover: dict | None = None,
) -> dict:
    recipe = recipe_block(hypotheses=hypotheses)
    inputs = {
        "polygon": poly_geojson,
        "polygon_area_m2": polygon_area_m2,
        "centroid_lonlat": list(centroid),
        "utm_zone_rule": "lon < -72 -> EPSG:32718, lon >= -72 -> EPSG:32719, zona del centroide (por codigo, no por el agente)",
        "datasets": datasets,
    }
    inputs["hash"] = sha256_text(canonical_json({k: v for k, v in inputs.items()}))
    m = {
        "schema_version": SCHEMA_VERSION,
        "recipe": recipe,
        "inputs": inputs,
        "grid": {
            "crs": f"EPSG:{epsg}", "epsg": epsg, "horizontal_datum": "WGS 84 / UTM", "pixel_m": pixel_m,
            "resampling": "bilinear", "shape_rows_cols": list(grid_shape),
            "vertical_datum": "EGM2008 (m s. n. m.)",
        },
        "results": {
            "terrain": terrain_stats,
            "comparison": comparison,
            "aux_layers": aux if aux is not None else {"status": "no_disponible"},
            "worldcover": worldcover if worldcover is not None else {"status": "no_solicitado"},
            "ground_truth_validation": validation if validation is not None else {"status": "no_ejecutado"},
            "solar": solar if solar is not None else {"status": "no_ejecutado"},
        },
        "confidence": confidence,
        VOLATILE_KEY: {
            "generated_at": generated_at or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "acquired": acquired,
            "paths": paths or {},
            "elapsed_s": elapsed_s or {},
            "context": context or {"status": "no_solicitado"},  # ventana de contexto del GLB: vitrina, fuera del hash
            "platform": platform.platform(),
            "python_executable": sys.executable,
        },
    }
    m["result_hash"] = hash_manifest(m)
    return _clean(m)


def hash_manifest(manifest: dict) -> str:
    """SHA-256 de los datos del informe: todo el manifiesto salvo ``volatile`` y el propio hash."""
    return sha256_text(canonical_json({k: v for k, v in manifest.items() if k not in EXCLUDED_FROM_HASH}))


def write_manifest(manifest: dict, out_dir: Path) -> Path:
    path = Path(out_dir) / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_manifest(out_dir: Path) -> dict:
    return json.loads((Path(out_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))


def result_hash(out_dir: Path) -> str:
    """Hash de resultados recalculado desde ``manifest.json`` (excluye marcas de tiempo, rutas y tiempos)."""
    return hash_manifest(read_manifest(out_dir))


def report_fields(manifest: dict) -> dict:
    """Unica fuente de valores para el texto del informe: todo sale del manifiesto (nada se recalcula ni se redacta)."""
    t = manifest["results"]["terrain"]
    c = manifest["confidence"]
    base = next(d for d in manifest["inputs"]["datasets"] if d["role"] == "base")
    inconclusive = bool(t.get("inconclusive")) or bool(c.get("omit_point_slope"))
    f = {
        "fuente": base["name"],
        "resolucion_m": manifest["grid"]["pixel_m"],
        "nivel": c["overall"],
        "n_pixeles": t["n_pixels"],
        "datum_vertical": manifest["grid"]["vertical_datum"],
        "crs": manifest["grid"]["crs"],
        "revision_humana": bool(c["human_review_required"]),
        "elevacion": {k: t.get(k) for k in ("elev_min", "elev_max", "elev_mean", "elev_range", "elevation_within_noise")},
        "curvas": {"intervalo_m": t.get("contour_interval_m"), "indicativas": bool(t.get("contours_indicative"))},
        "hash_receta": manifest["recipe"]["hash"],
        "hash_entradas": manifest["inputs"]["hash"],
    }
    if inconclusive:
        f["pendiente"] = {"estado": "no_concluyente"}
        f["orientacion"] = {"estado": "no_concluyente"}
        f["puntaje_relieve"] = None
    else:
        f["pendiente"] = {"estado": "ok", "clases_pct": t["slope_classes_pct"], "media_pct": t["slope_mean_pct"],
                          "media_grados": t["slope_mean_deg"]}
        f["orientacion"] = {"estado": t["aspect_status"], "dominante": t["aspect_dominant"], "rosa": t["aspect_rose"]}
        f["puntaje_relieve"] = t["relief_score"]
    f["asoleamiento"] = manifest["results"].get("solar") or {"status": "no_ejecutado"}
    cmp_ = manifest["results"]["comparison"]
    f["comparacion"] = None if cmp_.get("status") == "no_disponible" else {
        k: cmp_[k] for k in ("rms_diff_m", "mean_diff_m", "slope_diff_mean_pct", "datum_aligned", "stats_scope")}
    return f
