"""Tier 2 (spec sec. 2 y 8 criterio 5): correccion de sesgo de GLO-30 contra huellas ICESat-2 / GEDI.

Modelo: UN desplazamiento vertical constante (mediana de DEM - verdad en las huellas de entrenamiento). Es lo unico que
10-20 huellas por sitio permiten ajustar sin sobreajustar, y es lo que la spec llama "correccion de sesgo". La mediana
(no el promedio) para que un par de huellas atipicas bajo dosel no arrastren el ajuste.

La ganancia SOLO se mide con huellas de prueba separadas de las de entrenamiento (>= 10 de cada una): el RMSE de las
huellas con que se ajusto seria optimista. La separacion es por traza (granulo + haz) cuando hay >= 2 trazas, porque las
huellas de una misma pasada estan correlacionadas; con una sola traza se usan bloques espaciales contiguos. Con menos de
2 x 10 huellas utilizables el resultado es ``validacion_insuficiente`` y NO se calcula ningun estadistico.

Promocion a Tier 2 (spec sec. 8 criterio 5): el RMSE contra la referencia independiente baja >= 10 % en >= 3 de 5 sitios.
"""
from __future__ import annotations

import hashlib
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

from terreno.errors import RejectRequest
from terreno.reproject import Raster
from terreno.truth import Footprint, sample_residuals

MIN_EACH = 10  # huellas minimas de entrenamiento Y de prueba (plan Task 14; spec sec. 8 "~10 huellas")
PROMOTION_MIN_REDUCTION_PCT = 10.0  # spec sec. 8 criterio 5 (hipotesis de umbral)
PROMOTION_MIN_SITES = 3  # de 5 sitios
N_SITES = 5
INSUFFICIENT = "validacion_insuficiente"
CORRECTION_MODEL = "desplazamiento vertical constante = mediana(DEM - verdad) en las huellas de entrenamiento"


@dataclass
class Split:
    train: list
    test: list
    method: str  # "por_traza" | "bloques_espaciales" | "" (insuficiente)
    status: str  # "ok" | "validacion_insuficiente"


@dataclass
class Tier2Result:
    status: str  # "ok" | "validacion_insuficiente"
    n_usable: int
    n_train: int
    n_test: int
    split_method: str
    bias_fitted_m: float | None  # DEM - verdad (mediana, entrenamiento); negativo = DEM por debajo de la verdad
    rmse_raw_m: float | None  # sobre las huellas de PRUEBA, GLO-30 crudo
    rmse_corrected_m: float | None  # sobre las mismas huellas de PRUEBA, GLO-30 corregido
    reduction_pct: float | None  # 100 * (crudo - corregido) / crudo; 0 si el crudo ya es 0
    mae_raw_m: float | None = None
    mae_corrected_m: float | None = None
    bias_test_raw_m: float | None = None
    bias_test_corrected_m: float | None = None
    correction_model: str = CORRECTION_MODEL


def _insufficient(n_usable: int, n_train: int = 0, n_test: int = 0) -> Tier2Result:
    return Tier2Result(status=INSUFFICIENT, n_usable=n_usable, n_train=n_train, n_test=n_test, split_method="",
                       bias_fitted_m=None, rmse_raw_m=None, rmse_corrected_m=None, reduction_pct=None)


def fit_bias(raster: Raster, fps_train: list[Footprint], *, min_each: int = MIN_EACH) -> float:
    """Mediana de (DEM - verdad) sobre las huellas de entrenamiento utilizables. < ``min_each`` -> RejectRequest."""
    res = sample_residuals(raster, fps_train)
    if res.diffs.size < min_each:
        raise RejectRequest(
            f"{INSUFFICIENT}: {res.diffs.size} huellas de entrenamiento utilizables (minimo {min_each}); "
            "no se corrige el sesgo con tan pocas")
    return float(np.median(res.diffs))


def bias_correct(raster: Raster, fps_train: list[Footprint], out_path: Path | None = None) -> Raster:
    """GLO-30 corregido: ``DEM - sesgo``. Misma malla, CRS y nodata; el raster de entrada no se toca."""
    bias = fit_bias(raster, fps_train)
    src_path = Path(raster.path)
    if out_path is None:
        out_path = src_path.with_name(f"{src_path.stem}_biascorr_{int(round(bias * 1000)):+d}mm.tif")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        arr = src.read(1).astype("float64")
        nodata = src.nodata
    valid = np.isfinite(arr)
    if nodata is not None and not math.isnan(nodata):
        valid &= arr != nodata
    out = arr.copy()
    out[valid] = arr[valid] - bias
    profile.update(dtype="float32")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out.astype("float32"), 1)
    return Raster(path=out_path, epsg=raster.epsg, pixel_m=raster.pixel_m, nodata=raster.nodata)


def _sort_key(f: Footprint) -> tuple:
    return (f.source, f.granule, f.beam, f.date, f.lat, f.lon, f.h_orthometric_m)


def split_footprints(fps: list[Footprint], *, min_each: int = MIN_EACH, seed: int = 0) -> Split:
    """Separa entrenamiento y prueba (disjuntos, determinista, independiente del orden de entrada).

    1) Por traza (fuente, granulo, haz) si hay >= 2 trazas y ambos lados quedan con >= ``min_each``.
    2) Si no, 4 bloques espaciales contiguos (orden lat, lon) alternados entrenamiento / prueba / entrenamiento / prueba.
    Con menos de 2 x ``min_each`` huellas: ``validacion_insuficiente``.
    """
    fps = sorted(fps, key=_sort_key)
    n = len(fps)
    if n < 2 * min_each:
        return Split([], [], "", INSUFFICIENT)

    groups: dict[tuple, list[Footprint]] = {}
    for f in fps:
        groups.setdefault((f.source, f.granule, f.beam), []).append(f)
    if len(groups) >= 2:
        order = sorted(groups, key=lambda k: hashlib.sha1(f"{seed}|{k}".encode()).hexdigest())
        train, test = [], []
        for k in order:
            (train if len(train) <= len(test) else test).extend(groups[k])
        if len(train) >= min_each and len(test) >= min_each:
            return Split(sorted(train, key=_sort_key), sorted(test, key=_sort_key), "por_traza", "ok")

    spatial = sorted(fps, key=lambda f: (f.lat, f.lon, f.source, f.granule, f.beam, f.date, f.h_orthometric_m))
    bounds = [round(n * i / 4) for i in range(5)]
    chunks = [spatial[bounds[i]:bounds[i + 1]] for i in range(4)]
    train = chunks[0] + chunks[2]
    test = chunks[1] + chunks[3]
    if len(train) < min_each or len(test) < min_each:  # no deberia pasar con n >= 2 * min_each
        return Split([], [], "", INSUFFICIENT)
    return Split(sorted(train, key=_sort_key), sorted(test, key=_sort_key), "bloques_espaciales", "ok")


def _rmse(d: np.ndarray) -> float:
    return float(math.sqrt(float((d ** 2).mean())))


def evaluate_bias_correction(raster: Raster, fps: list[Footprint], *, min_each: int = MIN_EACH, seed: int = 0) -> Tier2Result:
    """Ajusta el sesgo con las huellas de entrenamiento y mide el RMSE crudo vs corregido SOLO en las de prueba."""
    usable = sample_residuals(raster, fps).used  # calidad buena y dentro del raster
    split = split_footprints(usable, min_each=min_each, seed=seed)
    if split.status != "ok":
        return _insufficient(len(usable))
    bias = fit_bias(raster, split.train, min_each=min_each)
    with tempfile.TemporaryDirectory(prefix="tier2_") as tmp:
        corrected = bias_correct(raster, split.train, out_path=Path(tmp) / "corregido.tif")
        raw = sample_residuals(raster, split.test).diffs
        cor = sample_residuals(corrected, split.test).diffs
    if raw.size < min_each or cor.size < min_each:  # huellas de prueba que caen en celdas sin dato
        return _insufficient(len(usable), len(split.train), int(raw.size))
    rmse_raw, rmse_cor = _rmse(raw), _rmse(cor)
    reduction = 100.0 * (rmse_raw - rmse_cor) / rmse_raw if rmse_raw > 0 else 0.0
    return Tier2Result(
        status="ok", n_usable=len(usable), n_train=len(split.train), n_test=int(raw.size), split_method=split.method,
        bias_fitted_m=bias, rmse_raw_m=rmse_raw, rmse_corrected_m=rmse_cor, reduction_pct=reduction,
        mae_raw_m=float(np.abs(raw).mean()), mae_corrected_m=float(np.abs(cor).mean()),
        bias_test_raw_m=float(raw.mean()), bias_test_corrected_m=float(cor.mean()),
    )


def tier2_verdict(results: list[Tier2Result | None], *, n_sites: int = N_SITES) -> dict:
    """Veredicto del criterio 5. ``None`` = sitio sin huellas / sin credenciales (cuenta como no evaluado).

    promovido: >= 3 sitios bajan el RMSE >= 10 %. no_promovido: ni con todos los no evaluados favorables se llegaria a 3.
    validacion_insuficiente: faltan sitios por evaluar que todavia podrian cambiar el resultado.
    """
    padded = list(results) + [None] * max(0, n_sites - len(results))
    ok = [r for r in padded if r is not None and r.status == "ok"]
    passing = [r for r in ok if r.reduction_pct is not None and r.reduction_pct >= PROMOTION_MIN_REDUCTION_PCT]
    failing = len(ok) - len(passing)
    unevaluated = len(padded) - len(ok)
    if len(passing) >= PROMOTION_MIN_SITES:
        verdict = "promovido"
    elif len(passing) + unevaluated >= PROMOTION_MIN_SITES:
        verdict = INSUFFICIENT
    else:
        verdict = "no_promovido"
    return {
        "verdict": verdict, "n_sites_total": len(padded), "n_sites_passing": len(passing), "n_sites_failing": failing,
        "n_sites_unevaluated": unevaluated, "min_reduction_pct": PROMOTION_MIN_REDUCTION_PCT,
        "min_sites": PROMOTION_MIN_SITES,
        "reductions_pct": [None if (r is None or r.reduction_pct is None) else round(r.reduction_pct, 3) for r in padded],
    }
