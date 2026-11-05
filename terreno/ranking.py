"""Pendiente de referencia por sitio y concordancia de rankings (spec sec. 8 criterio 6).

Dos series independientes, comparadas sitio a sitio:

* ``truth_pct``: pendiente del PLANO ajustado a (x, y, altura de la huella) -- solo datos de referencia ICESat-2/GEDI.
* ``context_pct``: pendiente MEDIA que GLO-30 entrega en esa misma zona (promedio de la pendiente Horn de todas las celdas
  del raster dentro del bbox de las huellas utilizables). Es la pendiente que el producto calcula con ``compute_terrain``
  (gdaldem ~ Horn), a la escala del entorno, y NO depende de las alturas de las huellas.

Antes (AUD-1 I6) se ordenaba el plano ajustado al DEM muestreado en las huellas (``truth + diffs``), que con residuos de
2-5 m frente a diferencias de pendiente de 3-25 % salia ~1 aunque el producto ordenara mal: era casi tautologico.
Ambas son pendientes gruesas a la escala de las huellas (hasta ~1 km), NO la del predio; sirven para ordenar sitios.
"""
from __future__ import annotations

import math

import numpy as np
import rasterio
from pyproj import Transformer

from terreno.reproject import Raster
from terreno.truth import _horn_slope_pct, sample_residuals

MIN_FOOTPRINTS = 10
MAX_CONDITION = 1e3  # huellas casi alineadas (una sola traza) no fijan la pendiente transversal
MIN_SPREAD_M = 150.0  # extension minima de las huellas en el eje menor


def plane_slope_pct(x, y, z) -> float | None:
    """Pendiente (%) del plano z = a + b x + c y por minimos cuadrados; None si no esta bien determinado."""
    x, y, z = (np.asarray(v, dtype="float64") for v in (x, y, z))
    if x.size < MIN_FOOTPRINTS or not (np.isfinite(x).all() and np.isfinite(y).all() and np.isfinite(z).all()):
        return None
    xy = np.column_stack([x - x.mean(), y - y.mean()])
    s = np.linalg.svd(xy, compute_uv=False)
    if s[1] <= 0 or s[0] / s[1] > MAX_CONDITION or s[1] / math.sqrt(x.size) < MIN_SPREAD_M / 4.0:
        return None
    A = np.column_stack([np.ones_like(x), xy])
    coef, *_ = np.linalg.lstsq(A, z - z.mean(), rcond=None)
    return float(100.0 * math.hypot(coef[1], coef[2]))


def context_mean_slope_pct(raster: Raster, x, y) -> float | None:
    """Pendiente Horn media (%) de las celdas del raster cuyo centro cae en el bbox de (x, y); None sin celdas validas."""
    with rasterio.open(raster.path) as src:
        arr = src.read(1).astype("float64")
        tf, nodata = src.transform, src.nodata
    if nodata is not None and not math.isnan(nodata):
        arr[arr == nodata] = np.nan
    slope = _horn_slope_pct(arr, float(abs(tf.a)))
    h, w = arr.shape
    cx = tf.c + (np.arange(w) + 0.5) * tf.a
    cy = tf.f + (np.arange(h) + 0.5) * tf.e
    cols = (cx >= float(np.min(x))) & (cx <= float(np.max(x)))
    rows = (cy >= float(np.min(y))) & (cy <= float(np.max(y)))
    sel = slope[np.ix_(rows, cols)]
    sel = sel[np.isfinite(sel)]
    return float(sel.mean()) if sel.size else None


def reference_slopes(raster: Raster, fps: list) -> dict:
    """{'n', 'truth_pct', 'context_pct'} con el mismo conjunto de huellas utilizables; los porcentajes son None si no se puede."""
    res = sample_residuals(raster, fps)
    n = len(res.used)
    out = {"n": n, "truth_pct": None, "context_pct": None}
    if n < MIN_FOOTPRINTS:
        return out
    fwd = Transformer.from_crs(4326, raster.epsg, always_xy=True)
    x, y = fwd.transform([f.lon for f in res.used], [f.lat for f in res.used])
    truth = np.array([f.h_orthometric_m for f in res.used])
    out["truth_pct"] = plane_slope_pct(x, y, truth)
    if out["truth_pct"] is not None:  # mismas huellas, mismas condiciones de determinacion del plano
        out["context_pct"] = context_mean_slope_pct(raster, x, y)
    return out


def _ranks(v) -> np.ndarray:
    v = np.asarray(v, dtype="float64")
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v))
    i = 0
    while i < len(v):  # rangos promedio en empates
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(a, b) -> float | None:
    """Correlacion de rangos de Spearman; None con < 3 pares o sin variacion en alguna de las series."""
    a, b = np.asarray(a, dtype="float64"), np.asarray(b, dtype="float64")
    if a.size != b.size or a.size < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])
