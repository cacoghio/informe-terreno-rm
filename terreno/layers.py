"""Capas auxiliares de Copernicus GLO-30 (EDM / FLM / WBM / HEM) para los factores 6 y 7 de la regla de confianza.

Verificado en el bucket (2026-10-23): existen en `{tile}/AUXFILES/{tile sin "_DEM"}_{EDM|FLM|WBM|HEM}.tif`, con la misma
malla que el DEM (3600 x 3600, EPSG:4326, PixelIsPoint). Significado de los valores (Copernicus DEM Product Handbook
v5.0, tablas 6-8; archivado en LICENCIAS/ no, es documentacion tecnica):
* EDM: 0 vacio, 1 sin editar, 2 relleno con datos externos, 3 interpolado, 4 suavizado, 5 aeropuerto, 6 negativos elevados,
  7 aplanado, 8 oceano, 9 lago, 10 rio, 11 orilla, 12 morfado, 13 desplazado.
* FLM: 0 vacio, 1 editado (no relleno), 2 sin editar/rellenar, 3 ASTER, 4 SRTM90, 5 SRTM30, 6 GMTED2010, 7 SRTM30plus,
  8 TerraSAR-X radargrametrico, 9 AW3D30, 100-102 DEM de Espana/Noruega.
* WBM: 0 sin agua, 1 oceano, 2 lago, 3 rio.   * HEM: desviacion estandar del error (m); -32767 = pixel editado.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from terreno.errors import RejectRequest
from terreno.retry import retry_read
from terreno.fetch import BASE_URL, GLO30_GRID, DemResult, TileGrid, _overlaps, _px_window, tile_extent

AUX_KINDS = ("EDM", "FLM", "WBM", "HEM")
HEM_EDITED = -32767.0
_EDM_WATER = (8, 9, 10, 11)
_EDM_FILLED = (2, 3)
_FLM_FILLED = (3, 4, 5, 6, 7, 8, 9, 100, 101, 102)
_TILE_RE = re.compile(r"_(S|N)(\d{2})_00_(W|E)(\d{3})_00_DEM$")


@dataclass
class AuxStats:
    n_pixels: int
    void_water_fill_pct: float | None  # factor 6: % vacios + rellenos + agua + aplanados bajo el poligono
    hem_p90_m: float | None  # factor 7: percentil 90 del HEM bajo el poligono (sin pixeles editados)
    counts: dict = field(default_factory=dict)
    n_hem_valid: int = 0
    available: bool = False  # EDM, FLM y WBM leidos
    note: str = ""
    tiles: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # capas (EDM/FLM/WBM/HEM) que NO se pudieron leer tras los reintentos

    def unavailable_factors(self) -> list[str]:
        """Factores de confianza medibles que quedaron sin dato por un fallo de lectura (no por diseno)."""
        out = []
        if any(k in self.unavailable for k in ("EDM", "FLM", "WBM")):
            out.append("void_water_pct")
        if "HEM" in self.unavailable:
            out.append("hem_p90_m")
        return out


def aux_url(base_url: str, tile: str, kind: str) -> str:
    if not tile.endswith("_DEM"):
        raise RejectRequest(f"nombre de tile inesperado: {tile}")
    return f"{base_url.rstrip('/')}/{tile}/AUXFILES/{tile[:-3]}{kind}.tif"


def _tile_idx(name: str) -> tuple[int, int]:
    m = _TILE_RE.search(name)
    if not m:
        raise RejectRequest(f"nombre de tile inesperado: {name}")
    lat = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
    lon = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
    return lat, lon


def fetch_aux_stats(
    poly: Polygon,
    dem: DemResult,
    *,
    opener: Callable | None = None,
    base_url: str = BASE_URL,
    grid: TileGrid = GLO30_GRID,
) -> AuxStats:
    """Estadisticas de EDM/FLM/WBM/HEM sobre los pixeles que toca el poligono (all_touched), leyendo solo la ventana.

    Una capa que falta o cuya geometria no coincide con la del tile NO se rellena: queda ``None`` (factor sin dato).
    """
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para fetch_aux_stats")
    opener = opener or rasterio.open
    values: dict[str, list[np.ndarray]] = {k: [] for k in AUX_KINDS}
    problems: dict[str, str] = {}
    urls: list[str] = []
    used_tiles: list[str] = []
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                      GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
        for name in sorted(dem.tiles):
            lat_idx, lon_idx = _tile_idx(name)
            c0, r0, c1, r1, cols, rows = _px_window(poly.bounds, lat_idx, lon_idx, grid)
            if not _overlaps(c0, r0, c1, r1, cols, rows):
                continue
            c0, r0, c1, r1 = max(c0, 0), max(r0, 0), min(c1, cols), min(r1, rows)
            used_tiles.append(name)
            minx_t, _, _, maxy_t = tile_extent(lat_idx, lon_idx, grid)
            tf = from_origin(minx_t + c0 / cols, maxy_t - r0 / rows, 1.0 / cols, 1.0 / rows)
            inside = geometry_mask([poly], out_shape=(r1 - r0, c1 - c0), transform=tf, all_touched=True, invert=True)
            for kind in AUX_KINDS:
                if kind in problems:
                    continue
                url = aux_url(base_url, name, kind)
                urls.append(url)
                def read_layer(url=url, lat_idx=lat_idx, lon_idx=lon_idx, cols=cols, rows=rows, r0=r0, r1=r1, c0=c0, c1=c1):
                    with opener(url) as src:
                        ok = (src.width == cols and src.height == rows
                              and np.allclose(tuple(src.bounds), tile_extent(lat_idx, lon_idx, grid), atol=1e-8))
                        if not ok:
                            return None, None
                        return src.read(1, window=((r0, r1), (c0, c1))), src.nodata

                try:
                    # un 503/timeout transitorio del bucket se reintenta (AUD-1 I7); un archivo que no existe, no
                    arr, nodata = retry_read(read_layer)
                except Exception as exc:
                    problems[kind] = f"{kind}: no se pudo leer ({str(exc).splitlines()[0][:120]})"
                    continue
                if arr is None:
                    problems[kind] = f"{kind}: geometria distinta de la del tile {name}"
                    continue
                if kind == "HEM":
                    arr = arr.astype("float64")
                    arr[(arr <= -32000.0) | ~np.isfinite(arr)] = np.nan
                    if nodata is not None and not math.isnan(nodata):
                        arr[arr == nodata] = np.nan
                values[kind].append(arr[inside])

    n_pixels = int(sum(a.size for a in values["EDM"])) if "EDM" not in problems else 0
    base = dict(tiles=used_tiles, urls=urls)
    have3 = not any(k in problems for k in ("EDM", "FLM", "WBM")) and used_tiles
    counts: dict = {}
    void_pct = None
    if have3:
        edm, flm, wbm = (np.concatenate(values[k]) for k in ("EDM", "FLM", "WBM"))
        n_pixels = int(edm.size)
        void = (edm == 0) | (flm == 0)
        water = (wbm >= 1) | np.isin(edm, _EDM_WATER)
        flat = edm == 7
        filled = np.isin(edm, _EDM_FILLED) | np.isin(flm, _FLM_FILLED)
        union = void | water | flat | filled
        counts = {"void": int(void.sum()), "water": int(water.sum()), "flattened": int(flat.sum()),
                  "filled": int(filled.sum()), "union": int(union.sum())}
        void_pct = 100.0 * float(union.sum()) / float(max(n_pixels, 1))
    hem_p90, n_hem = None, 0
    if "HEM" not in problems and values["HEM"]:
        hem = np.concatenate(values["HEM"])
        hem = hem[np.isfinite(hem)]
        n_hem = int(hem.size)
        if n_hem:
            hem_p90 = float(np.percentile(hem, 90))
    note = "; ".join(problems[k] for k in AUX_KINDS if k in problems)
    if not used_tiles:
        note = "ningun tile del DEM intersecta el poligono"
    elif hem_p90 is None and "HEM" not in problems:
        note = (note + "; " if note else "") + "HEM: todos los pixeles del predio estan editados (sin error estimado)"
    return AuxStats(n_pixels=n_pixels, void_water_fill_pct=void_pct, hem_p90_m=hem_p90, counts=counts,
                    n_hem_valid=n_hem, available=bool(have3), note=note,
                    unavailable=[k for k in AUX_KINDS if k in problems or not used_tiles], **base)
