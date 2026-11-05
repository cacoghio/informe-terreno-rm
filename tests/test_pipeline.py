import copy
import json
import re

import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from terreno.config import RECIPE_VERSION
from terreno.errors import RejectRequest
from terreno.fetch import DemResult
from terreno.layers import AuxStats
from terreno.manifest import (
    canonical_json,
    hash_manifest,
    read_manifest,
    report_fields,
    result_hash,
)
from terreno.pipeline import HOOK_ORDER, REJECTION_NAME, RunResult, run_pipeline
from tests.test_reproject import RES, make_dem

LON0, LAT0 = -70.6, -33.45
POLY = box(LON0 - 0.0015, LAT0 - 0.0015, LON0 + 0.0015, LAT0 + 0.0015)  # ~300 m -> ~100 px de 30 m
TINY = box(LON0 - 0.0003, LAT0 - 0.0003, LON0 + 0.0003, LAT0 + 0.0003)  # ~60 m -> 4 px


def ramp_fn(offset=0.0):
    return lambda lon, lat: 600.0 + offset + 20000.0 * (lon - LON0) + 12000.0 * (lat - LAT0)


class Clock:
    """Fecha de descarga distinta en cada llamada: no debe afectar el hash."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-10-{10 + self.n:02d}"


def fetchers(tmp_path, *, glo_offset=0.0, aw_offset=2.0, datum="EGM96", clock=None, glo_fn=None, aw_fn=None):
    clock = clock or Clock()

    def glo(poly, cache_dir):
        d = make_dem(cache_dir, LON0, LAT0, n=60, fn=glo_fn or ramp_fn(glo_offset), name="glo.tif")
        return DemResult(path=d.path, tiles=["Copernicus_DSM_COG_10_S34_00_W071_00_DEM"],
                         source_url="https://copernicus-dem-30m.s3.amazonaws.com", acquired=clock(),
                         nodata_fraction=0.0, tile_urls=["https://example/t.tif"], source="glo30")

    def aw(poly, cache_dir):
        d = make_dem(cache_dir, LON0, LAT0, n=60, fn=aw_fn or ramp_fn(aw_offset), name="aw.tif")
        return DemResult(path=d.path, tiles=["AW3D30"], source_url="https://portal.opentopography.org/API/globaldem?demtype=AW3D30",
                         acquired=clock(), nodata_fraction=0.0, source="aw3d30", vertical_datum=datum)

    return {"glo30": glo, "aw3d30": aw}


def aux_ok(poly, dem):
    return AuxStats(n_pixels=100, void_water_fill_pct=0.0, hem_p90_m=1.2, counts={"union": 0}, n_hem_valid=100,
                    available=True)


SHIFT = lambda lon, lat: np.full(np.shape(lon), 0.1)


def run(tmp_path, name, poly=POLY, **kw):
    kw.setdefault("fetch", fetchers(tmp_path))
    kw.setdefault("fetch_aux", aux_ok)
    kw.setdefault("shift_fn", SHIFT)
    return run_pipeline(poly, tmp_path / name, cache_dir=tmp_path / f"cache_{name}", **kw)


# --- determinismo (criterio 1) ---

def test_two_runs_same_hash(tmp_path):
    clk = Clock()  # compartido: las fechas de descarga de la 2a corrida son distintas
    a = run(tmp_path, "a", fetch=fetchers(tmp_path, clock=clk), now=lambda: "2026-10-23T10:00:00+00:00")
    b = run(tmp_path, "b", fetch=fetchers(tmp_path, clock=clk), now=lambda: "2030-01-01T00:00:00+00:00")  # otro momento, otras fechas de descarga, otra carpeta
    assert isinstance(a, RunResult)
    assert a.result_hash == b.result_hash == result_hash(tmp_path / "a") == result_hash(tmp_path / "b")
    # lo volatil SI difiere (si no, el test no probaria que se excluye)
    assert a.manifest["volatile"]["generated_at"] != b.manifest["volatile"]["generated_at"]
    assert a.manifest["volatile"]["acquired"] != b.manifest["volatile"]["acquired"]
    assert a.manifest["result_hash"] == a.result_hash


def test_same_out_dir_rerun_same_hash(tmp_path):
    kw = dict(fetch=fetchers(tmp_path))
    h1 = run_pipeline(POLY, tmp_path / "o", cache_dir=tmp_path / "c", fetch_aux=aux_ok, shift_fn=SHIFT, **kw).result_hash
    h2 = run_pipeline(POLY, tmp_path / "o", cache_dir=tmp_path / "c", fetch_aux=aux_ok, shift_fn=SHIFT, **kw).result_hash
    assert h1 == h2


def test_hash_changes_when_input_changes(tmp_path):
    a = run(tmp_path, "a")
    b = run(tmp_path, "b", fetch=fetchers(tmp_path, glo_offset=5.0))
    assert a.result_hash != b.result_hash
    c = run(tmp_path, "c", fetch=fetchers(tmp_path, glo_fn=lambda lon, lat: 600.0 + 40000.0 * (lon - LON0)))
    assert a.result_hash != c.result_hash


def test_hash_excludes_only_volatile_keys():
    m = {"a": 1, "result_hash": "x", "volatile": {"t": 1}, "results": {"x": 1.0000001}}
    m2 = {"a": 1, "result_hash": "y", "volatile": {"t": 2}, "results": {"x": 1.0000002}}
    assert hash_manifest(m) == hash_manifest(m2)  # flotantes a 6 decimales
    m3 = copy.deepcopy(m)
    m3["results"]["x"] = 1.001
    assert hash_manifest(m) != hash_manifest(m3)
    assert canonical_json({"b": 1, "a": float("nan")}) == '{"a":"NaN","b":1}'


# --- manifiesto ---

def test_manifest_has_all_required_fields(tmp_path):
    r = run(tmp_path, "a")
    m = read_manifest(tmp_path / "a")
    assert m == r.manifest or m["result_hash"] == r.manifest["result_hash"]
    # dataset, version, fecha, URL
    base, comp = m["inputs"]["datasets"]
    for d in (base, comp):
        for k in ("name", "version", "url", "tiles", "native_crs", "vertical_datum", "array_sha256", "source", "role"):
            assert d.get(k) not in (None, ""), (d.get("source"), k)
    assert base["vertical_datum"] == "EGM2008" and comp["vertical_datum"] == "EGM96"
    assert set(m["volatile"]["acquired"]) == {"glo30", "aw3d30"}
    # CRS y tamano de pixel
    assert m["grid"]["crs"] == "EPSG:32719" and m["grid"]["pixel_m"] == 30.0
    assert "EGM2008" in m["grid"]["vertical_datum"]
    # hash de entradas y de la receta; version de receta; versiones de herramientas
    assert re.fullmatch(r"[0-9a-f]{64}", m["inputs"]["hash"]) and re.fullmatch(r"[0-9a-f]{64}", m["recipe"]["hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", m["result_hash"])
    assert m["recipe"]["version"] == RECIPE_VERSION
    tools = m["recipe"]["tools"]
    assert "GDAL 3.8" in tools["gdal_cli"]
    for k in ("rasterio", "numpy", "pyproj", "shapely", "python", "gdal_rasterio_wheel"):
        assert tools[k]
    # hipotesis declaradas, confianza con hipotesis, zona UTM por regla
    assert m["recipe"]["hypotheses"] and m["confidence"]["hypothesis"] is True
    assert "centroide" in m["inputs"]["utm_zone_rule"]
    assert m["results"]["terrain"]["n_pixels"] >= 9 and m["results"]["comparison"]["status"] == "ok"
    assert m["results"]["comparison"]["datum_aligned"] is True
    assert m["results"]["aux_layers"]["hem_p90_m"] == 1.2
    assert m["results"]["ground_truth_validation"] == {"status": "no_ejecutado"}


def test_manifest_has_no_secrets_or_local_paths_in_hashed_part(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "SECRETO123")
    run(tmp_path, "a")
    text = (tmp_path / "a" / "manifest.json").read_text(encoding="utf-8")
    assert "SECRETO123" not in text
    m = json.loads(text)
    hashed = canonical_json({k: v for k, v in m.items() if k != "volatile"})
    assert str(tmp_path).replace("\\", "/") not in hashed.replace("\\\\", "/")


def test_report_numbers_come_from_manifest(tmp_path):
    r = run(tmp_path, "a")
    m = read_manifest(tmp_path / "a")
    f = report_fields(m)
    t = r.terrain.stats
    assert f["n_pixeles"] == t["n_pixels"]
    assert f["pendiente"]["media_pct"] == pytest.approx(t["slope_mean_pct"], abs=1e-6)
    assert f["elevacion"]["elev_max"] == pytest.approx(t["elev_max"], abs=1e-6)
    assert f["nivel"] == r.confidence.overall and f["resolucion_m"] == 30.0
    assert f["comparacion"]["rms_diff_m"] == pytest.approx(r.comparison.rms_diff_m, abs=1e-6)
    # el texto sale SOLO del manifiesto: si cambia el manifiesto, cambia el informe
    m2 = copy.deepcopy(m)
    m2["results"]["terrain"]["n_pixels"] = 12345
    m2["results"]["terrain"]["slope_mean_pct"] = 99.5
    m2["confidence"]["overall"] = "C"
    f2 = report_fields(m2)
    assert f2["n_pixeles"] == 12345 and f2["pendiente"]["media_pct"] == 99.5 and f2["nivel"] == "C"


def test_inconclusive_small_lot_reports_no_point_slope(tmp_path):
    r = run(tmp_path, "t", poly=TINY)
    assert r.terrain.stats["inconclusive"] is True
    assert "slope_mean_pct" not in r.terrain.stats  # claves omitidas
    assert r.confidence.overall == "C" and r.confidence.human_review_required and r.confidence.omit_point_slope
    f = report_fields(read_manifest(tmp_path / "t"))
    assert f["pendiente"] == {"estado": "no_concluyente"} and f["revision_humana"] is True


def test_c_level_report_fields_omit_slope_even_if_stats_exist(tmp_path):
    m = copy.deepcopy(run(tmp_path, "a").manifest)
    m["confidence"]["omit_point_slope"] = True
    assert report_fields(m)["pendiente"] == {"estado": "no_concluyente"}


# --- rechazo en vez de adivinar ---

def test_pipeline_rejects_instead_of_guessing(tmp_path):
    def mostly_nodata(poly, cache_dir):
        fn = lambda lon, lat: np.where(lon > LON0 - 0.004, np.nan, 600.0 + 0 * lon)  # ~70 % vacio
        d = make_dem(cache_dir, LON0, LAT0, n=60, fn=fn, name="bad.tif")
        return DemResult(path=d.path, tiles=["x"], source_url="u", acquired="2026-10-23", nodata_fraction=0.7)

    out = tmp_path / "out"
    with pytest.raises(RejectRequest, match="nodata"):
        run_pipeline(POLY, out, fetch={"glo30": mostly_nodata}, fetch_aux=aux_ok, cache_dir=tmp_path / "c")
    assert not (out / "report.pdf").exists() and not (out / "manifest.json").exists()
    rej = json.loads((out / REJECTION_NAME).read_text(encoding="utf-8"))
    assert rej["rejected"] is True and rej["stage"] == "reproject" and "nodata" in rej["reason"]


def test_rejection_removes_stale_results_of_previous_run(tmp_path):
    out = tmp_path / "out"
    run_pipeline(POLY, out, fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "c")
    assert (out / "manifest.json").exists()
    far = box(10, 50, 10.001, 50.001)
    with pytest.raises(RejectRequest):
        run_pipeline(far, out, fetch=fetchers(tmp_path), cache_dir=tmp_path / "c")
    assert not (out / "manifest.json").exists() and (out / REJECTION_NAME).exists()


def test_base_fetch_reject_propagates(tmp_path):
    def missing(poly, cache_dir):
        raise RejectRequest("tile no publicado: X")

    with pytest.raises(RejectRequest, match="tile no publicado"):
        run_pipeline(POLY, tmp_path / "o", fetch={"glo30": missing}, cache_dir=tmp_path / "c")
    assert json.loads((tmp_path / "o" / REJECTION_NAME).read_text(encoding="utf-8"))["stage"] == "fetch_dem"


def test_polygon_outside_chile_rejected_before_any_fetch(tmp_path):
    called = []
    with pytest.raises(RejectRequest):
        run_pipeline(box(10, 50, 10.001, 50.001), tmp_path / "o", fetch={"glo30": lambda *a: called.append(1)},
                     cache_dir=tmp_path / "c")
    assert not called


# --- comparacion opcional ---

def test_missing_aw3d30_key_degrades_to_B_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    f = fetchers(tmp_path)
    del f["aw3d30"]  # usa el fetch_aw3d30 real: sin clave -> RejectRequest, sin tocar la red
    r = run_pipeline(POLY, tmp_path / "o", fetch=f, fetch_aux=aux_ok, cache_dir=tmp_path / "c")
    assert r.comparison is None and "OPENTOPOGRAPHY_API_KEY" in r.comparison_unavailable_reason
    cmp_ = r.manifest["results"]["comparison"]
    assert cmp_["status"] == "no_disponible" and "OPENTOPOGRAPHY_API_KEY" in cmp_["reason"]
    assert r.confidence.by_factor["disagreement_rms_m"] == "sin_dato" and r.confidence.overall == "B"


def test_datum_not_aligned_is_flagged_in_manifest(tmp_path):
    def no_grid(lon, lat):
        from terreno.compare import DatumShiftUnavailable
        raise DatumShiftUnavailable("sin malla")

    r = run(tmp_path, "a", shift_fn=no_grid)
    cmp_ = r.manifest["results"]["comparison"]
    assert cmp_["datum_aligned"] is False and cmp_["datum_penalty"] is True
    assert r.confidence.by_factor["disagreement_rms_m"] == "sin_dato"


def test_glo30_only_run(tmp_path):
    r = run(tmp_path, "g", sources=("glo30",))
    assert r.comparison is None and len(r.manifest["inputs"]["datasets"]) == 1
    assert r.manifest["results"]["comparison"]["status"] == "no_disponible"


def test_canopy_stays_sin_dato_so_level_never_A(tmp_path):
    r = run(tmp_path, "a")
    assert r.confidence.by_factor["canopy_pct"] == "sin_dato"
    assert r.confidence.overall in ("B", "C")


def test_ground_truth_hook_runs_validation(tmp_path):
    from terreno.truth import Footprint

    def truth(poly, cache_dir):
        # 12 huellas sobre el plano del DEM sintetico (verdad = DEM + 1 m)
        out = []
        for i in range(12):
            lon, lat = LON0 - 0.001 + 0.00015 * i, LAT0 + 0.0002 * (i % 3)
            z = ramp_fn()(lon, lat)
            out.append(Footprint(lon, lat, z + 28.0, z + 1.0, "atl08", "2024-01-01", 1))
        return out

    r = run(tmp_path, "v", ground_truth=truth)
    v = r.manifest["results"]["ground_truth_validation"]
    assert v["status"] == "ok" and v["n"] == 12 and v["bias_m"] == pytest.approx(-1.0, abs=0.05)


# --- ganchos ---

def test_hooks_are_called_in_order_with_manifest_only(tmp_path):
    calls = []

    def mk(name):
        def hook(manifest, out_dir):
            calls.append(name)
            assert manifest["result_hash"]
            return out_dir / f"{name}.out"
        return hook

    r = run(tmp_path, "h", hooks={n: mk(n) for n in reversed(HOOK_ORDER)})
    assert calls == list(HOOK_ORDER)
    assert set(r.outputs) == set(HOOK_ORDER)


def test_unknown_hook_rejected(tmp_path):
    with pytest.raises(RejectRequest, match="gancho"):
        run(tmp_path, "h", hooks={"render_cat": lambda m, o: None})


def test_base_source_must_be_glo30(tmp_path):
    with pytest.raises(RejectRequest, match="glo30"):
        run(tmp_path, "h", sources=("aw3d30",))


# --- prueba viva (excluida por defecto): GLO-30 real, sin credenciales ---

@pytest.mark.network
def test_live_two_runs_same_hash_rm01_and_rm04(tmp_path):
    from terreno.geometry import SITES, load_polygon

    for key in ("rm01_plano_urbano", "rm04_piedemonte_alto"):
        poly = load_polygon(SITES[key]["polygon"])
        a = run_pipeline(poly, tmp_path / key / "1", cache_dir=tmp_path / "cache", sources=("glo30",))
        b = run_pipeline(poly, tmp_path / key / "2", cache_dir=tmp_path / "cache", sources=("glo30",))
        assert a.result_hash == b.result_hash
        assert a.manifest["results"]["aux_layers"]["status"] == "ok"
    # RM-01 (lote de ~800 m2, 1 px) es no concluyente / nivel C; RM-04 (6 ha, ~72 px, ~21 % de pendiente media) no es C por pendiente
    assert report_fields(read_manifest(tmp_path / "rm01_plano_urbano" / "1"))["pendiente"]["estado"] == "no_concluyente"


# --- ventana de contexto para el GLB (Task 13, gap 1) ---

def _big_fetchers(tmp_path, seen=None, fail_context=False):
    base = fetchers(tmp_path)

    def glo(poly, cache_dir, *, margin_px=2):
        if seen is not None:
            seen.append((poly.bounds, margin_px, str(cache_dir)))
        if fail_context and margin_px > 2:
            raise RejectRequest("tile no publicado: prueba")
        d = make_dem(cache_dir, LON0, LAT0, n=200, fn=ramp_fn(0.0), name="glo.tif")
        return DemResult(path=d.path, tiles=["Copernicus_DSM_COG_10_S34_00_W071_00_DEM"], source_url="https://x",
                         acquired="2026-10-23", nodata_fraction=0.0, tile_urls=["https://example/t.tif"], source="glo30")

    return {**base, "glo30": glo}


def test_context_raster_is_separate_wider_and_does_not_change_the_hash(tmp_path):
    seen = []
    a = run(tmp_path, "a", fetch=_big_fetchers(tmp_path, seen))
    b = run(tmp_path, "b", fetch=_big_fetchers(tmp_path), context_m=None)
    assert a.result_hash == b.result_hash  # el contexto no entra al hash de resultados
    paths = a.manifest["volatile"]["paths"]
    assert paths["context_raster_utm"] != paths["raster_utm"]
    with rasterio.open(paths["context_raster_utm"]) as c, rasterio.open(paths["raster_utm"]) as w:
        assert c.width > w.width + 60 and c.height > w.height + 60  # mucho mas que bbox +- 3 px
        assert w.width == a.manifest["grid"]["shape_rows_cols"][1]
    ctx = a.manifest["volatile"]["context"]
    assert ctx["status"] == "ok" and ctx["requested_m"] == 1500.0 and ctx["margin_px"] == 50 and ctx["shape_rows_cols"][0] > 100
    assert "context_raster_utm" not in b.manifest["volatile"]["paths"] and b.manifest["volatile"]["context"]["status"] == "no_solicitado"
    # el fetch de contexto pidio un margen en pixeles de 1 arcseg suficiente para 1500 m, en una carpeta aparte
    base_call, ctx_call = seen[0], seen[1]
    assert base_call[1] == 2 and ctx_call[1] >= 1500 / 30 and ctx_call[2] != base_call[2]
    assert "context_raster_utm" not in a.manifest["results"] and "context" not in a.manifest["results"]


def test_context_failure_is_not_a_rejection(tmp_path):
    a = run(tmp_path, "a", fetch=_big_fetchers(tmp_path, fail_context=True))
    ctx = a.manifest["volatile"]["context"]
    assert ctx["status"] == "no_disponible" and "tile no publicado" in ctx["reason"]
    assert "context_raster_utm" not in a.manifest["volatile"]["paths"]
    assert not (tmp_path / "a" / REJECTION_NAME).exists()
    b = run(tmp_path, "b", fetch=_big_fetchers(tmp_path), context_m=None)
    assert a.result_hash == b.result_hash


def test_fetchers_without_margin_param_still_work_and_bad_context_rejected(tmp_path):
    a = run(tmp_path, "a")  # los fetch sinteticos del resto de los tests no aceptan margin_px: cubren lo que cubren
    assert a.manifest["volatile"]["context"]["status"] == "ok"
    for bad in (0, -1.0, float("nan"), True, "x"):
        with pytest.raises(RejectRequest, match="context_m"):
            run(tmp_path, "z", context_m=bad)


def test_ground_truth_uses_the_context_raster_for_a_tiny_lot(tmp_path):
    """Un lote de ~4 px no tiene huellas ICESat-2/GEDI dentro: se valida contra el contexto (+-1500 m), no contra el predio."""
    from terreno.truth import Footprint

    def truth(poly, cache_dir):
        out = []
        for i in range(12):  # 400-700 m al este del lote: fuera del raster de trabajo, dentro del contexto
            lon, lat = LON0 + 0.005 + 0.0003 * i, LAT0 + 0.0002 * (i % 3)
            z = ramp_fn()(lon, lat)
            out.append(Footprint(lon, lat, z + 28.0, z + 1.0, "atl08", "2024-01-01", 1))
        return out

    r = run(tmp_path, "ctx", poly=TINY, fetch=_big_fetchers(tmp_path), ground_truth=truth, sources=("glo30",))
    v = r.manifest["results"]["ground_truth_validation"]
    assert v["status"] == "ok" and v["n"] == 12 and v["n_outside_raster"] == 0
    assert r.manifest["results"]["ground_truth_validation"]["bias_m"] == pytest.approx(-1.0, abs=0.1)


# --- AUD-1 I1: misma politica de raster y MISMO conjunto de huellas para GLO-30 y AW3D30 ---

def _footprints_east(n=24, x0=0.002, dx=0.0004, z_add=1.0):
    from terreno.truth import Footprint

    out = []
    for i in range(n):  # de ~190 m a ~900 m al este del lote
        lon, lat = LON0 + x0 + dx * i, LAT0 + 0.0002 * (i % 3)
        z = ramp_fn()(lon, lat)
        out.append(Footprint(lon, lat, z + 28.0, z + z_add, "atl08", "2024-01-01", 1))
    return out


def _aw_small_fetchers(tmp_path, seen):
    """GLO-30 grande (200 px) y AW3D30 que, pedido con margen, cubre menos que el contexto de GLO-30."""
    base = _big_fetchers(tmp_path)

    def aw(poly, cache_dir, *, margin_m=0.0):
        seen.append(margin_m)
        n = 60 if margin_m else 20  # 60 arcseg ~ 1550 m de ancho: no llega a las huellas mas lejanas (hasta ~1040 m)
        d = make_dem(cache_dir, LON0, LAT0, n=n, fn=ramp_fn(2.0), name="aw.tif")
        return DemResult(path=d.path, tiles=["AW3D30"], source_url="https://x", acquired="2026-10-23",
                         nodata_fraction=0.0, source="aw3d30", vertical_datum="EGM96")

    return {**base, "aw3d30": aw}


def test_aw3d30_is_scored_on_the_same_footprints_as_glo30_and_on_the_context_window(tmp_path):
    seen = []
    fps = _footprints_east()
    r = run(tmp_path, "i1", poly=TINY, fetch=_aw_small_fetchers(tmp_path, seen), ground_truth=lambda p, c: fps)
    v = r.manifest["results"]["ground_truth_validation"]
    assert r.context_raster is not None  # RunResult expone el raster de contexto
    assert any(m and m >= 1000 for m in seen)  # AW3D30 tambien se pidio sobre la ventana de contexto
    aw = v["aw3d30"]
    assert v["status"] == "ok" and aw["status"] == "ok"
    assert 10 <= v["n"] < len(fps)  # AW3D30 no cubre todas: el conjunto comun es menor que lo que GLO-30 solo cubriria
    assert aw["n"] == v["n"]  # mismo conjunto de huellas
    assert v["n_outside_raster"] + v["n"] == len(fps)  # las no comunes se cuentan como fuera
    assert aw["bias_m"] - v["bias_m"] == pytest.approx(2.1, abs=0.3)  # AW3D30 = GLO-30 + 2 m (+ geoide)


def test_aw3d30_reference_unavailable_is_explicit_and_glo30_uses_all_footprints(tmp_path):
    fetch = _big_fetchers(tmp_path)
    fps = _footprints_east()

    def aw_fails_on_context(poly, cache_dir, *, margin_m=0.0):
        if margin_m:
            raise RejectRequest("OpenTopography devolvio HTTP 500: prueba")
        return fetchers(tmp_path)["aw3d30"](poly, cache_dir)

    r = run(tmp_path, "i1b", poly=TINY, fetch={**fetch, "aw3d30": aw_fails_on_context}, ground_truth=lambda p, c: fps)
    v = r.manifest["results"]["ground_truth_validation"]
    assert v["status"] == "ok" and v["n"] == len(fps)
    assert v["aw3d30"]["status"] == "no_disponible" and "HTTP 500" in v["aw3d30"]["reason"]


# --- AUD-1 I4: la verdad de terreno es solo validacion; su fallo no rechaza el informe ---

@pytest.mark.parametrize("exc", [RejectRequest("granulo no disponible tras 3 intentos: prueba"), OSError("h5py: callback failed"),
                                 RuntimeError("boom")])
def test_ground_truth_failure_does_not_reject_the_report(tmp_path, exc):
    def truth(poly, cache_dir):
        raise exc

    r = run(tmp_path, "i4", ground_truth=truth)
    v = r.manifest["results"]["ground_truth_validation"]
    assert v["status"] == "no_disponible" and v["reason"]
    if isinstance(exc, RejectRequest):
        assert "granulo no disponible" in v["reason"]
    assert (tmp_path / "i4" / "manifest.json").exists() and not (tmp_path / "i4" / REJECTION_NAME).exists()
    assert r.validation is None and r.confidence.overall in "ABC"


def test_ground_truth_failure_hash_equals_run_without_truth_in_everything_but_that_section(tmp_path):
    def truth(poly, cache_dir):
        raise OSError("x")

    a = run(tmp_path, "a", ground_truth=truth)
    b = run(tmp_path, "b")
    for k in ("terrain", "comparison", "aux_layers", "solar"):
        assert a.manifest["results"][k] == b.manifest["results"][k]
    assert a.manifest["confidence"] == b.manifest["confidence"]


def test_unexpected_exception_still_writes_rejection_json(tmp_path, monkeypatch):
    import terreno.pipeline as pl

    def boom(*a, **k):
        raise ValueError("fallo inesperado de prueba")

    monkeypatch.setattr(pl, "compute_solar", boom)
    with pytest.raises(ValueError):
        run(tmp_path, "i4c")
    rej = json.loads((tmp_path / "i4c" / REJECTION_NAME).read_text(encoding="utf-8"))
    assert rej["rejected"] is True and rej["stage"] == "solar" and "ValueError" in rej["reason"]
    assert not (tmp_path / "i4c" / "manifest.json").exists()


def test_unexpected_exception_in_a_hook_writes_rejection_json_and_removes_the_manifest(tmp_path):
    def bad_hook(manifest, out_dir):
        raise KeyError("hook roto")

    with pytest.raises(KeyError):
        run(tmp_path, "i4d", hooks={"build_glb": bad_hook})
    rej = json.loads((tmp_path / "i4d" / REJECTION_NAME).read_text(encoding="utf-8"))
    assert rej["stage"] == "hook:build_glb" and "KeyError" in rej["reason"]
    assert not (tmp_path / "i4d" / "manifest.json").exists()


# --- AUD-1 I7: capa auxiliar que no se pudo leer ---

def aux_unreadable(poly, dem):
    return AuxStats(n_pixels=0, void_water_fill_pct=None, hem_p90_m=None, available=False, note="EDM: no se pudo leer (503)",
                    unavailable=["EDM", "FLM", "WBM", "HEM"])


def test_unreadable_aux_layers_make_the_factor_explicitly_unavailable_and_require_review(tmp_path):
    r = run(tmp_path, "i7", fetch_aux=aux_unreadable)
    c = r.manifest["confidence"]
    assert c["by_factor"]["void_water_pct"] == "no_disponible" and c["by_factor"]["hem_p90_m"] == "no_disponible"
    assert c["overall"] in ("B", "C") and c["human_review_required"] is True
    assert set(c["unavailable_factors"]) == {"void_water_pct", "hem_p90_m"} and c["review_reason"]
    assert r.manifest["results"]["aux_layers"]["unavailable"] == ["EDM", "FLM", "WBM", "HEM"]
    ok = run(tmp_path, "i7ok")
    assert ok.manifest["confidence"]["by_factor"]["void_water_pct"] != "no_disponible"
    assert ok.manifest["confidence"]["human_review_required"] is (ok.manifest["confidence"]["overall"] == "C")


def test_aux_reject_request_is_unavailable_not_sin_dato(tmp_path):
    def boom(poly, dem):
        raise RejectRequest("nombre de tile inesperado")

    r = run(tmp_path, "i7b", fetch_aux=boom)
    assert r.manifest["confidence"]["by_factor"]["void_water_pct"] == "no_disponible"


# --- v0.3.0: dosel/construido (ESA WorldCover) y dos sellos ---

def canopy_stats(pct, available=True):
    from terreno.worldcover import CanopyStats
    return lambda poly: CanopyStats(n_pixels=9, canopy_pct=pct, tree_pct=pct, built_pct=0.0, context_pct=20.0,
                                    available=available, note="" if available else "WorldCover: no se pudo leer (x)", tiles=["S36W072"])


def test_canopy_measured_enables_level_a_and_goes_in_the_manifest(tmp_path):
    r = run(tmp_path, "w", fetch_canopy=canopy_stats(4.0))
    assert r.confidence.by_factor["canopy_pct"] == "A" and r.confidence.values["canopy_pct"] == 4.0
    wc = r.manifest["results"]["worldcover"]
    assert wc["status"] == "ok" and wc["canopy_pct"] == 4.0 and "CC BY 4.0" in wc["source"]


def test_high_canopy_lowers_only_the_data_seal_and_never_to_C(tmp_path):
    r = run(tmp_path, "w2", fetch_canopy=canopy_stats(80.0))
    assert r.confidence.by_factor["canopy_pct"] == "B" and r.confidence.by_seal["confianza_dato"] == "B"
    assert "canopy_pct" in r.confidence.limiting_factors or r.confidence.overall == "B"


def test_canopy_unreadable_is_no_disponible_with_human_review(tmp_path):
    r = run(tmp_path, "w3", fetch_canopy=canopy_stats(None, available=False))
    assert r.confidence.by_factor["canopy_pct"] == "no_disponible" and r.confidence.human_review_required is True
    assert "canopy_pct" in r.confidence.unavailable_factors and r.manifest["results"]["worldcover"]["status"] == "no_disponible"


def test_canopy_reject_request_does_not_abort_the_report(tmp_path):
    def boom(poly):
        raise RejectRequest("poligono invalido")
    r = run(tmp_path, "w4", fetch_canopy=boom)
    assert r.confidence.by_factor["canopy_pct"] == "no_disponible"


def test_canopy_run_is_reproducible(tmp_path):
    a = run(tmp_path, "w5a", fetch_canopy=canopy_stats(4.0)).result_hash
    b = run(tmp_path, "w5b", fetch_canopy=canopy_stats(4.0)).result_hash
    assert a == b
    assert a != run(tmp_path, "w5c", fetch_canopy=canopy_stats(60.0)).result_hash


def test_manifest_carries_both_seals(tmp_path):
    r = run(tmp_path, "w6", fetch_canopy=canopy_stats(4.0))
    assert set(r.manifest["confidence"]["by_seal"]) == {"medibilidad_lote", "confianza_dato"}
