import numpy as np
import pytest
from shapely.geometry import box

from terreno.asserts import (
    assert_crs,
    assert_meters,
    assert_nodata_below,
    assert_pixel_size,
    assert_plausible_range,
    assert_polygon_inside,
)
from terreno.errors import RejectRequest
from terreno.reproject import Raster
from tests.fixtures.rasters import utm_ramp, write_raster


def good(tmp_path, **kw):
    return utm_ramp(tmp_path / "ok.tif", slope_x=0.1, **kw)


def test_wrong_crs_rejected(tmp_path):
    r = good(tmp_path, epsg=32718)  # raster en 32718 pero el centroide exige 32719
    with pytest.raises(RejectRequest) as e:
        assert_crs(r, 32719)
    assert "32718" in e.value.reason and "32719" in e.value.reason
    assert_crs(good(tmp_path, epsg=32719), 32719)  # camino feliz


def test_crs_declared_vs_file_mismatch_rejected(tmp_path):
    r = good(tmp_path, epsg=32718)
    lying = Raster(path=r.path, epsg=32719, pixel_m=30.0, nodata=float("nan"))
    with pytest.raises(RejectRequest):
        assert_crs(lying, 32719)


def test_degrees_not_meters_rejected(tmp_path):
    r = write_raster(tmp_path / "deg.tif", np.full((5, 5), 600.0), epsg=4326, res=1 / 3600, left=-70.6, top=-33.4)
    with pytest.raises(RejectRequest) as e:
        assert_meters(r)
    assert "metros" in e.value.reason
    assert_meters(good(tmp_path))


@pytest.mark.parametrize("res,ok", [(30.0, True), (30.4, True), (29.6, True), (31.0, False), (10.0, False), (90.0, False)])
def test_pixel_size(tmp_path, res, ok):
    r = write_raster(tmp_path / f"p{res}.tif", np.full((5, 5), 600.0), epsg=32719, res=res, left=348000, top=6297000)
    if ok:
        assert_pixel_size(r)
    else:
        with pytest.raises(RejectRequest):
            assert_pixel_size(r)


def test_pixel_size_non_square_rejected(tmp_path):
    r = write_raster(tmp_path / "ns.tif", np.full((5, 5), 600.0), epsg=32719, res=30.0, res_y=25.0, left=348000, top=6297000)
    with pytest.raises(RejectRequest):
        assert_pixel_size(r)


def test_nodata_over_threshold_rejected(tmp_path):
    arr = np.full((10, 10), 600.0)
    arr[:6, :] = np.nan  # 60 %
    r = write_raster(tmp_path / "nd.tif", arr, epsg=32719, res=30.0, left=348000, top=6297000)
    with pytest.raises(RejectRequest) as e:
        assert_nodata_below(r, 0.5)
    assert "nodata" in e.value.reason
    assert_nodata_below(r, 0.6)  # el borde exacto pasa (fraccion <= max)
    assert_nodata_below(good(tmp_path), 0.0)


def test_nodata_declared_value_counts(tmp_path):
    arr = np.full((10, 10), 600.0)
    arr[:6, :] = -9999.0
    r = write_raster(tmp_path / "nd2.tif", arr, epsg=32719, res=30.0, left=348000, top=6297000, nodata=-9999.0)
    with pytest.raises(RejectRequest):
        assert_nodata_below(r, 0.5)


def test_implausible_elevation_rejected(tmp_path):
    arr = np.full((10, 10), 600.0)
    arr[3, 3] = 99999.0
    r = write_raster(tmp_path / "hi.tif", arr, epsg=32719, res=30.0, left=348000, top=6297000)
    with pytest.raises(RejectRequest) as e:
        assert_plausible_range(r)
    assert "99999" in e.value.reason
    arr[3, 3] = -9999.0  # nodata sin declarar
    r2 = write_raster(tmp_path / "lo.tif", arr, epsg=32719, res=30.0, left=348000, top=6297000)
    with pytest.raises(RejectRequest):
        assert_plausible_range(r2)
    assert_plausible_range(good(tmp_path))
    assert_plausible_range(r, lo=0.0, hi=100000.0)  # limites configurables


def test_all_nodata_not_plausible(tmp_path):
    r = write_raster(tmp_path / "all.tif", np.full((4, 4), np.nan), epsg=32719, res=30.0, left=348000, top=6297000)
    with pytest.raises(RejectRequest):
        assert_plausible_range(r)


def test_polygon_outside_raster_rejected():
    bounds = (348000.0, 6296400.0, 348600.0, 6297000.0)
    assert_polygon_inside(box(348100, 6296500, 348200, 6296600), bounds)
    with pytest.raises(RejectRequest):
        assert_polygon_inside(box(348500, 6296500, 348700, 6296600), bounds)  # sobresale
    with pytest.raises(RejectRequest):
        assert_polygon_inside(box(0, 0, 10, 10), bounds)
    # poligono en grados contra bounds en metros (mezcla de CRS) -> fuera
    with pytest.raises(RejectRequest):
        assert_polygon_inside(box(-70.60, -33.45, -70.59, -33.44), bounds)


def test_inverted_bbox_rejected():
    poly = box(348100, 6296500, 348200, 6296600)
    for bad in [(348600.0, 6296400.0, 348000.0, 6297000.0),  # minx > maxx
                (348000.0, 6297000.0, 348600.0, 6296400.0),  # miny > maxy
                (348000.0, 6296400.0, 348000.0, 6297000.0),  # ancho cero
                (348000.0, float("nan"), 348600.0, 6297000.0)]:
        with pytest.raises(RejectRequest) as e:
            assert_polygon_inside(poly, bad)
        assert "bbox" in e.value.reason
