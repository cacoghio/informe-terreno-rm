"""compare_sources: GLO-30 vs AW3D30 (spec sec. 3 #11 y sec. 4 factor 4).

Datums verticales (verificados en la documentacion oficial, ver LEDGER):
* GLO-30 = EGM2008 (spec).
* AW3D30 = altura ortometrica "converted from the ellipsoidal height based on ITRF97 and GRS80, using EGM96
  geoid model" (JAXA, AW3D30 Product Description v3.1, Table 1), enteros de 1 m, mar = 0, vacio = -9999.
Antes de restar, ``b`` se lleva al datum de ``a`` con las mallas de geoide de PROJ (EGM96 -> EGM2008). Si la
conversion no se puede hacer (sin red / sin malla / datum desconocido) NO se resta "a ciegas" en silencio:
``datum_aligned=False`` y ``datum_penalty=True``, que ``score_confidence`` trata como factor sin dato (tope B).
El raster de AW3D30 no se redistribuye: solo se guardan en cache local (clave de API nunca en disco).
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
import shapely
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from rasterio.warp import reproject as rio_reproject
from shapely.geometry import Polygon

from terreno.asserts import assert_crs, assert_meters, assert_pixel_size, valid_mask
from terreno.config import run_gdal
from terreno.errors import RejectRequest
from terreno.fetch import DemResult
from terreno.geometry import centroid_zone, utm_epsg_for

OPENTOPO_URL = "https://portal.opentopography.org/API/globaldem"
API_KEY_ENV = "OPENTOPOGRAPHY_API_KEY"
GLO30_DATUM = "EGM2008"
AW3D30_DATUM = "EGM96"
MARGIN_DEG = 2.0 / 3600.0  # 2 px de margen alrededor del bbox, como fetch_dem
MIN_POLY_PIXELS = 9  # bajo esto el RMS del poligono no es estadistica: se usa el entorno
AW3D30_VOID = -9999.0


class DatumShiftUnavailable(Exception):
    """No hay forma verificable de convertir entre los dos datums verticales."""


# --- descarga de AW3D30 (OpenTopography globaldem) ---

MIN_SIDE_M = 450.0  # OpenTopography rechaza (HTTP 400) ventanas con un lado < ~250 m; margen de seguridad


def aw3d30_params(poly: Polygon, api_key: str, margin_m: float = 0.0) -> dict:
    """``margin_m``: margen extra (m) alrededor del bbox; la validacion contra huellas pide la ventana de contexto."""
    minx, miny, maxx, maxy = poly.bounds
    lat_mid = (miny + maxy) / 2
    extra_lat = margin_m / 111_320.0
    extra_lon = margin_m / (111_320.0 * max(math.cos(math.radians(lat_mid)), 0.1))
    south, north = miny - MARGIN_DEG - extra_lat, maxy + MARGIN_DEG + extra_lat
    west, east = minx - MARGIN_DEG - extra_lon, maxx + MARGIN_DEG + extra_lon
    # Lotes chicos: se amplia la ventana, centrada, hasta MIN_SIDE_M por lado. compare_sources recorta a la malla de GLO-30.
    lat0 = (south + north) / 2
    half_lat = MIN_SIDE_M / 2 / 111_320.0
    half_lon = MIN_SIDE_M / 2 / (111_320.0 * max(math.cos(math.radians(lat0)), 0.1))
    if north - south < 2 * half_lat:
        south, north = lat0 - half_lat, lat0 + half_lat
    lon0 = (west + east) / 2
    if east - west < 2 * half_lon:
        west, east = lon0 - half_lon, lon0 + half_lon
    return {
        "demtype": "AW3D30",
        "south": round(south, 6),
        "north": round(north, 6),
        "west": round(west, 6),
        "east": round(east, 6),
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }


def _http_get(url, params, timeout):
    import requests

    return requests.get(url, params=params, timeout=timeout)


def fetch_aw3d30(
    poly: Polygon,
    cache_dir: Path,
    *,
    api_key: str | None = None,
    getter: Callable | None = None,
    timeout: float = 120.0,
    margin_m: float = 0.0,
) -> DemResult:
    """Descarga AW3D30 (ventana del poligono + 2 px, + ``margin_m`` m si se pide) por la API globaldem de OpenTopography.

    La clave sale de ``OPENTOPOGRAPHY_API_KEY`` (nunca se escribe en disco ni en el manifiesto).
    """
    key = api_key or __import__("os").environ.get(API_KEY_ENV)
    if not key:
        raise RejectRequest(f"falta {API_KEY_ENV}")
    if not isinstance(poly, Polygon) or poly.is_empty or not poly.is_valid:
        raise RejectRequest("poligono invalido para fetch_aw3d30")
    minx, miny, maxx, maxy = poly.bounds
    for x, y in ((minx, miny), (maxx, maxy)):
        utm_epsg_for(x, y)  # fuera de Chile -> RejectRequest

    cache_dir = Path(cache_dir)
    params = aw3d30_params(poly, key, margin_m)
    tag = hashlib.sha1(
        "|".join(f"{params[k]}" for k in ("south", "north", "west", "east")).encode()
    ).hexdigest()[:16]
    out = cache_dir / "aw3d30" / f"aw3d30_{tag}.tif"
    clean_url = f"{OPENTOPO_URL}?demtype=AW3D30"

    if not out.exists():
        getter = getter or _http_get
        try:
            resp = getter(OPENTOPO_URL, params, timeout)
        except Exception as exc:
            raise RejectRequest(f"no se pudo consultar OpenTopography: {str(exc).replace(key, '***')}")
        status = getattr(resp, "status_code", None)
        if status != 200:
            body = (getattr(resp, "content", b"") or b"")[:300].decode("utf-8", "replace").replace(key, "***")
            raise RejectRequest(f"OpenTopography devolvio HTTP {status}: {body}")
        try:
            with MemoryFile(resp.content) as mem, mem.open() as src:
                if src.crs is None or src.crs.to_epsg() != 4326:
                    raise RejectRequest("AW3D30 de OpenTopography no esta en EPSG:4326")
                arr = src.read(1).astype("float32")
                nodata = src.nodata
                transform = src.transform
        except RejectRequest:
            raise
        except Exception as exc:
            raise RejectRequest(f"respuesta de OpenTopography no es un GeoTIFF legible: {exc}")
        arr[arr == np.float32(AW3D30_VOID)] = np.nan
        if nodata is not None and not math.isnan(nodata):
            arr[arr == np.float32(nodata)] = np.nan
        arr[~np.isfinite(arr)] = np.nan
        if not np.isfinite(arr).any():
            raise RejectRequest("AW3D30: todo el recorte esta vacio (-9999): no hay elevacion")
        acquired = _dt.date.today().isoformat()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        with rasterio.open(
            tmp, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
            crs="EPSG:4326", transform=transform, nodata=float("nan"), compress="deflate",
        ) as dst:
            dst.write(arr, 1)
            dst.update_tags(ACQUIRED=acquired, SOURCE_URL=clean_url, DATUM=AW3D30_DATUM)
        tmp.replace(out)

    with rasterio.open(out) as src:
        arr = src.read(1)
        acquired = src.tags().get("ACQUIRED", "")
    return DemResult(
        path=out,
        tiles=["AW3D30 (JAXA) via OpenTopography globaldem"],
        source_url=clean_url,
        acquired=acquired,
        nodata_fraction=float(1.0 - np.isfinite(arr).mean()),
        tile_urls=[],
        source="aw3d30",
        vertical_datum=AW3D30_DATUM,
    )


# --- conversion de datum vertical ---

def pyproj_shift_egm96_to_egm2008(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Suma (m) que lleva una altura EGM96 a EGM2008, con las mallas de geoide de PROJ.

    Usa la red de PROJ (cdn.proj.org: us_nga_egm96_15.tif y us_nga_egm08_25.tif, la segunda ~80 MB pero se lee
    por rangos). Lanza DatumShiftUnavailable si PROJ solo dispone de la transformacion "ballpark" (sin malla).
    """
    from pyproj import network
    from pyproj.transformer import TransformerGroup

    prev = network.is_network_enabled()
    network.set_network_enabled(True)
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            group = TransformerGroup("EPSG:4326+5773", "EPSG:4326+3855", always_xy=True)
        grid_based = [t for t in group.transformers if "ballpark" not in t.description.lower()]
        if not grid_based:
            raise DatumShiftUnavailable("PROJ no dispone de las mallas de geoide EGM96/EGM2008 (solo ballpark)")
        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        _, _, z = grid_based[0].transform(lon, lat, np.zeros_like(lon))
        z = np.asarray(z, dtype="float64")
    except DatumShiftUnavailable:
        raise
    except Exception as exc:
        raise DatumShiftUnavailable(f"fallo al aplicar las mallas de geoide: {exc}")
    finally:
        network.set_network_enabled(prev)
    return z


def _default_shift(a_datum: str, b_datum: str) -> Callable | None:
    if (b_datum, a_datum) == ("EGM96", "EGM2008"):
        return pyproj_shift_egm96_to_egm2008
    return None


# --- comparacion ---

@dataclass
class ComparisonResult:
    rms_diff_m: float
    mean_diff_m: float
    slope_diff_mean_pct: float
    diff_map_path: Path
    n_pixels: int
    stats_scope: str  # "poligono" | "entorno"
    datum_a: str
    datum_b: str
    datum_aligned: bool
    datum_penalty: bool
    datum_shift_mean_m: float | None
    datum_note: str
    slope_diff_signed_mean_pct: float = 0.0
    sign_convention: str = "diferencia = b - a (AW3D30 - GLO-30)"
    extra: dict = field(default_factory=dict)


def _read(r) -> tuple[np.ndarray, rasterio.Affine, float | None]:
    with rasterio.open(r.path) as src:
        return src.read(1).astype("float32"), src.transform, src.nodata


def _slope_pct(path: Path, out: Path) -> np.ndarray:
    if out.exists():
        out.unlink()
    run_gdal("gdaldem", "slope", str(path), str(out), "-p", "-of", "GTiff")
    with rasterio.open(out) as src:
        s = src.read(1).astype("float32")
        nd = src.nodata
    s[~valid_mask(s, nd)] = np.nan
    return s


def compare_sources(
    a: "Raster",  # noqa: F821
    b: "Raster",  # noqa: F821
    poly: Polygon,
    *,
    a_datum: str = GLO30_DATUM,
    b_datum: str = AW3D30_DATUM,
    shift_fn: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
    out_dir: Path | None = None,
) -> ComparisonResult:
    """Alinea ``b`` a la malla de ``a`` (bilineal), lleva ``b`` al datum de ``a`` y mide la diferencia b - a."""
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para compare_sources")
    epsg = centroid_zone(poly)
    for label, r in (("a", a), ("b", b)):
        try:
            assert_crs(r, epsg)
        except RejectRequest as exc:
            raise RejectRequest(f"mezcla de CRS en compare_sources (raster {label}): {exc.reason}")
        assert_meters(r)
        assert_pixel_size(r, expected=30.0, tol=0.5)

    out = Path(out_dir) if out_dir is not None else Path(a.path).parent
    out.mkdir(parents=True, exist_ok=True)

    a_arr, a_tf, a_nd = _read(a)
    b_arr, b_tf, b_nd = _read(b)
    a_arr[~valid_mask(a_arr, a_nd)] = np.nan
    b_arr[~valid_mask(b_arr, b_nd)] = np.nan

    # 1. misma malla: remuestrear b a la de a
    b_on_a = np.full(a_arr.shape, np.nan, dtype="float32")
    rio_reproject(
        source=b_arr, destination=b_on_a, src_transform=b_tf, src_crs=f"EPSG:{epsg}", src_nodata=float("nan"),
        dst_transform=a_tf, dst_crs=f"EPSG:{epsg}", dst_nodata=float("nan"),
        resampling=Resampling.bilinear, init_dest_nodata=True,
    )

    # 2. mismo datum vertical
    shift_mean: float | None = None
    note = ""
    if a_datum == b_datum:
        aligned, note = True, f"ambas fuentes en {a_datum}"
    else:
        fn = shift_fn if shift_fn is not None else _default_shift(a_datum, b_datum)
        if fn is None:
            aligned = False
            note = f"datum de b ({b_datum!r}) no convertible a {a_datum} con las mallas disponibles"
        else:
            rows, cols = a_arr.shape
            xs = a_tf.c + (np.arange(cols) + 0.5) * a_tf.a
            ys = a_tf.f + (np.arange(rows) + 0.5) * a_tf.e
            X, Y = np.meshgrid(xs, ys)
            lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(X, Y)
            try:
                shift = np.asarray(fn(lon, lat), dtype="float64")
                if shift.shape != a_arr.shape:
                    raise DatumShiftUnavailable(f"forma de la correccion {shift.shape} != {a_arr.shape}")
                use = np.isfinite(b_on_a)
                if not np.isfinite(shift[use]).all():
                    raise DatumShiftUnavailable("la correccion de geoide devolvio valores no finitos")
            except DatumShiftUnavailable as exc:
                aligned, note, shift = False, f"conversion de datum {b_datum}->{a_datum} no disponible: {exc}", None
            else:
                b_on_a = (b_on_a + shift.astype("float32")).astype("float32")
                shift_mean = float(shift[use].mean()) if use.any() else None
                aligned = True
                note = f"b convertido de {b_datum} a {a_datum} con mallas de geoide"

    # 3. estadisticas (poligono; si hay < 9 pixeles, el entorno completo de la malla)
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    inside = geometry_mask([poly_utm], out_shape=a_arr.shape, transform=a_tf, all_touched=False, invert=True)
    both = np.isfinite(a_arr) & np.isfinite(b_on_a)
    mask = inside & both
    scope = "poligono"
    if int(mask.sum()) < MIN_POLY_PIXELS:
        mask, scope = both, "entorno"
    n = int(mask.sum())
    if n == 0:
        raise RejectRequest("compare_sources: las dos fuentes no se solapan (0 pixeles comparables)")
    diff = (b_on_a - a_arr).astype("float64")
    d = diff[mask]
    mean_d = float(d.mean())
    rms_d = float(math.sqrt(float((d ** 2).mean())))

    # 4. pendiente: ambos por gdaldem sobre la misma malla
    b_path = out / f"{Path(b.path).stem}_on_{Path(a.path).stem}.tif"
    with rasterio.open(
        b_path, "w", driver="GTiff", height=a_arr.shape[0], width=a_arr.shape[1], count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=a_tf, nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(b_on_a, 1)
    a_clean = out / f"{Path(a.path).stem}_cmp_a.tif"
    with rasterio.open(
        a_clean, "w", driver="GTiff", height=a_arr.shape[0], width=a_arr.shape[1], count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=a_tf, nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(a_arr, 1)
    sa = _slope_pct(a_clean, out / f"{a_clean.stem}_slope.tif")
    sb = _slope_pct(b_path, out / f"{b_path.stem}_slope.tif")
    sm = mask & np.isfinite(sa) & np.isfinite(sb)
    if sm.any():
        sd = (sb[sm] - sa[sm]).astype("float64")
        slope_abs, slope_signed = float(np.abs(sd).mean()), float(sd.mean())
    else:
        slope_abs, slope_signed = float("nan"), float("nan")

    # 5. mapa de diferencia (GeoTIFF; la figura la dibuja el informe)
    map_path = out / f"diff_{Path(b.path).stem}_minus_{Path(a.path).stem}.tif"
    dm = np.where(both, diff, np.nan).astype("float32")
    with rasterio.open(
        map_path, "w", driver="GTiff", height=dm.shape[0], width=dm.shape[1], count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=a_tf, nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(dm, 1)

    return ComparisonResult(
        rms_diff_m=rms_d,
        mean_diff_m=mean_d,
        slope_diff_mean_pct=slope_abs,
        diff_map_path=map_path,
        n_pixels=n,
        stats_scope=scope,
        datum_a=a_datum,
        datum_b=b_datum,
        datum_aligned=aligned,
        datum_penalty=not aligned,
        datum_shift_mean_m=shift_mean,
        datum_note=note,
        slope_diff_signed_mean_pct=slope_signed,
        extra={"b_aligned_path": str(b_path)},  # AW3D30 en la malla de GLO-30 y ya en EGM2008 (si datum_aligned)
    )
