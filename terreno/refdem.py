"""refdem: DSM de REFERENCIA fino para medir el error real de GLO-30 / AW3D30 (VALIDACION INTERNA SOLAMENTE).

Fuentes: dos nubes de puntos de OpenTopography (E. Hussain, BGS; CC BY 4.0 con la nota 'no se permite el uso comercial'):
  * 'pleiades': OTDS.022021.32719.1, Pleiades 2016, nube de 5 m, ERDAS 2015, EPSG:32719.
  * 'spot':     OTDS.022021.32719.2, SPOT 2014, nube de 10 m, ERDAS 2015, EPSG:32719.

CORTAFUEGOS (spec: los datos de referencia no deben llegar a informe, visor, GLB, video ni publicado/):
  * este modulo no importa ninguno de los modulos que producen entregables y NINGUNO de ellos lo importa (hay tests que lo
    verifican con ast);
  * solo escribe en la cache git-ignorada (``cache/refdem``) y en el directorio que el llamador indique (AUDITORIA/);
  * las funciones devuelven Raster/dict; jamas generan PDF, GLB, video ni publicado/.

Convenciones: sesgo = DEM evaluado - referencia (igual que ``truth``); DSM contra DSM; datum vertical EGM2008.
Uso reproducible:  ``python -m terreno.refdem --site rm04_piedemonte_alto --source pleiades``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import laspy
import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.warp import reproject as rio_reproject
from shapely.geometry import Polygon

from terreno.errors import RejectRequest
from terreno.geometry import SITES, centroid_zone, load_polygon
from terreno.reproject import Raster
from terreno.truth import _horn_slope_pct, assert_geoid_function, egm2008_undulation_pyproj

POC_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = POC_DIR / "cache" / "refdem"  # git-ignorada (cache/ en .gitignore)
AUDIT_DIR = POC_DIR.parent / "AUDITORIA"

EXPECTED_EPSG = 32719
GLO30_RES_M = 30.0
CONTEXT_M = 1500.0  # igual que la ventana de contexto del GLB
REF_MARGIN_M = 150.0  # la referencia se rasteriza algo mas ancha que la ventana de contexto
MIN_COVER = 0.5  # fraccion minima de celdas de referencia con dato cerca del lote para considerar el sitio 'dentro'
MIN_BLOCK_FILL = 0.5  # fraccion minima de celdas finas validas para promediar un pixel de 30 m
MIN_LOT_PIXELS = 9  # spec sec. 4: menos de 9 pixeles no es estadistica
NOISE_CLASSES = (7, 18)  # LAS: 7 = ruido bajo, 18 = ruido alto (LAStools lasnoise)
SLOPE_CLASSES_PCT = (("<5", 0.0, 5.0), ("5-15", 5.0, 15.0), ("15-30", 15.0, 30.0), (">30", 30.0, math.inf))
STATS = ("median", "max")
ARCSEC_M = 30.87

# Datum vertical de las nubes: ver AUDITORIA/error_vs_referencia_fina.md (seccion de evidencia). Valor por defecto fijado
# con la medicion empirica contra GLO-30 en zonas planas; las nubes NO declaran datum (VLR GeoKeys sin VerticalCSType).
DEFAULT_VERTICAL = "ellipsoidal"
VERTICALS = ("ellipsoidal", "orthometric")

LICENCIA = ("CC BY 4.0 con la nota del portal: 'this data is not permitted for commercial use' "
            "(uso no comercial: solo validacion interna, nunca en entregables)")
DATASETS = {
    "pleiades": {
        "file": "pleiades_denoised_UTM.laz", "res_m": 5.0, "epoca": "2016", "sensor": "Pleiades tri-stereo 0,5 m",
        "doi": "10.5069/G9CC0XWZ", "opentopography_id": "OTDS.022021.32719.1", "licencia": LICENCIA,
        "titulo": "Survey of the San Ramon Fault, Santiago, Chile with 2016 Pleiades imagery",
        "url": "https://opentopography.s3.sdsc.edu/dataspace/OTDS.022021.32719.1/pointcloud/pleiades_denoised_UTM.laz",
    },
    "spot": {
        "file": "spot_point_cloud_UTM.laz", "res_m": 10.0, "epoca": "2014", "sensor": "SPOT-6 estereo",
        "doi": "10.5069/G97M064K", "opentopography_id": "OTDS.022021.32719.2", "licencia": LICENCIA,
        "titulo": "Survey over Santiago, Chile from 2014 SPOT imagery",
        "url": "https://opentopography.s3.sdsc.edu/dataspace/OTDS.022021.32719.2/pointcloud/spot_point_cloud_UTM.laz",
    },
}


# ----------------------------------------------------------------------------------------------------------------------
# 1. nube de puntos -> raster fino
# ----------------------------------------------------------------------------------------------------------------------

def _check_bbox(bbox) -> tuple[float, float, float, float]:
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise RejectRequest("bbox_utm mal formado")
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise RejectRequest("bbox_utm no finito")
    if minx >= maxx or miny >= maxy:
        raise RejectRequest(f"bbox_utm invertido o vacio: ({minx:g}, {miny:g}, {maxx:g}, {maxy:g})")
    return minx, miny, maxx, maxy


def _snap(bbox, res: float) -> tuple[float, float, float, float]:
    """Amplia el bbox hasta multiplos de ``res`` (la malla queda alineada a la de 30 m cuando res divide 30)."""
    minx, miny, maxx, maxy = bbox
    return (math.floor(minx / res) * res, math.floor(miny / res) * res,
            math.ceil(maxx / res) * res, math.ceil(maxy / res) * res)


def _check_header_crs(header, path, expected_epsg: int) -> None:
    try:
        crs = header.parse_crs()
    except Exception as exc:
        raise RejectRequest(f"CRS ilegible en {Path(path).name}: {exc}")
    epsg = crs.to_epsg() if crs is not None else None
    if epsg is None:
        raise RejectRequest(f"{Path(path).name} no declara CRS: no se asume (se exige EPSG:{expected_epsg})")
    if epsg != expected_epsg:
        raise RejectRequest(f"CRS de {Path(path).name} = EPSG:{epsg}, se exige EPSG:{expected_epsg}")


def _collect_points(laz_path: Path, bbox, chunk_size: int, expected_epsg: int, noise=NOISE_CLASSES):
    """Lee la nube por bloques y devuelve (x, y, z) de los puntos dentro de ``bbox`` [minx, maxx) x [miny, maxy)."""
    minx, miny, maxx, maxy = bbox
    xs, ys, zs = [], [], []
    with laspy.open(str(laz_path)) as f:
        _check_header_crs(f.header, laz_path, expected_epsg)
        for pts in f.chunk_iterator(chunk_size):
            x, y = np.asarray(pts.x), np.asarray(pts.y)
            keep = (x >= minx) & (x < maxx) & (y >= miny) & (y < maxy)
            if noise:
                keep &= ~np.isin(np.asarray(pts.classification), noise)
            if keep.any():
                xs.append(x[keep])
                ys.append(y[keep])
                zs.append(np.asarray(pts.z)[keep])
    if not xs:
        e = np.empty(0, dtype="float64")
        return e, e, e
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def _write_raster(path: Path, arr: np.ndarray, left: float, top: float, res: float, epsg: int, tags: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with rasterio.open(
        tmp, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
        crs=f"EPSG:{epsg}", transform=Affine(res, 0.0, left, 0.0, -res, top), nodata=float("nan"), compress="deflate",
    ) as dst:
        dst.write(arr.astype("float32"), 1)
        dst.update_tags(**{k: str(v) for k, v in tags.items()})
    tmp.replace(path)


def rasterize_point_cloud(
    laz_path, bbox_utm, res_m: float, stat: str = "median", *,
    out_dir=None, chunk_size: int = 5_000_000, expected_epsg: int = EXPECTED_EPSG,
) -> Raster:
    """Rasteriza una nube LAS/LAZ (UTM) a una malla de ``res_m`` con la mediana o el maximo por celda.

    Solo se conservan los puntos del bbox (lectura por bloques: la nube de 160 M puntos no se carga entera). La malla se
    ajusta HACIA AFUERA a multiplos de ``res_m``. Celdas sin puntos = NaN. Se descartan las clases de ruido 7 y 18.
    Las alturas salen TAL COMO ESTAN en la nube (sin conversion de datum): ver ``reference_dsm``.
    """
    if stat not in STATS:
        raise RejectRequest(f"estadistico no soportado: {stat!r} (use 'median' o 'max')")
    try:
        res = float(res_m)
    except (TypeError, ValueError):
        raise RejectRequest("res_m no numerico")
    if not math.isfinite(res) or res <= 0:
        raise RejectRequest(f"res_m invalido: {res_m!r}")
    bbox = _snap(_check_bbox(bbox_utm), res)
    laz_path = Path(laz_path)
    if not laz_path.exists():
        raise RejectRequest(
            f"falta la nube de puntos {laz_path.name}: descargarla (ver DATASETS[...]['url']) en {laz_path.parent}")
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_CACHE / "rasters"
    st = laz_path.stat()
    key = hashlib.sha1(f"{laz_path.name}|{st.st_size}|{st.st_mtime_ns}|{bbox}|{res}|{stat}|{NOISE_CLASSES}".encode()
                       ).hexdigest()[:16]
    out = out_dir / f"{laz_path.stem}_{stat}_{res:g}m_{key}.tif"
    if out.exists():
        return Raster(path=out, epsg=expected_epsg, pixel_m=res, nodata=float("nan"))

    minx, miny, maxx, maxy = bbox
    x, y, z = _collect_points(laz_path, bbox, chunk_size, expected_epsg)
    ncols = int(round((maxx - minx) / res))
    nrows = int(round((maxy - miny) / res))
    arr = np.full((nrows, ncols), np.nan, dtype="float64")
    if z.size:
        col = np.minimum(((x - minx) // res).astype("int64"), ncols - 1)
        rowS = np.minimum(((y - miny) // res).astype("int64"), nrows - 1)  # fila contada desde el sur
        cell = rowS * ncols + col
        order = np.lexsort((z, cell))
        cell_s, z_s = cell[order], z[order]
        first = np.r_[0, np.flatnonzero(np.diff(cell_s)) + 1]
        count = np.diff(np.r_[first, cell_s.size])
        if stat == "median":
            val = 0.5 * (z_s[first + (count - 1) // 2] + z_s[first + count // 2])
        else:
            val = z_s[first + count - 1]
        c = cell_s[first]
        arr[nrows - 1 - c // ncols, c % ncols] = val
    _write_raster(out, arr, minx, maxy, res, expected_epsg, {
        "SOURCE_FILE": laz_path.name, "STAT": stat, "RES_M": res, "VERTICAL_DATUM": "as-in-point-cloud",
        "NOTE": "REFERENCIA INTERNA - no publicar (CC BY 4.0 no comercial)",
    })
    return Raster(path=out, epsg=expected_epsg, pixel_m=res, nodata=float("nan"))


# ----------------------------------------------------------------------------------------------------------------------
# 2. DSM de referencia de un sitio piloto
# ----------------------------------------------------------------------------------------------------------------------

def _geoid_grid(left: float, top: float, res: float, shape, epsg: int, geoid_fn) -> np.ndarray:
    """Ondulacion N (m) en el centro de cada celda: se evalua en una malla gruesa y se interpola (N varia ~1 mm/m)."""
    rows, cols = shape
    ci = np.unique(np.r_[np.arange(0, cols, max(cols // 24, 1)), cols - 1])
    ri = np.unique(np.r_[np.arange(0, rows, max(rows // 24, 1)), rows - 1])
    xs = left + (ci + 0.5) * res
    ys = top - (ri + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(X.ravel(), Y.ravel())
    n = np.asarray(geoid_fn(np.asarray(lon), np.asarray(lat)), dtype="float64").reshape(X.shape)
    if not np.isfinite(n).all():
        raise RejectRequest("conversion de geoide: ondulacion no finita")
    # interpolacion separable (columnas y luego filas)
    tmp = np.vstack([np.interp(np.arange(cols), ci, n[i]) for i in range(n.shape[0])])
    return np.vstack([np.interp(np.arange(rows), ri, tmp[:, j]) for j in range(cols)]).T


def reference_dsm(
    site_key: str, source: str = "pleiades", res_m: float | None = None, *,
    laz_path=None, cache_dir=None, sites: dict | None = None, context_m: float = CONTEXT_M,
    stat: str = "median", vertical: str = DEFAULT_VERTICAL, geoid_fn=None, expected_epsg: int = EXPECTED_EPSG,
) -> Raster | None:
    """DSM de referencia (EGM2008) del sitio piloto + ventana de contexto, o None si el sitio queda fuera de la huella.

    ``vertical`` dice en que datum vienen las alturas de la nube: 'ellipsoidal' (h WGS84; se resta el geoide EGM2008) u
    'orthometric' (ya EGM2008; no se toca). El resultado queda siempre en EGM2008, malla alineada a 30 m, y en la cache.
    """
    if source not in DATASETS:
        raise RejectRequest(f"fuente de referencia desconocida: {source!r} (use {sorted(DATASETS)})")
    if vertical not in VERTICALS:
        raise RejectRequest(f"vertical invalido: {vertical!r} (use {VERTICALS})")
    sites = SITES if sites is None else sites
    if site_key not in sites:
        raise RejectRequest(f"sitio desconocido: {site_key!r}")
    ds = DATASETS[source]
    res = float(ds["res_m"] if res_m is None else res_m)
    cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
    laz = Path(laz_path) if laz_path is not None else cache_dir / ds["file"]
    if not laz.exists():
        raise RejectRequest(f"falta la nube de puntos {laz.name}: descargar de {ds['url']} a {laz.parent}")

    poly = load_polygon(sites[site_key]["polygon"])
    if centroid_zone(poly) != expected_epsg:
        return None  # la nube esta en otra zona UTM: fuera de la huella
    fwd = Transformer.from_crs(4326, expected_epsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    pminx, pminy, pmaxx, pmaxy = poly_utm.bounds
    m = float(context_m) + REF_MARGIN_M
    bbox = _snap((pminx - m, pminy - m, pmaxx + m, pmaxy + m), GLO30_RES_M)

    with laspy.open(str(laz)) as f:
        _check_header_crs(f.header, laz, expected_epsg)
        hmin, hmax = f.header.mins, f.header.maxs
    if bbox[2] <= hmin[0] or bbox[0] >= hmax[0] or bbox[3] <= hmin[1] or bbox[1] >= hmax[1]:
        return None  # la ventana ni toca la caja del encabezado

    raw = rasterize_point_cloud(laz, bbox, res, stat, out_dir=cache_dir / "rasters", expected_epsg=expected_epsg)
    with rasterio.open(raw.path) as src:
        arr = src.read(1).astype("float64")
        tf = src.transform
    # cobertura cerca del lote: una huella con huecos (franja del satelite) no vale como 'dentro'
    c0 = max(int((pminx - REF_MARGIN_M - tf.c) // res), 0)
    c1 = min(int(math.ceil((pmaxx + REF_MARGIN_M - tf.c) / res)), arr.shape[1])
    r0 = max(int((tf.f - (pmaxy + REF_MARGIN_M)) // res), 0)
    r1 = min(int(math.ceil((tf.f - (pminy - REF_MARGIN_M)) / res)), arr.shape[0])
    near = arr[r0:r1, c0:c1]
    if near.size == 0 or np.isfinite(near).mean() < MIN_COVER:
        return None

    gname = getattr(geoid_fn, "__name__", "egm2008") if geoid_fn is not None else "egm2008"
    key = hashlib.sha1(f"{raw.path.name}|{vertical}|{gname}".encode()).hexdigest()[:16]
    out = cache_dir / "rasters" / f"ref_{source}_{res:g}m_egm2008_{key}.tif"
    if not out.exists():
        n_mean = 0.0
        if vertical == "ellipsoidal":
            if geoid_fn is None:
                assert_geoid_function(egm2008_undulation_pyproj)
                geoid_fn = egm2008_undulation_pyproj
            n = _geoid_grid(tf.c, tf.f, res, arr.shape, expected_epsg, geoid_fn)
            arr = arr - n
            n_mean = float(n.mean())
        _write_raster(out, arr, tf.c, tf.f, res, expected_epsg, {
            "SOURCE": source, "DOI": ds["doi"], "STAT": stat, "RES_M": res, "VERTICAL_DATUM": "EGM2008",
            "VERTICAL_INPUT": vertical, "GEOID_N_MEAN_M": f"{n_mean:.3f}", "EPOCH": ds["epoca"],
            "NOTE": "REFERENCIA INTERNA - no publicar (CC BY 4.0 no comercial)",
        })
    return Raster(path=out, epsg=expected_epsg, pixel_m=res, nodata=float("nan"))


# ----------------------------------------------------------------------------------------------------------------------
# 3. comparacion GLO-30 (u otro DEM de 30 m) contra la referencia
# ----------------------------------------------------------------------------------------------------------------------

def _read(r: Raster):
    with rasterio.open(r.path) as src:
        arr = src.read(1).astype("float64")
        nd = src.nodata
        tf = src.transform
        epsg = src.crs.to_epsg() if src.crs else None
    if nd is not None and not math.isnan(nd):
        arr[arr == nd] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    return arr, tf, epsg


def _metrics(d: np.ndarray) -> dict:
    d = np.asarray(d, dtype="float64")
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"n": 0, "bias_m": None, "mae_m": None, "rmse_m": None, "nmad_m": None, "p90_abs_m": None,
                "mediana_m": None}
    med = float(np.median(d))
    return {
        "n": int(d.size), "bias_m": float(d.mean()), "mae_m": float(np.abs(d).mean()),
        "rmse_m": float(math.sqrt(float((d ** 2).mean()))), "nmad_m": float(1.4826 * np.median(np.abs(d - med))),
        "p90_abs_m": float(np.percentile(np.abs(d), 90)), "mediana_m": med,
    }


def _slope_deg(arr: np.ndarray, res: float) -> np.ndarray:
    return np.degrees(np.arctan(_horn_slope_pct(arr, res) / 100.0))


def _slope_error(s_eval: np.ndarray, s_ref: np.ndarray, mask: np.ndarray) -> dict:
    m = mask & np.isfinite(s_eval) & np.isfinite(s_ref)
    if not m.any():
        return {"n": 0, "mae_deg": None, "bias_deg": None, "rmse_deg": None}
    d = s_eval[m] - s_ref[m]
    return {"n": int(m.sum()), "mae_deg": float(np.abs(d).mean()), "bias_deg": float(d.mean()),
            "rmse_deg": float(math.sqrt(float((d ** 2).mean())))}


def _block_mean(canvas: np.ndarray, k: int):
    """Promedio por bloques k x k exigiendo MIN_BLOCK_FILL de celdas validas; devuelve (media, relleno)."""
    h, w = canvas.shape[0] // k, canvas.shape[1] // k
    b = canvas.reshape(h, k, w, k)
    ok = np.isfinite(b)
    cnt = ok.sum(axis=(1, 3))
    s = np.where(ok, b, 0.0).sum(axis=(1, 3))
    fill = cnt / float(k * k)
    mean = np.where(fill >= MIN_BLOCK_FILL, s / np.maximum(cnt, 1), np.nan)
    return mean, fill


def _upsample(arr30: np.ndarray, tf30: Affine, shape, tf_fine: Affine, epsg: int) -> np.ndarray:
    dst = np.full(shape, np.nan, dtype="float32")
    rio_reproject(
        source=arr30.astype("float32"), destination=dst, src_transform=tf30, src_crs=f"EPSG:{epsg}",
        src_nodata=float("nan"), dst_transform=tf_fine, dst_crs=f"EPSG:{epsg}", dst_nodata=float("nan"),
        resampling=Resampling.bilinear, init_dest_nodata=True,
    )
    return dst.astype("float64")


class _Prep:
    """Todos los arreglos de la comparacion, en la malla de 30 m y en la malla fina de la referencia."""


def _prepare(glo30: Raster, ref: Raster, poly: Polygon, context_m: float) -> _Prep:
    g, gtf, gepsg = _read(glo30)
    r, rtf, repsg = _read(ref)
    if gepsg is None or repsg is None or gepsg != repsg or glo30.epsg != gepsg or ref.epsg != repsg:
        raise RejectRequest(f"mezcla de CRS entre el DEM (EPSG:{gepsg}) y la referencia (EPSG:{repsg})")
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para compare_to_reference")
    if centroid_zone(poly) != gepsg:
        raise RejectRequest(f"el poligono cae en la zona EPSG:{centroid_zone(poly)} y los rasters estan en EPSG:{gepsg}")
    res_g, res_r = abs(gtf.a), abs(rtf.a)
    k = int(round(res_g / res_r))
    if k < 1 or not math.isclose(k * res_r, res_g, rel_tol=1e-6) or not math.isclose(abs(rtf.e), res_r, rel_tol=1e-9):
        raise RejectRequest(f"la resolucion de la referencia ({res_r:g} m) no divide la del DEM ({res_g:g} m)")
    oc = (rtf.c - gtf.c) / res_r
    orow = (gtf.f - rtf.f) / res_r
    if not (math.isclose(oc, round(oc), abs_tol=1e-6) and math.isclose(orow, round(orow), abs_tol=1e-6)):
        raise RejectRequest("la referencia no esta alineada con la malla del DEM (bordes de celda no coinciden)")
    oc, orow = int(round(oc)), int(round(orow))

    H, W = g.shape
    canvas = np.full((H * k, W * k), np.nan, dtype="float64")
    r0, c0 = max(orow, 0), max(oc, 0)
    r1, c1 = min(orow + r.shape[0], H * k), min(oc + r.shape[1], W * k)
    if r1 > r0 and c1 > c0:
        canvas[r0:r1, c0:c1] = r[r0 - orow:r1 - orow, c0 - oc:c1 - oc]
    ref30, fill = _block_mean(canvas, k)

    fwd = Transformer.from_crs(4326, gepsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    pminx, pminy, pmaxx, pmaxy = poly_utm.bounds
    tf_fine = Affine(res_r, 0.0, gtf.c, 0.0, -res_r, gtf.f)

    def centres(tf, rows, cols):
        xs = tf.c + (np.arange(cols) + 0.5) * tf.a
        ys = tf.f + (np.arange(rows) + 0.5) * tf.e
        return np.meshgrid(xs, ys)

    def window(tf, rows, cols):
        X, Y = centres(tf, rows, cols)
        return (X >= pminx - context_m) & (X <= pmaxx + context_m) & (Y >= pminy - context_m) & (Y <= pmaxy + context_m)

    p = _Prep()
    p.k, p.res_g, p.res_r, p.epsg = k, res_g, res_r, gepsg
    p.gtf, p.tf_fine = gtf, tf_fine
    p.glo, p.ref30, p.ref_fine, p.fill = g, ref30, canvas, fill
    p.glo_up = _upsample(g, gtf, canvas.shape, tf_fine, gepsg)
    p.ref30_up = _upsample(ref30, gtf, canvas.shape, tf_fine, gepsg)
    p.win30 = window(gtf, H, W)
    p.win_fine = window(tf_fine, H * k, W * k)
    p.lot30 = geometry_mask([poly_utm], out_shape=g.shape, transform=gtf, all_touched=False, invert=True)
    p.lot_fine = geometry_mask([poly_utm], out_shape=canvas.shape, transform=tf_fine, all_touched=False, invert=True)
    p.poly_utm = poly_utm
    p.diff30 = g - ref30
    p.diff_fine = p.glo_up - canvas
    p.s_glo, p.s_ref = _slope_deg(g, res_g), _slope_deg(ref30, res_g)
    p.s_ref_pct = _horn_slope_pct(ref30, res_g)
    p.sf_glo, p.sf_ref = _slope_deg(p.glo_up, res_r), _slope_deg(canvas, res_r)
    return p


def _block(p: _Prep, mask30: np.ndarray, mask_fine: np.ndarray) -> dict:
    m30 = mask30 & np.isfinite(p.diff30)
    out = _metrics(p.diff30[m30])
    out["slope"] = _slope_error(p.s_glo, p.s_ref, m30)
    mf = mask_fine & np.isfinite(p.diff_fine)
    nat = _metrics(p.diff_fine[mf])
    nat["res_m"] = p.res_r
    nat["slope"] = _slope_error(p.sf_glo, p.sf_ref, mf)
    out["nativo"] = nat
    # detalle que la propia malla de 30 m ya pierde, medido contra la referencia (independiente del DEM evaluado)
    lost = (p.ref30_up - p.ref_fine)[mf]
    lost = lost[np.isfinite(lost)]
    out["detalle_perdido_30m_rmse_m"] = float(math.sqrt(float((lost ** 2).mean()))) if lost.size else None
    return out


def compare_to_reference(glo30: Raster, ref: Raster, poly: Polygon, context: bool = True, *,
                         context_m: float = CONTEXT_M) -> dict:
    """Error de un DEM de 30 m (GLO-30 o AW3D30 ya alineado, EGM2008) frente a la referencia fina. DSM contra DSM.

    La referencia se promedia (media, >= 50 % de celdas finas validas) a la malla del DEM antes de restar. En paralelo
    se compara a la resolucion nativa de la referencia contra el DEM remuestreado (bilineal) para mostrar el detalle que
    la malla de 30 m no ve. ``context`` mide la ventana poligono +- ``context_m``; el lote se mide solo con >= 9 pixeles.
    Sesgo = DEM - referencia. Clases de pendiente por la pendiente de la REFERENCIA (30 m): <5, 5-15, 15-30, >30 %.
    """
    p = _prepare(glo30, ref, poly, float(context_m))
    res: dict = {
        "convencion": "sesgo = DEM evaluado - referencia (DSM contra DSM); pendiente de Horn en grados",
        "res_referencia_m": p.res_r, "res_dem_m": p.res_g, "context_m": float(context_m) if context else None,
        "agregacion": f"media de celdas de {p.res_r:g} m por pixel de {p.res_g:g} m (>= {MIN_BLOCK_FILL:.0%} validas)",
        "context": None, "lote": None,
    }
    if context:
        m30 = p.win30 & np.isfinite(p.glo)
        n_glo = int(m30.sum())
        c = _block(p, p.win30, p.win_fine)
        if c["n"] == 0:
            raise RejectRequest("compare_to_reference: la referencia no cubre la ventana de contexto (0 pixeles comparables)")
        c["pixeles_dem_ventana"] = n_glo
        c["cobertura_ref"] = float(c["n"] / n_glo) if n_glo else 0.0
        by = {}
        for name, lo, hi in SLOPE_CLASSES_PCT:
            cm = p.win30 & np.isfinite(p.s_ref_pct) & (p.s_ref_pct >= lo) & (p.s_ref_pct < hi)
            by[name] = _metrics(p.diff30[cm])
        c["by_slope_class"] = by
        res["context"] = c
    n_lot = int((p.lot30 & np.isfinite(p.diff30)).sum())
    res["lote_n_pixeles"] = n_lot
    if n_lot >= MIN_LOT_PIXELS:
        res["lote"] = _block(p, p.lot30, p.lot_fine)
    return res


MIN_GEOID_N_M = 5.0  # bajo esto el geoide no distingue elipsoidal de ortometrica


def datum_evidence(median_glo_minus_cloud_m: float, geoid_n_m: float) -> dict:
    """Decide empiricamente el datum vertical de la nube con la mediana de (nube cruda - GLO-30) en la ventana.

    Si las alturas fueran elipsoidales (h), nube - GLO-30 ~ N (ondulacion EGM2008, ~28 m en Santiago); si fueran
    ortometricas (EGM2008), ~ 0. Gana la hipotesis cuyo residuo es menor. No concluyente si N es chica (< 5 m).
    """
    d = -float(median_glo_minus_cloud_m)  # nube cruda - GLO-30
    n = float(geoid_n_m)
    res_ell, res_ort = d - n, d
    if abs(n) < MIN_GEOID_N_M:
        verdict = "no_concluyente"
    else:
        verdict = "elipsoidal" if abs(res_ell) < abs(res_ort) else "ortometrica"
    return {"mediana_nube_menos_glo30_m": d, "ondulacion_egm2008_m": n, "residual_si_elipsoidal_m": res_ell,
            "residual_si_ortometrica_m": res_ort, "veredicto": verdict}


def render_diff_map(glo30: Raster, ref: Raster, poly: Polygon, out_png, *, context_m: float = CONTEXT_M,
                    title: str = "") -> Path:
    """PNG pequeno (interno) con el mapa de diferencia DEM - referencia a 30 m y a la resolucion nativa."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = _prepare(glo30, ref, poly, float(context_m))

    def crop(win):
        rows, cols = np.where(win)
        return rows.min(), rows.max() + 1, cols.min(), cols.max() + 1

    r0, r1, c0, c1 = crop(p.win30)
    d30 = np.where(p.win30, p.diff30, np.nan)[r0:r1, c0:c1]
    k = p.k
    f0, f1, g0, g1 = r0 * k, r1 * k, c0 * k, c1 * k
    dfi = np.where(p.win_fine, p.diff_fine, np.nan)[f0:f1, g0:g1]
    finite = np.concatenate([d30[np.isfinite(d30)], dfi[np.isfinite(dfi)]])
    vmax = max(float(np.percentile(np.abs(finite), 98)), 1.0) if finite.size else 1.0
    x0 = p.gtf.c + c0 * p.res_g
    x1 = p.gtf.c + c1 * p.res_g
    y1 = p.gtf.f - r0 * p.res_g
    y0 = p.gtf.f - r1 * p.res_g
    ext = (x0, x1, y0, y1)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.8), constrained_layout=True)
    for ax, d, name in ((axes[0], d30, f"{p.res_g:g} m (referencia promediada)"),
                        (axes[1], dfi, f"{p.res_r:g} m (DEM remuestreado)")):
        im = ax.imshow(d, cmap="RdBu_r", vmin=-vmax, vmax=vmax, extent=ext, interpolation="nearest")
        xs, ys = p.poly_utm.exterior.xy
        ax.plot(xs, ys, color="black", lw=1.2)
        ax.set_title(name, fontsize=9)
        ax.tick_params(labelsize=7)
        ax.ticklabel_format(style="plain", useOffset=False)
        ax.set_aspect("equal")
    fig.colorbar(im, ax=axes, shrink=0.8, label="DEM - referencia (m)")
    fig.suptitle((title + "  |  " if title else "") + "USO INTERNO (referencia CC BY 4.0 no comercial)", fontsize=9)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110)
    plt.close(fig)
    return out_png


# ----------------------------------------------------------------------------------------------------------------------
# 4. corrida por sitio y CLI
# ----------------------------------------------------------------------------------------------------------------------

def _glo30_context(poly: Polygon, cache: Path, context_m: float) -> Raster:
    """GLO-30 en la ventana de contexto (misma logica que el pipeline, sin importarlo: cortafuegos)."""
    from terreno.fetch import fetch_dem
    from terreno.reproject import reproject

    cache.mkdir(parents=True, exist_ok=True)
    lat = abs(poly.centroid.y)
    fetch_margin = math.ceil(context_m / (ARCSEC_M * math.cos(math.radians(lat)))) + 4
    dem = fetch_dem(poly, cache, margin_px=fetch_margin)
    return reproject(dem, poly, margin_px=math.ceil(context_m / GLO30_RES_M))


def _aw3d30_aligned(poly: Polygon, glo: Raster, cache: Path, context_m: float):
    """AW3D30 (OpenTopography) llevado a la malla y al datum de GLO-30 con compare_sources. None si no hay clave."""
    import os

    from shapely.geometry import box

    from terreno.compare import API_KEY_ENV, compare_sources, fetch_aw3d30

    if not os.environ.get(API_KEY_ENV):
        return None, {"estado": "no_disponible", "razon": f"falta {API_KEY_ENV}"}
    m_lat = (context_m + 3 * GLO30_RES_M) / 111_320.0
    m_lon = m_lat / max(math.cos(math.radians(poly.centroid.y)), 0.1)
    minx, miny, maxx, maxy = poly.bounds
    wide = box(minx - m_lon, miny - m_lat, maxx + m_lon, maxy + m_lat)
    dem = fetch_aw3d30(wide, cache)
    from terreno.reproject import reproject

    aw = reproject(dem, poly, margin_px=math.ceil(context_m / GLO30_RES_M))
    cmp_ = compare_sources(glo, aw, poly, out_dir=cache / "aw3d30_aligned")
    r = Raster(path=Path(cmp_.extra["b_aligned_path"]), epsg=glo.epsg, pixel_m=GLO30_RES_M, nodata=float("nan"))
    return r, {"estado": "ejecutado", "datum_alineado": bool(cmp_.datum_aligned), "nota_datum": cmp_.datum_note,
               "desfase_geoide_medio_m": cmp_.datum_shift_mean_m, "rms_diff_vs_glo30_m": cmp_.rms_diff_m}


def analyze_site(site: str, source: str, res_m: float | None, out_dir, *, aw3d30: bool = True, cache_dir=None,
                 context_m: float = CONTEXT_M, vertical: str = DEFAULT_VERTICAL) -> dict:
    """Corre la validacion de un sitio contra una referencia; escribe solo el PNG en ``out_dir``. Devuelve un dict JSON."""
    ds = DATASETS[source]
    cache = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
    out_dir = Path(out_dir)
    info = {"site": site, "source": source, "referencia": {k: ds[k] for k in
            ("titulo", "doi", "opentopography_id", "epoca", "sensor", "licencia")},
            "vertical_nube": vertical, "datum_comparacion": "EGM2008"}
    ref = reference_dsm(site, source, res_m, cache_dir=cache, context_m=context_m, vertical=vertical)
    if ref is None:
        info["estado"] = "fuera_de_huella"
        return info
    poly = load_polygon(SITES[site]["polygon"])
    glo = _glo30_context(poly, cache / "glo30" / site, context_m)
    info["estado"] = "ok"
    # evidencia del datum: nube CRUDA (sin restar geoide) contra GLO-30, en la misma ventana
    raw = reference_dsm(site, source, res_m, cache_dir=cache, context_m=context_m, vertical="orthometric")
    raw_med = compare_to_reference(glo, raw, poly, context_m=context_m)["context"]["mediana_m"]
    n_site = float(egm2008_undulation_pyproj(np.array([poly.centroid.x]), np.array([poly.centroid.y]))[0])
    info["evidencia_datum"] = datum_evidence(raw_med, n_site)
    info["res_referencia_m"] = ref.pixel_m
    info["glo30"] = compare_to_reference(glo, ref, poly, context_m=context_m)
    info["png_glo30"] = str(render_diff_map(glo, ref, poly, out_dir / f"diff_{site}_{source}_glo30.png",
                                            context_m=context_m, title=f"{site} - GLO-30 vs {source}"))
    if aw3d30:
        try:
            aw, meta = _aw3d30_aligned(poly, glo, cache / "glo30" / site, context_m)
        except RejectRequest as exc:
            aw, meta = None, {"estado": "no_disponible", "razon": exc.reason}
        info["aw3d30_meta"] = meta
        if aw is not None:
            info["aw3d30"] = compare_to_reference(aw, ref, poly, context_m=context_m)
    return info


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m terreno.refdem",
        description="Error real de GLO-30 / AW3D30 contra un DSM de referencia fino (Pleiades 2016 / SPOT 2014). "
                    "VALIDACION INTERNA: nada de esto entra a informe, visor, GLB, video ni publicado/.")
    p.add_argument("--site", required=True, help="clave de data/sites.json o 'all'")
    p.add_argument("--source", required=True, choices=sorted(DATASETS) + ["all"])
    p.add_argument("--res", type=float, default=None, help="resolucion de la referencia en m (5 pleiades, 10 spot)")
    p.add_argument("--out-dir", default=str(AUDIT_DIR), help="carpeta de salida del JSON y los PNG (interna)")
    p.add_argument("--cache-dir", default=None, help=f"cache git-ignorada (por defecto {DEFAULT_CACHE})")
    p.add_argument("--no-aw3d30", action="store_true", help="omitir AW3D30 (necesita OPENTOPOGRAPHY_API_KEY)")
    p.add_argument("--vertical", choices=VERTICALS, default=DEFAULT_VERTICAL,
                   help="datum vertical de la nube de puntos (ver evidencia en AUDITORIA)")
    p.add_argument("--context-m", type=float, default=CONTEXT_M)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    sites = sorted(SITES) if args.site == "all" else [args.site]
    sources = sorted(DATASETS) if args.source == "all" else [args.source]
    for s in sites:
        if s not in SITES:
            print(f"sitio desconocido: {s!r}; disponibles: {sorted(SITES)}", file=sys.stderr)
            return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for s in sites:
        for src in sources:
            try:
                info = analyze_site(s, src, args.res, out_dir, aw3d30=not args.no_aw3d30, cache_dir=args.cache_dir,
                                    context_m=args.context_m, vertical=args.vertical)
            except RejectRequest as exc:
                info = {"site": s, "source": src, "estado": "rechazado", "razon": exc.reason}
                rc = 1
            path = out_dir / f"error_vs_referencia_fina_{s}_{src}.json"
            path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{s} / {src}: {info.get('estado')} -> {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
