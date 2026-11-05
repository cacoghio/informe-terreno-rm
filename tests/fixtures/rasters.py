"""Rasters sinteticos para tests de reproject / asserts / terrain."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

from terreno.reproject import Raster


def write_raster(
    path: Path,
    arr: np.ndarray,
    *,
    epsg: int,
    res: float,
    left: float,
    top: float,
    nodata: float | None = float("nan"),
    res_y: float | None = None,
    flip_y: bool = False,
) -> Raster:
    """Escribe un GeoTIFF float32 north-up (o south-up si flip_y) y devuelve el Raster."""
    arr = np.asarray(arr, dtype="float32")
    ry = res if res_y is None else res_y
    e = ry if flip_y else -ry
    transform = Affine(res, 0.0, left, 0.0, e, top)
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=transform, nodata=nodata,
    ) as dst:
        dst.write(arr, 1)
    return Raster(path=Path(path), epsg=epsg, pixel_m=float(res), nodata=float("nan") if nodata is None else nodata)


def utm_ramp(path: Path, *, epsg: int = 32719, n: int = 20, res: float = 30.0,
             left: float = 348000.0, top: float = 6297000.0, slope_x: float = 0.0,
             slope_y: float = 0.0, base: float = 600.0, **kw) -> Raster:
    """Plano inclinado en UTM: z = base + slope_x*(x-left) + slope_y*(y-top) (pendientes en m/m)."""
    xs = left + (np.arange(n) + 0.5) * res
    ys = top - (np.arange(n) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    z = base + slope_x * (X - left) + slope_y * (Y - top)
    return write_raster(path, z, epsg=epsg, res=res, left=left, top=top, **kw)
