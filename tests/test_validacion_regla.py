import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import box

from tools.elegir_sitios_validacion import elegir
from tools.evaluar_regla_validacion import evaluar


def rows(spec):
    return [{"dato": lv, "lote": lv, "level": lv, "rmse": r} for lv, r in spec]


def test_rule_passes_when_ordered_enough_sites_and_correlated():
    r = evaluar(rows([("A", 2), ("A", 2.5), ("A", 3), ("B", 4), ("B", 5), ("B", 4.5), ("C", 8), ("C", 9), ("C", 7)]))
    assert r["ordenado"] and r["suficiente"] and r["discrimina"] and r["spearman"] >= 0.5


def test_rule_fails_with_too_few_sites_in_a_level():
    r = evaluar(rows([("A", 2), ("B", 4), ("B", 5), ("B", 4.5), ("C", 8), ("C", 9), ("C", 7)]))
    assert r["ordenado"] and not r["suficiente"] and not r["discrimina"]


def test_rule_fails_when_a_is_worse_than_b():
    r = evaluar(rows([("A", 6), ("A", 7), ("A", 6.5), ("B", 4), ("B", 5), ("B", 4.5), ("C", 8), ("C", 9), ("C", 7)]))
    assert not r["ordenado"] and not r["discrimina"]


def test_rule_fails_when_levels_are_empty():
    assert not evaluar(rows([("B", 4), ("B", 5), ("C", 8)]))["discrimina"]


def test_elegir_picks_a_candidate_that_meets_slope_and_canopy_ranges():
    tf = from_origin(0.0, 3000.0, 30.0, 30.0)  # 100 x 100 px de 30 m
    slope = np.full((100, 100), 40.0, dtype="float32")
    slope[:, :50] = 5.0  # mitad oeste llana
    dosel = np.full((100, 100), 60.0, dtype="float32")
    dosel[:50, :] = 2.0  # mitad norte sin dosel
    zona = box(0, 0, 3000, 3000)
    spec = dict(area_m2=10_000.0, pend=(0, 12), dosel=(0, 8))
    r = elegir(spec, slope, dosel, tf, zona, seed=1)
    assert r is not None and r["cx"] < 1500 and r["cy"] > 1500
    assert r["pendiente_ventana_pct"] <= 12 and r["dosel_ventana_pct"] <= 8
    assert elegir(spec, slope, dosel, tf, zona, seed=1) == r  # determinista


def test_elegir_returns_none_when_no_candidate_fits():
    tf = from_origin(0.0, 3000.0, 30.0, 30.0)
    slope = np.full((100, 100), 40.0, dtype="float32")
    dosel = np.zeros((100, 100), dtype="float32")
    assert elegir(dict(area_m2=10_000.0, pend=(0, 12), dosel=(0, 8)), slope, dosel, tf, box(0, 0, 3000, 3000), seed=1) is None
