"""tools/elegir_pilotos_rm.py: seleccion reproducible de los 8 pilotos RM con datos publicos (Task RM-3)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from shapely.geometry import box

from tools import elegir_pilotos_rm as ep

SITES_PATH = Path(__file__).resolve().parent.parent / "data" / "sites.json"
PAUSADOS_PATH = Path(__file__).resolve().parent.parent / "data" / "sites_regiones_pausados.json"

KEYS = [
    "rm01_plano_urbano", "rm02_plano_periurbano", "rm03_piedemonte_oriente", "rm04_piedemonte_alto",
    "rm05_piedemonte_sur", "rm06_cerro_isla_urbano", "rm07_lomaje_norte", "rm08_precordillera",
]


# --- funciones puras --------------------------------------------------------------------------------------------------

def test_square_lot_has_requested_area_and_is_axis_aligned():
    sq = ep.cuadrado_utm(350_000.0, 6_300_000.0, 10_000.0)
    assert sq.bounds == (349_950.0, 6_299_950.0, 350_050.0, 6_300_050.0)
    assert sq.area == pytest.approx(10_000.0)


def test_window_mean_uses_pixel_centres_and_min_window():
    tf = Affine(30.0, 0, 0.0, 0, -30.0, 3000.0)  # 100 x 100 px
    arr = np.full((100, 100), 2.0, dtype="float32")
    arr[:, 50:] = 20.0
    sat = ep.integral(arr)
    # lote de 30 m en x=1500: la ventana minima (150 m) abarca 5 px, todos al borde pero con centros a ambos lados
    m_left = ep.ventana_media(sat, tf, 1000.0, 1500.0, 30.0)
    m_right = ep.ventana_media(sat, tf, 2500.0, 1500.0, 30.0)
    assert m_left == pytest.approx(2.0) and m_right == pytest.approx(20.0)
    assert 2.0 < ep.ventana_media(sat, tf, 1500.0, 1500.0, 30.0) < 20.0


def test_window_mean_ignores_nan_and_is_nan_when_empty():
    tf = Affine(30.0, 0, 0.0, 0, -30.0, 3000.0)
    arr = np.full((100, 100), np.nan, dtype="float32")
    arr[:, :50] = 5.0
    sat = ep.integral(arr)
    assert ep.ventana_media(sat, tf, 1000.0, 1500.0, 30.0) == pytest.approx(5.0)
    assert np.isnan(ep.ventana_media(sat, tf, 2500.0, 1500.0, 30.0))


def _scene():
    tf = Affine(30.0, 0, 0.0, 0, -30.0, 6000.0)  # 200 x 200 px = 6 x 6 km
    slope = np.full((200, 200), 2.0, dtype="float32")
    slope[:, 100:] = 15.0  # mitad este: piedemonte
    elev = np.full((200, 200), 600.0, dtype="float32")
    return slope, elev, tf


SPEC = {"key": "t", "area_m2": 10_000.0, "pendiente_pct": (10.0, 20.0), "referencias": (), "elev_m": None, "ref_fallback": None}


def test_choose_picks_valid_candidate_closest_to_centroid_of_valid_set():
    slope, elev, tf = _scene()
    zona = box(0, 0, 6000, 6000)
    r = ep.elegir_candidato(SPEC, slope, elev, tf, zona, {}, seed=1)
    assert r is not None
    assert r["cx"] > 3000.0  # mitad con 15 % de pendiente
    assert 10.0 <= r["pendiente_media_ventana_pct"] <= 20.0
    # centro del conjunto valido ~ (4500, 3000): el elegido esta cerca (paso de la grilla)
    assert abs(r["cx"] - 4500.0) < 2 * ep.PASO_M and abs(r["cy"] - 3000.0) < 2 * ep.PASO_M


def test_choose_is_reproducible_and_seed_only_shifts_the_grid():
    slope, elev, tf = _scene()
    zona = box(0, 0, 6000, 6000)
    a = ep.elegir_candidato(SPEC, slope, elev, tf, zona, {}, seed=ep.SEED)
    b = ep.elegir_candidato(SPEC, slope, elev, tf, zona, {}, seed=ep.SEED)
    assert a == b
    c = ep.elegir_candidato(SPEC, slope, elev, tf, zona, {}, seed=ep.SEED + 1)
    assert (c["cx"], c["cy"]) != (a["cx"], a["cy"]) or c["cx"] > 3000.0
    assert 10.0 <= c["pendiente_media_ventana_pct"] <= 20.0


def test_choose_requires_lot_inside_zone_with_margin_and_inside_reference_footprint():
    slope, elev, tf = _scene()
    zona = box(0, 0, 6000, 6000)
    huella = box(3600, 2000, 5000, 4000)  # solo una parte del piedemonte tiene referencia
    spec = SPEC | {"referencias": ("spot",)}
    r = ep.elegir_candidato(spec, slope, elev, tf, zona, {"spot": huella}, seed=1)
    half = (10_000.0 ** 0.5) / 2
    lot = box(r["cx"] - half, r["cy"] - half, r["cx"] + half, r["cy"] + half)
    assert huella.buffer(-ep.MARGEN_REF_M).contains(lot)


def test_choose_returns_none_when_no_candidate_meets_slope_range():
    slope, elev, tf = _scene()
    r = ep.elegir_candidato(SPEC | {"pendiente_pct": (40.0, 60.0)}, slope, elev, tf, box(0, 0, 6000, 6000), {}, seed=1)
    assert r is None


def test_choose_applies_elevation_range():
    slope, elev, tf = _scene()
    elev[:, 150:] = 1500.0
    spec = SPEC | {"elev_m": (1000.0, 2000.0)}
    r = ep.elegir_candidato(spec, slope, elev, tf, box(0, 0, 6000, 6000), {}, seed=1)
    assert r["cx"] > 4500.0


def test_fallback_reference_used_only_when_primary_has_no_candidate():
    slope, elev, tf = _scene()
    zona = box(0, 0, 6000, 6000)
    huellas = {"pleiades": box(100, 100, 200, 200), "spot": box(3000, 500, 5800, 5500)}
    spec = SPEC | {"referencias": ("pleiades", "spot"), "ref_fallback": ("spot",)}
    r = ep.seleccionar(spec, slope, elev, tf, zona, huellas, seed=1)
    assert r["referencias_usadas"] == ["spot"] and r["uso_fallback"] is True
    spec2 = SPEC | {"referencias": ("spot",), "ref_fallback": None}
    assert ep.seleccionar(spec2, slope, elev, tf, zona, huellas, seed=1)["uso_fallback"] is False


# --- especificacion de los 8 pilotos ----------------------------------------------------------------------------------

def test_pilot_specs_follow_the_plan():
    assert [p["key"] for p in ep.PILOTOS] == KEYS
    areas = [p["area_m2"] for p in ep.PILOTOS]
    assert sum(1 for a in areas if 600 <= a <= 1000) == 2          # 2 lotes urbanos chicos
    assert sum(1 for a in areas if 5_000 <= a <= 20_000) == 3      # 3 medianos (0,5-2 ha)
    assert sum(1 for a in areas if 50_000 <= a <= 90_000) == 3     # 3 grandes (5-9 ha)
    by = {p["key"]: p for p in ep.PILOTOS}
    assert set(by["rm03_piedemonte_oriente"]["referencias"]) == {"pleiades", "spot"}
    assert set(by["rm04_piedemonte_alto"]["referencias"]) == {"pleiades", "spot"}
    assert by["rm05_piedemonte_sur"]["ref_fallback"] == ("spot",)
    assert by["rm01_plano_urbano"]["referencias"] == ("spot",)
    assert by["rm07_lomaje_norte"]["referencias"] == () and by["rm08_precordillera"]["referencias"] == ()


# --- el sites.json generado (sin red) ---------------------------------------------------------------------------------

def test_sites_json_has_eight_rm_pilots_with_public_provenance():
    from shapely.geometry import shape

    from terreno.geometry import SITES, load_polygon

    assert list(SITES) == KEYS
    for key, s in SITES.items():
        assert {"nombre", "comuna_referencial", "tipo_terreno", "polygon", "area_m2", "fuente_poligono",
                "cobertura_referencia", "verdad_de_terreno"} <= set(s), key
        assert s["fuente_poligono"] == "lote de prueba estándar (datos públicos)", key
        assert set(s["cobertura_referencia"]) == {"spot", "pleiades", "icesat2_gedi"}, key
        poly = load_polygon(s["polygon"])
        assert abs(s["area_m2"] - ep.area_geodesica_m2(poly)) / s["area_m2"] < 0.01, key
        assert s["verdad_de_terreno"]["elevacion_medida"] is None
        assert shape(s["polygon"]).is_valid


def test_sites_json_has_no_private_attributes():
    text = SITES_PATH.read_text(encoding="utf-8").lower()
    for bad in ("rol", "permiso", "direccion", "id_" + "predio", "toc" + "toc", "info" + "gis", "contacta" + "bilidad", "zona_prc"):
        assert f'"{bad}' not in text, bad


def test_reference_coverage_flags_match_the_pilot_spec():
    from terreno.geometry import SITES

    for p in ep.PILOTOS:
        cov = SITES[p["key"]]["cobertura_referencia"]
        for ref in p["referencias"]:
            if ref == "pleiades" and p["ref_fallback"] and not cov["pleiades"]:
                continue
            assert cov[ref] is True, (p["key"], ref)
    assert SITES["rm07_lomaje_norte"]["cobertura_referencia"]["icesat2_gedi"] is True


def test_paused_regional_pilots_are_plain_public_boxes():
    data = json.loads(PAUSADOS_PATH.read_text(encoding="utf-8"))
    assert set(data["sitios"]) == {"vina_renaca", "puerto_varas", "coquimbo"}
    for key, s in data["sitios"].items():
        assert set(s) == {"nombre", "bbox_aproximado_4326", "nota"}, key
        lon0, lat0, lon1, lat1 = s["bbox_aproximado_4326"]
        assert lon0 < lon1 and lat0 < lat1
        assert 0.002 <= lon1 - lon0 <= 0.05
    text = PAUSADOS_PATH.read_text(encoding="utf-8").lower()
    for bad in ("rol", "permiso", "direccion", "toc" + "toc"):
        assert f'"{bad}' not in text
