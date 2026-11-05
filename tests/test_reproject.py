import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform as shp_transform

from terreno.errors import RejectRequest
from terreno.fetch import DemResult
from terreno.reproject import Raster, reproject
from tests.fixtures.rasters import utm_ramp, write_raster

RES = 1.0 / 3600.0


def make_dem(tmp_path, lon0, lat0, n=40, fn=None, name="dem.tif", **kw) -> DemResult:
    """DEM sintetico EPSG:4326 (1 arcsec), centrado en (lon0, lat0), con z = fn(lon, lat)."""
    fn = fn or (lambda lon, lat: 600.0 + 1000.0 * (lon - lon0) + 500.0 * (lat - lat0))
    left, top = lon0 - n / 2 * RES, lat0 + n / 2 * RES
    cx = left + (np.arange(n) + 0.5) * RES
    cy = top - (np.arange(n) + 0.5) * RES
    lon, lat = np.meshgrid(cx, cy)
    r = write_raster(tmp_path / name, fn(lon, lat), epsg=4326, res=RES, left=left, top=top, **kw)
    return DemResult(path=r.path, tiles=["synthetic"], source_url="test://", acquired="2026-10-23", nodata_fraction=0.0)


def small_poly(lon0, lat0, half=0.0004):
    return box(lon0 - half, lat0 - half, lon0 + half, lat0 + half)


def test_reproject_output_is_utm_30m_float32(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45)
    r = reproject(dem, small_poly(-70.6, -33.45))
    assert isinstance(r, Raster)
    assert r.epsg == 32719 and r.pixel_m == 30.0
    with rasterio.open(r.path) as src:
        assert src.crs.to_epsg() == 32719
        assert src.dtypes[0] == "float32"
        assert src.res == (30.0, 30.0)
        # malla alineada a multiplos de 30 m (determinista)
        assert src.transform.c % 30.0 == pytest.approx(0.0, abs=1e-6) or src.transform.c % 30.0 == pytest.approx(30.0, abs=1e-6)
        assert src.width >= 3 and src.height >= 3


def test_reproject_preserves_known_value(tmp_path):
    lon0, lat0 = -70.6, -33.45
    dem = make_dem(tmp_path, lon0, lat0)
    r = reproject(dem, small_poly(lon0, lat0))
    to_ll = Transformer.from_crs(32719, 4326, always_xy=True)
    with rasterio.open(r.path) as src:
        arr = src.read(1)
        t = src.transform
    rows, cols = arr.shape
    xs = t.c + (np.arange(cols) + 0.5) * t.a
    ys = t.f + (np.arange(rows) + 0.5) * t.e
    X, Y = np.meshgrid(xs, ys)
    lon, lat = to_ll.transform(X, Y)
    expected = 600.0 + 1000.0 * (lon - lon0) + 500.0 * (lat - lat0)
    ok = np.isfinite(arr)
    assert ok.mean() > 0.8
    assert np.abs(arr[ok] - expected[ok]).max() < 0.5
    # punto conocido: el pixel central
    i, j = rows // 2, cols // 2
    assert abs(arr[i, j] - expected[i, j]) < 0.5


@pytest.mark.parametrize("lon0,lat0,epsg", [(-70.6, -33.45, 32719), (-72.5, -35.5, 32718), (-71.99, -34.0, 32719)])
def test_zone_comes_from_centroid_not_from_caller(tmp_path, lon0, lat0, epsg):
    dem = make_dem(tmp_path, lon0, lat0)
    r = reproject(dem, small_poly(lon0, lat0))
    assert r.epsg == epsg
    with rasterio.open(r.path) as src:
        assert src.crs.to_epsg() == epsg


def test_polygon_crossing_72w_single_zone(tmp_path):
    # centroide en -72.0001 (18S) con un lado en 19S: una sola zona, la del centroide
    lon0, lat0 = -72.0001, -35.0
    dem = make_dem(tmp_path, lon0, lat0)
    poly = box(-72.0003, lat0 - 0.0004, -71.9999, lat0 + 0.0004)
    assert poly.bounds[0] < -72.0 < poly.bounds[2]
    assert reproject(dem, poly).epsg == 32718


def test_mixed_crs_dem_in_utm_rejected(tmp_path):
    # una capa en UTM vive en EPSG:32719; un DemResult en UTM con poligono 4326 es mezcla -> falla dura
    r = utm_ramp(tmp_path / "utm.tif", epsg=32719, slope_x=0.05)
    dem = DemResult(path=r.path, tiles=["x"], source_url="t://", acquired="2026-10-23", nodata_fraction=0.0)
    with pytest.raises(RejectRequest) as e:
        reproject(dem, small_poly(-70.6, -33.45))
    assert "CRS" in e.value.reason


def test_mixed_crs_polygon_in_meters_rejected(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45)
    utm_poly = box(348000, 6296000, 348100, 6296100)  # metros (32719), no grados
    with pytest.raises(RejectRequest) as e:
        reproject(dem, utm_poly)
    assert "CRS" in e.value.reason


def test_polygon_outside_dem_rejected(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45)
    with pytest.raises(RejectRequest):
        reproject(dem, small_poly(-70.5, -33.45))  # ~9 km al este del DEM


def test_inverted_source_raster_rejected(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45, flip_y=True)
    with pytest.raises(RejectRequest) as e:
        reproject(dem, small_poly(-70.6, -33.45))
    assert "north-up" in e.value.reason or "invertid" in e.value.reason


def test_mostly_nodata_dem_rejected(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45, fn=lambda lon, lat: np.where(lat > -33.4535, np.nan, 600.0))
    with pytest.raises(RejectRequest) as e:
        reproject(dem, small_poly(-70.6, -33.45))
    assert "nodata" in e.value.reason


def test_implausible_dem_values_rejected(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45, fn=lambda lon, lat: np.full_like(lon, 99999.0))
    with pytest.raises(RejectRequest) as e:
        reproject(dem, small_poly(-70.6, -33.45))
    assert "plausible" in e.value.reason or "99999" in e.value.reason


def test_reproject_is_deterministic(tmp_path):
    dem = make_dem(tmp_path, -70.6, -33.45)
    poly = small_poly(-70.6, -33.45)
    with rasterio.open(reproject(dem, poly).path) as s1:
        a1 = s1.read(1)
    with rasterio.open(reproject(dem, poly).path) as s2:
        a2 = s2.read(1)
    np.testing.assert_array_equal(a1, a2)


@pytest.mark.network
def test_live_reproject_rm01(tmp_path):
    from terreno.fetch import fetch_dem
    from terreno.geometry import SITES, load_polygon

    poly = load_polygon(SITES["rm01_plano_urbano"]["polygon"])
    r = reproject(fetch_dem(poly, tmp_path), poly)
    assert r.epsg == 32719
    with rasterio.open(r.path) as src:
        arr = src.read(1)
    assert 590.0 < float(np.nanmedian(arr)) < 625.0
    assert np.isfinite(arr).all()


# --- margen configurable (ventana de contexto, Task 13 gap 1) ---

def test_margin_px_extends_grid_and_keeps_alignment(tmp_path):
    lon0, lat0 = -70.6, -33.45
    dem = make_dem(tmp_path, lon0, lat0, n=300)  # ~ 7,7 km x 9 km
    poly = small_poly(lon0, lat0)
    base = reproject(dem, poly)  # el nombre de salida depende del DEM: se lee antes de la 2a llamada
    with rasterio.open(base.path) as a:
        a_arr, a_t = a.read(1), a.transform
    wide = reproject(dem, poly, margin_px=50)
    with rasterio.open(wide.path) as b:
        b_arr, b_t = b.read(1), b.transform
    assert b_arr.shape[1] - a_arr.shape[1] == 2 * (50 - 3) and b_arr.shape[0] - a_arr.shape[0] == 2 * (50 - 3)
    # misma malla (multiplos de 30 m) y mismos valores en la parte comun
    assert (b_t.c - a_t.c) % 30.0 == pytest.approx(0.0, abs=1e-6)
    ox = int(round((a_t.c - b_t.c) / 30.0))
    oy = int(round((b_t.f - a_t.f) / 30.0))
    np.testing.assert_allclose(b_arr[oy:oy + a_arr.shape[0], ox:ox + a_arr.shape[1]], a_arr, atol=1e-3, equal_nan=True)


def test_margin_px_is_clipped_to_dem_coverage_and_validated(tmp_path):
    lon0, lat0 = -70.6, -33.45
    dem = make_dem(tmp_path, lon0, lat0, n=60)  # ~ 1,5 x 1,8 km: no alcanza para 1500 m de margen
    wide = reproject(dem, small_poly(lon0, lat0), margin_px=50)
    with rasterio.open(wide.path) as b, rasterio.open(dem.path) as d:
        assert b.width * 30 < 2 * 1500  # recortado a lo que cubre el DEM
    for bad in (0, -1, 2.5, True, 5000):
        with pytest.raises(RejectRequest, match="margin_px"):
            reproject(dem, small_poly(lon0, lat0), margin_px=bad)
