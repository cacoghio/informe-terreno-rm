import numpy as np
import pytest
from shapely.geometry import box

from terreno.fetch import GLO30_GRID, TileGrid, DemResult
from terreno.layers import (
    AUX_KINDS,
    AuxStats,
    aux_url,
    fetch_aux_stats,
)
from tests.fixtures.make_tiles import tile_name, write_aux

GRID3 = TileGrid(rows=3, cols_for=lambda lat_idx: 3)
T071 = tile_name(-34, -71)
# El poligono cubre los 9 pixeles del tile W071/S34 (centros en lon -71, -70.667, -70.333; lat -33, -33.333, -33.667)
ALL9 = box(-71.2, -33.9, -70.2, -33.0)
ONE = box(-71.01, -33.35, -70.99, -33.31)  # solo el pixel centro-izquierda (lon -71, lat -33.333)


def dem(tiles=(T071,), base="C:/x"):
    return DemResult(path=None, tiles=list(tiles), source_url=base, acquired="2026-10-23", nodata_fraction=0.0)


def write_all(root, edm=1, flm=2, wbm=0, hem=1.5):
    write_aux(root, -34, -71, "EDM", edm)
    write_aux(root, -34, -71, "FLM", flm)
    write_aux(root, -34, -71, "WBM", wbm)
    write_aux(root, -34, -71, "HEM", hem, dtype="float32", nodata=-32767.0)


def run(poly, root, **kw):
    return fetch_aux_stats(poly, dem(base=str(root).replace("\\", "/")), base_url=str(root).replace("\\", "/"),
                           grid=GRID3, **kw)


def test_aux_url_pattern_matches_real_bucket():
    assert aux_url("https://copernicus-dem-30m.s3.amazonaws.com", T071, "EDM") == (
        "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_S34_00_W071_00_DEM/AUXFILES/"
        "Copernicus_DSM_COG_10_S34_00_W071_00_EDM.tif")
    assert set(AUX_KINDS) == {"EDM", "FLM", "WBM", "HEM"}


def test_clean_land_is_zero_void_and_hem_p90(tmp_path):
    write_all(tmp_path, hem=1.5)
    s = run(ALL9, tmp_path)
    assert isinstance(s, AuxStats)
    assert s.n_pixels == 9 and s.available is True
    assert s.void_water_fill_pct == pytest.approx(0.0)
    assert s.hem_p90_m == pytest.approx(1.5)


def test_water_fill_and_flatten_counted(tmp_path):
    edm = np.ones((3, 3)); edm[0, 0] = 9; edm[0, 1] = 7; edm[1, 1] = 2; edm[2, 2] = 4  # lago, aplanado, relleno, suavizado
    wbm = np.zeros((3, 3)); wbm[0, 0] = 2
    flm = np.full((3, 3), 2); flm[1, 1] = 5  # SRTM
    write_all(tmp_path, edm=edm, flm=flm, wbm=wbm)
    s = run(ALL9, tmp_path)
    # pixeles: (0,0) lago, (0,1) aplanado, (1,1) relleno -> 3 de 9; el suavizado (4) no cuenta como vacio/agua
    assert s.void_water_fill_pct == pytest.approx(100.0 * 3 / 9)
    assert s.counts["water"] == 1 and s.counts["flattened"] == 1 and s.counts["filled"] == 1


def test_wbm_alone_marks_water(tmp_path):
    wbm = np.zeros((3, 3)); wbm[2, 0] = 1  # oceano
    write_all(tmp_path, wbm=wbm)
    s = run(ALL9, tmp_path)
    assert s.void_water_fill_pct == pytest.approx(100.0 / 9)


def test_hem_excludes_edited_pixels_and_uses_p90(tmp_path):
    hem = np.arange(1, 10, dtype="float32").reshape(3, 3)
    hem[0, 0] = -32767.0
    write_all(tmp_path, hem=hem)
    s = run(ALL9, tmp_path)
    vals = hem[hem > 0]
    assert s.hem_p90_m == pytest.approx(float(np.percentile(vals, 90)))
    assert s.n_hem_valid == 8


def test_hem_all_edited_is_none(tmp_path):
    write_all(tmp_path, hem=-32767.0)
    s = run(ALL9, tmp_path)
    assert s.hem_p90_m is None and s.void_water_fill_pct is not None


def test_only_pixels_under_polygon_count(tmp_path):
    edm = np.ones((3, 3)); edm[0, 0] = 9
    write_all(tmp_path, edm=edm)
    s = run(ONE, tmp_path)
    assert s.n_pixels == 1
    assert s.void_water_fill_pct == pytest.approx(0.0)  # el pixel de agua esta fuera del poligono


def test_missing_aux_layer_is_unavailable_not_zero(tmp_path):
    write_aux(tmp_path, -34, -71, "EDM", 1)  # faltan FLM, WBM y HEM
    s = run(ALL9, tmp_path)
    assert s.void_water_fill_pct is None and s.hem_p90_m is None
    assert s.available is False and "FLM" in s.note


def test_geometry_mismatch_is_unavailable(tmp_path):
    write_aux(tmp_path, -34, -71, "EDM", 1, n=4)  # otra grilla que el DEM
    write_aux(tmp_path, -34, -71, "FLM", 2)
    write_aux(tmp_path, -34, -71, "WBM", 0)
    write_aux(tmp_path, -34, -71, "HEM", 1.0, dtype="float32", nodata=-32767.0)
    s = run(ALL9, tmp_path)
    assert s.void_water_fill_pct is None and "EDM" in s.note


@pytest.mark.network
def test_live_aux_layers_rm03(tmp_path):
    from terreno.fetch import fetch_dem
    from terreno.geometry import SITES, load_polygon

    poly = load_polygon(SITES["rm03_piedemonte_oriente"]["polygon"])
    d = fetch_dem(poly, tmp_path)
    s = fetch_aux_stats(poly, d)
    assert s.available is True and s.n_pixels >= 5
    assert 0.0 <= s.void_water_fill_pct <= 100.0
    assert s.hem_p90_m is None or 0.0 < s.hem_p90_m < 30.0


# --- AUD-1 I7: fallos de lectura de capas auxiliares: reintento y estado explicito ---

def _flaky_opener(fail_times, exc_factory):
    import rasterio

    calls = {"n": 0}

    def opener(url):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc_factory()
        return rasterio.open(url)

    return opener, calls


def test_transient_read_errors_of_aux_layers_are_retried(tmp_path, monkeypatch):
    import terreno.layers as layers

    monkeypatch.setattr(layers.time, "sleep", lambda s: None)
    write_all(tmp_path)
    opener, calls = _flaky_opener(2, lambda: OSError("HTTP response code: 503"))
    s = run(ALL9, tmp_path, opener=opener)
    assert s.available is True and s.unavailable == [] and s.void_water_fill_pct == pytest.approx(0.0)
    assert calls["n"] >= 3 + 3  # 2 reintentos del primer archivo + 4 capas (EDM, FLM, WBM, HEM)... al menos


def test_persistent_failure_marks_the_factors_explicitly_unavailable(tmp_path, monkeypatch):
    import terreno.layers as layers

    monkeypatch.setattr(layers.time, "sleep", lambda s: None)
    write_all(tmp_path)

    def always_503(url):
        raise OSError("HTTP response code: 503")

    s = run(ALL9, tmp_path, opener=always_503)
    assert s.available is False and s.void_water_fill_pct is None and s.hem_p90_m is None
    assert set(s.unavailable) == {"EDM", "FLM", "WBM", "HEM"}
    assert s.unavailable_factors() == ["void_water_pct", "hem_p90_m"]
    assert "503" in s.note


def test_a_missing_layer_file_is_not_retried(tmp_path, monkeypatch):
    import terreno.layers as layers

    monkeypatch.setattr(layers.time, "sleep", lambda s: pytest.fail("no se espera por un archivo que no existe"))
    calls = {"n": 0}

    def missing(url):
        calls["n"] += 1
        raise OSError(f"{url}: No such file or directory")

    s = run(ALL9, tmp_path, opener=missing)
    assert calls["n"] == 4 and s.available is False  # un intento por capa, ninguno repetido
    assert s.unavailable_factors() == ["void_water_pct", "hem_p90_m"]


def test_hem_alone_unavailable_only_marks_the_hem_factor(tmp_path, monkeypatch):
    import terreno.layers as layers

    monkeypatch.setattr(layers.time, "sleep", lambda s: None)
    write_aux(tmp_path, -34, -71, "EDM", 1)
    write_aux(tmp_path, -34, -71, "FLM", 2)
    write_aux(tmp_path, -34, -71, "WBM", 0)  # falta HEM
    s = run(ALL9, tmp_path)
    assert s.unavailable_factors() == ["hem_p90_m"] and s.void_water_fill_pct == pytest.approx(0.0)
