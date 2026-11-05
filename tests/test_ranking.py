import numpy as np
import pytest

from terreno.ranking import plane_slope_pct, reference_slopes, spearman
from terreno.truth import Footprint
from tests.fixtures.rasters import utm_ramp
from tests.test_truth import LEFT, RES, TOP, TO_LL, raster


def scatter(n=30):
    rng = np.random.default_rng(1)
    return rng.uniform(0, 600, n), rng.uniform(0, 600, n)


def test_plane_slope_recovers_known_slope():
    x, y = scatter()
    z = 100 + 0.08 * x + 0.06 * y  # pendiente = 10 %
    assert plane_slope_pct(x, y, z) == pytest.approx(10.0, abs=1e-6)


def test_plane_slope_none_for_collinear_footprints_or_too_few():
    x = np.linspace(0, 900, 30)
    assert plane_slope_pct(x, 0.5 * x, 100 + 0.05 * x) is None  # una sola traza: la pendiente transversal no esta determinada
    xs, ys = scatter(9)
    assert plane_slope_pct(xs, ys, xs) is None  # menos de 10


def test_plane_slope_none_for_tiny_spread():
    rng = np.random.default_rng(2)
    x, y = rng.uniform(0, 40, 20), rng.uniform(0, 40, 20)
    assert plane_slope_pct(x, y, 0.1 * x) is None


def test_spearman_basics():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4, 5], [1, 2, 2, 4, 5]) == pytest.approx(0.9746794, abs=1e-6)  # empates con rango promedio
    assert spearman([1, 2], [1, 2]) is None and spearman([1, 1, 1], [1, 2, 3]) is None


def _fps_on_truth_ramp(slope_x, n=25, seed=3, base=600.0):
    fps = []
    rng = np.random.default_rng(seed)
    for i in range(n):
        x, y = LEFT + rng.uniform(1.5, 17.5) * RES, TOP - rng.uniform(1.5, 17.5) * RES
        lon, lat = TO_LL.transform(x, y)
        z = base + slope_x * (x - LEFT)
        fps.append(Footprint(lon=lon, lat=lat, h_ellipsoid_m=z + 27, h_orthometric_m=z, source="atl08", date="2024-01-01",
                             quality_flag=1))
    return fps


def test_reference_slopes_context_vs_truth_on_a_ramp(tmp_path):
    r = raster(tmp_path, slope_x=0.1)  # DEM: 10 % hacia el este
    out = reference_slopes(r, _fps_on_truth_ramp(0.2))  # la verdad sube 20 %: el DEM subestima la pendiente
    assert out["n"] == 25
    assert out["truth_pct"] == pytest.approx(20.0, abs=0.2) and out["context_pct"] == pytest.approx(10.0, abs=0.3)
    assert "dem_pct" not in out  # AUD-1 I6: ya no se ajusta un plano al DEM muestreado en las huellas (tautologico)


def test_context_slope_comes_from_the_raster_not_from_the_footprint_heights(tmp_path):
    """AUD-1 I6: la pendiente que entrega el producto no puede depender de las alturas de las huellas, ni la de referencia del DEM."""
    r10 = raster(tmp_path, slope_x=0.1)
    a = reference_slopes(r10, _fps_on_truth_ramp(0.2))
    b = reference_slopes(r10, _fps_on_truth_ramp(0.05, base=900.0))  # otra verdad, mismo DEM
    assert a["context_pct"] == pytest.approx(b["context_pct"], abs=1e-9)
    assert a["truth_pct"] != pytest.approx(b["truth_pct"], abs=1.0)
    r30 = utm_ramp(tmp_path / "r30.tif", n=20, res=RES, left=LEFT, top=TOP, slope_x=0.3)  # otro DEM, misma verdad
    c = reference_slopes(r30, _fps_on_truth_ramp(0.2))
    assert c["truth_pct"] == pytest.approx(a["truth_pct"], abs=1e-9)
    assert c["context_pct"] == pytest.approx(30.0, abs=0.5)


def test_reference_slopes_insufficient(tmp_path):
    r = raster(tmp_path)
    assert reference_slopes(r, []) == {"n": 0, "truth_pct": None, "context_pct": None}
