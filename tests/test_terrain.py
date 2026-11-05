import math
import sqlite3

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from shapely.geometry import Polygon

from terreno.errors import RejectRequest
from terreno.terrain import (
    SLOPE_CLASS_KEYS,
    TerrainResult,
    classify_slope,
    compute_terrain,
    relief_score,
)
from tests.fixtures.rasters import utm_ramp, write_raster

LEFT, TOP, RES, N = 350000.0, 6298000.0, 30.0, 20


def ll_poly(c0, r0, c1, r1, epsg=32719):
    """Poligono EPSG:4326 que cubre los pixeles [c0,c1) x [r0,r1) de la malla de prueba (bordes en limites de pixel)."""
    x0, x1 = LEFT + c0 * RES, LEFT + c1 * RES
    y0, y1 = TOP - r1 * RES, TOP - r0 * RES
    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    pts = [to_ll.transform(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    return Polygon(pts)


NINE = ll_poly(8, 8, 11, 11)  # 3 x 3 = 9 pixeles


def ramp(tmp_path, **kw):
    return utm_ramp(tmp_path / "dem.tif", n=N, res=RES, left=LEFT, top=TOP, **kw)


def test_plane_10_percent_slope(tmp_path):
    res = compute_terrain(ramp(tmp_path, slope_x=0.1), ll_poly(5, 5, 15, 15))
    assert isinstance(res, TerrainResult)
    s = res.stats
    assert s["n_pixels"] == 100 and s["inconclusive"] is False
    assert s["slope_mean_pct"] == pytest.approx(10.0, abs=0.2)
    assert s["slope_classes_pct"]["5-15"] == pytest.approx(100.0)
    assert set(s["slope_classes_pct"]) == set(SLOPE_CLASS_KEYS) == {"<5", "5-15", "15-30", ">30"}
    assert sum(s["slope_classes_pct"].values()) == pytest.approx(100.0)
    assert s["slope_mean_deg"] == pytest.approx(math.degrees(math.atan(0.1)), abs=0.1)
    for p in (res.slope_path, res.aspect_path, res.hillshade_path, res.contours_path):
        assert p.exists()
    assert res.contours_path.suffix == ".gpkg"


def test_elevation_stats_over_polygon(tmp_path):
    res = compute_terrain(ramp(tmp_path, slope_x=0.1, base=600.0), ll_poly(5, 5, 15, 15))
    s = res.stats
    # columnas 5..14, centros en x = 5.5..14.5 px -> z = 600 + 0.1 * 30 * (5.5..14.5)
    assert s["elev_min"] == pytest.approx(600 + 3.0 * 5.5, abs=1e-3)
    assert s["elev_max"] == pytest.approx(600 + 3.0 * 14.5, abs=1e-3)
    assert s["elev_mean"] == pytest.approx(600 + 3.0 * 10.0, abs=1e-3)
    assert s["elev_range"] == pytest.approx(27.0, abs=1e-3)
    assert s["elevation_within_noise"] is False


def test_flat_terrain_no_dominant_aspect(tmp_path):
    for kw in ({}, {"slope_x": 0.02}):  # plano exacto y 2 % (< 3 %)
        res = compute_terrain(ramp(tmp_path, **kw), NINE)
        s = res.stats
        assert s["aspect_dominant"] is None
        assert s["aspect_status"] == "sin_orientacion_predominante"
        assert s["slope_classes_pct"]["<5"] == pytest.approx(100.0)


@pytest.mark.parametrize(
    "sx,sy,label,idx",
    [(-0.1, 0.0, "E", 2), (0.1, 0.0, "O", 6), (0.0, -0.1, "N", 0), (0.0, 0.1, "S", 4),
     (-0.1, -0.1, "NE", 1), (0.1, -0.1, "NO", 7), (0.1, 0.1, "SO", 5), (-0.1, 0.1, "SE", 3)],
)
def test_aspect_directions(tmp_path, sx, sy, label, idx):
    # z baja hacia el este si slope_x < 0 -> la ladera mira al este
    res = compute_terrain(ramp(tmp_path, slope_x=sx, slope_y=sy), ll_poly(5, 5, 15, 15))
    s = res.stats
    assert s["aspect_dominant"] == label
    assert s["aspect_status"] == "ok"
    assert len(s["aspect_rose"]) == 8
    assert s["aspect_rose"][idx] == pytest.approx(100.0)
    assert sum(s["aspect_rose"]) == pytest.approx(100.0)


def test_aspect_east_facing(tmp_path):
    res = compute_terrain(ramp(tmp_path, slope_x=-0.15), NINE)
    assert res.stats["aspect_dominant"] == "E"
    assert res.stats["slope_classes_pct"]["15-30"] == pytest.approx(100.0)


def test_small_polygon_inconclusive(tmp_path):
    five = ll_poly(8, 9, 13, 10)  # 5 x 1 pixeles
    res = compute_terrain(ramp(tmp_path, slope_x=0.1), five)
    s = res.stats
    assert s["n_pixels"] == 5
    assert s["inconclusive"] is True
    for k in ("slope_classes_pct", "slope_mean_pct", "slope_mean_deg", "aspect_rose", "relief_score"):
        assert k not in s, k  # nunca una cifra de pendiente
    assert s["aspect_dominant"] is None and s["aspect_status"] == "no_concluyente"
    assert s["elev_min"] is not None  # la elevacion si se informa


def test_pixel_count_boundary_8_vs_9(tmp_path):
    dem = ramp(tmp_path, slope_x=0.1)
    assert compute_terrain(dem, ll_poly(8, 8, 17, 9)).stats["inconclusive"] is False  # 9 px en fila
    assert compute_terrain(dem, ll_poly(8, 8, 16, 9)).stats["inconclusive"] is True  # 8 px


def test_polygon_without_pixel_centers_is_inconclusive(tmp_path):
    tiny = ll_poly(8, 8, 9, 9)
    # reducir a 10 m dentro de un pixel (sin centros): no cubre ningun centro
    to_ll = Transformer.from_crs(32719, 4326, always_xy=True)
    x, y = LEFT + 8 * RES + 3, TOP - 8 * RES - 3
    pts = [to_ll.transform(x + dx, y + dy) for dx, dy in [(0, 0), (8, 0), (8, -8), (0, -8), (0, 0)]]
    res = compute_terrain(ramp(tmp_path, slope_x=0.1), Polygon(pts))
    assert res.stats["n_pixels"] == 0 and res.stats["inconclusive"] is True
    assert res.stats["elev_min"] is None and res.stats["elev_range"] is None


def test_noise_range_matches_the_calibrated_elev_range_threshold():
    """El aviso 'dentro del ruido' del informe y el corte C del factor rango de elevacion son el mismo numero."""
    from terreno.confidence import load_rules
    from terreno.terrain import NOISE_RANGE_M
    assert NOISE_RANGE_M == load_rules()["factors"]["elev_range_m"]["B"]["value"] == 4


@pytest.mark.parametrize("bump,expected", [(3.0, True), (3.99, True), (4.0, False), (20.0, False)])
def test_elevation_range_within_noise_flag(tmp_path, bump, expected):
    arr = np.full((N, N), 600.0)
    arr[9, 9] += bump
    r = write_raster(tmp_path / "bump.tif", arr, epsg=32719, res=RES, left=LEFT, top=TOP)
    s = compute_terrain(r, NINE).stats
    assert s["elev_range"] == pytest.approx(bump, abs=1e-3)
    assert s["elevation_within_noise"] is expected


def test_contours_interval_10m(tmp_path):
    res = compute_terrain(ramp(tmp_path, slope_x=0.1, base=600.0), NINE)
    con = sqlite3.connect(res.contours_path)
    try:
        layers = [r[0] for r in con.execute("select table_name from gpkg_contents")]
        assert "contour" in layers
        elevs = [r[0] for r in con.execute("select distinct elev from contour order by elev")]
    finally:
        con.close()
    assert len(elevs) >= 5
    assert all(abs(e / 10.0 - round(e / 10.0)) < 1e-6 for e in elevs)
    assert np.allclose(np.diff(elevs), 10.0)
    assert res.stats["contour_interval_m"] == 10


def test_stats_only_inside_polygon(tmp_path):
    # Mitad izquierda plana; mitad derecha muy empinada (50 %). El poligono esta en la plana, lejos.
    xs = np.arange(N)
    z = np.where(xs[None, :] < 12, 600.0, 600.0 + (xs[None, :] - 11) * 15.0) * np.ones((N, 1))
    r = write_raster(tmp_path / "split.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    s = compute_terrain(r, ll_poly(3, 5, 8, 15)).stats
    assert s["slope_classes_pct"]["<5"] == pytest.approx(100.0)
    s2 = compute_terrain(r, ll_poly(14, 5, 19, 15)).stats
    assert s2["slope_classes_pct"][">30"] == pytest.approx(100.0)


def test_classify_slope_boundaries():
    got = classify_slope(np.array([0.0, 4.99, 5.0, 14.99, 15.0, 29.99, 30.0, 80.0]))
    assert got == {"<5": 25.0, "5-15": 25.0, "15-30": 25.0, ">30": 25.0}


def test_relief_score_bounded_and_monotonic():
    slopes = [0, 1, 3, 5, 8, 12, 15, 20, 25, 35, 50, 80]
    ranges = [0, 4, 8, 15, 20, 40, 80, 150, 300, 1000]
    grid = [[relief_score(s, r) for r in ranges] for s in slopes]
    assert all(1 <= v <= 7 and isinstance(v, int) for row in grid for v in row)
    for row in grid:
        assert row == sorted(row)
    for col in zip(*grid):
        assert list(col) == sorted(col)
    assert grid[0][0] == 1 and grid[-1][-1] == 7


def test_relief_score_is_flagged_as_hypothesis(tmp_path):
    s = compute_terrain(ramp(tmp_path, slope_x=0.1), NINE).stats
    assert 1 <= s["relief_score"] <= 7
    assert s["relief_score_hypothesis"]["is_hypothesis"] is True
    assert "formula" in s["relief_score_hypothesis"]


def test_wrong_crs_raster_rejected(tmp_path):
    r = utm_ramp(tmp_path / "w.tif", epsg=32718, n=N, res=RES, left=LEFT, top=TOP, slope_x=0.1)
    with pytest.raises(RejectRequest) as e:
        compute_terrain(r, NINE)  # el centroide exige 32719
    assert "32718" in e.value.reason


def test_polygon_outside_raster_rejected(tmp_path):
    with pytest.raises(RejectRequest):
        compute_terrain(ramp(tmp_path, slope_x=0.1), ll_poly(30, 30, 35, 35))


def test_polygon_in_meters_rejected(tmp_path):
    from shapely.geometry import box

    with pytest.raises(RejectRequest) as e:
        compute_terrain(ramp(tmp_path, slope_x=0.1), box(350100, 6297700, 350300, 6297900))
    assert "CRS" in e.value.reason


def test_deterministic(tmp_path):
    a = compute_terrain(ramp(tmp_path, slope_x=0.07, slope_y=0.03), NINE).stats
    b = compute_terrain(ramp(tmp_path, slope_x=0.07, slope_y=0.03), NINE).stats
    assert a == b


def test_gdal_slope_matches_numpy_horn(tmp_path):
    """Chequeo cruzado: la pendiente % de gdaldem coincide con un calculo Horn propio en un relieve ondulado."""
    xs = (np.arange(N) + 0.5) * RES
    X, Y = np.meshgrid(xs, xs)
    z = 600 + 8 * np.sin(X / 90.0) + 6 * np.cos(Y / 70.0)
    r = write_raster(tmp_path / "wavy.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    res = compute_terrain(r, ll_poly(5, 5, 15, 15))
    with rasterio.open(res.slope_path) as src:
        g = src.read(1)
    zz = z
    dzdx = ((zz[:-2, 2:] + 2 * zz[1:-1, 2:] + zz[2:, 2:]) - (zz[:-2, :-2] + 2 * zz[1:-1, :-2] + zz[2:, :-2])) / (8 * RES)
    dzdy = ((zz[2:, :-2] + 2 * zz[2:, 1:-1] + zz[2:, 2:]) - (zz[:-2, :-2] + 2 * zz[:-2, 1:-1] + zz[:-2, 2:])) / (8 * RES)
    horn = 100 * np.hypot(dzdx, dzdy)
    np.testing.assert_allclose(g[1:-1, 1:-1], horn, atol=0.01)


@pytest.mark.network
def test_live_pilots_rm01_inconclusive_rm04_steep(tmp_path):
    from terreno.fetch import fetch_dem
    from terreno.geometry import SITES, load_polygon
    from terreno.reproject import reproject

    def run(key):
        p = load_polygon(SITES[key]["polygon"])
        return compute_terrain(reproject(fetch_dem(p, tmp_path / key), p), p).stats

    n = run("rm01_plano_urbano")  # lote de ~800 m2 ~ 1 pixel de 30 m: el piloto urbano sale "no concluyente"
    assert n["inconclusive"] is True and n["n_pixels"] < 9 and "slope_classes_pct" not in n
    assert 500 < n["elev_mean"] < 700
    v = run("rm04_piedemonte_alto")  # piedemonte alto: pendiente media > 15 %
    assert v["inconclusive"] is False and v["slope_mean_pct"] > 15
