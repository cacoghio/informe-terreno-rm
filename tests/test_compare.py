import io
import os

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from terreno.compare import (
    AW3D30_DATUM,
    ComparisonResult,
    DatumShiftUnavailable,
    aw3d30_params,
    compare_sources,
    fetch_aw3d30,
)
from terreno.errors import RejectRequest
from terreno.fetch import DemResult
from tests.fixtures.rasters import utm_ramp, write_raster

LEFT, TOP, RES, N = 350000.0, 6298000.0, 30.0, 20


def ll_poly(c0, r0, c1, r1, epsg=32719):
    x0, x1 = LEFT + c0 * RES, LEFT + c1 * RES
    y0, y1 = TOP - r1 * RES, TOP - r0 * RES
    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    pts = [to_ll.transform(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    return Polygon(pts)


POLY = ll_poly(5, 5, 15, 15)  # 10 x 10 px


def ramp(tmp_path, name, **kw):
    return utm_ramp(tmp_path / name, n=N, res=RES, left=LEFT, top=TOP, **kw)


EGM2008 = "EGM2008"


# --- compare_sources ---

def test_identical_rasters_zero_diff(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1, slope_y=0.05)
    b = ramp(tmp_path, "b.tif", slope_x=0.1, slope_y=0.05)
    res = compare_sources(a, b, POLY, b_datum=EGM2008)
    assert isinstance(res, ComparisonResult)
    assert res.rms_diff_m == pytest.approx(0.0, abs=1e-4)
    assert res.mean_diff_m == pytest.approx(0.0, abs=1e-4)
    assert res.slope_diff_mean_pct == pytest.approx(0.0, abs=1e-3)
    assert res.diff_map_path.exists() and res.diff_map_path.suffix == ".tif"
    assert res.datum_aligned is True and res.datum_penalty is False
    assert res.n_pixels == 100 and res.stats_scope == "poligono"


def test_constant_offset_detected(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1)
    b = ramp(tmp_path, "b.tif", slope_x=0.1, base=605.0)  # b = a + 5 m
    res = compare_sources(a, b, POLY, b_datum=EGM2008)
    assert res.mean_diff_m == pytest.approx(5.0, abs=1e-4)
    assert res.rms_diff_m == pytest.approx(5.0, abs=1e-4)
    assert res.slope_diff_mean_pct == pytest.approx(0.0, abs=1e-3)  # un offset no cambia la pendiente
    with rasterio.open(res.diff_map_path) as src:
        d = src.read(1)
    assert np.nanmax(d) == pytest.approx(5.0, abs=1e-4)


def test_misaligned_grids_are_resampled(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1, slope_y=0.04)
    # b: misma superficie (plano), pero malla desplazada 13 m y 7 m y con otra extension
    left_b, top_b, res_b, n_b = LEFT - 13.0, TOP + 7.0, 30.0, 26
    xs = left_b + (np.arange(n_b) + 0.5) * res_b
    ys = top_b - (np.arange(n_b) + 0.5) * res_b
    X, Y = np.meshgrid(xs, ys)
    z = 600.0 + 0.1 * (X - LEFT) + 0.04 * (Y - TOP)
    b = write_raster(tmp_path / "b.tif", z, epsg=32719, res=res_b, left=left_b, top=top_b)
    res = compare_sources(a, b, POLY, b_datum=EGM2008)
    assert res.rms_diff_m < 0.01  # bilineal es exacto sobre un plano
    assert res.n_pixels == 100


def test_small_polygon_uses_surrounding_window(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1)
    b = ramp(tmp_path, "b.tif", slope_x=0.1, base=602.0)
    small = ll_poly(8, 8, 10, 10)  # 4 px < 9
    res = compare_sources(a, b, small, b_datum=EGM2008)
    assert res.stats_scope == "entorno"
    assert res.n_pixels > 9
    assert res.rms_diff_m == pytest.approx(2.0, abs=1e-4)


def test_datum_shift_applied_and_aligned(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1)
    b = ramp(tmp_path, "b.tif", slope_x=0.1)
    shift_calls = []

    def shift(lon, lat):  # EGM96 -> EGM2008: +0.6 m en todo el sitio
        shift_calls.append(lon.shape)
        return np.full(lon.shape, 0.6)

    res = compare_sources(a, b, POLY, b_datum="EGM96", shift_fn=shift)
    assert shift_calls
    assert res.datum_aligned is True and res.datum_penalty is False
    assert res.datum_shift_mean_m == pytest.approx(0.6)
    assert res.mean_diff_m == pytest.approx(0.6, abs=1e-4)
    assert (res.datum_a, res.datum_b) == ("EGM2008", "EGM96")


def test_datum_shift_unavailable_flags_penalty(tmp_path):
    a = ramp(tmp_path, "a.tif", slope_x=0.1)
    b = ramp(tmp_path, "b.tif", slope_x=0.1)

    def no_grid(lon, lat):
        raise DatumShiftUnavailable("malla de geoide no disponible")

    res = compare_sources(a, b, POLY, b_datum="EGM96", shift_fn=no_grid)
    assert res.datum_aligned is False and res.datum_penalty is True
    assert res.datum_shift_mean_m is None
    assert "geoide" in res.datum_note


def test_unknown_datum_pair_is_not_aligned(tmp_path):
    a = ramp(tmp_path, "a.tif")
    b = ramp(tmp_path, "b.tif")
    res = compare_sources(a, b, POLY, b_datum="por confirmar")
    assert res.datum_aligned is False and res.datum_penalty is True


def test_nonfinite_datum_shift_is_unavailable(tmp_path):
    a = ramp(tmp_path, "a.tif")
    b = ramp(tmp_path, "b.tif")
    res = compare_sources(a, b, POLY, b_datum="EGM96", shift_fn=lambda lon, lat: np.full(lon.shape, np.inf))
    assert res.datum_aligned is False


def test_mixed_crs_rejected(tmp_path):
    a = ramp(tmp_path, "a.tif")
    b = utm_ramp(tmp_path / "b.tif", epsg=32718, n=N, res=RES, left=LEFT, top=TOP)
    with pytest.raises(RejectRequest, match="CRS"):
        compare_sources(a, b, POLY, b_datum=EGM2008)


def test_no_overlap_rejected(tmp_path):
    a = ramp(tmp_path, "a.tif")
    b = utm_ramp(tmp_path / "b.tif", n=N, res=RES, left=LEFT + 100000.0, top=TOP)
    with pytest.raises(RejectRequest):
        compare_sources(a, b, POLY, b_datum=EGM2008)


# --- fetch_aw3d30 (sin red: getter inyectado) ---

def aw3d_bytes(n=12, void=False):
    """GeoTIFF Int16 como lo entrega OpenTopography: -9999 en vacios."""
    arr = (700 + np.arange(n * n).reshape(n, n) % 7).astype("int16")
    if void:
        arr[:] = -9999
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=n, width=n, count=1, dtype="int16", crs="EPSG:4326",
                      transform=from_origin(-70.61, -33.44, 1 / 3600, 1 / 3600), nodata=-9999) as dst:
            dst.write(arr, 1)
        return mem.read()


class FakeResponse:
    def __init__(self, content, status=200, ctype="image/tiff"):
        self.content, self.status_code, self.headers = content, status, {"Content-Type": ctype}


def poly_small():
    return Polygon([(-70.6005, -33.4505), (-70.5995, -33.4505), (-70.5995, -33.4495), (-70.6005, -33.4495),
                    (-70.6005, -33.4505)])


def test_missing_api_key_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    called = []
    with pytest.raises(RejectRequest, match="OPENTOPOGRAPHY_API_KEY"):
        fetch_aw3d30(poly_small(), tmp_path, getter=lambda *a, **k: called.append(1))
    assert not called  # sin clave no se toca la red


def test_fetch_aw3d30_builds_request_and_hides_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "SECRETO123")
    seen = {}

    def getter(url, params, timeout):
        seen.update(url=url, params=dict(params))
        return FakeResponse(aw3d_bytes())

    dem = fetch_aw3d30(poly_small(), tmp_path, getter=getter)
    p = seen["params"]
    assert p["demtype"] == "AW3D30" and p["outputFormat"] == "GTiff" and p["API_Key"] == "SECRETO123"
    assert p["west"] < -70.6005 and p["east"] > -70.5995 and p["south"] < -33.4505 and p["north"] > -33.4495
    assert isinstance(dem, DemResult)
    assert dem.source == "aw3d30" and dem.vertical_datum == AW3D30_DATUM == "EGM96"
    assert "SECRETO123" not in dem.source_url and "SECRETO123" not in repr(dem)
    assert "API_Key" not in dem.source_url
    with rasterio.open(dem.path) as src:
        assert src.crs.to_epsg() == 4326 and src.dtypes[0] == "float32"
        assert np.isfinite(src.read(1)).all()
    # el dato crudo con la clave no queda en cache
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"SECRETO123" not in f.read_bytes()


def test_fetch_aw3d30_void_converted_to_nan_and_all_void_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "k")
    with pytest.raises(RejectRequest, match="vac"):
        fetch_aw3d30(poly_small(), tmp_path, getter=lambda u, p, t: FakeResponse(aw3d_bytes(void=True)))


def test_fetch_aw3d30_http_error_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "k")
    xml = b"<error>Error: API Key required for access.</error>"
    with pytest.raises(RejectRequest, match="OpenTopography"):
        fetch_aw3d30(poly_small(), tmp_path, getter=lambda u, p, t: FakeResponse(xml, status=401, ctype="application/xml"))


def test_fetch_aw3d30_outside_chile_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "k")
    far = Polygon([(10, 50), (10.001, 50), (10.001, 50.001), (10, 50.001), (10, 50)])
    with pytest.raises(RejectRequest):
        fetch_aw3d30(far, tmp_path, getter=lambda u, p, t: FakeResponse(b""))


def test_fetch_aw3d30_cache_hit_avoids_network(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "k")
    n = []

    def getter(u, p, t):
        n.append(1)
        return FakeResponse(aw3d_bytes())

    d1 = fetch_aw3d30(poly_small(), tmp_path, getter=getter)
    d2 = fetch_aw3d30(poly_small(), tmp_path, getter=getter)
    assert len(n) == 1 and d1.path == d2.path and d2.acquired == d1.acquired


# --- pruebas vivas (excluidas por defecto) ---

@pytest.mark.network
def test_live_default_datum_shift_matches_known_grids():
    """EGM96 -> EGM2008 con las mallas de PROJ (descarga por red): en Nunoa la diferencia es ~0.12 m."""
    from terreno.compare import pyproj_shift_egm96_to_egm2008

    s = pyproj_shift_egm96_to_egm2008(np.array([-70.6]), np.array([-33.45]))
    assert s[0] == pytest.approx(0.12, abs=0.1)
    s0 = pyproj_shift_egm96_to_egm2008(np.array([0.0]), np.array([0.0]))
    assert abs(s0[0]) < 0.5  # EGM96 17.16 m vs EGM2008 17.2 m


@pytest.mark.network
def test_live_aw3d30_vs_glo30_rm01(tmp_path):
    if not os.environ.get("OPENTOPOGRAPHY_API_KEY"):
        pytest.skip("falta la variable de entorno OPENTOPOGRAPHY_API_KEY (clave gratuita de OpenTopography, "
                    "la crea el fundador); sin ella no se puede bajar AW3D30")
    from terreno.fetch import fetch_dem
    from terreno.geometry import SITES, load_polygon
    from terreno.reproject import reproject

    poly = load_polygon(SITES["rm01_plano_urbano"]["polygon"])
    glo = reproject(fetch_dem(poly, tmp_path / "glo"), poly)
    aw_dem = fetch_aw3d30(poly, tmp_path / "aw")
    aw = reproject(aw_dem, poly)
    res = compare_sources(glo, aw, poly, b_datum=aw_dem.vertical_datum)
    assert res.datum_aligned is True
    assert 0.0 <= res.rms_diff_m <= 6.0, res  # plan: RMS esperable del orden de 1-6 m; fuera de rango -> revisar datums


def test_aw3d30_window_meets_opentopography_minimum_for_a_tiny_lot():
    """OpenTopography rechaza (HTTP 400) ventanas con un lado < ~250 m: un lote chico (~30 m) debe ampliarse."""
    import math

    lot = Polygon([(-70.6035, -33.4523), (-70.6032, -33.4523), (-70.6032, -33.4520), (-70.6035, -33.4520)])
    p = aw3d30_params(lot, "k")
    lat0 = (p["south"] + p["north"]) / 2
    height_m = (p["north"] - p["south"]) * 111_320.0
    width_m = (p["east"] - p["west"]) * 111_320.0 * math.cos(math.radians(lat0))
    assert height_m >= 400.0 and width_m >= 400.0
    assert p["south"] < lot.bounds[1] < lot.bounds[3] < p["north"]  # sigue conteniendo el lote
    assert p["west"] < lot.bounds[0] < lot.bounds[2] < p["east"]


def test_aw3d30_window_is_unchanged_for_a_large_polygon():
    big = Polygon([(-71.526, -32.968), (-71.522, -32.968), (-71.522, -32.965), (-71.526, -32.965)])
    p = aw3d30_params(big, "k")
    assert p["south"] == pytest.approx(-32.968 - 2 / 3600, abs=1e-6)
    assert p["east"] == pytest.approx(-71.522 + 2 / 3600, abs=1e-6)
