"""check_acceptance.py: cada criterio da PASS/FAIL/MANUAL/BLOQUEADO con evidencia; solo FAIL da codigo de salida != 0."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import check_acceptance as ca  # noqa: E402
from terreno.manifest import hash_manifest  # noqa: E402

KEYS = ("a", "b", "c", "d", "e")


def all_pass(ids):
    return {i: "passed" for i in ids} | {"__returncode__": 0}


def run_ok(stages=None, **kw):
    r = {"status": "ok", "result_hash": "h" * 64, "level": "B", "limiting_factors": ["slope_mean_pct"],
         "factors": {"canopy_pct": "sin_dato"}, "stages_s": stages or {"fetch_dem": 1.0, "compute_terrain": 0.5, "build_glb": 2.0,
                                                                       "render_pdf": 3.0, "context": 1.0, "total": 9.0},
         "peak_rss_mb": 300.0, "glb_bytes": 5000, "pdf_bytes": 100_000, "pdf_pages": 6, "published": True,
         "aw3d30": {"status": ca.BLOCKED, "detail": "falta OPENTOPOGRAPHY_API_KEY"},
         "truth": {"status": ca.BLOCKED, "detail": "faltan credenciales de NASA Earthdata"},
         "tier2": {"status": ca.BLOCKED, "detail": "faltan credenciales de NASA Earthdata"}}
    r.update(kw)
    return r


def metrics(**per_run):
    return {"machine": {"cpu_count": 12, "ram_gb": 16.0, "platform": "Windows"}, "credentials": {},
            "sites": [{"site": k, "hash_match": True, "run1": run_ok(**per_run), "run2": run_ok(**per_run),
                       "video": {"status": "ok", "wall_s": 180.0, "frames": 540, "gl": "d3d11"}} for k in KEYS]}


def ctx(tmp_path, m, **kw):
    c = ca.Ctx(out=tmp_path / "out", out2=tmp_path / "out2", licenses=tmp_path / "lic", metrics=m, keys=KEYS,
               pytest_runner=kw.pop("pytest_runner", all_pass), ffprobe_duration=kw.pop("ffprobe_duration", lambda p: 45.0),
               pdf_text=kw.pop("pdf_text", lambda p: ""))
    return c


def write_site(tmp_path, k, *, same_hash=True, video=True):
    for base, alt in ((tmp_path / "out", False), (tmp_path / "out2", True)):
        d = base / k
        (d / "publicado").mkdir(parents=True)
        m = {"x": 1 if not (alt and not same_hash) else 2, "volatile": {"t": str(alt)}}
        m["result_hash"] = hash_manifest(m)
        (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    d = tmp_path / "out" / k
    for f in ("report.pdf", "terreno.glb", "predio.geojson", "publicado/index.html"):
        (d / f).write_bytes(b"x")
    if video:
        (d / "video.mp4").write_bytes(b"x")
        (d / "video.render.json").write_text(json.dumps({"problems": []}), encoding="utf-8")


# --- criterio 1 ---

def test_c1_pass_when_hashes_equal_and_everything_exists(tmp_path):
    for k in KEYS:
        write_site(tmp_path, k)
    c = ca.check_1(ctx(tmp_path, metrics()))
    assert c.status == ca.PASS and len(c.evidence) == 5 and "igual" in c.evidence[0]


def test_c1_fail_when_second_run_hash_differs(tmp_path):
    for k in KEYS:
        write_site(tmp_path, k, same_hash=(k != "c"))
    c = ca.check_1(ctx(tmp_path, metrics()))
    assert c.status == ca.FAIL and any(e.startswith("c:") and "hash distinto" in e for e in c.evidence)


def test_c1_fail_when_video_missing_or_rejected_site(tmp_path):
    for k in KEYS:
        write_site(tmp_path, k, video=(k != "b"))
    m = metrics()
    m["sites"][3]["run1"] = {"status": "rechazado", "reason": "tile no publicado"}
    c = ca.check_1(ctx(tmp_path, m))
    assert c.status == ca.FAIL
    assert any(e.startswith("b:") and "video.mp4" in e for e in c.evidence)
    assert any(e.startswith("d:") and "rechazado" in e for e in c.evidence)


def test_c1_fail_without_metrics_or_with_out_of_range_video(tmp_path):
    assert ca.check_1(ctx(tmp_path, None)).status == ca.FAIL
    for k in KEYS:
        write_site(tmp_path, k)
    c = ca.check_1(ctx(tmp_path, metrics(), ffprobe_duration=lambda p: 75.0))
    assert c.status == ca.FAIL and "fuera de 30-60" in " ".join(c.evidence)


# --- criterio 2 ---

def test_c2_pass_fail_and_missing_test(tmp_path):
    assert ca.check_2(ctx(tmp_path, metrics())).status == ca.PASS
    one = next(iter(ca.REJECTION_TESTS["CRS equivocado / mezcla de CRS"]))
    c = ca.check_2(ctx(tmp_path, metrics(), pytest_runner=lambda ids: all_pass(ids) | {one: "failed"}))
    assert c.status == ca.FAIL
    c = ca.check_2(ctx(tmp_path, metrics(), pytest_runner=lambda ids: {i: "passed" for i in ids if i != one} | {"__returncode__": 0}))
    assert c.status == ca.FAIL and "no_encontrada" in " ".join(c.evidence)


def test_c2_names_only_existing_tests():
    """Cada prueba nombrada debe existir de verdad (si se renombra, el criterio no puede quedar en silencio)."""
    import re
    for group in ca.REJECTION_TESTS.values():
        for tid in group:
            f, name = tid.split("::")
            assert re.search(rf"def {name}\(", (ROOT / f).read_text(encoding="utf-8")), tid


# --- criterios 3 a 6 ---

def test_c3_to_c6_blocked_without_credentials_with_the_missing_item(tmp_path):
    c = ctx(tmp_path, metrics())
    r3, r4, r5, r6 = ca.check_3(c), ca.check_4(c), ca.check_5(c), ca.check_6(c)
    for r in (r3, r4, r5, r6):
        assert r.status == ca.BLOCKED
    assert "OPENTOPOGRAPHY_API_KEY" in r3.evidence[0] and "Earthdata" in r4.evidence[0] and "Earthdata" in r5.evidence[0]
    assert "sin_dato" in " ".join(r6.evidence) and "ningun sitio puede ser A" in " ".join(r6.evidence)


def aw(rms, aligned=True):
    return {"status": "ejecutado", "rms_diff_m": rms, "mean_diff_m": 0.4, "slope_diff_mean_pct": 1.1, "datum_aligned": aligned,
            "stats_scope": "poligono"}


def test_c3_pass_needs_map_range_and_datum(tmp_path):
    for k in KEYS:
        (tmp_path / "out" / k / "work").mkdir(parents=True)
        (tmp_path / "out" / k / "work" / "diff_x_minus_y.tif").write_bytes(b"x")
    assert ca.check_3(ctx(tmp_path, metrics(aw3d30=aw(2.5)))).status == ca.PASS
    assert ca.check_3(ctx(tmp_path, metrics(aw3d30=aw(9.0)))).status == ca.FAIL  # fuera del orden de magnitud
    assert ca.check_3(ctx(tmp_path, metrics(aw3d30=aw(2.5, aligned=False)))).status == ca.FAIL
    (tmp_path / "out" / "a" / "work" / "diff_x_minus_y.tif").unlink()
    assert ca.check_3(ctx(tmp_path, metrics(aw3d30=aw(2.5)))).status == ca.FAIL  # falta el mapa de un sitio


def truth_ok(n=30, rmse=3.0, **kw):
    return {"status": "ok", "n": n, "n_footprints": n, "bias_m": 1.0, "mae_m": 2.0, "rmse_m": rmse,
            "aw3d30": {"status": "ok", "n": n, "bias_m": 1.5, "mae_m": 2.2, "rmse_m": rmse + 0.3}, **kw}


def test_c4_pass_with_numbers_or_declared_insufficient(tmp_path):
    m = metrics(truth=truth_ok())
    m["sites"][0]["run1"]["truth"] = {"status": "validacion_insuficiente", "n_footprints": 4}
    c = ca.check_4(ctx(tmp_path, m))
    assert c.status == ca.PASS
    assert "validacion insuficiente" in c.evidence[0] and "RMSE 3.00" in c.evidence[1]


def t2(red, n=30):
    return {"status": "ok", "n_usable": n, "n_train": n // 2, "n_test": n // 2, "split_method": "por_traza", "bias_fitted_m": -1.0,
            "rmse_raw_m": 4.0, "rmse_corrected_m": 4.0 * (1 - red / 100), "reduction_pct": red}


def test_c5_verdicts(tmp_path):
    def with_t2(reds):
        m = metrics()
        for s, red in zip(m["sites"], reds):
            s["run1"]["tier2"] = t2(red)
        return ctx(tmp_path, m)
    promoted = ca.check_5(with_t2([30, 20, 11, -2, 0]))
    assert promoted.status == ca.PASS and "promovido" in promoted.evidence[-1]
    not_promoted = ca.check_5(with_t2([30, 9, 5, -2, 0]))
    assert not_promoted.status == ca.PASS and "no_promovido" in not_promoted.evidence[-2]
    m = metrics()
    for s in m["sites"]:
        s["run1"]["tier2"] = {"status": "validacion_insuficiente", "n_usable": 4}
    assert ca.check_5(ctx(tmp_path, m)).status == ca.FAIL  # no hay veredicto con datos insuficientes


def test_c6_discrimination_and_ranking(tmp_path):
    def build(levels_rmse, truth_slopes, dem_slopes):
        m = metrics()
        for s, (lv, rm), ts, ds in zip(m["sites"], levels_rmse, truth_slopes, dem_slopes):
            s["run1"]["level"] = lv
            s["run1"]["truth"] = truth_ok(rmse=rm, reference_slope={"n": 30, "truth_pct": ts, "context_pct": ds})
        return ctx(tmp_path, m)
    good = ca.check_6(build([("B", 2.0), ("B", 2.5), ("C", 5.0), ("C", 6.0), ("B", 3.0)], [2, 8, 15, 25, 5], [3, 9, 14, 30, 6]))
    assert good.status == ca.PASS and "Discrimina" in " ".join(good.evidence)
    bad = ca.check_6(build([("B", 6.0), ("B", 5.5), ("C", 2.0), ("C", 2.2), ("B", 5.0)], [2, 8, 15, 25, 5], [3, 9, 14, 30, 6]))
    assert bad.status == ca.FAIL and "NO discrimina" in " ".join(bad.evidence)
    rank = ca.check_6(build([("B", 2.0), ("B", 2.5), ("C", 5.0), ("C", 6.0), ("B", 3.0)], [2, 8, 15, 25, 5], [30, 20, 10, 3, 6]))
    assert rank.status == ca.FAIL and "Spearman" in " ".join(rank.evidence)


def test_c6_ignores_the_old_tautological_dem_plane_key(tmp_path):
    """AUD-1 I6: un 'dem_pct' (plano ajustado al DEM muestreado en las huellas) ya no puede dar PASS al criterio."""
    m = metrics()
    for s, (lv, rm), ts in zip(m["sites"], [("B", 2.0), ("B", 2.5), ("C", 5.0), ("C", 6.0), ("B", 3.0)], [2, 8, 15, 25, 5]):
        s["run1"]["level"] = lv
        s["run1"]["truth"] = truth_ok(rmse=rm, reference_slope={"n": 30, "truth_pct": ts, "dem_pct": ts})
    c = ca.check_6(ctx(tmp_path, m))
    assert c.status == ca.FAIL and "no evaluable" in " ".join(c.evidence)


# --- criterios 7 a 9 ---

def test_c7_manual_lists_what_exists_and_what_is_missing(tmp_path):
    for k in KEYS:
        write_site(tmp_path, k)
    c = ca.check_7(ctx(tmp_path, metrics()))
    assert c.status == ca.MANUAL and "6 pags" in c.evidence[0] and "FALTA" in c.evidence[-1]


def test_c8_manual_archive_inventory_and_attribution_check(tmp_path):
    from terreno import texts_es as T
    lic = tmp_path / "lic"
    lic.mkdir()
    for n in ("GLO30_x.txt", "AW3D30_x.pdf", "GEDTM30_x.txt", "ATL08_x.txt", "GEDI_x.txt", "EOSDIS_x.txt"):
        (lic / n).write_text("x", encoding="utf-8")
    for k in KEYS:
        write_site(tmp_path, k)
    good = T.ATRIBUCION_GLO30 + " ... " + T.NO_RESPONSABILIDAD_GLO30
    c = ca.check_8(ctx(tmp_path, metrics(), pdf_text=lambda p: good))
    assert c.status == ca.MANUAL and not any("NO ARCHIVADO" in e for e in c.evidence)
    assert sum("presentes" in e for e in c.evidence) == 5 and any("JAXA" in e and "FALTA" in e for e in c.evidence)
    c = ca.check_8(ctx(tmp_path, metrics(), pdf_text=lambda p: "sin atribucion"))
    assert c.status == ca.FAIL
    (lic / "GEDTM30_x.txt").unlink()
    assert any("GEDTM30: NO ARCHIVADO" in e for e in ca.check_8(ctx(tmp_path, metrics(), pdf_text=lambda p: good)).evidence)


def test_c9_pass_when_everything_measured_fail_without_video(tmp_path):
    c = ca.check_9(ctx(tmp_path, metrics()))
    assert c.status == ca.PASS and "memoria pico 300 MB" in c.evidence[1] and "video 180 s" in c.evidence[1]
    m = metrics()
    del m["sites"][2]["video"]
    c = ca.check_9(ctx(tmp_path, m))
    assert c.status == ca.FAIL and "FALTA MEDIR" in " ".join(c.evidence)


# --- salida ---

def test_exit_code_is_nonzero_only_for_fail(tmp_path):
    ok = [ca.Criterion(1, "t", ca.PASS), ca.Criterion(2, "t", ca.MANUAL), ca.Criterion(3, "t", ca.BLOCKED)]
    assert ca.exit_code(ok) == 0
    assert ca.exit_code(ok + [ca.Criterion(4, "t", ca.FAIL)]) == 1


def test_render_table_prints_nine_criteria_and_summary(tmp_path):
    res = ca.evaluate(ctx(tmp_path, metrics()))
    txt = ca.render_table(res)
    assert [c.n for c in res] == list(range(1, 10))
    for n in range(1, 10):
        assert f". " in txt and f"{n}. " in txt
    assert "RESUMEN:" in txt and "BLOQUEADO_CREDENCIALES=" in txt


def test_junit_parser_aggregates_parametrized_cases(tmp_path):
    """Una prueba parametrizada (nombre[param]) cuenta como una: pasa solo si pasan todas sus variantes."""
    xml = tmp_path / "r.xml"
    xml.write_text("""<testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_x[1]"/><testcase classname="tests.test_a" name="test_x[2]"><failure message="m"/></testcase>
      <testcase classname="tests.test_a" name="test_y[1]"/><testcase classname="tests.test_a" name="test_y[2]"/>
      <testcase classname="tests.test_a" name="test_z"><skipped/></testcase></testsuite></testsuites>""", encoding="utf-8")
    got = ca.parse_junit(xml)
    assert got == {"tests/test_a.py::test_x": "failed", "tests/test_a.py::test_y": "passed", "tests/test_a.py::test_z": "skipped"}


def test_c9_reports_the_software_gl_video_measurement_when_present(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "video_software_gl.json").write_text(
        json.dumps({"site": "x", "wall_s": 215.3, "frames": 540, "gl": "software"}), encoding="utf-8")
    c = ca.check_9(ctx(tmp_path, metrics()))
    assert any("sin GPU" in e and "215 s" in e for e in c.evidence)


def test_c6_uses_the_data_confidence_seal_when_present(tmp_path):
    """v0.3.0: el nivel final mezcla medibilidad y dato; el criterio se evalua con el sello 'confianza_dato'."""
    def build(rows):
        m = metrics()
        for s, (final, dato, rm, ts, ds) in zip(m["sites"], rows):
            s["run1"]["level"] = final
            s["run1"]["seals"] = {"medibilidad_lote": final, "confianza_dato": dato}
            s["run1"]["truth"] = truth_ok(rmse=rm, reference_slope={"n": 30, "truth_pct": ts, "context_pct": ds})
        return ctx(tmp_path, m)
    # El nivel final desordena (C con poco error, B con mucho) pero el sello del dato ordena A<=B<=C
    rows = [("C", "A", 2.5, 2, 3), ("C", "A", 3.0, 8, 9), ("B", "B", 5.0, 15, 14), ("B", "C", 8.0, 25, 30), ("B", "B", 6.0, 5, 6)]
    c = ca.check_6(build(rows))
    assert c.status == ca.PASS
    text = " ".join(c.evidence)
    assert "Confianza del dato" in text and "Discrimina" in text and "NO discrimina" not in text
    bad = [("B", "C", 2.5, 2, 3), ("B", "C", 3.0, 8, 9), ("C", "A", 5.0, 15, 14), ("C", "A", 8.0, 25, 30), ("B", "B", 6.0, 5, 6)]
    assert ca.check_6(build(bad)).status == ca.FAIL
