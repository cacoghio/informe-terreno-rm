import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

from terreno.errors import RejectRequest
from terreno.worldcover import CanopyStats, fetch_canopy_stats, tile_name, tile_url

# Tesela sintetica S36W072 (lat -36..-33, lon -72..-69) de 300 x 300 px (0.01 grados). Cada fila de 100 pixeles es una banda:
# filas 0-99 (lat -33..-34) = 10 arbolado a la izquierda de lon -70.5 y 50 construido a la derecha; 100-199 = 30 pasto;
# 200-299 = 80 agua.
def write_tile(root, name="S36W072", fill=None):
    arr = np.full((300, 300), 80, dtype="uint8")
    arr[:100, :150] = 10
    arr[:100, 150:] = 50
    arr[100:200, :] = 30
    if fill is not None:
        arr = fill(arr)
    path = root / f"ESA_WorldCover_10m_2021_v200_{name}_Map.tif"
    with rasterio.open(path, "w", driver="GTiff", height=300, width=300, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_origin(-72.0, -33.0, 0.01, 0.01)) as dst:
        dst.write(arr, 1)
    return path


def run(poly, root, **kw):
    return fetch_canopy_stats(poly, base_url=str(root).replace("\\", "/"), **kw)


def test_tile_name_and_url_match_real_bucket():
    assert tile_name(-36, -72) == "S36W072"
    assert tile_name(3, 12) == "N03E012"
    assert tile_url("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map", "S36W072") == (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_S36W072_Map.tif")


def test_returns_canopy_stats_with_tile(tmp_path):
    write_tile(tmp_path)
    s = run(box(-71.9, -33.9, -71.6, -33.1), tmp_path)
    assert isinstance(s, CanopyStats) and s.available and s.tiles == ["S36W072"]


def test_lot_in_tree_zone(tmp_path):
    write_tile(tmp_path)
    s = run(box(-71.9, -33.9, -71.6, -33.1), tmp_path)  # lat -33.9..-33.1 -> filas 10-89: solo arbolado
    assert s.canopy_pct == pytest.approx(100.0) and s.tree_pct == pytest.approx(100.0) and s.built_pct == pytest.approx(0.0)


def test_lot_in_grass_zone_is_zero_canopy(tmp_path):
    write_tile(tmp_path)
    s = run(box(-71.9, -34.9, -71.6, -34.1), tmp_path)  # filas 110-189: pasto
    assert s.canopy_pct == pytest.approx(0.0) and s.available is True


def test_mixed_tree_and_built_counts_both(tmp_path):
    write_tile(tmp_path)
    s = run(box(-70.6, -33.9, -70.4, -33.1), tmp_path)  # cruza lon -70.5: mitad arbolado, mitad construido
    assert s.canopy_pct == pytest.approx(100.0)
    assert s.tree_pct == pytest.approx(50.0, abs=3) and s.built_pct == pytest.approx(50.0, abs=3)


def test_half_grass_half_built(tmp_path):
    write_tile(tmp_path)
    s = run(box(-70.4, -34.1, -70.2, -33.9), tmp_path)  # lat -34.1..-33.9 cruza -34: mitad construido (fila<100) y mitad pasto
    assert s.canopy_pct == pytest.approx(50.0, abs=3)


def test_water_is_not_canopy(tmp_path):
    write_tile(tmp_path)
    s = run(box(-71.9, -35.9, -71.6, -35.1), tmp_path)
    assert s.canopy_pct == pytest.approx(0.0)


def test_lot_smaller_than_one_pixel_still_reads_the_touching_pixel(tmp_path):
    write_tile(tmp_path)
    s = run(box(-71.7002, -33.5002, -71.6998, -33.4998), tmp_path)
    assert s.available and s.n_pixels >= 1 and s.canopy_pct == pytest.approx(100.0)


def test_context_percent_is_reported_separately(tmp_path):
    write_tile(tmp_path)
    lot = box(-71.9, -34.9, -71.6, -34.1)  # pasto en el lote
    s = run(lot, tmp_path, context_m=60_000.0)  # ventana enorme: pasa el borde de la tesela (sin vecinas)
    assert s.canopy_pct == pytest.approx(0.0) and s.available is True  # el entorno falla, el predio no
    assert s.context_pct is None
    lot = box(-70.8, -34.9, -70.5, -34.1)
    s2 = run(lot, tmp_path, context_m=30_000.0)  # ~0.27 grados: sube hasta la banda de arbolado/construido
    assert s2.canopy_pct == pytest.approx(0.0) and s2.context_pct is not None and s2.context_pct > 0.0


def test_zero_is_nodata_and_excluded(tmp_path):
    write_tile(tmp_path, fill=lambda a: np.where(a == 30, 0, a).astype("uint8"))
    s = run(box(-71.9, -34.9, -71.6, -34.1), tmp_path)
    assert s.available is False and s.canopy_pct is None and "clase valida" in s.note


def test_missing_tile_is_unavailable_not_invented(tmp_path):
    s = run(box(-71.9, -34.9, -71.6, -34.1), tmp_path)
    assert s.available is False and s.canopy_pct is None and s.note.startswith("WorldCover: no se pudo leer")


def test_polygon_crossing_a_tile_edge_asks_for_both_tiles(tmp_path):
    write_tile(tmp_path)  # solo existe S36W072: la vecina S36W075 falta
    s = run(box(-72.5, -33.9, -71.5, -33.1), tmp_path)
    assert s.available is False and s.canopy_pct is None  # nunca se promedia solo la mitad leida


def test_invalid_polygon_is_rejected():
    with pytest.raises(RejectRequest):
        fetch_canopy_stats(Polygon())
