"""Biblioteca de aserciones duras (spec sec. 7). Nunca ``assert``: siempre RejectRequest.

Las aserciones leen el archivo (no confian solo en los campos del dataclass) para que un Raster mal
declarado no pase.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import rasterio
from shapely.geometry import box

from terreno.errors import RejectRequest

if TYPE_CHECKING:  # evita el import circular con reproject
    from terreno.reproject import Raster


def _open(r: "Raster"):
    try:
        return rasterio.open(r.path)
    except Exception as exc:
        raise RejectRequest(f"no se puede abrir el raster {r.path}: {exc}")


def valid_mask(arr: np.ndarray, nodata: float | None) -> np.ndarray:
    """True donde hay dato: finito y distinto del nodata declarado."""
    ok = np.isfinite(arr)
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        ok &= arr != np.float32(nodata)
    return ok


def assert_crs(r: "Raster", epsg: int) -> None:
    with _open(r) as src:
        file_epsg = src.crs.to_epsg() if src.crs else None
    if file_epsg != epsg:
        raise RejectRequest(f"CRS del raster {file_epsg} != esperado {epsg}")
    if r.epsg != epsg:
        raise RejectRequest(f"CRS declarado del raster {r.epsg} != esperado {epsg}")


def assert_meters(r: "Raster") -> None:
    with _open(r) as src:
        crs = src.crs
    if crs is None or not crs.is_projected:
        raise RejectRequest("el raster no esta en un CRS proyectado: unidades en grados, se exigen metros")
    units = (crs.linear_units or "").lower()
    if units not in ("metre", "meter", "metres", "meters"):
        raise RejectRequest(f"unidades del CRS no son metros: {units!r}")


def assert_pixel_size(r: "Raster", expected: float = 30.0, tol: float = 0.5) -> None:
    with _open(r) as src:
        rx, ry = abs(src.transform.a), abs(src.transform.e)
    for label, v in (("x", rx), ("y", ry), ("declarado", float(r.pixel_m))):
        if not math.isfinite(v) or abs(v - expected) > tol:
            raise RejectRequest(f"tamano de pixel {label} {v:g} m fuera de {expected:g} +- {tol:g} m")


def assert_nodata_below(r: "Raster", max_frac: float) -> None:
    with _open(r) as src:
        arr = src.read(1)
        nodata = src.nodata
    frac = 1.0 - float(valid_mask(arr, nodata).mean())
    if frac > max_frac:
        raise RejectRequest(f"nodata {frac:.1%} supera el umbral {max_frac:.1%}")


def assert_plausible_range(r: "Raster", lo: float = -100.0, hi: float = 7000.0) -> None:
    with _open(r) as src:
        arr = src.read(1)
        nodata = src.nodata
    ok = valid_mask(arr, nodata)
    if not ok.any():
        raise RejectRequest("raster sin ningun valor valido (todo nodata)")
    vmin, vmax = float(arr[ok].min()), float(arr[ok].max())
    if vmin < lo or vmax > hi:
        raise RejectRequest(
            f"elevacion no plausible: rango observado [{vmin:g}, {vmax:g}] m fuera de [{lo:g}, {hi:g}] m"
        )


def assert_polygon_inside(poly, bounds) -> None:
    """``poly`` y ``bounds`` (minx, miny, maxx, maxy) deben estar en el mismo CRS."""
    try:
        minx, miny, maxx, maxy = (float(v) for v in bounds)
    except (TypeError, ValueError):
        raise RejectRequest("bbox mal formado")
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise RejectRequest("bbox no finito")
    if minx >= maxx or miny >= maxy:
        raise RejectRequest(f"bbox invertido o vacio: ({minx:g}, {miny:g}, {maxx:g}, {maxy:g})")
    if poly is None or poly.is_empty:
        raise RejectRequest("poligono vacio")
    if not box(minx, miny, maxx, maxy).covers(poly):
        raise RejectRequest(
            f"el poligono {tuple(round(v, 6) for v in poly.bounds)} no cae dentro del raster "
            f"({minx:g}, {miny:g}, {maxx:g}, {maxy:g})"
        )
