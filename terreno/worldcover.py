"""Cobertura de dosel y construido desde ESA WorldCover 10 m (v200, ano 2021) para el factor ``canopy_pct``.

Fuente publica y gratuita, sin cuenta: bucket ``esa-worldcover`` (AWS, eu-central-1), teselas COG de 3 x 3 grados
nombradas por su esquina suroeste (Santiago = S36W072). Licencia CC BY 4.0: se cita en el informe (``texts_es``).

Clases usadas (Product User Manual v2): 10 = arbolado, 50 = construido. Matorral (20) y pasto/cultivo (30/40) NO cuentan:
no cambian el DSM de forma apreciable. El factor es el % de pixeles del predio en 10 o 50: el DSM de GLO-30 incluye esa
altura encima del suelo, asi que es la parte del predio donde la pendiente y la cota no son de terreno desnudo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import Polygon, box

from terreno.errors import RejectRequest
from terreno.retry import retry_read

BASE_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
TILE_DEG = 3
CLASS_TREE, CLASS_BUILT = 10, 50
CANOPY_CLASSES = (CLASS_TREE, CLASS_BUILT)
CONTEXT_M = 1000.0  # ventana de contexto (solo diagnostico, no entra al factor)
M_PER_DEG = 111_320.0


@dataclass
class CanopyStats:
    n_pixels: int  # pixeles de 10 m dentro del predio con clase valida
    canopy_pct: float | None  # % del predio en arbolado o construido (el factor de confianza)
    tree_pct: float | None = None
    built_pct: float | None = None
    context_pct: float | None = None  # mismo % en la ventana de +-CONTEXT_M alrededor (diagnostico)
    available: bool = False
    note: str = ""
    tiles: list[str] = field(default_factory=list)


def tile_name(lat_sw: int, lon_sw: int) -> str:
    return f"{'N' if lat_sw >= 0 else 'S'}{abs(lat_sw):02d}{'E' if lon_sw >= 0 else 'W'}{abs(lon_sw):03d}"


def tile_url(base_url: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/ESA_WorldCover_10m_2021_v200_{name}_Map.tif"


def _tiles_for(bounds: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    minx, miny, maxx, maxy = bounds
    lats = range(math.floor(miny / TILE_DEG) * TILE_DEG, math.ceil(maxy / TILE_DEG) * TILE_DEG, TILE_DEG)
    lons = range(math.floor(minx / TILE_DEG) * TILE_DEG, math.ceil(maxx / TILE_DEG) * TILE_DEG, TILE_DEG)
    return [(la, lo) for la in lats for lo in lons]


def _class_values(geom, opener: Callable, base_url: str, *, masked: bool) -> tuple[np.ndarray, list[str]]:
    """Clases WorldCover bajo ``geom`` (solo pixeles dentro si ``masked``; toda la ventana si no), de todas las teselas que toca."""
    minx, miny, maxx, maxy = geom.bounds
    parts: list[np.ndarray] = []
    used: list[str] = []
    for la, lo in _tiles_for(geom.bounds):
        cb = (max(minx, lo), max(miny, la), min(maxx, lo + TILE_DEG), min(maxy, la + TILE_DEG))
        if cb[0] >= cb[2] or cb[1] >= cb[3]:
            continue
        name = tile_name(la, lo)
        used.append(name)

        def read(url=tile_url(base_url, name), cb=cb):
            with opener(url) as src:
                w = src.window(*cb).round_offsets().round_lengths()
                w = rasterio.windows.Window(w.col_off, w.row_off, max(int(w.width), 1), max(int(w.height), 1))
                return src.read(1, window=w), src.window_transform(w)

        arr, tf = retry_read(read)
        if not masked:
            parts.append(arr.ravel())
            continue
        inside = geometry_mask([geom], out_shape=arr.shape, transform=tf, all_touched=False, invert=True)
        if not inside.any():  # lote mas chico que un pixel de 10 m: se toma el que toca
            inside = geometry_mask([geom], out_shape=arr.shape, transform=tf, all_touched=True, invert=True)
        parts.append(arr[inside])
    vals = np.concatenate(parts) if parts else np.empty(0, dtype="uint8")
    return vals, used


def _pct(vals: np.ndarray, classes) -> float | None:
    valid = vals[vals > 0]  # 0 = sin dato
    if not valid.size:
        return None
    return 100.0 * float(np.isin(valid, classes).sum()) / float(valid.size)


def fetch_canopy_stats(poly: Polygon, *, opener: Callable | None = None, base_url: str = BASE_URL,
                       context_m: float = CONTEXT_M) -> CanopyStats:
    """% de arbolado + construido en el predio (y en su entorno, como diagnostico). Sin dato si la tesela no se puede leer."""
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para fetch_canopy_stats")
    opener = opener or rasterio.open
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                      GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
        try:
            lot, tiles = _class_values(poly, opener, base_url, masked=True)
        except Exception as exc:  # noqa: BLE001 - red, tesela ausente, 503: el factor queda 'no_disponible', no se inventa
            return CanopyStats(n_pixels=0, canopy_pct=None, available=False,
                               note=f"WorldCover: no se pudo leer ({str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__})")
        try:  # el entorno es solo diagnostico: si no se puede leer, el factor del predio sigue valiendo
            dlat = context_m / M_PER_DEG
            dlon = dlat / max(math.cos(math.radians(poly.centroid.y)), 0.1)
            minx, miny, maxx, maxy = poly.bounds
            ctx, _ = _class_values(box(minx - dlon, miny - dlat, maxx + dlon, maxy + dlat), opener, base_url, masked=False)
        except Exception:  # noqa: BLE001
            ctx = np.empty(0, dtype="uint8")
    pct = _pct(lot, CANOPY_CLASSES)
    return CanopyStats(
        n_pixels=int((lot > 0).sum()), canopy_pct=pct, tree_pct=_pct(lot, (CLASS_TREE,)), built_pct=_pct(lot, (CLASS_BUILT,)),
        context_pct=_pct(ctx, CANOPY_CLASSES), available=pct is not None,
        note="" if pct is not None else "WorldCover: el predio no tiene pixeles con clase valida", tiles=tiles)
