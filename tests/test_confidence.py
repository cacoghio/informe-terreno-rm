import json
import re
from pathlib import Path

import pytest

from terreno.compare import ComparisonResult
from terreno.confidence import (
    RULES_PATH,
    Confidence,
    factors_from_results,
    load_rules,
    score_confidence,
)
from terreno.errors import RejectRequest
from terreno.layers import AuxStats

ALL_A = {"pixels": 100, "elev_range_m": 40.0, "slope_mean_pct": 5.0, "disagreement_rms_m": 1.0,
         "canopy_pct": 0.0, "void_water_pct": 0.0, "hem_p90_m": 1.0}


def level_of(factor, value):
    f = dict(ALL_A)
    f[factor] = value
    return score_confidence(f).by_factor[factor]


# un caso por cada fila del borde de la tabla de la spec (sec. 4)
@pytest.mark.parametrize("factor,value,expected", [
    ("pixels", 25, "A"), ("pixels", 24, "B"), ("pixels", 9, "B"), ("pixels", 8, "C"), ("pixels", 0, "C"),
    # calibrado RM 2026-10-31: error relativo de GLO-30 en el lote (vs Pleiades/SPOT) 0,7-2,6 m -> B >= 4 m, A > 8 m
    ("elev_range_m", 8.01, "A"), ("elev_range_m", 8.0, "B"), ("elev_range_m", 4.0, "B"), ("elev_range_m", 3.99, "C"),
    ("slope_mean_pct", 19.99, "A"), ("slope_mean_pct", 20.0, "B"), ("slope_mean_pct", 30.0, "B"), ("slope_mean_pct", 30.01, "C"),
    ("disagreement_rms_m", 2.99, "A"), ("disagreement_rms_m", 3.0, "B"), ("disagreement_rms_m", 6.0, "B"), ("disagreement_rms_m", 6.01, "C"),
    ("canopy_pct", 9.99, "A"), ("canopy_pct", 10.0, "B"), ("canopy_pct", 50.0, "B"), ("canopy_pct", 50.01, "B"), ("canopy_pct", 100.0, "B"),
    ("void_water_pct", 1.99, "A"), ("void_water_pct", 2.0, "B"), ("void_water_pct", 10.0, "B"), ("void_water_pct", 10.01, "C"),
    ("hem_p90_m", 2.99, "A"), ("hem_p90_m", 3.0, "B"), ("hem_p90_m", 6.0, "B"), ("hem_p90_m", 6.01, "C"),
])
def test_factor_edges_follow_spec_table(factor, value, expected):
    assert level_of(factor, value) == expected


def test_all_a_is_a():
    c = score_confidence(ALL_A)
    assert isinstance(c, Confidence)
    assert c.overall == "A" and c.human_review_required is False and c.omit_point_slope is False
    assert c.hypothesis is True
    assert set(c.by_factor.values()) == {"A"}


def test_worst_factor_wins():
    f = dict(ALL_A, pixels=30, slope_mean_pct=25.0, disagreement_rms_m=7.0)  # A, B, C
    c = score_confidence(f)
    assert c.overall == "C"
    assert c.by_factor["slope_mean_pct"] == "B" and c.by_factor["disagreement_rms_m"] == "C"
    assert c.limiting_factors == ["disagreement_rms_m"]


def test_missing_factor_caps_at_B():
    for missing in ALL_A:
        f = dict(ALL_A)
        f[missing] = None
        c = score_confidence(f)
        assert c.overall == "B", missing
        assert c.by_factor[missing] == "sin_dato"
    # una clave ausente equivale a None
    f = dict(ALL_A)
    del f["canopy_pct"]
    assert score_confidence(f).by_factor["canopy_pct"] == "sin_dato"
    assert score_confidence(f).overall == "B"


def test_missing_factor_does_not_hide_a_C():
    f = dict(ALL_A, canopy_pct=None, hem_p90_m=7.0)
    assert score_confidence(f).overall == "C"


def test_all_missing_is_B_not_A():
    assert score_confidence({}).overall == "B"


def test_nan_is_sin_dato():
    assert score_confidence(dict(ALL_A, hem_p90_m=float("nan"))).by_factor["hem_p90_m"] == "sin_dato"


def test_C_requires_human_review_and_omits_point_slope():
    c = score_confidence(dict(ALL_A, pixels=5))
    assert c.overall == "C" and c.human_review_required is True and c.omit_point_slope is True
    b = score_confidence(dict(ALL_A, pixels=20))
    assert b.overall == "B" and b.human_review_required is False and b.omit_point_slope is False


def test_by_section_can_differ():
    """La pendiente puede ser C mientras la elevacion es B (spec sec. 4)."""
    c = score_confidence(dict(ALL_A, slope_mean_pct=45.0, canopy_pct=None))
    assert c.by_section["pendiente"] == "C"
    assert c.by_section["elevacion"] == "B"  # canopy sin dato -> tope B, la pendiente no la afecta
    assert c.by_section["comparacion_fuentes"] == "A"
    assert c.overall == "C"


def test_unknown_factor_and_bad_values_rejected():
    with pytest.raises(RejectRequest, match="factor"):
        score_confidence(dict(ALL_A, inventado=1.0))
    with pytest.raises(RejectRequest):
        score_confidence(dict(ALL_A, pixels=-3))
    with pytest.raises(RejectRequest):
        score_confidence(dict(ALL_A, slope_mean_pct="alto"))


def test_rules_file_matches_spec_table():
    """Los cortes del JSON son los numeros literales de la spec (sec. 4 / Global Constraints del plan)."""
    rules = load_rules()
    assert rules["hypothesis"] is True
    spec = {  # factor -> (A, B) segun la tabla de la spec
        "pixels": (25, 9), "elev_range_m": (8, 4), "slope_mean_pct": (20, 30), "disagreement_rms_m": (3, 6),
        "canopy_pct": (10, 50), "void_water_pct": (2, 10), "hem_p90_m": (3, 6),
    }
    assert set(rules["factors"]) == set(spec)
    for name, (a, b) in spec.items():
        assert rules["factors"][name]["A"]["value"] == a, name
        assert rules["factors"][name]["B"]["value"] == b, name


def test_rules_file_matches_spec_markdown():
    """Extra: compara con la tabla de SPEC-PRODUCTO-v0.md si el archivo esta junto al repo."""
    spec_path = Path(__file__).resolve().parents[2] / "SPEC-PRODUCTO-v0.md"
    if not spec_path.exists():
        pytest.skip("SPEC-PRODUCTO-v0.md no esta junto al repo")
    rows = {}
    keys = {"Píxeles": "pixels", "Rango de elevación": "elev_range_m", "Pendiente media": "slope_mean_pct",
            "Desacuerdo": "disagreement_rms_m", "Cobertura de dosel": "canopy_pct", "Vacíos": "void_water_pct",
            "Incertidumbre propia": "hem_p90_m"}
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5:
            for prefix, key in keys.items():
                if cells[0].startswith(prefix):
                    rows[key] = {float(x.replace(",", ".")) for c in cells[1:4] for x in re.findall(r"\d+(?:[.,]\d+)?", c)}
    assert set(rows) == set(keys.values())
    rules = load_rules()
    for key, nums in rows.items():
        f = rules["factors"][key]
        assert {float(f["A"]["value"]), float(f["B"]["value"])} == nums, key


def test_thresholds_are_not_hardcoded():
    """Cambiar el JSON cambia el resultado (los umbrales no estan en el codigo)."""
    rules = json.loads(Path(RULES_PATH).read_text(encoding="utf-8"))
    rules["factors"]["pixels"]["A"]["value"] = 1000
    assert score_confidence(ALL_A, rules=rules).by_factor["pixels"] == "B"
    assert score_confidence(ALL_A).by_factor["pixels"] == "A"


# --- factors_from_results: lote chico (~6 px, sin claves de pendiente) y datum no alineado ---

def small_lot_stats():
    return {"n_pixels": 6, "inconclusive": True, "elev_min": 605.0, "elev_max": 608.0, "elev_mean": 606.5,
            "elev_range": 3.0, "elevation_within_noise": True, "aspect_dominant": None, "aspect_status": "no_concluyente"}


def comparison(rms=2.0, aligned=True):
    return ComparisonResult(
        rms_diff_m=rms, mean_diff_m=0.5, slope_diff_mean_pct=1.0, diff_map_path=Path("d.tif"), n_pixels=100,
        stats_scope="poligono", datum_a="EGM2008", datum_b="EGM96", datum_aligned=aligned,
        datum_penalty=not aligned, datum_shift_mean_m=0.1 if aligned else None, datum_note="")


def aux(void=0.0, hem=1.3):
    return AuxStats(n_pixels=14, void_water_fill_pct=void, hem_p90_m=hem, available=True)


def test_small_lot_inconclusive_stats_without_slope_keys():
    f = factors_from_results(small_lot_stats(), comparison(), aux())
    assert f["pixels"] == 6 and f["slope_mean_pct"] is None
    c = score_confidence(f)
    assert c.overall == "C" and c.human_review_required and c.omit_point_slope
    assert c.by_factor["pixels"] == "C" and c.by_factor["elev_range_m"] == "C"
    assert c.by_factor["slope_mean_pct"] == "sin_dato"
    assert c.by_section["pendiente"] == "C"


def test_disagreement_factor_removes_the_mean_offset():
    """Calibracion RM: la diferencia media AW3D30 - GLO-30 (+0,5 a +4,5 m) es el sesgo propio de AW3D30 (coincide con su
    sesgo extra contra ICESat-2/GEDI en los 8 sitios) y no cambia pendiente ni relieve: el factor usa el RMS centrado."""
    stats = {"n_pixels": 100, "elev_range": 40.0, "slope_mean_pct": 5.0}
    c = comparison(rms=3.0, aligned=True)  # mean_diff_m = 0,5
    f = factors_from_results(stats, c, aux())
    assert f["disagreement_rms_m"] == pytest.approx((3.0 ** 2 - 0.5 ** 2) ** 0.5)
    # Colina (rm07): RMS 3,09 con media 3,02 -> 0,65 m, nivel A en ese factor (antes B)
    c7 = ComparisonResult(
        rms_diff_m=3.087013, mean_diff_m=3.016826, slope_diff_mean_pct=1.0, diff_map_path=Path("d.tif"), n_pixels=90,
        stats_scope="poligono", datum_a="EGM2008", datum_b="EGM96", datum_aligned=True, datum_penalty=False,
        datum_shift_mean_m=0.1, datum_note="")
    f7 = factors_from_results(stats, c7, aux())
    assert f7["disagreement_rms_m"] == pytest.approx(0.655, abs=0.01)
    assert score_confidence(dict(ALL_A, disagreement_rms_m=f7["disagreement_rms_m"])).by_factor["disagreement_rms_m"] == "A"


def test_unaligned_datum_makes_disagreement_sin_dato():
    stats = {"n_pixels": 100, "elev_range": 40.0, "slope_mean_pct": 5.0}
    f = factors_from_results(stats, comparison(rms=1.0, aligned=False), aux())
    assert f["disagreement_rms_m"] is None
    c = score_confidence(f)
    assert c.by_factor["disagreement_rms_m"] == "sin_dato" and c.overall == "B"


def test_no_comparison_and_no_aux_are_sin_dato():
    stats = {"n_pixels": 100, "elev_range": 40.0, "slope_mean_pct": 5.0}
    f = factors_from_results(stats, None, None)
    assert f["disagreement_rms_m"] is None and f["void_water_pct"] is None and f["hem_p90_m"] is None
    assert f["canopy_pct"] is None
    assert score_confidence(f).overall == "B"


def test_canopy_is_never_invented():
    stats = {"n_pixels": 100, "elev_range": 40.0, "slope_mean_pct": 5.0}
    assert factors_from_results(stats, comparison(), aux())["canopy_pct"] is None
    assert factors_from_results(stats, comparison(), aux(), canopy_pct=12.0)["canopy_pct"] == 12.0


# --- AUD-1 I7: un factor medible que no se pudo leer es 'no_disponible', explicito, y exige revision humana ---

def test_unavailable_measurable_factor_is_explicit_and_cannot_hide_behind_a_silent_B():
    f = dict(ALL_A)
    f["void_water_pct"] = None
    c = score_confidence(f, unavailable=["void_water_pct"])
    assert c.by_factor["void_water_pct"] == "no_disponible"
    assert c.overall == "B" and c.limiting_factors == ["void_water_pct"]
    assert c.human_review_required is True and c.omit_point_slope is False
    assert c.unavailable_factors == ["void_water_pct"] and "void_water_pct" in c.review_reason
    # por diseno (dosel, sin fuente en el POC) sigue siendo 'sin_dato': tope B pero sin revision humana por eso
    g = dict(ALL_A)
    g["canopy_pct"] = None
    c2 = score_confidence(g)
    assert c2.by_factor["canopy_pct"] == "sin_dato" and c2.overall == "B" and c2.human_review_required is False
    assert c2.unavailable_factors == []


def test_unavailable_factor_does_not_hide_a_C_and_known_values_win():
    f = dict(ALL_A)
    f.update(void_water_pct=None, slope_mean_pct=45.0)
    c = score_confidence(f, unavailable=["void_water_pct"])
    assert c.overall == "C" and c.by_factor["void_water_pct"] == "no_disponible" and c.human_review_required
    # si el valor si esta, 'unavailable' no lo pisa
    h = score_confidence(dict(ALL_A), unavailable=["void_water_pct"])
    assert h.by_factor["void_water_pct"] == "A" and h.unavailable_factors == [] and h.overall == "A"


def test_unavailable_with_unknown_factor_name_rejected():
    with pytest.raises(RejectRequest):
        score_confidence(dict(ALL_A), unavailable=["nope"])


# --- v0.3.0: dos sellos (medibilidad del lote / confianza del dato) ---

def test_rules_v030_declare_two_seals_and_every_factor_belongs_to_exactly_one():
    rules = load_rules()
    seals = rules["seals"]
    assert set(seals) == {"medibilidad_lote", "confianza_dato"}
    owned = [f for s in seals.values() for f in s["factors"]]
    assert sorted(owned) == sorted(rules["factors"]) and len(owned) == len(set(owned))


def test_small_flat_clean_lot_is_unmeasurable_but_data_is_good():
    # Nunez-like: 1 pixel y 1 m de rango (el lote no se puede medir), pero pendiente, desacuerdo y dosel son buenos
    c = score_confidence(dict(ALL_A, pixels=1, elev_range_m=1.0))
    assert c.by_seal == {"medibilidad_lote": "C", "confianza_dato": "A"} and c.overall == "C"


def test_big_steep_lot_is_measurable_but_data_is_weak():
    # Pirque-like: lote grande (muchos pixeles, mucho desnivel) pero pendiente de 52 %
    c = score_confidence(dict(ALL_A, pixels=90, elev_range_m=120.0, slope_mean_pct=52.0))
    assert c.by_seal == {"medibilidad_lote": "A", "confianza_dato": "C"} and c.overall == "C"


def test_overall_is_still_the_worst_of_both_seals():
    for f in ({"pixels": 10}, {"slope_mean_pct": 25.0}, {"canopy_pct": 60.0}, {"elev_range_m": 5.0}):
        c = score_confidence(dict(ALL_A, **f))
        order = {"A": 0, "B": 1, "C": 2}
        assert order[c.overall] == max(order[v] for v in c.by_seal.values())


def test_missing_factor_caps_only_its_own_seal_at_b():
    c = score_confidence(dict(ALL_A, canopy_pct=None))
    assert c.by_seal["confianza_dato"] == "B" and c.by_seal["medibilidad_lote"] == "A"


def test_canopy_stats_feed_the_factor_and_explicit_value_wins():
    from terreno.worldcover import CanopyStats
    stats = {"n_pixels": 100, "elev_range": 40.0, "slope_mean_pct": 5.0}
    cs = CanopyStats(n_pixels=50, canopy_pct=37.5, available=True)
    assert factors_from_results(stats, canopy=cs)["canopy_pct"] == 37.5
    assert factors_from_results(stats, canopy_pct=12.0, canopy=cs)["canopy_pct"] == 12.0
    assert factors_from_results(stats, canopy=CanopyStats(n_pixels=0, canopy_pct=None))["canopy_pct"] is None


def test_canopy_alone_never_makes_the_level_C():
    """worst_level=B: el dosel baja a B (no a C) mientras no haya evidencia de que el error sea mayor en pendiente alta."""
    c = score_confidence(dict(ALL_A, canopy_pct=100.0))
    assert c.by_factor["canopy_pct"] == "B" and c.overall == "B" and c.human_review_required is False
    assert load_rules()["factors"]["canopy_pct"]["worst_level"] == "B"


def test_worst_level_is_read_from_the_rules_file():
    rules = load_rules()
    del rules["factors"]["canopy_pct"]["worst_level"]
    assert score_confidence(dict(ALL_A, canopy_pct=100.0), rules=rules).by_factor["canopy_pct"] == "C"
