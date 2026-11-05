"""Tiles GLO-30 sinteticos (3x3 px) con la misma geometria que los reales.

Un tile real `S34_00_W071` (3600 x 3600 px, PixelIsPoint) cubre
[-71 - 0.5/3600, -70 - 0.5/3600] x [-34 + 0.5/3600, -33 + 0.5/3600], con los centros de pixel en grados
enteros. Los sinteticos usan N px por lado y el mismo patron: centros en L + k/N.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from rasterio.transform import from_origin

BASE = "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM"


def tile_name(lat_idx: int, lon_idx: int) -> str:
    ns = "S" if lat_idx < 0 else "N"
    ew = "W" if lon_idx < 0 else "E"
    return BASE.format(ns=ns, lat=abs(lat_idx), ew=ew, lon=abs(lon_idx))


def ramp(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Superficie global continua: 1000 + 100*(lon+71) + 50*(lat+34) (metros)."""
    return 1000.0 + 100.0 * (lon + 71.0) + 50.0 * (lat + 34.0)


def write_tile(
    root: Path,
    lat_idx: int,
    lon_idx: int,
    n: int = 3,
    func: Callable[[np.ndarray, np.ndarray], np.ndarray] = ramp,
    nodata: float | None = None,
    fill: float | None = None,
    size: tuple[int, int] | None = None,
) -> Path:
    rows, cols = size if size else (n, n)
    name = tile_name(lat_idx, lon_idx)
    path = Path(root) / name / f"{name}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    top = lat_idx + 1 + 0.5 / rows
    left = lon_idx - 0.5 / cols
    transform = from_origin(left, top, 1.0 / cols, 1.0 / rows)
    cx = lon_idx + np.arange(cols) / cols
    cy = lat_idx + 1 - np.arange(rows) / rows
    lon, lat = np.meshgrid(cx, cy)
    data = func(lon, lat).astype("float32")
    if fill is not None:
        data[:] = fill
    with rasterio.open(
        path, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def write_aux(
    root: Path,
    lat_idx: int,
    lon_idx: int,
    kind: str,
    values: np.ndarray | float,
    n: int = 3,
    dtype: str = "uint8",
    nodata: float | None = None,
) -> Path:
    """Capa auxiliar sintetica (EDM/FLM/WBM/HEM) en `{tile}/AUXFILES/{tile sin _DEM}_{KIND}.tif`, misma malla que el DEM."""
    name = tile_name(lat_idx, lon_idx)
    path = Path(root) / name / "AUXFILES" / f"{name[:-3]}{kind}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    top = lat_idx + 1 + 0.5 / n
    left = lon_idx - 0.5 / n
    data = np.broadcast_to(np.asarray(values), (n, n)).astype(dtype)
    with rasterio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1, dtype=dtype, crs="EPSG:4326",
        transform=from_origin(left, top, 1.0 / n, 1.0 / n), nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path
