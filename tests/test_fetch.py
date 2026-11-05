import re

import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from terreno.errors import RejectRequest
from terreno.fetch import (
    BASE_URL,
    GLO30_GRID,
    DemResult,
    TileGrid,
    fetch_dem,
    tile_extent,
    tile_name,
    tile_url,
)
from tests.fixtures.make_tiles import ramp, tile_name as synth_name, write_tile

# Grilla sintetica: tiles de 3x3 px, misma geometria (PixelIsPoint) que los reales.
GRID3 = TileGrid(rows=3, cols_for=lambda lat_idx: 3)

T071 = synth_name(-34, -71)
T070 = synth_name(-34, -70)

# Poligono chico (~220 m) dentro del tile W071 / S34
INSIDE = box(-70.600, -33.447, -70.598, -33.445)
# Poligono que cruza el borde W071 | W070 (-70 - 1/6 = -70.16667)
ACROSS = box(-70.1680, -33.447, -70.1654, -33.445)


class CountingOpener:
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return rasterio.open(url)


def run(poly, tmp_path, tiles, *, available=None, opener=None, **kw):
    root = tmp_path / "remote"
    for spec in tiles:
        write_tile(root, *spec[:2], **(spec[2] if len(spec) > 2 else {}))
    names = available if available is not None else {synth_name(s[0], s[1]) for s in tiles}
    return fetch_dem(
        poly, tmp_path / "cache", opener=opener, tile_list=set(names),
        base_url=str(root).replace("\\", "/"), grid=GRID3, **kw,
    )


def mosaic_values(res):
    with rasterio.open(res.path) as src:
        arr = src.read(1)
        t = src.transform
        crs = src.crs
    rows, cols = arr.shape
    lon = t.c + (np.arange(cols) + 0.5) * t.a
    lat = t.f + (np.arange(rows) + 0.5) * t.e
    lon, lat = np.meshgrid(lon, lat)
    return arr, lon, lat, crs, t


# --- nombres, URL y geometria de tiles (valores observados en el bucket real) ---

def test_tile_name_and_url_pattern_for_santiago():
    assert tile_name(-34, -71) == "Copernicus_DSM_COG_10_S34_00_W071_00_DEM"
    assert tile_name(0, 6) == "Copernicus_DSM_COG_10_N00_00_E006_00_DEM"
    assert tile_url(BASE_URL, -34, -71) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_S34_00_W071_00_DEM/Copernicus_DSM_COG_10_S34_00_W071_00_DEM.tif"
    )


def test_glo30_grid_and_extent_match_real_headers():
    assert GLO30_GRID.rows == 3600
    assert GLO30_GRID.cols_for(-34) == 3600
    assert GLO30_GRID.cols_for(-50) == 3600
    assert GLO30_GRID.cols_for(-51) == 2400  # S51: 2400 columnas (verificado en el bucket)
    assert GLO30_GRID.cols_for(-60) == 2400
    assert GLO30_GRID.cols_for(-61) == 1800
    # bounds reales leidos de S34_W071 y S51_W073 (rasterio, 2026-10-23)
    np.testing.assert_allclose(
        tile_extent(-34, -71, GLO30_GRID),
        (-71.00013888888888, -33.99986111111111, -70.00013888888888, -32.99986111111111), atol=1e-12)
    np.testing.assert_allclose(
        tile_extent(-51, -73, GLO30_GRID),
        (-73.00020833333333, -50.99986111111111, -72.00020833333333, -49.99986111111111), atol=1e-12)


# --- fetch_dem con tiles sinteticos ---

def test_single_tile_window(tmp_path):
    op = CountingOpener()
    res = run(INSIDE, tmp_path, [(-34, -71)], opener=op)
    assert isinstance(res, DemResult)
    assert res.tiles == [T071]
    assert len(op.calls) == 1  # los vecinos de margen no publicados no se abren
    arr, lon, lat, crs, t = mosaic_values(res)
    assert crs.to_epsg() == 4326
    assert np.isfinite(arr).all()
    np.testing.assert_allclose(arr, ramp(lon, lat), atol=1e-3)
    # el recorte cubre el bbox del poligono
    minx, miny, maxx, maxy = INSIDE.bounds
    left, top = t.c, t.f
    right, bottom = left + arr.shape[1] * t.a, top + arr.shape[0] * t.e
    assert left <= minx and right >= maxx and bottom <= miny and top >= maxy


def test_polygon_across_two_tiles_has_no_seam(tmp_path):
    res = run(ACROSS, tmp_path, [(-34, -71), (-34, -70)])
    assert res.tiles == sorted([T070, T071])
    arr, lon, lat, crs, t = mosaic_values(res)
    assert arr.shape == (3, 6)  # un solo mosaico: 2 tiles de 3 columnas
    assert np.isfinite(arr).all()  # sin huecos
    np.testing.assert_allclose(arr, ramp(lon, lat), atol=1e-3)
    # sin costura: el paso en el borde (col 2 -> 3) es igual al de cualquier otra columna
    steps = np.diff(arr[0])
    np.testing.assert_allclose(steps, steps[0], atol=1e-3)


def test_missing_tile_rejected(tmp_path):
    with pytest.raises(RejectRequest) as e:
        run(ACROSS, tmp_path, [(-34, -71)], available={T071})
    assert "tile no publicado" in e.value.reason
    assert T070 in e.value.reason


def test_missing_required_tile_checked_before_reading(tmp_path):
    op = CountingOpener()
    with pytest.raises(RejectRequest):
        run(ACROSS, tmp_path, [(-34, -71), (-34, -70)], available={T071}, opener=op)
    assert op.calls == []  # verificado contra tileList antes de leer


@pytest.mark.parametrize("kw", [{"fill": float("nan")}, {"nodata": -32767.0, "fill": -32767.0}])
def test_all_nodata_rejected(tmp_path, kw):
    with pytest.raises(RejectRequest) as e:
        run(INSIDE, tmp_path, [(-34, -71, kw)])
    assert "nodata" in e.value.reason


@pytest.mark.parametrize("nodata", [None, -32767.0])
def test_partial_nodata_fraction(tmp_path, nodata):
    def holes(lon, lat):
        z = ramp(lon, lat)
        z[lon > -70.5] = np.nan if nodata is None else nodata  # columna derecha
        return z

    res = run(INSIDE, tmp_path, [(-34, -71, {"func": holes, "nodata": nodata})])
    assert res.nodata_fraction == pytest.approx(3 / 9)
    arr = mosaic_values(res)[0]
    assert np.isnan(arr).sum() == 3  # el nodata de la fuente se normaliza a NaN


def test_manifest_fields_present(tmp_path):
    res = run(INSIDE, tmp_path, [(-34, -71)])
    assert res.path.exists()
    assert res.tiles == [T071]
    assert res.source_url.startswith("http") or "remote" in res.source_url
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", res.acquired)
    assert isinstance(res.nodata_fraction, float) and res.nodata_fraction == 0.0
    assert res.vertical_datum == "EGM2008"
    assert res.source == "glo30"
    assert len(res.tile_urls) == 1 and res.tile_urls[0].endswith(f"{T071}.tif")


def test_cache_hit_avoids_network(tmp_path):
    first = run(INSIDE, tmp_path, [(-34, -71)], opener=CountingOpener())

    def forbidden(url):
        raise AssertionError("no debe abrir la red con cache caliente")

    again = fetch_dem(
        INSIDE, tmp_path / "cache", opener=forbidden, tile_list={T071},
        base_url=str(tmp_path / "remote").replace("\\", "/"), grid=GRID3,
    )
    assert again.acquired == first.acquired
    a1 = mosaic_values(first)[0]
    a2 = mosaic_values(again)[0]
    np.testing.assert_array_equal(a1, a2)


def test_two_cold_runs_same_array(tmp_path):
    r1 = run(ACROSS, tmp_path / "a", [(-34, -71), (-34, -70)])
    r2 = run(ACROSS, tmp_path / "b", [(-34, -71), (-34, -70)])
    np.testing.assert_array_equal(mosaic_values(r1)[0], mosaic_values(r2)[0])


def test_unexpected_tile_geometry_rejected(tmp_path):
    with pytest.raises(RejectRequest) as e:
        run(INSIDE, tmp_path, [(-34, -71, {"size": (4, 4)})])
    assert "geometr" in e.value.reason


def test_unknown_source_rejected(tmp_path):
    with pytest.raises(RejectRequest):
        fetch_dem(INSIDE, tmp_path, source="srtm", tile_list=set())


def test_polygon_outside_chile_rejected(tmp_path):
    with pytest.raises(RejectRequest):
        fetch_dem(box(10.0, 50.0, 10.001, 50.001), tmp_path, tile_list=set())


def test_huge_polygon_rejected_before_any_download(tmp_path):
    with pytest.raises(RejectRequest) as e:
        fetch_dem(box(-70.0, -33.0, -67.0, -30.0), tmp_path, tile_list=set())
    assert "tiles" in e.value.reason


# --- prueba viva (excluida por defecto) ---

@pytest.mark.network
def test_live_rm01(tmp_path):
    from terreno.geometry import SITES, load_polygon

    poly = load_polygon(SITES["rm01_plano_urbano"]["polygon"])
    res = fetch_dem(poly, tmp_path)
    assert res.tiles == ["Copernicus_DSM_COG_10_S34_00_W071_00_DEM"]
    with rasterio.open(res.path) as src:
        arr = src.read(1)
    med = float(np.nanmedian(arr))
    assert 500.0 <= med <= 700.0, med
    assert res.nodata_fraction == 0.0
    assert res.source_url.startswith("https://copernicus-dem-30m.s3.amazonaws.com")


@pytest.mark.network
def test_live_polygon_across_real_tile_border_matches_source_pixels(tmp_path):
    """Borde real W071 | W070 (-70 - 0.5/3600): el mosaico coincide pixel a pixel con cada tile."""
    poly = box(-70.0010, -33.4000, -69.9990, -33.3980)
    res = fetch_dem(poly, tmp_path)
    assert res.tiles == [
        "Copernicus_DSM_COG_10_S34_00_W070_00_DEM",
        "Copernicus_DSM_COG_10_S34_00_W071_00_DEM",
    ]
    arr, lon, lat, _, _ = mosaic_values(res)
    assert np.isfinite(arr).all()
    # 5 puntos a cada lado del borde, muestreados directamente de cada tile
    row = arr.shape[0] // 2
    sel = [i for i in range(arr.shape[1]) if abs(lon[row, i] + 70.0) < 0.0012][:8]
    assert len(sel) >= 4
    for i in sel:
        x, y = float(lon[row, i]), float(lat[row, i])
        name = "S34_00_W070" if x >= -70.0 - 1e-9 else "S34_00_W071"
        url = f"{BASE_URL}/Copernicus_DSM_COG_10_{name}_00_DEM/Copernicus_DSM_COG_10_{name}_00_DEM.tif"
        with rasterio.open(url) as src:
            v = next(src.sample([(x, y)]))[0]
        assert arr[row, i] == pytest.approx(float(v), abs=1e-3), (x, y)


# --- margen configurable (ventana de contexto del GLB, Task 13 gap 1) ---

def _fine_run(poly, tmp_path, tiles, available, margin_px):
    n = 1200
    grid = TileGrid(rows=n, cols_for=lambda lat_idx: n)
    root = tmp_path / "remote"
    for lat_idx, lon_idx in tiles:
        write_tile(root, lat_idx, lon_idx, n=n)
    return fetch_dem(poly, tmp_path / "cache", tile_list=set(available), base_url=str(root).replace("\\", "/"),
                     grid=grid, margin_px=margin_px)


def test_margin_px_widens_window_and_default_is_two(tmp_path):
    poly = box(-70.600, -33.447, -70.598, -33.445)  # 2.4 px de 1/1200 grado
    base = _fine_run(poly, tmp_path / "a", [(-34, -71)], {T071}, 2)
    wide = _fine_run(poly, tmp_path / "b", [(-34, -71)], {T071}, 60)
    with rasterio.open(base.path) as a, rasterio.open(wide.path) as b:
        assert b.width - a.width == 2 * (60 - 2) and b.height - a.height == 2 * (60 - 2)
    default = fetch_dem(poly, tmp_path / "c", tile_list={T071}, base_url=str(tmp_path / "a" / "remote").replace("\\", "/"),
                        grid=TileGrid(rows=1200, cols_for=lambda lat_idx: 1200))
    with rasterio.open(base.path) as a, rasterio.open(default.path) as d:
        assert a.shape == d.shape


def test_wide_margin_tiles_are_optional_not_required(tmp_path):
    poly = box(-70.9990, -33.447, -70.9970, -33.445)  # pegado al borde oeste del tile W071
    only = _fine_run(poly, tmp_path / "a", [(-34, -71)], {T071}, 40)  # el vecino W072 no esta publicado: no rechaza
    assert only.tiles == [T071]
    both = _fine_run(poly, tmp_path / "b", [(-34, -71), (-34, -72)], {T071, synth_name(-34, -72)}, 40)
    assert both.tiles == sorted([T071, synth_name(-34, -72)])
    with rasterio.open(only.path) as a, rasterio.open(both.path) as b:
        assert b.width > a.width  # con el vecino publicado el contexto cruza el borde del tile


@pytest.mark.parametrize("bad", [-1, 0.5, True, 5000])
def test_margin_px_validated(tmp_path, bad):
    with pytest.raises(RejectRequest, match="margin_px"):
        _fine_run(INSIDE, tmp_path, [(-34, -71)], {T071}, bad)


def test_tile_list_download_retries_a_transient_dns_or_connection_error(tmp_path, monkeypatch):
    """Un corte de red (DNS) al bajar tileList.txt no debe rechazar el sitio al primer intento."""
    import requests

    import terreno.fetch as fetch_mod
    import terreno.retry as retry_mod

    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Resp:
        content = b"Copernicus_DSM_COG_10_S34_00_W071_00_DEM\n"

        def raise_for_status(self):
            return None

    def flaky_get(url, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise requests.exceptions.ConnectionError("Failed to resolve 'copernicus-dem-30m.s3.amazonaws.com'")
        return Resp()

    monkeypatch.setattr(requests, "get", flaky_get)
    names = fetch_mod._load_tile_list(tmp_path)
    assert "Copernicus_DSM_COG_10_S34_00_W071_00_DEM" in names and calls["n"] == 3


def test_tile_list_persistent_network_failure_is_still_a_rejection(tmp_path, monkeypatch):
    import requests

    import terreno.fetch as fetch_mod
    import terreno.retry as retry_mod

    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)

    def down(url, timeout):
        raise requests.exceptions.ConnectionError("Failed to resolve host")

    monkeypatch.setattr(requests, "get", down)
    with pytest.raises(RejectRequest, match="tileList"):
        fetch_mod._load_tile_list(tmp_path)
