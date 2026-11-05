"""compute_terrain: pendiente, orientacion, sombreado y curvas con el GDAL CLI de QGIS 3.36.1.

Todos los numeros salen de aqui (y del manifiesto); el agente no calcula nada. Estadisticas solo sobre
los pixeles cuyo centro cae dentro del poligono (``geometry_mask`` con all_touched=False).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import Polygon

from terreno.asserts import (
    assert_crs,
    assert_meters,
    assert_nodata_below,
    assert_pixel_size,
    assert_plausible_range,
    assert_polygon_inside,
    valid_mask,
)
from terreno.config import run_gdal
from terreno.errors import RejectRequest
from terreno.geometry import centroid_zone
from terreno.reproject import MAX_NODATA_FRACTION, Raster, _assert_geographic_polygon

SLOPE_CLASS_KEYS = ("<5", "5-15", "15-30", ">30")
ASPECT_LABELS = ("N", "NE", "E", "SE", "S", "SO", "O", "NO")
MIN_PIXELS = 9  # < 9 pixeles (~3x3) -> "no concluyente" (spec sec. 3 #4 y sec. 4)
FLAT_SLOPE_PCT = 3.0  # bajo esta pendiente media: "sin orientacion predominante" (spec sec. 3 #5)
NOISE_RANGE_M = 4.0  # desnivel dentro del ruido: 2 x error relativo medido en RM (~2 m); = corte C de elev_range_m
CONTOUR_INTERVAL_M = 10

# Hipotesis (spec sec. 3 #6: "hipotesis de formula"): conteo de umbrales superados por pendiente media y por rango.
_RELIEF_SLOPE_CUTS = (3.0, 8.0, 15.0, 25.0, 35.0, 50.0)
_RELIEF_RANGE_CUTS = (8.0, 20.0, 40.0, 80.0, 150.0, 300.0)
RELIEF_FORMULA = (
    "score = 1 + (a + b + 1) // 2, con a = n.o de cortes {slope} superados por la pendiente media (%) y "
    "b = n.o de cortes {rng} superados por el rango de elevacion (m); monotona en ambos, entera, 1-7"
).format(slope=list(_RELIEF_SLOPE_CUTS), rng=list(_RELIEF_RANGE_CUTS))


@dataclass
class TerrainResult:
    slope_path: Path
    aspect_path: Path
    hillshade_path: Path
    contours_path: Path
    stats: dict


def classify_slope(values: np.ndarray) -> dict[str, float]:
    """% de pixeles por clase (intervalos cerrados a la izquierda: [5,15), [15,30), [30,inf))."""
    v = np.asarray(values, dtype="float64")
    n = v.size
    if n == 0:
        raise RejectRequest("sin pixeles de pendiente que clasificar")
    counts = (
        int((v < 5.0).sum()),
        int(((v >= 5.0) & (v < 15.0)).sum()),
        int(((v >= 15.0) & (v < 30.0)).sum()),
        int((v >= 30.0).sum()),
    )
    return {k: 100.0 * c / n for k, c in zip(SLOPE_CLASS_KEYS, counts)}


def relief_score(slope_mean_pct: float, elev_range_m: float) -> int:
    """Puntaje 1-7 de complejidad del relieve. HIPOTESIS: orden relativo, no medida fisica."""
    a = sum(slope_mean_pct > c for c in _RELIEF_SLOPE_CUTS)
    b = sum(elev_range_m > c for c in _RELIEF_RANGE_CUTS)
    return 1 + (a + b + 1) // 2


def _gdal_out(path: Path) -> str:
    path = Path(path)
    if path.exists():
        path.unlink()
    return str(path)


def _read_gdal_out(path: Path) -> tuple[np.ndarray, float | None]:
    with rasterio.open(path) as src:
        return src.read(1).astype("float32"), src.nodata


def compute_terrain(r: Raster, poly: Polygon, *, out_dir: Path | None = None) -> TerrainResult:
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para compute_terrain")
    _assert_geographic_polygon(poly)
    epsg = centroid_zone(poly)

    assert_crs(r, epsg)
    assert_meters(r)
    assert_pixel_size(r, expected=30.0, tol=0.5)
    assert_nodata_below(r, MAX_NODATA_FRACTION)
    assert_plausible_range(r)

    with rasterio.open(r.path) as src:
        arr = src.read(1).astype("float32")
        nodata = src.nodata
        transform = src.transform
        bounds = tuple(src.bounds)
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    assert_polygon_inside(poly_utm, bounds)

    out = Path(out_dir) if out_dir is not None else Path(r.path).parent
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(r.path).stem
    slope_p, aspect_p = out / f"{stem}_slope.tif", out / f"{stem}_aspect.tif"
    hill_p, cont_p = out / f"{stem}_hillshade.tif", out / f"{stem}_contours.gpkg"

    src_s = str(r.path)
    run_gdal("gdaldem", "slope", src_s, _gdal_out(slope_p), "-p", "-of", "GTiff")
    run_gdal("gdaldem", "aspect", src_s, _gdal_out(aspect_p), "-zero_for_flat", "-of", "GTiff")
    run_gdal("gdaldem", "hillshade", src_s, _gdal_out(hill_p), "-z", "1.0", "-of", "GTiff")
    run_gdal("gdal_contour", "-i", str(CONTOUR_INTERVAL_M), "-a", "elev", "-nln", "contour",
             "-f", "GPKG", src_s, _gdal_out(cont_p))
    for p in (slope_p, aspect_p, hill_p, cont_p):
        if not p.exists():
            raise RejectRequest(f"GDAL no produjo {p.name}")

    slope, s_nd = _read_gdal_out(slope_p)
    aspect, a_nd = _read_gdal_out(aspect_p)

    inside = geometry_mask([poly_utm], out_shape=arr.shape, transform=transform, all_touched=False, invert=True)
    elev_ok = inside & valid_mask(arr, nodata)
    n_pixels = int(elev_ok.sum())

    stats: dict = {
        "n_pixels": n_pixels,
        "pixel_m": float(r.pixel_m),
        "contour_interval_m": CONTOUR_INTERVAL_M,
        "contours_indicative": True,
        "min_pixels_for_slope": MIN_PIXELS,
    }
    if n_pixels > 0:
        z = arr[elev_ok].astype("float64")
        stats.update(
            elev_min=float(z.min()), elev_max=float(z.max()), elev_mean=float(z.mean()),
            elev_range=float(z.max() - z.min()),
        )
        stats["elevation_within_noise"] = bool(stats["elev_range"] < NOISE_RANGE_M)
    else:
        stats.update(elev_min=None, elev_max=None, elev_mean=None, elev_range=None, elevation_within_noise=None)

    slope_ok = elev_ok & valid_mask(slope, s_nd)
    n_slope = int(slope_ok.sum())
    stats["n_slope_pixels"] = n_slope
    stats["relief_score_hypothesis"] = {"is_hypothesis": True, "formula": RELIEF_FORMULA}

    if n_pixels < MIN_PIXELS or n_slope < MIN_PIXELS:
        # "No concluyente": nunca una cifra de pendiente (ni clases, ni media, ni rosa, ni puntaje).
        stats.update(inconclusive=True, aspect_dominant=None, aspect_status="no_concluyente")
        return TerrainResult(slope_p, aspect_p, hill_p, cont_p, stats)

    sv = slope[slope_ok].astype("float64")
    mean_pct = float(sv.mean())
    stats.update(
        inconclusive=False,
        slope_classes_pct=classify_slope(sv),
        slope_mean_pct=mean_pct,
        slope_mean_deg=float(math.degrees(math.atan(mean_pct / 100.0))),
    )

    rose = [0.0] * 8
    sel = slope_ok & valid_mask(aspect, a_nd) & (slope >= FLAT_SLOPE_PCT)
    if sel.any():
        sector = np.floor(((aspect[sel].astype("float64") + 22.5) % 360.0) / 45.0).astype(int) % 8
        counts = np.bincount(sector, minlength=8)
        rose = [100.0 * float(c) / float(counts.sum()) for c in counts]
    stats["aspect_rose"] = rose
    if mean_pct < FLAT_SLOPE_PCT or not sel.any():
        stats.update(aspect_dominant=None, aspect_status="sin_orientacion_predominante")
    else:
        stats.update(aspect_dominant=ASPECT_LABELS[int(np.argmax(rose))], aspect_status="ok")

    stats["relief_score"] = relief_score(mean_pct, stats["elev_range"])
    return TerrainResult(slope_p, aspect_p, hill_p, cont_p, stats)
