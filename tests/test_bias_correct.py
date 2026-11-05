"""Tier 2 (spec sec. 2 y 8 criterio 5): correccion de sesgo con huellas de entrenamiento SEPARADAS de las de prueba."""
import dataclasses

import pytest
import rasterio

from terreno.bias_correct import (
    MIN_EACH,
    PROMOTION_MIN_REDUCTION_PCT,
    PROMOTION_MIN_SITES,
    Tier2Result,
    bias_correct,
    evaluate_bias_correction,
    fit_bias,
    split_footprints,
    tier2_verdict,
)
from terreno.errors import RejectRequest
from terreno.reproject import Raster
from terreno.truth import Footprint, validate
from tests.test_truth import LEFT, RES, TOP, TO_LL, fps_on, raster


def key(f):
    return (f.lon, f.lat)


def with_tracks(fps, per_track):
    """Reparte las huellas en trazas (granulo, haz) de `per_track` huellas cada una."""
    return [dataclasses.replace(f, granule=f"G{i // per_track}", beam=f"gt{i // per_track}") for i, f in enumerate(fps)]


# --- fit_bias / bias_correct ---

def test_fit_bias_is_median_dem_minus_truth(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    fps = fps_on(r, 14, add=3.0) + fps_on(r, 2, add=100.0, x0=1.0)  # 2 atipicos con buena calidad
    assert fit_bias(r, fps) == pytest.approx(-3.0, abs=1e-3)  # mediana: el atipico no arrastra el ajuste


def test_bias_correct_removes_constant_bias(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    train = fps_on(r, 12, add=3.0)  # la verdad esta 3 m sobre el DEM
    test = fps_on(r, 12, add=3.0, x0=9.0)
    before = validate(r, test)
    assert before.bias_m == pytest.approx(-3.0, abs=1e-3)
    r2 = bias_correct(r, train)
    assert isinstance(r2, Raster)
    after = validate(r2, test)
    assert after.bias_m == pytest.approx(0.0, abs=1e-3) and after.rmse_m == pytest.approx(0.0, abs=1e-3)


def test_bias_correct_keeps_grid_and_does_not_touch_input(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    with rasterio.open(r.path) as s:
        a0, tf0, crs0, shape0 = s.read(1), s.transform, s.crs, s.shape
    r2 = bias_correct(r, fps_on(r, 12, add=2.0))
    assert r2.path != r.path and r2.path.exists()
    assert (r2.epsg, r2.pixel_m) == (r.epsg, r.pixel_m)
    with rasterio.open(r.path) as s:
        assert (s.read(1) == a0).all()  # el original no cambia
    with rasterio.open(r2.path) as s:
        assert s.transform == tf0 and s.crs == crs0 and s.shape == shape0
        assert (s.read(1) - a0).mean() == pytest.approx(2.0, abs=1e-4)  # DEM = verdad - 2 -> se sube 2 m


def test_bias_correct_below_minimum_reports_insufficient_not_a_number(tmp_path):
    r = raster(tmp_path)
    with pytest.raises(RejectRequest, match="validacion_insuficiente"):
        bias_correct(r, fps_on(r, MIN_EACH - 1, add=3.0))


def test_bias_correct_ignores_bad_quality_in_the_count(tmp_path):
    r = raster(tmp_path)
    with pytest.raises(RejectRequest, match="validacion_insuficiente"):
        bias_correct(r, fps_on(r, 12, add=3.0, flag=0))


# --- split_footprints ---

def test_split_gives_at_least_min_each_disjoint_and_deterministic(tmp_path):
    r = raster(tmp_path)
    fps = fps_on(r, 30, add=1.0, dx=0.25)
    s1 = split_footprints(fps)
    s2 = split_footprints(list(reversed(fps)))  # el orden de entrada no importa
    assert s1.status == "ok"
    assert len(s1.train) >= MIN_EACH and len(s1.test) >= MIN_EACH
    assert len(s1.train) + len(s1.test) == 30
    assert not ({key(f) for f in s1.train} & {key(f) for f in s1.test})
    assert [key(f) for f in s1.train] == [key(f) for f in s2.train]
    assert [key(f) for f in s1.test] == [key(f) for f in s2.test]


def test_split_keeps_each_track_on_one_side(tmp_path):
    r = raster(tmp_path)
    fps = with_tracks(fps_on(r, 40, add=1.0, dx=0.25), per_track=10)  # 4 trazas
    s = split_footprints(fps)
    assert s.status == "ok" and s.method == "por_traza"
    tr = {(f.granule, f.beam) for f in s.train}
    te = {(f.granule, f.beam) for f in s.test}
    assert tr and te and not (tr & te)  # una huella de prueba nunca comparte traza con las de entrenamiento


def test_split_falls_back_to_spatial_blocks_when_one_track(tmp_path):
    r = raster(tmp_path)
    s = split_footprints(fps_on(r, 24, add=1.0, dx=0.25))  # una sola traza (todas con granule vacio)
    assert s.status == "ok" and s.method == "bloques_espaciales"
    assert len(s.train) >= MIN_EACH and len(s.test) >= MIN_EACH


@pytest.mark.parametrize("n", [0, 1, 19])
def test_split_below_twice_the_minimum_is_insufficient(tmp_path, n):
    r = raster(tmp_path)
    s = split_footprints(fps_on(r, n, add=1.0))
    assert s.status == "validacion_insuficiente"


# --- evaluate_bias_correction ---

def test_evaluate_reports_rmse_drop_on_test_only(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    fps = with_tracks(fps_on(r, 40, add=3.0, dx=0.25), per_track=10)
    res = evaluate_bias_correction(r, fps)
    assert isinstance(res, Tier2Result) and res.status == "ok"
    assert res.n_train >= MIN_EACH and res.n_test >= MIN_EACH and res.n_train + res.n_test == 40
    assert res.rmse_raw_m == pytest.approx(3.0, abs=1e-3)
    assert res.rmse_corrected_m == pytest.approx(0.0, abs=1e-3)
    assert res.reduction_pct == pytest.approx(100.0, abs=0.1)
    assert res.bias_fitted_m == pytest.approx(-3.0, abs=1e-3)


def test_evaluate_does_not_show_a_gain_when_there_is_no_bias(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    fps = with_tracks(fps_on(r, 40, add=0.0, dx=0.25), per_track=10)
    res = evaluate_bias_correction(r, fps)
    assert res.status == "ok" and res.rmse_raw_m == pytest.approx(0.0, abs=1e-6)
    assert res.reduction_pct == 0.0  # sin sesgo no hay ganancia (y no se divide por cero)


def test_evaluate_insufficient_has_no_statistics(tmp_path):
    r = raster(tmp_path)
    res = evaluate_bias_correction(r, fps_on(r, 15, add=3.0))
    assert res.status == "validacion_insuficiente"
    assert res.rmse_raw_m is None and res.rmse_corrected_m is None and res.reduction_pct is None


def test_evaluate_counts_only_usable_footprints(tmp_path):
    r = raster(tmp_path)
    inside = fps_on(r, 12, add=3.0)
    out = []
    for i in range(20):  # fuera del raster (a 10 km)
        lon, lat = TO_LL.transform(LEFT + 10_000 + 30 * i, TOP - 100)
        out.append(Footprint(lon=lon, lat=lat, h_ellipsoid_m=600.0, h_orthometric_m=600.0, source="atl08",
                             date="2024-01-01", quality_flag=1))
    bad = fps_on(r, 20, add=3.0, flag=0, x0=8.0)
    res = evaluate_bias_correction(r, inside + out + bad)
    assert res.status == "validacion_insuficiente" and res.n_usable == 12


def test_evaluate_is_deterministic(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    fps = with_tracks(fps_on(r, 40, add=3.0, dx=0.25), per_track=10)
    a, b = evaluate_bias_correction(r, fps), evaluate_bias_correction(r, list(reversed(fps)))
    assert dataclasses.asdict(a) == dataclasses.asdict(b)


# --- veredicto (spec sec. 8 criterio 5) ---

def t2(red, status="ok"):
    return Tier2Result(status=status, n_usable=30, n_train=15, n_test=15, split_method="por_traza", bias_fitted_m=-1.0,
                       rmse_raw_m=4.0 if status == "ok" else None, rmse_corrected_m=3.0 if status == "ok" else None,
                       reduction_pct=red if status == "ok" else None)


def test_threshold_constants_are_the_spec_ones():
    assert PROMOTION_MIN_REDUCTION_PCT == 10.0 and PROMOTION_MIN_SITES == 3 and MIN_EACH == 10


def test_verdict_promoted_with_three_of_five_at_10_percent():
    v = tier2_verdict([t2(10.0), t2(25.0), t2(10.0), t2(-5.0), t2(0.0)])
    assert v["verdict"] == "promovido" and v["n_sites_passing"] == 3


def test_verdict_not_promoted_when_two_pass_and_rest_measured():
    v = tier2_verdict([t2(10.0), t2(25.0), t2(9.99), t2(-5.0), t2(0.0)])
    assert v["verdict"] == "no_promovido" and v["n_sites_passing"] == 2


def test_verdict_indeterminate_when_missing_sites_could_still_change_it():
    v = tier2_verdict([t2(30.0), t2(25.0), t2(0.0, status="validacion_insuficiente"), None, t2(-1.0)])
    assert v["verdict"] == "validacion_insuficiente" and v["n_sites_unevaluated"] == 2


def test_verdict_not_promoted_when_even_all_missing_sites_cannot_reach_three():
    v = tier2_verdict([t2(30.0), t2(-25.0), t2(-3.0), t2(-1.0), None])
    assert v["verdict"] == "no_promovido"


def test_verdict_without_any_footprints_is_insufficient_not_a_pass():
    v = tier2_verdict([None] * 5)
    assert v["verdict"] == "validacion_insuficiente" and v["n_sites_passing"] == 0
