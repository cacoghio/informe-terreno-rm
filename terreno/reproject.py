"""reproject: DEM EPSG:4326 -> UTM (zona del centroide) a 30 m, bilineal, con aserciones duras."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject as rio_reproject
from rasterio.warp import transform_bounds
from shapely.geometry import Polygon
import shapely

from terreno.asserts import (
    assert_crs,
    assert_meters,
    assert_nodata_below,
    assert_pixel_size,
    assert_plausible_range,
    assert_polygon_inside,
)
from terreno.errors import RejectRequest
from terreno.fetch import DemResult
from terreno.geometry import centroid_zone

PIXEL_M = 30.0
MARGIN_PX = 3  # margen alrededor del bbox del poligono, en pixeles de salida
MAX_MARGIN_PX = 1000  # tope del margen configurable (30 km)
# Hipotesis: la spec solo fija el corte de confianza C (>10 % de vacios); rechazamos sobre 50 %.
MAX_NODATA_FRACTION = 0.5


@dataclass
class Raster:
    path: Path
    epsg: int
    pixel_m: float
    nodata: float


def _assert_geographic_polygon(poly: Polygon) -> None:
    minx, miny, maxx, maxy = poly.bounds
    if max(abs(minx), abs(maxx)) > 180.0 or max(abs(miny), abs(maxy)) > 90.0:
        raise RejectRequest(
            "mezcla de CRS: el poligono tiene coordenadas fuera de grados (parecen metros proyectados); "
            "se exige EPSG:4326"
        )


def reproject(dem: DemResult, poly: Polygon, *, margin_px: int = MARGIN_PX) -> Raster:
    """``margin_px``: margen de la malla de salida alrededor del bbox del poligono (3 por defecto = ventana de trabajo;
    ~50 para la ventana de contexto del GLB). Siempre se recorta a lo que cubre el DEM."""
    if isinstance(margin_px, bool) or not isinstance(margin_px, int) or not (1 <= margin_px <= MAX_MARGIN_PX):
        raise RejectRequest(f"margin_px invalido: {margin_px!r} (entero entre 1 y {MAX_MARGIN_PX})")
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para reproject")
    _assert_geographic_polygon(poly)
    epsg = centroid_zone(poly)  # la zona nunca viene de un parametro libre

    try:
        src = rasterio.open(dem.path)
    except Exception as exc:
        raise RejectRequest(f"no se puede abrir el DEM {dem.path}: {exc}")
    with src:
        if src.crs is None or src.crs.to_epsg() != 4326:
            got = src.crs.to_epsg() if src.crs else None
            raise RejectRequest(f"mezcla de CRS: el DEM esta en EPSG:{got} y el poligono en EPSG:4326")
        t = src.transform
        if not (t.a > 0 and t.e < 0 and t.b == 0 and t.d == 0):
            raise RejectRequest("DEM con geotransform invertido o rotado (se exige north-up)")
        src_bounds = tuple(src.bounds)
        src_arr = src.read(1).astype("float32")
        src_nodata = src.nodata
        src_transform = src.transform

    if src_nodata is not None and not math.isnan(src_nodata):
        src_arr[src_arr == np.float32(src_nodata)] = np.nan
    src_arr[~np.isfinite(src_arr)] = np.nan

    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))

    # Extension de salida: bbox del poligono +- 3 px, restringida a lo que cubre el DEM, en malla de 30 m
    # alineada a multiplos de 30 (determinista, independiente del recorte del DEM).
    pminx, pminy, pmaxx, pmaxy = poly_utm.bounds
    sminx, sminy, smaxx, smaxy = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *src_bounds, densify_pts=21)
    m = margin_px * PIXEL_M
    left = max(math.floor((pminx - m) / PIXEL_M) * PIXEL_M, math.ceil(sminx / PIXEL_M) * PIXEL_M)
    right = min(math.ceil((pmaxx + m) / PIXEL_M) * PIXEL_M, math.floor(smaxx / PIXEL_M) * PIXEL_M)
    bottom = max(math.floor((pminy - m) / PIXEL_M) * PIXEL_M, math.ceil(sminy / PIXEL_M) * PIXEL_M)
    top = min(math.ceil((pmaxy + m) / PIXEL_M) * PIXEL_M, math.floor(smaxy / PIXEL_M) * PIXEL_M)
    # Si el recorte no contiene al poligono el bbox queda invertido/vacio y assert_polygon_inside lo rechaza.
    width = int(round((right - left) / PIXEL_M))
    height = int(round((top - bottom) / PIXEL_M))
    if width < 1 or height < 1:
        raise RejectRequest("el DEM no cubre el poligono en la zona UTM (recorte vacio)")
    assert_polygon_inside(poly_utm, (left, bottom, right, top))

    dst_transform = Affine(PIXEL_M, 0.0, left, 0.0, -PIXEL_M, top)
    dst = np.full((height, width), np.nan, dtype="float32")
    rio_reproject(
        source=src_arr,
        destination=dst,
        src_transform=src_transform,
        src_crs="EPSG:4326",
        src_nodata=float("nan"),
        dst_transform=dst_transform,
        dst_crs=f"EPSG:{epsg}",
        dst_nodata=float("nan"),
        resampling=Resampling.bilinear,
        init_dest_nodata=True,
    )

    out = Path(dem.path).with_name(f"{Path(dem.path).stem}_utm{epsg}.tif")
    with rasterio.open(
        out, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=dst_transform, nodata=float("nan"), compress="deflate",
    ) as dst_ds:
        dst_ds.write(dst, 1)

    r = Raster(path=out, epsg=epsg, pixel_m=PIXEL_M, nodata=float("nan"))
    assert_crs(r, epsg)
    assert_meters(r)
    assert_pixel_size(r, expected=PIXEL_M, tol=0.5)
    assert_nodata_below(r, MAX_NODATA_FRACTION)
    assert_plausible_range(r)
    return r
