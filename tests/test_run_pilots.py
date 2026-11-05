"""run_pilots.py: corrida de los cinco sitios x 2, metricas y estado de credenciales (Task 14)."""
import csv
import json
import sys
from pathlib import Path

import pytest
from shapely.geometry import box

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_pilots as rp  # noqa: E402
from terreno.errors import RejectRequest  # noqa: E402
from tests.test_pipeline import LON0, LAT0, POLY, SHIFT, aux_ok, fetchers  # noqa: E402

NO_CREDS = {"opentopography": False, "earthdata": False}


def site_run(tmp_path, name, *, live=False, creds=NO_CREDS, fetch=None, **kw):
    return rp.run_site("sintetico", POLY, tmp_path / name, live=live, creds=creds, fetch=fetch or fetchers(tmp_path),
                       fetch_aux=aux_ok, shift_fn=SHIFT, **kw)


# --- credenciales (nunca se leen valores, solo si existen) ---

def test_credentials_absent(tmp_path, monkeypatch):
    for k in ("OPENTOPOGRAPHY_API_KEY", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "NETRC"):
        monkeypatch.delenv(k, raising=False)
    c = rp.credential_state(home=tmp_path)
    assert c["opentopography"] is False and c["earthdata"] is False
    assert "OPENTOPOGRAPHY_API_KEY" in c["missing"]["opentopography"]
    assert "_netrc" in c["missing"]["earthdata"] and "EARTHDATA_USERNAME" in c["missing"]["earthdata"]


def test_credentials_present_values_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "SECRETO-123")
    monkeypatch.setenv("EARTHDATA_USERNAME", "usuario-x")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "clave-y")
    c = rp.credential_state(home=tmp_path)
    assert c["opentopography"] is True and c["earthdata"] is True
    blob = json.dumps(c)
    assert "SECRETO-123" not in blob and "usuario-x" not in blob and "clave-y" not in blob


def test_earthdata_netrc_file_counts_only_with_the_urs_host(tmp_path, monkeypatch):
    for k in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "NETRC"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "_netrc").write_text("machine example.org login a password b\n", encoding="utf-8")
    assert rp.credential_state(home=tmp_path)["earthdata"] is False
    (tmp_path / "_netrc").write_text("machine urs.earthdata.nasa.gov login a password b\n", encoding="utf-8")
    assert rp.credential_state(home=tmp_path)["earthdata"] is True


# --- una corrida ---

def test_run_site_records_the_metrics_the_plan_asks_for(tmp_path):
    m = site_run(tmp_path, "a")
    assert m["status"] == "ok" and m["site"] == "sintetico"
    assert m["level"] in "ABC" and m["human_review_required"] in (True, False)
    assert set(m["factors"]) == {"pixels", "elev_range_m", "slope_mean_pct", "disagreement_rms_m", "canopy_pct",
                                 "void_water_pct", "hem_p90_m"}
    assert m["n_pixels"] > 0 and len(m["result_hash"]) == 64
    for stage in ("fetch_dem", "reproject", "compute_terrain", "build_glb", "render_pdf", "publish_viewer", "total"):
        assert m["stages_s"][stage] >= 0.0, stage
    assert m["peak_rss_mb"] > 10
    assert m["glb_bytes"] > 0 and m["pdf_pages"] >= 5 and m["pdf_bytes"] > 0
    assert m["published"] is True
    for f in ("report.pdf", "terreno.glb", "predio.geojson", "manifest.json", "publicado/index.html"):
        assert (tmp_path / "a" / f).exists(), f


def test_without_credentials_aw3d30_and_truth_are_blocked_with_the_exact_missing_item(tmp_path):
    m = site_run(tmp_path, "a", live=True)
    assert m["aw3d30"]["status"] == "BLOQUEADO_CREDENCIALES"
    assert "OPENTOPOGRAPHY_API_KEY" in m["aw3d30"]["detail"]
    assert m["truth"]["status"] == "BLOQUEADO_CREDENCIALES"
    assert "_netrc" in m["truth"]["detail"] or "EARTHDATA" in m["truth"]["detail"]
    assert m["tier2"]["status"] == "BLOQUEADO_CREDENCIALES"
    assert m["factors"]["disagreement_rms_m"] == "sin_dato"


def test_not_live_does_not_use_credentials_even_if_present(tmp_path):
    m = site_run(tmp_path, "a", live=False, creds={"opentopography": True, "earthdata": True})
    assert m["aw3d30"]["status"] == "no_solicitado" and m["truth"]["status"] == "no_solicitado"


def test_live_with_key_runs_aw3d30_and_records_the_disagreement(tmp_path):
    m = site_run(tmp_path, "a", live=True, creds={"opentopography": True, "earthdata": False})
    assert m["aw3d30"]["status"] == "ejecutado"
    assert m["aw3d30"]["rms_diff_m"] is not None and m["aw3d30"]["datum_aligned"] is True
    assert m["factors"]["disagreement_rms_m"] != "sin_dato"


def test_live_with_earthdata_records_validation_and_tier2(tmp_path):
    from tests.test_truth import fps_on  # huellas sobre un plano UTM; solo importa que sean validas en el raster real
    seen = {}

    def footprints(poly, cache):
        seen["called"] = True
        return []  # sin huellas: validacion insuficiente, no un numero

    m = site_run(tmp_path, "a", live=True, creds={"opentopography": False, "earthdata": True}, footprints_fn=footprints)
    assert seen["called"]
    assert m["truth"]["status"] == "validacion_insuficiente" and m["truth"]["n_footprints"] == 0
    assert m["truth"]["rmse_m"] is None
    assert m["tier2"]["status"] == "validacion_insuficiente"


def test_rejection_is_a_recorded_result_not_a_crash(tmp_path):
    def boom(poly, cache_dir):
        raise RejectRequest("tile no publicado (prueba)")

    m = site_run(tmp_path, "a", fetch={"glo30": boom})
    assert m["status"] == "rechazado" and "tile no publicado" in m["reason"] and m["stage"] == "fetch_dem"
    assert m["result_hash"] is None
    assert not (tmp_path / "a" / "report.pdf").exists()


def test_unexpected_error_is_recorded_as_error(tmp_path):
    def boom(poly, cache_dir):
        raise OSError("disco lleno (prueba)")

    m = site_run(tmp_path, "a", fetch={"glo30": boom})
    assert m["status"] == "error" and "disco lleno" in m["reason"]


def test_same_inputs_give_the_same_hash_in_two_runs(tmp_path):
    a = site_run(tmp_path, "a")
    b = site_run(tmp_path, "b")
    assert a["result_hash"] == b["result_hash"]


# --- resumen y archivos ---

def fake(hash_, status="ok"):
    return {"status": status, "result_hash": hash_, "level": "B", "stages_s": {"total": 3.0}, "peak_rss_mb": 200.0,
            "reason": None if status == "ok" else "x", "stage": None}


def test_summarize_hash_match_and_mismatch():
    ok = rp.summarize("a", {"nombre": "A", "fuente_poligono": "lote de prueba estándar (datos públicos)"}, fake("h"), fake("h"))
    bad = rp.summarize("a", {"nombre": "A", "fuente_poligono": "lote de prueba estándar (datos públicos)"}, fake("h"), fake("g"))
    rej = rp.summarize("a", {"nombre": "A", "fuente_poligono": "lote de prueba estándar (datos públicos)"}, fake(None, "rechazado"), fake(None, "rechazado"))
    assert ok["hash_match"] is True and bad["hash_match"] is False
    assert rej["hash_match"] is None  # sin resultado no hay hash que comparar


def test_write_metrics_json_and_flat_csv(tmp_path):
    row = rp.summarize("rm01_plano_urbano", {"nombre": "RM-01", "fuente_poligono": "lote de prueba estándar (datos públicos)"}, site_run(tmp_path, "a"), site_run(tmp_path, "b"))
    row["video"] = {"status": "ok", "wall_s": 12.5, "frames": 540, "bytes": 1000}
    jpath, cpath = rp.write_metrics([row], tmp_path / "out", machine={"cpu_count": 12}, credentials=NO_CREDS)
    data = json.loads(jpath.read_text(encoding="utf-8"))
    assert data["sites"][0]["site"] == "rm01_plano_urbano" and data["machine"]["cpu_count"] == 12
    assert data["credentials"] == NO_CREDS
    rows = list(csv.DictReader(cpath.open(encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["site"] == "rm01_plano_urbano"
    assert rows[0]["hash_match"] == "True" and rows[0]["level"] in "ABC"
    assert rows[0]["aw3d30_status"] == "no_solicitado" or rows[0]["aw3d30_status"].startswith("BLOQUEADO")
    assert float(rows[0]["t_fetch_dem_s"]) >= 0 and float(rows[0]["video_wall_s"]) == 12.5
    assert float(rows[0]["peak_rss_mb"]) > 10


def test_machine_info_has_cpu_and_ram():
    m = rp.machine_info()
    assert m["cpu_count"] >= 1 and m["ram_gb"] > 1 and m["python"].startswith("3.")


def test_viewer_url_for_real_out_dirs_is_encoded_and_same_origin():
    u = rp.viewer_site_url("http://127.0.0.1:8000", ROOT / "out" / "ñuñoa")
    assert u.startswith("http://127.0.0.1:8000/viewer/index.html?site=/out/")
    assert "ñ" not in u and "%C3%B1" in u


# --- validacion contra huellas: GLO-30 y AW3D30 (spec sec. 8 criterio 4) ---

def synthetic_footprints(n, *, per_track=10):
    from tests.test_pipeline import ramp_fn
    from terreno.truth import Footprint
    z = ramp_fn(0.0)
    out = []
    for i in range(n):
        lon = LON0 - 0.0010 + 0.0020 * (i / max(n - 1, 1))
        lat = LAT0 + (i % 5 - 2) * 0.0003
        out.append(Footprint(lon=lon, lat=lat, h_ellipsoid_m=z(lon, lat) + 27.0, h_orthometric_m=float(z(lon, lat)),
                             source="atl08", date="2024-01-01", quality_flag=1, granule=f"G{i // per_track}",
                             beam=f"gt{i // per_track}"))
    return out


def test_live_truth_stats_for_glo30_and_aw3d30_and_tier2(tmp_path):
    fps = synthetic_footprints(40)
    m = site_run(tmp_path, "a", live=True, creds={"opentopography": True, "earthdata": True},
                 footprints_fn=lambda poly, cache: fps)
    tr = m["truth"]
    assert tr["status"] == "ok" and tr["n_footprints"] == 40
    assert abs(tr["bias_m"]) < 0.6 and tr["rmse_m"] < 0.6  # GLO-30 sintetico = la verdad
    aw = tr["aw3d30"]
    assert aw["status"] == "ok" and aw["n"] == 40 and aw["datum_aligned"] is True
    assert aw["bias_m"] == pytest.approx(2.1, abs=0.6)  # AW3D30 sintetico = verdad + 2 m (+0,1 m de EGM96 -> EGM2008)
    assert set(tr["reference_slope"]) == {"n", "truth_pct", "context_pct", "report_lot_slope_pct"} and tr["reference_slope"]["n"] == 40
    assert tr["aw3d30"]["same_footprints_as_glo30"] is True
    assert (tmp_path / "a" / "footprints.json").exists()
    assert m["tier2"]["status"] == "ok" and m["tier2"]["n_train"] + m["tier2"]["n_test"] == 40


def test_live_truth_with_few_footprints_reports_insufficient_and_no_numbers(tmp_path):
    m = site_run(tmp_path, "a", live=True, creds={"opentopography": True, "earthdata": True},
                 footprints_fn=lambda poly, cache: synthetic_footprints(7))
    tr = m["truth"]
    assert tr["status"] == "validacion_insuficiente" and tr["rmse_m"] is None
    assert tr["aw3d30"]["status"] == "validacion_insuficiente" and tr["aw3d30"]["rmse_m"] is None
    assert m["tier2"]["status"] == "validacion_insuficiente" and m["tier2"]["rmse_raw_m"] is None


# --- AUD-1 I1: misma politica de raster (contexto) para criterio 4, Tier 2 y criterio 6 ---

def test_tiny_lot_truth_tier2_and_reference_slope_use_the_context_raster(tmp_path):
    """Huellas a 400-770 m del lote: fuera del raster de trabajo, dentro del contexto. Todo se evalua sobre el contexto."""
    from terreno.truth import Footprint
    from tests.test_pipeline import TINY, _big_fetchers, ramp_fn

    fps = []
    for i in range(40):
        lon, lat = LON0 + 0.004 + 0.0001 * i, LAT0 + 0.0003 * (i % 7) - 0.0009
        z = ramp_fn()(lon, lat)
        fps.append(Footprint(lon, lat, z + 28.0, z + 1.0, "atl08", "2024-01-01", 1, granule=f"G{i // 10}", beam=f"gt{i // 10}"))
    m = rp.run_site("sintetico", TINY, tmp_path / "t", live=True, creds={"opentopography": False, "earthdata": True},
                    fetch=_big_fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, footprints_fn=lambda p, c: fps)
    assert m["status"] == "ok"
    assert m["truth"]["status"] == "ok" and m["truth"]["n"] == 40 and m["truth"]["n_outside_raster"] == 0
    assert m["tier2"]["status"] == "ok" and m["tier2"]["n_train"] + m["tier2"]["n_test"] == 40
    assert m["truth"]["reference_slope"]["n"] == 40 and m["truth"]["reference_slope"]["context_pct"] is not None


def test_ground_truth_failure_is_recorded_not_a_rejection_of_the_site(tmp_path):
    def broken(poly, cache):
        raise RejectRequest("granulo no disponible tras 3 intentos: prueba")

    m = site_run(tmp_path, "a", live=True, creds={"opentopography": False, "earthdata": True}, footprints_fn=broken)
    assert m["status"] == "ok"  # el informe se entrega
    assert m["truth"]["status"] == "no_disponible" and "granulo no disponible" in m["truth"]["detail"]
    assert m["tier2"]["status"] == "validacion_insuficiente"


def test_real_footprint_fetch_uses_one_shared_cache_for_both_runs(tmp_path, monkeypatch):
    """Las huellas ICESat-2/GEDI son un insumo externo fijo: corrida 1 y 2 comparten cache (cada lectura remota tarda minutos)."""
    import terreno.truth as truth_mod

    seen = []

    def fake_fetch(poly, buffer_m, cache_dir, **kw):
        seen.append(Path(cache_dir).resolve())
        return []

    monkeypatch.setattr(truth_mod, "fetch_footprints", fake_fetch)
    monkeypatch.setattr(rp, "SHARED_TRUTH_CACHE", tmp_path / "shared_truth")
    creds = {"opentopography": False, "earthdata": True}
    for name in ("run1", "run2"):
        rp.run_site("sintetico", POLY, tmp_path / name, live=True, creds=creds, fetch=fetchers(tmp_path), fetch_aux=aux_ok,
                    shift_fn=SHIFT)
    assert len(seen) == 2 and seen[0] == seen[1] == (tmp_path / "shared_truth").resolve()
    assert not str(seen[0]).startswith(str((tmp_path / "run1").resolve()))


def test_real_aw3d30_fetch_uses_a_shared_cache_that_survives_the_out_dir_wipe(tmp_path, monkeypatch):
    """OpenTopography permite 50 llamadas/24 h: AW3D30 (lote y contexto) va a una cache compartida fuera de out/,
    que run_site no borra, y conserva ``margin_m`` para la ventana de contexto."""
    import inspect

    import terreno.compare as compare_mod

    seen = []

    def fake_fetch(poly, cache_dir, *, margin_m=0.0, **kw):
        seen.append((Path(cache_dir).resolve(), margin_m))
        raise RejectRequest("sin red en la prueba")

    monkeypatch.setattr(compare_mod, "fetch_aw3d30", fake_fetch)
    monkeypatch.setattr(rp, "SHARED_AW3D30_CACHE", tmp_path / "shared_aw3d30")
    assert "margin_m" in inspect.signature(rp.fetch_aw3d30_shared).parameters
    with pytest.raises(RejectRequest):
        rp.fetch_aw3d30_shared(POLY, tmp_path / "run1" / "cache", margin_m=1500.0)
    assert seen == [((tmp_path / "shared_aw3d30").resolve(), 1500.0)]
    # run_site en vivo usa ese fetcher para AW3D30 cuando no se inyecta otro
    used = {}
    monkeypatch.setattr(rp, "run_pipeline", lambda poly, out, **kw: used.update(kw) or (_ for _ in ()).throw(
        RejectRequest("corte")))
    rp.run_site("sintetico", POLY, tmp_path / "run2", live=True, creds={"opentopography": True, "earthdata": False},
                fetch_aux=aux_ok, shift_fn=SHIFT)
    assert used["fetch"]["aw3d30"] is rp.fetch_aw3d30_shared
