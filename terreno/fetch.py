"""fetch_dem: Copernicus GLO-30 por ventana COG, verificado contra tileList.txt.

Geometria real de los tiles (verificada con rasterio contra el bucket, 2026-10-23):
cada tile `{S|N}{lat:02d}_00_{W|E}{lon:03d}` es PixelIsPoint, cubre 1 grado y sus *centros* de pixel caen en
grados enteros, de modo que su extension es
    x: [lon - 0.5/cols, lon + 1 - 0.5/cols],  y: [lat + 0.5/rows, lat + 1 + 0.5/rows]
con rows = 3600 y cols = 3600 (|lat| < 50), 2400 (50-60), 1800 (60-70) ...
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from terreno.errors import RejectRequest
from terreno.geometry import utm_epsg_for

BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
TILE_LIST_NAME = "tileList.txt"
VERTICAL_DATUM = "EGM2008"
MAX_REQUIRED_TILES = 4  # un poligono <= 5 km2 toca como maximo 4 tiles
MARGIN_PX = 2
MAX_MARGIN_PX = 2000  # tope del margen configurable (~60 km): evita pedir media region por error


@dataclass(frozen=True)
class TileGrid:
    rows: int
    cols_for: Callable[[int], int]


def _glo30_cols(lat_idx: int) -> int:
    """Columnas por tile segun la banda de latitud (lat_idx = esquina sur del tile)."""
    low = lat_idx if lat_idx >= 0 else -(lat_idx + 1)  # menor |lat| que cubre el tile
    if low < 50:
        return 3600
    if low < 60:
        return 2400
    if low < 70:
        return 1800
    if low < 80:
        return 1200
    if low < 85:
        return 720
    return 360


GLO30_GRID = TileGrid(rows=3600, cols_for=_glo30_cols)


@dataclass
class DemResult:
    path: Path
    tiles: list[str]
    source_url: str
    acquired: str
    nodata_fraction: float
    tile_urls: list[str] = field(default_factory=list)
    source: str = "glo30"
    vertical_datum: str = VERTICAL_DATUM


def tile_name(lat_idx: int, lon_idx: int) -> str:
    ns = "S" if lat_idx < 0 else "N"
    ew = "W" if lon_idx < 0 else "E"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_idx):02d}_00_{ew}{abs(lon_idx):03d}_00_DEM"


def tile_url(base_url: str, lat_idx: int, lon_idx: int) -> str:
    n = tile_name(lat_idx, lon_idx)
    return f"{base_url.rstrip('/')}/{n}/{n}.tif"


def tile_extent(lat_idx: int, lon_idx: int, grid: TileGrid = GLO30_GRID) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) del tile, segun la geometria PixelIsPoint observada."""
    cols = grid.cols_for(lat_idx)
    rows = grid.rows
    return (
        lon_idx - 0.5 / cols,
        lat_idx + 0.5 / rows,
        lon_idx + 1 - 0.5 / cols,
        lat_idx + 1 + 0.5 / rows,
    )


def _px_window(bounds, lat_idx, lon_idx, grid):
    """Ventana (c0, r0, c1, r1) entera sin recortar del bbox en la grilla del tile."""
    cols, rows = grid.cols_for(lat_idx), grid.rows
    minx_t, _, _, maxy_t = tile_extent(lat_idx, lon_idx, grid)
    minx, miny, maxx, maxy = bounds
    eps = 1e-9
    c0 = math.floor((minx - minx_t) * cols + eps)
    c1 = math.ceil((maxx - minx_t) * cols - eps)
    r0 = math.floor((maxy_t - maxy) * rows + eps)
    r1 = math.ceil((maxy_t - miny) * rows - eps)
    return c0, r0, c1, r1, cols, rows


def _overlaps(c0, r0, c1, r1, cols, rows) -> bool:
    return c1 > 0 and c0 < cols and r1 > 0 and r0 < rows


def _load_tile_list(cache_dir: Path) -> set[str]:
    import requests

    from terreno.retry import retry_read

    path = cache_dir / TILE_LIST_NAME
    if not path.exists():
        def _get():
            resp = requests.get(f"{BASE_URL}/{TILE_LIST_NAME}", timeout=60)
            resp.raise_for_status()
            return resp

        try:
            r = retry_read(_get)  # cortes de DNS/red transitorios no rechazan el sitio al primer intento
        except Exception as exc:
            raise RejectRequest(f"no se pudo verificar tileList.txt del bucket: {exc}")
        path.write_bytes(r.content)
    names = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    if not names:
        raise RejectRequest("tileList.txt vacio")
    return names


def _read_window_file(opener, url, name, lat_idx, lon_idx, win, grid, out: Path) -> str:
    """Lee una ventana del tile, normaliza nodata a NaN y la guarda. Devuelve la fecha de descarga."""
    c0, r0, c1, r1, cols, rows = win
    exp = tile_extent(lat_idx, lon_idx, grid)
    try:
        ctx = opener(url)
    except Exception as exc:
        raise RejectRequest(f"no se pudo abrir el tile {name}: {exc}")
    with ctx as src:
        ok = (
            src.width == cols and src.height == rows and src.count >= 1
            and src.crs is not None and src.crs.to_epsg() == 4326
            and np.allclose(tuple(src.bounds), exp, atol=1e-8)
        )
        if not ok:
            raise RejectRequest(
                f"geometria inesperada del tile {name}: {src.width}x{src.height}, bounds {tuple(src.bounds)}; "
                f"se esperaba {cols}x{rows}, bounds {exp}"
            )
        arr = src.read(1, window=((r0, r1), (c0, c1))).astype("float32")
        nodata = src.nodata
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        arr[arr == np.float32(nodata)] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    minx_t, _, _, maxy_t = exp
    transform = from_origin(minx_t + c0 / cols, maxy_t - r0 / rows, 1.0 / cols, 1.0 / rows)
    acquired = _dt.date.today().isoformat()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with rasterio.open(
        tmp, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(arr, 1)
        dst.update_tags(ACQUIRED=acquired, SOURCE_URL=url, TILE=name)
    tmp.replace(out)
    return acquired


def fetch_dem(
    poly: Polygon,
    cache_dir: Path,
    source: str = "glo30",
    *,
    opener: Callable | None = None,
    tile_list: set[str] | None = None,
    base_url: str = BASE_URL,
    grid: TileGrid = GLO30_GRID,
    margin_px: int = MARGIN_PX,
) -> DemResult:
    """``margin_px``: margen (pixeles de 1 arcseg) alrededor del bbox; 2 por defecto. Se amplia solo para la ventana de
    contexto del GLB: los tiles que tocan unicamente el margen son opcionales (se leen si estan publicados)."""
    if source != "glo30":
        raise RejectRequest(f"fuente no soportada por fetch_dem: {source!r} (solo 'glo30')")
    if not isinstance(poly, Polygon) or poly.is_empty or not poly.is_valid:
        raise RejectRequest("poligono invalido para fetch_dem")
    if isinstance(margin_px, bool) or not isinstance(margin_px, int) or not (1 <= margin_px <= MAX_MARGIN_PX):
        raise RejectRequest(f"margin_px invalido: {margin_px!r} (entero entre 1 y {MAX_MARGIN_PX})")
    minx, miny, maxx, maxy = poly.bounds
    for x, y in ((minx, miny), (maxx, maxy)):
        utm_epsg_for(x, y)  # fuera de Chile -> RejectRequest

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    opener = opener or rasterio.open

    # 1. tiles requeridos (tocan el bbox) y opcionales (solo por el margen de 2 px)
    required: dict[tuple[int, int], tuple] = {}
    optional: dict[tuple[int, int], tuple] = {}
    reach = math.ceil(margin_px / 3600.0) + 1  # grados enteros de vecinos que el margen puede alcanzar
    for lat_idx in range(math.floor(miny) - reach, math.floor(maxy) + reach + 1):
        for lon_idx in range(math.floor(minx) - reach, math.floor(maxx) + reach + 1):
            c0, r0, c1, r1, cols, rows = _px_window(poly.bounds, lat_idx, lon_idx, grid)
            if _overlaps(c0, r0, c1, r1, cols, rows):
                required[(lat_idx, lon_idx)] = (c0, r0, c1, r1, cols, rows)
            elif _overlaps(c0 - margin_px, r0 - margin_px, c1 + margin_px, r1 + margin_px, cols, rows):
                optional[(lat_idx, lon_idx)] = (c0, r0, c1, r1, cols, rows)
    if len(required) > MAX_REQUIRED_TILES:
        raise RejectRequest(
            f"el poligono toca {len(required)} tiles (maximo {MAX_REQUIRED_TILES}); area demasiado grande"
        )

    # 2. verificar contra tileList.txt ANTES de leer
    published = tile_list if tile_list is not None else _load_tile_list(cache_dir)
    for key in sorted(required):
        if tile_name(*key) not in published:
            raise RejectRequest(f"tile no publicado: {tile_name(*key)}")
    chosen = dict(required)
    for key, win in optional.items():
        if tile_name(*key) in published:
            chosen[key] = win

    # 3. leer ventanas (con cache por ventana) -> archivos recortados
    window_files: list[Path] = []
    urls: list[str] = []
    names: list[str] = []
    acquired_dates: list[str] = []
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_MAX_RETRY="3",
        GDAL_HTTP_RETRY_DELAY="2",
    ):
        for (lat_idx, lon_idx) in sorted(chosen, key=lambda k: tile_name(*k)):
            c0, r0, c1, r1, cols, rows = chosen[(lat_idx, lon_idx)]
            c0, r0 = max(c0 - margin_px, 0), max(r0 - margin_px, 0)
            c1, r1 = min(c1 + margin_px, cols), min(r1 + margin_px, rows)
            name = tile_name(lat_idx, lon_idx)
            url = tile_url(base_url, lat_idx, lon_idx)
            out = cache_dir / "windows" / f"{name}__{rows}x{cols}_r{r0}-{r1}_c{c0}-{c1}.tif"
            if not out.exists():
                _read_window_file(opener, url, name, lat_idx, lon_idx, (c0, r0, c1, r1, cols, rows), grid, out)
            with rasterio.open(out) as w:
                acquired_dates.append(w.tags().get("ACQUIRED", ""))
            window_files.append(out)
            urls.append(url)
            names.append(name)

    # 4. mosaico unico (misma malla; resoluciones distintas -> rechazo, no remuestreo silencioso)
    dsets = [rasterio.open(p) for p in window_files]
    try:
        res0 = (abs(dsets[0].transform.a), abs(dsets[0].transform.e))
        for d in dsets[1:]:
            if not (np.isclose(abs(d.transform.a), res0[0], rtol=1e-6) and np.isclose(abs(d.transform.e), res0[1], rtol=1e-6)):
                raise RejectRequest(
                    "el poligono cruza una banda de resolucion distinta de GLO-30 (p. ej. 50 S): no soportado en el POC"
                )
        mosaic, transform = merge(dsets, nodata=float("nan"), dtype="float32")
    finally:
        for d in dsets:
            d.close()
    arr = mosaic[0]

    # 5. nodata: todo vacio -> rechazo; fraccion se informa
    finite = np.isfinite(arr)
    if not finite.any():
        raise RejectRequest("todo el recorte es nodata: no hay elevacion para el poligono")
    inside = geometry_mask([poly], out_shape=arr.shape, transform=transform, all_touched=True, invert=True)
    if not (finite & inside).any():
        raise RejectRequest("todos los pixeles bajo el poligono son nodata")
    nodata_fraction = float(1.0 - finite.mean())

    key = hashlib.sha1("|".join(p.name for p in window_files).encode()).hexdigest()[:16]
    path = cache_dir / "mosaics" / f"glo30_{key}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(arr.astype("float32"), 1)

    return DemResult(
        path=path,
        tiles=names,
        source_url=base_url.rstrip("/"),
        acquired=max(acquired_dates),
        nodata_fraction=nodata_fraction,
        tile_urls=urls,
        source=source,
        vertical_datum=VERTICAL_DATUM,
    )
