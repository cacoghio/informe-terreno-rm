import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from terreno import texts_es as T
from terreno.errors import RejectRequest
from terreno.glb import build_glb_hook
from terreno.manifest import hash_manifest, report_fields
from terreno.pipeline import REJECTION_NAME, run_pipeline
from terreno.report import fmt_es, render_pdf, render_pdf_hook
from tests.test_pipeline import POLY, SHIFT, TINY, aux_ok, fetchers


def pdf_text(path, first=None, last=None) -> str:
    cmd = ["pdftotext", "-enc", "UTF-8"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    out = subprocess.run(cmd + [str(path), "-"], capture_output=True, check=True).stdout.decode("utf-8")
    return re.sub(r"\s+", " ", out).strip()


def n_pages(path) -> int:
    out = subprocess.run(["pdfinfo", str(path)], capture_output=True, check=True).stdout.decode("utf-8", "replace")
    return int(re.search(r"Pages:\s+(\d+)", out).group(1))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def reseal(m: dict) -> dict:
    """Un manifiesto editado a mano deja de ser valido: se vuelve a sellar el hash (lo hace solo el pipeline)."""
    m["result_hash"] = hash_manifest(m)
    return m


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("rep")
    ok = run_pipeline(POLY, base / "ok", fetch=fetchers(base), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=base / "c1",
                      hooks={"build_glb": build_glb_hook})
    tiny = run_pipeline(TINY, base / "tiny", fetch=fetchers(base), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=base / "c2",
                        hooks={"build_glb": build_glb_hook})
    def no_key(poly, cache_dir):
        raise RejectRequest("falta OPENTOPOGRAPHY_API_KEY")

    nocmp = run_pipeline(POLY, base / "nocmp", fetch={**fetchers(base), "aw3d30": no_key}, fetch_aux=aux_ok, cache_dir=base / "c3")
    return {"base": base, "ok": ok, "tiny": tiny, "nocmp": nocmp}


@pytest.fixture(scope="module")
def pdf_ok(runs):
    return render_pdf(runs["ok"].manifest, runs["base"] / "ok" / "report.pdf", site_name="Sitio sintético")


@pytest.fixture(scope="module")
def pdf_tiny(runs):
    return render_pdf(runs["tiny"].manifest, runs["base"] / "tiny" / "report.pdf")


@pytest.fixture(scope="module")
def pdf_c(runs):
    m = copy.deepcopy(runs["ok"].manifest)  # predio conclusivo (100 px) pero nivel C: el informe omite la pendiente puntual
    m["confidence"].update(overall="C", human_review_required=True, omit_point_slope=True)
    reseal(m)
    return render_pdf(m, runs["base"] / "c" / "report.pdf"), m


# ---------- los 7 tests del plan ----------

def test_page1_has_source_resolution_level_before_results(runs, pdf_ok):
    f = report_fields(runs["ok"].manifest)
    p1 = pdf_text(pdf_ok, 1, 1)
    full = pdf_text(pdf_ok)
    assert f["fuente"] in p1 and "30 m" in p1
    assert re.search(r"Nivel de confianza:? " + f["nivel"], p1)
    assert re.search(rf"Píxeles de 30 m dentro del predio:? {f['n_pixeles']}\b", p1)
    first_result = full.index("3. Elevación")
    for needle in (f["fuente"], "Resolución real", "Nivel de confianza", "Píxeles de 30 m dentro del predio"):
        assert full.index(needle) < first_result, needle
    assert "3. Elevación" not in p1  # los resultados empiezan despues de la calidad del dato


def test_contains_fixed_disclaimer_and_not_covered_list(pdf_ok):
    t = pdf_text(pdf_ok)
    for s in (T.DESCARGO, T.FILTRO_DE_ESCRITORIO, T.AVISO_RESOLUCION, T.NO_AVAL, T.NO_CUBRE_TITULO, *T.NO_CUBRE_ITEMS):
        assert norm(s) in t, s


def test_contains_glo30_attribution_and_liability_sentence(pdf_ok):
    t = pdf_text(pdf_ok)
    assert norm(T.ATRIBUCION_GLO30) in t and norm(T.NO_RESPONSABILIDAD_GLO30) in t
    assert norm(T.ATRIBUCION_GLO30) == (
        "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided "
        "under COPERNICUS by the European Union and ESA; all rights reserved")


def test_inconclusive_site_shows_no_point_slope(runs, pdf_tiny):
    m = runs["tiny"].manifest
    assert m["results"]["terrain"]["inconclusive"] is True
    t = pdf_text(pdf_tiny)
    assert "no concluyente" in t.lower()
    low = t.lower().split("14. anexo de trazabilidad")[0]  # el anexo lista las hipotesis de la receta (texto generico)
    for forbidden in ("pendiente media", "5-15 %", "15-30 %", "puntaje de complejidad: ", "orientación predominante: "):
        assert forbidden not in low, forbidden
    assert not re.search(r"\b[1-7] ?/ ?7\b", t)  # sin puntaje de relieve 1-7
    # ningun valor de pendiente del dato original aparece (el predio no tiene claves de pendiente)
    assert "slope_mean_pct" not in m["results"]["terrain"] and "revisión humana obligatoria" in low
    # los demas apartados siguen y las 14 secciones estan
    assert "14. Anexo de trazabilidad" in t


def test_level_C_marks_human_review(pdf_c, pdf_ok):
    path, m = pdf_c
    t = pdf_text(path)
    assert "revisión humana obligatoria" in t.lower()
    assert norm(T.REVISION_HUMANA_C) in t
    assert "revisión humana obligatoria" in pdf_text(path, 1, 1).lower()  # visible en la pagina 1
    # y en un nivel que no es C NO aparece
    assert "revisión humana obligatoria" not in pdf_text(pdf_ok).lower()


def test_curves_labeled_indicative(runs, pdf_ok):
    t = pdf_text(pdf_ok)
    assert "9. Curvas de nivel indicativas" in t
    assert norm(T.CURVAS_INDICATIVAS) in t and norm(T.CURVAS_SIN_DESCARGA) in t
    assert "equidistancia de 10 m" in t


def test_vertical_exaggeration_labeled(pdf_ok):
    t = pdf_text(pdf_ok)
    assert "Exageración vertical ×1,5" in t or "Exageración ×1,5" in t
    assert "12. Visor 3D y video" in t


# ---------- rechazo: nunca un PDF sin pipeline valido ----------

def test_pdf_not_produced_when_pipeline_rejected(tmp_path):
    out = tmp_path / "o"
    called = []

    def bad_fetch(poly, cache_dir):
        raise RejectRequest("tile no publicado: X")

    with pytest.raises(RejectRequest):
        run_pipeline(POLY, out, fetch={"glo30": bad_fetch}, cache_dir=tmp_path / "c",
                     hooks={"render_pdf": lambda m, o: called.append(render_pdf_hook(m, o))})
    assert not called and not (out / "report.pdf").exists() and (out / REJECTION_NAME).exists()


def test_render_pdf_refuses_next_to_a_rejection(tmp_path, runs):
    out = tmp_path / "o"
    out.mkdir()
    (out / REJECTION_NAME).write_text(json.dumps({"rejected": True, "stage": "x", "reason": "r"}), encoding="utf-8")
    with pytest.raises(RejectRequest, match="rechaz"):
        render_pdf(runs["ok"].manifest, out / "report.pdf")
    assert not (out / "report.pdf").exists()


def test_tampered_or_incomplete_manifest_rejected(tmp_path, runs):
    m = copy.deepcopy(runs["ok"].manifest)
    m["results"]["terrain"]["elev_max"] = 9999.0  # sin volver a sellar el hash
    with pytest.raises(RejectRequest, match="hash"):
        render_pdf(m, tmp_path / "a.pdf")
    m2 = copy.deepcopy(runs["ok"].manifest)
    del m2["confidence"]
    with pytest.raises(RejectRequest, match="manifiesto"):
        render_pdf(reseal(m2), tmp_path / "b.pdf")
    assert not (tmp_path / "a.pdf").exists() and not (tmp_path / "b.pdf").exists()


# ---------- reglas de honestidad ----------

def test_elevation_with_tolerance_and_noise_flag(runs, pdf_ok):
    s = runs["ok"].manifest["results"]["terrain"]
    t = pdf_text(pdf_ok)
    assert f"{fmt_es(s['elev_min'], 1)} m (±4 m)" in t and f"{fmt_es(s['elev_max'], 1)} m (±4 m)" in t
    assert "EGM2008" in t and "m s. n. m." in t
    assert s["elevation_within_noise"] is False and T.RUIDO_DEL_DATO not in t
    # rango < 4 m: se dice que esta dentro del ruido
    m = copy.deepcopy(runs["ok"].manifest)
    m["results"]["terrain"].update(elev_range=3.0, elevation_within_noise=True)
    p = render_pdf(reseal(m), runs["base"] / "noise" / "report.pdf")
    assert norm(T.RUIDO_DEL_DATO) in pdf_text(p)
    assert "menor que ~4 m" in norm(pdf_text(p)) and "2×LE90" not in pdf_text(p)


def test_report_numbers_come_from_manifest(runs):
    m = copy.deepcopy(runs["ok"].manifest)
    m["results"]["terrain"].update(n_pixels=4321, elev_min=1234.5, slope_mean_pct=21.3)
    reseal(m)
    t = pdf_text(render_pdf(m, runs["base"] / "edited" / "report.pdf"))
    assert re.search(r"Píxeles de 30 m dentro del predio:? 4\.321", t)  # separador de miles es-CL
    assert "1.234,5 m (±4 m)" in t and "21,3 %" in t


def test_conclusive_site_shows_slope_classes_aspect_and_score(runs, pdf_ok):
    f = report_fields(runs["ok"].manifest)
    t = pdf_text(pdf_ok)
    assert f"{fmt_es(f['pendiente']['media_pct'], 1)} %" in t and "5-15 %" in t
    assert "Puntaje de complejidad del relieve" in t and f"{f['puntaje_relieve']} de 7" in t
    assert "Orientación predominante" in t and f["orientacion"]["dominante"] in t


def test_level_C_with_slope_stats_hides_slope_values(pdf_c):
    path, m = pdf_c
    t = pdf_text(path)
    assert fmt_es(m["results"]["terrain"]["slope_mean_pct"], 1) + " %" not in t
    assert "pendiente media" not in t.lower().split("14. anexo de trazabilidad")[0] and "no concluyente" in t.lower()
    assert not re.search(r"\b[1-7] de 7\b", re.sub(r"hoja \d+ de \d+", "", t))  # (sin el pie "hoja N de M")


def test_every_page_declares_resolution_and_level(pdf_ok):
    for p in range(1, n_pages(pdf_ok) + 1):
        t = pdf_text(pdf_ok, p, p)
        assert "resolución real 30 m" in t and re.search(r"nivel [ABC]", t), p


def test_sentinel2_note_only_when_texture_used(runs):
    m = runs["ok"].manifest
    without = pdf_text(render_pdf(m, runs["base"] / "s0" / "report.pdf"))
    with_s2 = pdf_text(render_pdf(m, runs["base"] / "s1" / "report.pdf", sentinel2_year=2026))
    assert "Sentinel" not in without
    assert T.SENTINEL2_TEMPLATE.format(year=2026) in with_s2


def test_comparison_section_with_and_without_aw3d30(runs, pdf_ok):
    t = pdf_text(pdf_ok)
    f = report_fields(runs["ok"].manifest)
    assert "11. Comparación de fuentes" in t and f"{fmt_es(f['comparacion']['rms_diff_m'], 2)} m" in t
    assert norm(T.NOTA_AW3D30) in t
    t2 = pdf_text(render_pdf(runs["nocmp"].manifest, runs["base"] / "nocmp" / "report.pdf"))
    assert "no disponible" in t2.lower() and "OPENTOPOGRAPHY_API_KEY" in t2 and "AW3D30" in t2
    assert norm(T.NOTA_AW3D30) not in t2


def test_all_fourteen_sections_in_order(pdf_ok):
    t = pdf_text(pdf_ok)
    heads = ["1. Ficha del terreno", "2. Calidad del dato", "3. Elevación", "4. Pendiente por clases", "5. Orientación",
             "6. Puntaje de complejidad del relieve", "7. Asoleamiento y sombra por relieve", "8. Perfiles de elevación",
             "9. Curvas de nivel indicativas", "10. Contexto hídrico de ladera", "11. Comparación de fuentes",
             "12. Visor 3D y video", "13. Qué este informe no cubre", "14. Anexo de trazabilidad"]
    pos = [t.index(h) for h in heads]
    assert pos == sorted(pos)


def test_traceability_annex_comes_from_manifest(runs, pdf_ok):
    m = runs["ok"].manifest
    t = pdf_text(pdf_ok)
    for v in (m["result_hash"], m["recipe"]["hash"], m["inputs"]["hash"], m["recipe"]["version"], m["grid"]["crs"],
              m["recipe"]["tools"]["rasterio"], m["inputs"]["datasets"][0]["array_sha256"]):
        assert str(v) in t.replace(" ", ""), v if len(str(v)) < 20 else str(v)[:12]


def test_layout_no_empty_pages_and_reasonable_length(pdf_ok, pdf_tiny):
    for p in (pdf_ok, pdf_tiny):
        n = n_pages(p)
        assert 4 <= n <= 14, n
        for i in range(1, n + 1):
            assert len(pdf_text(p, i, i)) > 400, (p, i)


def test_missing_work_files_degrade_to_notes_not_crash(runs):
    m = copy.deepcopy(runs["ok"].manifest)
    m["volatile"]["paths"] = {}
    p = render_pdf(reseal(m), runs["base"] / "nopaths" / "report.pdf")
    t = pdf_text(p)
    assert "figura no disponible" in t.lower() and "14. Anexo de trazabilidad" in t


# ---------- gancho y determinismo ----------

def test_hook_writes_report_pdf_and_keeps_result_hash(tmp_path):
    def go(name):
        return run_pipeline(POLY, tmp_path / name, fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                            cache_dir=tmp_path / f"c_{name}", hooks={"build_glb": build_glb_hook, "render_pdf": render_pdf_hook},
                            now=lambda: "2026-10-23T10:00:00+00:00")

    a, b = go("a"), go("b")
    assert a.outputs["render_pdf"] == tmp_path / "a" / "report.pdf" and (tmp_path / "a" / "report.pdf").exists()
    assert a.result_hash == b.result_hash
    # los textos (datos) del PDF son identicos entre corridas; los bytes no se hashean
    assert pdf_text(tmp_path / "a" / "report.pdf") == pdf_text(tmp_path / "b" / "report.pdf")
    assert n_pages(tmp_path / "a" / "report.pdf") == n_pages(tmp_path / "b" / "report.pdf")


def test_fixed_texts_are_constants():
    for name in ("DESCARGO", "NO_CUBRE_TITULO", "ATRIBUCION_GLO30", "NO_RESPONSABILIDAD_GLO30", "AVISO_RESOLUCION",
                 "SENTINEL2_TEMPLATE"):
        assert isinstance(getattr(T, name), str) and getattr(T, name)
    assert isinstance(T.NO_CUBRE_ITEMS, tuple) and len(T.NO_CUBRE_ITEMS) >= 8


def test_fmt_es():
    assert fmt_es(1234.5, 1) == "1.234,5" and fmt_es(0.25, 2) == "0,25" and fmt_es(-3.04, 1) == "-3,0"
    assert fmt_es(4958, 0) == "4.958"


def test_default_hooks_cover_glb_and_pdf_and_match_hook_order(tmp_path):
    from terreno.hooks import default_hooks
    from terreno.pipeline import HOOK_ORDER

    h = default_hooks()
    assert set(h) == {"build_glb", "render_pdf"} and set(h) <= set(HOOK_ORDER)
    r = run_pipeline(POLY, tmp_path / "o", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "c", hooks=h)
    out = tmp_path / "o"
    assert [p.name for p in (r.outputs["build_glb"], r.outputs["render_pdf"])] == ["terreno.glb", "report.pdf"]
    assert (out / "layers" / "curvas.png").exists() and (out / "predio.geojson").exists()


# --- AUD-1 I3: la fecha de descarga no es la fecha de adquisicion del dato ---

def test_manifest_records_the_data_acquisition_epoch_of_each_dataset(runs):
    ds = {d["source"]: d for d in runs["ok"].manifest["inputs"]["datasets"]}
    assert "TanDEM-X" in ds["glo30"]["acquisition_epoch"] and "2011-2015" in ds["glo30"]["acquisition_epoch"]
    assert "2006" in ds["aw3d30"]["acquisition_epoch"]
    assert "v3.2" in ds["aw3d30"]["version"] and "v3.1" not in ds["aw3d30"]["version"]  # AUD-1 M3: OpenTopography sirve v3.2


def test_pdf_labels_the_download_date_and_states_the_tandem_x_epoch(runs, pdf_ok):
    full = pdf_text(pdf_ok)
    p1 = pdf_text(pdf_ok, 1, 1)
    assert "Fecha de adquisición del dato" not in full and "Fecha de adquisición:" not in full
    assert "Fecha de descarga" in p1
    assert "TanDEM-X" in p1 and "2011-2015" in p1
    acquired = runs["ok"].manifest["volatile"]["acquired"]["glo30"]
    assert acquired in p1  # la fecha mostrada sigue siendo la de descarga, rotulada como tal
    annex = pdf_text(pdf_ok, n_pages(pdf_ok) - 2, n_pages(pdf_ok))
    assert "Época de adquisición" in annex and "Fecha de descarga" in annex


# --- AUD-1 I7: el PDF dice que un factor medible no se pudo leer ---

def test_pdf_states_an_unreadable_measurable_factor_and_requires_review(tmp_path):
    from tests.test_pipeline import aux_unreadable

    r = run_pipeline(POLY, tmp_path / "o", fetch=fetchers(tmp_path), fetch_aux=aux_unreadable, shift_fn=SHIFT,
                     cache_dir=tmp_path / "c")
    pdf = render_pdf(r.manifest, tmp_path / "o" / "report.pdf")
    t = pdf_text(pdf)
    assert "no disponible" in t.lower() and "no se pudo leer" in t.lower()
    assert "revisión humana" in t.lower() or "revision humana" in t.lower()
    sec2 = t.split("2. Calidad del dato")[1].split("3. Elevación")[0]
    assert "no disponible" in sec2.lower() and "sin dato (tope B)" not in sec2.split("Vacíos")[1].split("Incertidumbre")[0]


# --- AUD-1 M7: la figura 3D no imprime numeros que no estan en el manifiesto ---

def test_3d_figure_has_no_numeric_axis_labels(runs, tmp_path, monkeypatch):
    import terreno.report_figs as rf

    seen = []
    real = rf._png

    def capture(fig):
        if getattr(fig, "axes", None) and hasattr(fig.axes[0], "get_zlim"):
            seen.append(fig)
        return real(fig)

    monkeypatch.setattr(rf, "_png", capture)
    pdf = render_pdf(runs["ok"].manifest, tmp_path / "r.pdf")
    assert len(seen) == 1
    ax = seen[0].axes[0]
    for labels in (ax.get_xticklabels(), ax.get_yticklabels(), ax.get_zticklabels()):
        assert all(t.get_text() == "" for t in labels)  # ni cotas de la ventana ni coordenadas locales
    texts = " ".join(t.get_text() for t in seen[0].texts)
    assert "×" in texts  # la exageracion vertical sigue rotulada en la figura
    assert "cualitativa" in pdf_text(pdf).lower()


# --- v0.3.0: dos sellos y dosel medido ---

def test_pdf_shows_both_seals_and_worldcover_attribution(tmp_path):
    from terreno.worldcover import CanopyStats
    cs = CanopyStats(n_pixels=9, canopy_pct=4.0, tree_pct=4.0, built_pct=0.0, context_pct=10.0, available=True,
                     tiles=["S36W072"])
    r = run_pipeline(POLY, tmp_path / "o", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                     cache_dir=tmp_path / "c", fetch_canopy=lambda poly: cs)
    t = norm(pdf_text(render_pdf(r.manifest, tmp_path / "o" / "report.pdf")))
    assert "medibilidad del lote" in t.lower() and "confianza del dato" in t.lower()
    assert "esa worldcover" in t.lower() and "cc by 4.0" in t.lower()
    assert "no medido" not in t.lower()


def test_pdf_without_worldcover_has_no_worldcover_attribution(runs, pdf_ok):
    assert "worldcover project" not in pdf_text(pdf_ok).lower()


def test_pdf_names_worldcover_when_its_read_failed(tmp_path):
    from terreno.worldcover import CanopyStats
    bad = CanopyStats(n_pixels=0, canopy_pct=None, available=False, note="WorldCover: no se pudo leer (503)")
    r = run_pipeline(POLY, tmp_path / "o", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                     cache_dir=tmp_path / "c", fetch_canopy=lambda poly: bad)
    t = norm(pdf_text(render_pdf(r.manifest, tmp_path / "o" / "report.pdf"))).lower()
    assert "no disponible" in t and "worldcover" in t and "capa auxiliar de copernicus" not in t
