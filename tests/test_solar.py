import math

import numpy as np
import pytest

from terreno.pipeline import run_pipeline
from terreno.solar import SUN_DATES, compute_solar, sun_position
from tests.fixtures.rasters import write_raster
from tests.test_pipeline import POLY, SHIFT, aux_ok, fetchers
from tests.test_terrain import LEFT, RES, TOP, ll_poly


def test_sun_position_known_values():
    # solsticio de diciembre en el hemisferio sur: sol alto al mediodia (90 - 33.45 + 23.44), hacia el norte
    el, az = sun_position(-33.45, "2026-12-21", 12.0)
    assert el == pytest.approx(90 - 33.45 + 23.44, abs=0.6) and min(az, 360.0 - az) < 0.5
    # solsticio de junio: 90 - 33.45 - 23.44 = 33.1 grados
    assert sun_position(-33.45, "2026-06-21", 12.0)[0] == pytest.approx(33.1, abs=0.6)
    # a las 6 h solares en el equinoccio sale por el este, a las 18 h se pone por el oeste
    el6, az6 = sun_position(-33.45, "2026-03-20", 6.0)
    assert abs(el6) < 2.0 and az6 == pytest.approx(90.0, abs=3.0)
    assert sun_position(-33.45, "2026-03-20", 18.0)[1] == pytest.approx(270.0, abs=3.0)
    # medianoche: bajo el horizonte
    assert sun_position(-33.45, "2026-06-21", 0.0)[0] < 0


def flat_raster(tmp_path, n=40, ridge=None):
    z = np.full((n, n), 600.0)
    if ridge is not None:
        z = ridge(z)
    return write_raster(tmp_path / "d.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)


def test_flat_site_hours_equal_geometric_day_length(tmp_path):
    r = flat_raster(tmp_path)
    s = compute_solar(r, ll_poly(17, 17, 23, 23))
    assert s["status"] == "ok" and len(s["horizon_deg"]) == 36
    assert max(s["horizon_deg"]) <= 0.5
    d = s["dates"]
    assert set(d) == {x[1] for x in SUN_DATES}
    for key, v in d.items():
        assert v["direct_sun_h"] == pytest.approx(v["day_length_h"], abs=0.05), key
    # largo del dia a -33.45: ~10,0 h en junio y ~14,3 h en diciembre (sin refraccion)
    assert d["2026-06-21"]["day_length_h"] == pytest.approx(10.0, abs=0.35)
    assert d["2026-12-21"]["day_length_h"] == pytest.approx(14.3, abs=0.35)
    assert d["2026-03-20"]["day_length_h"] == pytest.approx(12.0, abs=0.2)
    assert s["latitude"] == pytest.approx(-33.45, abs=0.2)


def test_ridge_to_the_east_blocks_morning_sun(tmp_path):
    def ridge(z):
        z[:, 28:] += 60.0  # cresta a ~ 330 m al este del punto
        return z

    r = flat_raster(tmp_path, ridge=ridge)
    s = compute_solar(r, ll_poly(17, 17, 23, 23))
    east = s["horizon_deg"][9]  # azimut 90 grados
    assert east == pytest.approx(math.degrees(math.atan(60.0 / 270.0)), abs=3.0)  # ~12 grados
    assert s["horizon_deg"][27] <= 0.5  # oeste despejado
    j = s["dates"]["2026-06-21"]
    assert j["direct_sun_h"] < j["day_length_h"] - 0.3  # pierde sol de la manana
    assert s["ray_length_m"]["min"] > 100.0


def test_solar_is_pure_function_of_raster(tmp_path):
    r = flat_raster(tmp_path)
    p = ll_poly(17, 17, 23, 23)
    assert compute_solar(r, p) == compute_solar(r, p)


def test_pipeline_manifest_carries_solar_and_stays_deterministic(tmp_path):
    a = run_pipeline(POLY, tmp_path / "a", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "ca")
    b = run_pipeline(POLY, tmp_path / "b", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "cb")
    s = a.manifest["results"]["solar"]
    assert s["status"] == "ok" and len(s["horizon_deg"]) == 36 and "2026-06-21" in s["dates"]
    assert a.result_hash == b.result_hash
