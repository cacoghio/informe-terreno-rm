"""Los nueve criterios de aceptacion del POC (SPEC-PRODUCTO-v0 sec. 8) con la evidencia concreta de cada uno.

    python check_acceptance.py

Estados: PASS | FAIL | MANUAL | BLOQUEADO_CREDENCIALES. El codigo de salida es distinto de 0 SOLO si hay algun FAIL;
BLOQUEADO_CREDENCIALES y MANUAL no son fallas. Lee out/metrics.json (lo escribe run_pilots.py) y vuelve a comprobar los
archivos y los hashes en disco; no se fia solo de las metricas. No afirma un criterio sin haber corrido su evidencia.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, MANUAL, BLOCKED = "PASS", "FAIL", "MANUAL", "BLOQUEADO_CREDENCIALES"
DELIVERABLES = ("report.pdf", "terreno.glb", "predio.geojson", "manifest.json", "publicado/index.html", "video.mp4")
RMS_MAX_M = 6.0  # orden de magnitud esperable del desacuerdo GLO-30 vs AW3D30 (plan Task 5: 1-6 m)
SPEARMAN_MIN = 0.7  # umbral propuesto para "el ranking coincide" (la spec no lo define; hipotesis)

# Pruebas con entradas deliberadamente malas (criterio 2), agrupadas por lo que ejercitan
REJECTION_TESTS = {
    "CRS equivocado / mezcla de CRS": [
        "tests/test_asserts.py::test_wrong_crs_rejected", "tests/test_asserts.py::test_crs_declared_vs_file_mismatch_rejected",
        "tests/test_asserts.py::test_degrees_not_meters_rejected", "tests/test_reproject.py::test_mixed_crs_dem_in_utm_rejected",
        "tests/test_reproject.py::test_mixed_crs_polygon_in_meters_rejected", "tests/test_agent_tools.py::test_agent_cannot_override_crs"],
    "bbox invertido / poligono fuera": [
        "tests/test_asserts.py::test_inverted_bbox_rejected", "tests/test_reproject.py::test_inverted_source_raster_rejected",
        "tests/test_asserts.py::test_polygon_outside_raster_rejected", "tests/test_reproject.py::test_polygon_outside_dem_rejected",
        "tests/test_geometry.py::test_inverted_or_empty_polygon_rejected", "tests/test_geometry.py::test_outside_chile_rejected",
        "tests/test_pipeline.py::test_polygon_outside_chile_rejected_before_any_fetch"],
    "limite de zona UTM (72 O)": [
        "tests/test_geometry.py::test_utm_zone_boundary", "tests/test_geometry.py::test_polygon_crossing_72w_has_single_declared_zone",
        "tests/test_reproject.py::test_polygon_crossing_72w_single_zone",
        "tests/test_reproject.py::test_zone_comes_from_centroid_not_from_caller",
        "tests/test_agent_tools.py::test_utm_zone_comes_from_the_centroid_not_from_the_agent"],
    "nodata excesivo": [
        "tests/test_asserts.py::test_nodata_over_threshold_rejected", "tests/test_reproject.py::test_mostly_nodata_dem_rejected",
        "tests/test_asserts.py::test_all_nodata_not_plausible"],
    "rango de elevacion implausible": [
        "tests/test_asserts.py::test_implausible_elevation_rejected", "tests/test_reproject.py::test_implausible_dem_values_rejected"],
    "nunca produce informe tras un rechazo": [
        "tests/test_pipeline.py::test_pipeline_rejects_instead_of_guessing",
        "tests/test_pipeline.py::test_rejection_removes_stale_results_of_previous_run",
        "tests/test_pipeline.py::test_base_fetch_reject_propagates",
        "tests/test_agent_tools.py::test_run_agent_rejection_outcome_leaves_no_report_files"],
}


@dataclass
class Criterion:
    n: int
    title: str
    status: str
    evidence: list = field(default_factory=list)


@dataclass
class Ctx:
    out: Path = ROOT / "out"
    out2: Path = ROOT / "out_run2"
    licenses: Path = ROOT.parent / "LICENCIAS"
    metrics: dict | None = None
    keys: tuple = ()
    pytest_runner: object = None  # callable(list[str]) -> dict[id] = "passed" | "failed" | ...
    ffprobe_duration: object = None  # callable(Path) -> float | None
    pdf_text: object = None  # callable(Path) -> str


# ------------------------------------------------------------------ utilidades

def _f(x, nd=2, unit="") -> str:
    return "n/d" if x is None else f"{x:.{nd}f}{unit}"


def default_keys() -> tuple:
    from terreno.geometry import SITES
    return tuple(SITES)


def load_metrics(out: Path) -> dict | None:
    p = Path(out) / "metrics.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def parse_junit(xml: Path) -> dict:
    """{'tests/x.py::nombre': passed|failed|skipped}. Las variantes parametrizadas (nombre[p]) se agrupan: pasa si pasan todas."""
    rank = {"passed": 0, "skipped": 1, "failed": 2}
    out: dict = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        name = re.sub(r"\[.*\]$", "", case.get("name", ""))
        nid = f"{case.get('classname', '').replace('.', '/')}.py::{name}"
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed"
            elif child.tag == "skipped" and state == "passed":
                state = "skipped"
        if nid not in out or rank[state] > rank[out[nid]]:
            out[nid] = state
    return out


def run_pytest(ids: list[str]) -> dict:
    """Corre las pruebas nombradas y devuelve {id: estado} leyendo el XML de JUnit (no el texto de la consola)."""
    with tempfile.TemporaryDirectory(prefix="acc_") as tmp:
        xml = Path(tmp) / "r.xml"
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *ids],
                              cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        out: dict = parse_junit(xml) if xml.exists() else {}
        out["__returncode__"] = proc.returncode
        out["__tail__"] = (proc.stdout or proc.stderr)[-400:]
    return out


def ffprobe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe or not Path(path).exists():
        return None
    r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def pdf_text(path: Path) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    r = subprocess.run([exe, "-enc", "UTF-8", str(path), "-"], capture_output=True)
    return re.sub(r"\s+", " ", r.stdout.decode("utf-8", "replace"))


def _sites(ctx: Ctx) -> list[dict]:
    by = {r["site"]: r for r in (ctx.metrics or {}).get("sites", [])}
    return [by.get(k) or {"site": k, "run1": {"status": "sin_corrida"}, "run2": {"status": "sin_corrida"}} for k in ctx.keys]


def _ok_runs(ctx: Ctx) -> list[dict]:
    return [r["run1"] | {"site": r["site"]} for r in _sites(ctx) if r["run1"].get("status") == "ok"]


# ------------------------------------------------------------------ criterios

def check_1(ctx: Ctx) -> Criterion:
    from terreno.manifest import hash_manifest
    c = Criterion(1, "Reproducible de punta a punta: polígono -> PDF + GLB + visor + video, 8 sitios RM, mismo hash en 2 corridas", PASS)
    if ctx.metrics is None:
        c.status = FAIL
        c.evidence.append(f"no existe {ctx.out / 'metrics.json'}: correr `python run_pilots.py` (y `--video`)")
        return c
    for row in _sites(ctx):
        k = row["site"]
        r1, r2 = row["run1"], row["run2"]
        problems = []
        if r1.get("status") != "ok" or r2.get("status") != "ok":
            problems.append(f"corridas: {r1.get('status')}/{r2.get('status')} ({r1.get('reason') or r2.get('reason') or ''})")
            c.status = FAIL
            c.evidence.append(f"{k}: " + "; ".join(problems))
            continue
        m1, m2 = ctx.out / k / "manifest.json", ctx.out2 / k / "manifest.json"
        if not (m1.exists() and m2.exists()):
            problems.append("falta manifest.json de alguna corrida")
        else:
            a, b = json.loads(m1.read_text(encoding="utf-8")), json.loads(m2.read_text(encoding="utf-8"))
            if not (a["result_hash"] == b["result_hash"] == hash_manifest(a) == hash_manifest(b)):
                problems.append(f"hash distinto entre corridas o manifiesto alterado ({a['result_hash'][:12]} vs {b['result_hash'][:12]})")
        missing = [d for d in DELIVERABLES if not (ctx.out / k / d).exists()]
        if missing:
            problems.append(f"faltan entregables: {missing}")
        dur = ctx.ffprobe_duration(ctx.out / k / "video.mp4")
        if dur is not None and not (30.0 <= dur <= 60.0):
            problems.append(f"video de {dur:.1f} s fuera de 30-60 s")
        render = ctx.out / k / "video.render.json"
        if render.exists():
            probs = json.loads(render.read_text(encoding="utf-8")).get("problems") or []
            if probs:
                problems.append(f"errores de consola/visor durante la captura: {probs[:2]}")
        if problems:
            c.status = FAIL
            c.evidence.append(f"{k}: " + "; ".join(problems))
        else:
            c.evidence.append(f"{k}: hash {a['result_hash'][:16]}... igual en out/ y out_run2/ (recalculado desde el manifiesto); "
                              f"PDF, GLB, predio.geojson, visor publicado y video de {_f(dur, 1)} s presentes; sin errores de visor")
    return c


def check_2(ctx: Ctx) -> Criterion:
    c = Criterion(2, "Cero errores silenciosos de CRS: entradas malas fallan o se rechazan, nunca producen informe", PASS)
    ids = [t for group in REJECTION_TESTS.values() for t in group]
    res = ctx.pytest_runner(ids)
    for label, group in REJECTION_TESTS.items():
        states = {t: res.get(t, "no_encontrada") for t in group}
        bad = {t.split("::")[1]: s for t, s in states.items() if s != "passed"}
        if bad:
            c.status = FAIL
        c.evidence.append(f"{label}: {sum(s == 'passed' for s in states.values())}/{len(group)} pruebas pasan"
                          + (f"  FALLAN/NO ENCONTRADAS: {bad}" if bad else ""))
    if res.get("__returncode__") not in (0, None) and c.status == PASS:
        c.status = FAIL
        c.evidence.append(f"pytest devolvio {res.get('__returncode__')}: {res.get('__tail__', '')}")
    return c


def check_3(ctx: Ctx) -> Criterion:
    c = Criterion(3, "Desacuerdo GLO-30 vs AW3D30 reportado en cada sitio (cifra + mapa), consistente con las especificaciones", PASS)
    rows = _sites(ctx)
    blocked = [r["site"] for r in rows if (r["run1"].get("aw3d30") or {}).get("status") == BLOCKED]
    if rows and len(blocked) == len(rows):
        c.status = BLOCKED
        detail = rows[0]["run1"]["aw3d30"].get("detail", "")
        c.evidence += [f"BLOQUEADO_CREDENCIALES: {detail}",
                       "El codigo (terreno/compare.py, 4 pruebas offline + datum EGM96->EGM2008 verificado) esta listo; "
                       "falta correr `python run_pilots.py --live` con la clave.",
                       "Sin AW3D30, el factor de desacuerdo queda sin_dato y limita todo informe a B como maximo."]
        return c
    for r in rows:
        aw = r["run1"].get("aw3d30") or {}
        if aw.get("status") != "ejecutado":
            c.status = FAIL
            c.evidence.append(f"{r['site']}: AW3D30 {aw.get('status')} {aw.get('detail', '')}")
            continue
        diff_map = list((ctx.out / r["site"] / "work").glob("diff_*.tif"))
        rms = aw["rms_diff_m"]
        problems = []
        if not diff_map:
            problems.append("falta el mapa de diferencia")
        if not aw.get("datum_aligned"):
            problems.append("datum no alineado (EGM96->EGM2008 no se pudo aplicar)")
        if not (0.0 <= rms <= RMS_MAX_M):
            problems.append(f"RMS {rms:.2f} m fuera del orden de magnitud esperado (<= {RMS_MAX_M:g} m): investigar datums")
        if problems:
            c.status = FAIL
        c.evidence.append(f"{r['site']}: RMS {_f(rms)} m, media {_f(aw.get('mean_diff_m'))} m, dif. pendiente {_f(aw.get('slope_diff_mean_pct'))} pp, "
                          f"alcance {aw.get('stats_scope')}, mapa {'si' if diff_map else 'NO'}" + (f"  PROBLEMAS: {problems}" if problems else ""))
    return c


def check_4(ctx: Ctx) -> Criterion:
    c = Criterion(4, "Comparacion contra ICESat-2/GEDI en cada sitio (n huellas, sesgo, MAE, RMSE de GLO-30 y AW3D30)", PASS)
    rows = _sites(ctx)
    blocked = [r["site"] for r in rows if (r["run1"].get("truth") or {}).get("status") == BLOCKED]
    if rows and len(blocked) == len(rows):
        c.status = BLOCKED
        c.evidence += [f"BLOQUEADO_CREDENCIALES: {rows[0]['run1']['truth'].get('detail', '')}",
                       "El codigo (terreno/truth.py: busqueda CMR, lectura ATL08/GEDI, geoide asertado, validate con minimo 10 huellas) esta probado "
                       "offline con archivos HDF5 sinteticos; la lectura real de Earthdata sigue SIN VERIFICAR.",
                       "Levantamientos propios: ninguno (solo datos publicos); sites.json marca elevacion_medida = null en los ocho pilotos RM."]
        return c
    for r in rows:
        tr = r["run1"].get("truth") or {}
        if tr.get("status") not in ("ok", "validacion_insuficiente"):
            c.status = FAIL
            c.evidence.append(f"{r['site']}: validacion {tr.get('status')} {tr.get('detail', '')}")
            continue
        aw = tr.get("aw3d30") or {}
        if tr["status"] == "validacion_insuficiente":
            c.evidence.append(f"{r['site']}: {tr.get('n_footprints')} huellas (< 10 utilizables) -> validacion insuficiente; no se calcula nada")
        else:
            c.evidence.append(f"{r['site']}: n={tr['n']}  GLO-30 sesgo {_f(tr['bias_m'])} MAE {_f(tr['mae_m'])} RMSE {_f(tr['rmse_m'])} m;  "
                              f"AW3D30 " + (f"n={aw.get('n')} sesgo {_f(aw.get('bias_m'))} MAE {_f(aw.get('mae_m'))} RMSE {_f(aw.get('rmse_m'))} m"
                                            if aw.get("status") == "ok" else str(aw.get("status")) ))
    c.evidence.append("Levantamientos propios: ninguno obtenido todavia (parte 'si llega' de la spec, pendiente).")
    return c


def check_5(ctx: Ctx) -> Criterion:
    from terreno.bias_correct import Tier2Result, tier2_verdict
    c = Criterion(5, "Veredicto Tier 2: la correccion de sesgo baja >= 10 % el RMSE en >= 3 sitios de 8 (huellas de prueba separadas; umbral heredado de 3 de 5, a recalibrar)", PASS)
    rows = _sites(ctx)
    t2s = [(r["site"], r["run1"].get("tier2") or {}) for r in rows]
    if rows and all(t.get("status") == BLOCKED for _, t in t2s):
        c.status = BLOCKED
        c.evidence += [f"BLOQUEADO_CREDENCIALES: {t2s[0][1].get('detail', '')}",
                       "terreno/bias_correct.py (bias_correct, split entrenamiento/prueba con >= 10 de cada, tier2_verdict) esta implementado y probado "
                       "con huellas sinteticas; el veredicto real necesita huellas de los 8 sitios.",
                       "Mientras tanto: Tier 2 NO se promueve; el producto queda en Tier 1 con los limites documentados."]
        return c
    results = []
    for k, t in t2s:
        if t.get("status") in ("ok", "validacion_insuficiente"):
            fields = {f: (t.get(f) or 0) if f.startswith("n_") else t.get(f) for f in Tier2Result.__dataclass_fields__ if f != "correction_model"}
            results.append(Tier2Result(**fields))
            c.evidence.append(f"{k}: " + (f"entrenamiento {t['n_train']} / prueba {t['n_test']} ({t['split_method']}); RMSE crudo {_f(t['rmse_raw_m'])} "
                                          f"-> corregido {_f(t['rmse_corrected_m'])} m ({_f(t['reduction_pct'], 1)} %)"
                                          if t["status"] == "ok" else f"validacion insuficiente ({t.get('n_usable')} huellas utilizables, se necesitan 20)"))
        else:
            results.append(None)
            c.evidence.append(f"{k}: {t.get('status')} {t.get('detail', '')}")
    v = tier2_verdict(results)
    c.evidence.append(f"VEREDICTO: {v['verdict']} ({v['n_sites_passing']} sitios con reduccion >= {v['min_reduction_pct']:g} % de {v['n_sites_total']}; "
                      f"{v['n_sites_unevaluated']} sin evaluar)")
    if v["verdict"] == "validacion_insuficiente":
        c.status = FAIL
        c.evidence.append("No hay veredicto: faltan huellas en sitios que todavia podrian cambiar el resultado.")
    elif v["verdict"] == "no_promovido":
        c.evidence.append("No se promueve: queda documentado como limite (la spec lo cuenta como criterio cumplido).")
    return c


def check_6(ctx: Ctx) -> Criterion:
    from terreno.ranking import spearman
    c = Criterion(6, "Regla A/B/C evaluada: el sello de confianza del dato ordena el error (A<=B<=C) y el ranking de pendiente coincide con la referencia", PASS)
    runs = _ok_runs(ctx)
    levels = {k: [r["site"] for r in runs if r.get("level") == k] for k in "ABC"}
    c.evidence.append("Niveles asignados: " + "; ".join(f"{k}: {len(v)} {v}" for k, v in levels.items()))
    sd = [r["site"] for r in runs if (r.get("factors") or {}).get("canopy_pct") == "sin_dato"]
    if sd:
        c.evidence.append(f"Factor dosel/construido = sin_dato en {len(sd)}/{len(runs)} sitios -> ningun sitio puede ser A; "
                          "la comparacion 'error en C > error en A' no se puede hacer hasta incorporar una capa de dosel.")
    lim = {}
    for r in runs:
        for f in r.get("limiting_factors") or []:
            lim[f] = lim.get(f, 0) + 1
    c.evidence.append(f"Factores que fijan el nivel (conteo): {lim}")
    seal_lv = {r["site"]: (r.get("seals") or {}).get("confianza_dato") for r in runs}
    use_seal = all(seal_lv.get(r["site"]) for r in runs) and bool(runs)
    if use_seal:
        c.evidence.append("Sellos (v0.3.0). Medibilidad del lote: " + "; ".join(
            f"{r['site']} {(r.get('seals') or {}).get('medibilidad_lote')}" for r in runs))
        c.evidence.append("Confianza del dato: " + "; ".join(f"{k} {v}" for k, v in seal_lv.items()))
    truth = [(r["site"], seal_lv[r["site"]] if use_seal else r.get("level"), r.get("truth") or {}) for r in runs]
    have = [(k, lv, t) for k, lv, t in truth if t.get("status") == "ok"]
    if not have:
        c.status = BLOCKED
        det = next((t.get("detail") for _, _, t in truth if t.get("detail")), "sin huellas utilizables")
        c.evidence.append(f"BLOQUEADO_CREDENCIALES: {det}")
        c.evidence.append("Sin error medido por sitio no se puede decir si los niveles discriminan; el ranking de pendiente contra la referencia "
                          "tampoco (la referencia es el plano ajustado a las huellas).")
        return c
    by_level: dict = {}
    for k, lv, t in have:
        by_level.setdefault(lv, []).append(t["rmse_m"])
    means = {lv: sum(v) / len(v) for lv, v in by_level.items()}
    if use_seal:  # informativo: el nivel final mezcla las dos preguntas, por eso ya no es el que se evalua
        all_lv: dict = {}
        for r in runs:
            t = r.get("truth") or {}
            if t.get("status") == "ok":
                all_lv.setdefault(r.get("level"), []).append(t["rmse_m"])
        c.evidence.append("Referencia (nivel final, mezcla ambas preguntas): " + "; ".join(
            f"{lv}: {sum(v) / len(v):.2f} m (n={len(v)})" for lv, v in sorted(all_lv.items())))
    c.evidence.append(("RMSE medio vs huellas por sello Confianza del dato: " if use_seal else "RMSE medio vs huellas por nivel: ") + "; ".join(f"{lv}: {m:.2f} m (n={len(by_level[lv])})" for lv, m in sorted(means.items())))
    order = [lv for lv in "ABC" if lv in means]
    discrimina = len(order) >= 2 and all(means[a] <= means[b] for a, b in zip(order, order[1:])) and means[order[-1]] > means[order[0]]
    c.evidence.append("Discrimina (error creciente A<=B<=C)" if discrimina else
                      "NO discrimina (o hay un solo nivel representado): documentar y cambiar los factores, no el criterio")
    ref = [(k, t["reference_slope"]) for k, _, t in have if (t.get("reference_slope") or {}).get("truth_pct") is not None
           and t["reference_slope"].get("context_pct") is not None]
    rho = spearman([r["context_pct"] for _, r in ref], [r["truth_pct"] for _, r in ref]) if len(ref) >= 3 else None
    c.evidence.append("Ranking de pendiente (pendiente media de GLO-30 en el entorno de las huellas vs plano ajustado solo a las "
                      "alturas de las huellas): " +
                      (f"Spearman = {rho:.2f} con {len(ref)} sitios (umbral propuesto {SPEARMAN_MIN})" if rho is not None
                       else f"no evaluable ({len(ref)} sitios con pendiente de referencia; se necesitan >= 3)"))
    if not discrimina or rho is None or rho < SPEARMAN_MIN:
        c.status = FAIL
        c.evidence.append("Criterio no demostrado: debe quedar documentado en VALIDACION-TECNICA.md que la regla no discrimina / el ranking no coincide.")
    return c


def check_7(ctx: Ctx) -> Criterion:
    c = Criterion(7, "Informe legible por un no experto (3-5 lectores ajenos responden 3 preguntas)", MANUAL)
    pdfs = [(r["site"], ctx.out / r["site"] / "report.pdf", r["run1"].get("pdf_pages")) for r in _sites(ctx)]
    listos = ", ".join(f"{k} ({p} pags)" for k, path, p in pdfs if path.exists())
    c.evidence.append("Informes listos para entregar: " + (listos or "ninguno"))
    c.evidence.append("Protocolo: cada lector recibe UN informe y contesta (1) ¿es plano o empinado?, (2) ¿qué tan seguro es?, (3) ¿qué no cubre? "
                      "Se anota si la respuesta coincide con lo que dice el informe (pendiente/ nivel A-B-C / lista 'no cubre').")
    c.evidence.append("FALTA: 3-5 lectores ajenos al proyecto; 0 respuestas registradas. El informe sigue en borrador legal: no mostrarlo a terceros "
                      "sin la revision del abogado (criterio 8).")
    return c


def check_8(ctx: Ctx) -> Criterion:
    from terreno import texts_es as T
    c = Criterion(8, "Licencias: textos de GLO-30 vigente, AW3D30 y GEDTM30 leídos y archivados; atribuciones correctas en el informe", MANUAL)
    lic = ctx.licenses
    names = sorted(p.name for p in lic.glob("*")) if lic.exists() else []
    groups = {"GLO-30": "GLO30_", "AW3D30 (descripcion + pagina)": "AW3D30_", "GEDTM30": "GEDTM30_", "ATL08": "ATL08_",
              "GEDI": "GEDI_", "NASA EOSDIS (ATL08/GEDI)": "EOSDIS_"}
    for label, prefix in groups.items():
        found = [n for n in names if n.startswith(prefix)]
        c.evidence.append(f"{label}: " + (f"{len(found)} archivo(s) archivado(s) ({found[0]}...)" if found else "NO ARCHIVADO"))
    c.evidence.append("FALTA (manual): leer los 'Terms for Use' de JAXA (https://earth.jaxa.jp/policy/en.html; la pagina es JavaScript y no se pudo "
                      "archivar como texto) y guardarlos con fecha en LICENCIAS/.")
    c.evidence.append("Archivado pero SIN lectura legal: GEDTM30 trae licencia CC BY 4.0 en los metadatos Y la frase 'Use for testing purposes only' "
                      "en su aviso; el POC no usa GEDTM30. GLO-30: dos textos de licencia con formulas de atribucion distintas (2010-2014 vs "
                      "'year of production'); el informe usa la formula de la spec (COPDEM-30).")
    need = [" ".join(T.ATRIBUCION_GLO30.split()), " ".join(T.NO_RESPONSABILIDAD_GLO30.split())] if hasattr(T, "ATRIBUCION_GLO30") else []
    if need:
        for r in _sites(ctx):
            pdf = ctx.out / r["site"] / "report.pdf"
            if not pdf.exists():
                continue
            text = ctx.pdf_text(pdf)
            ok = all(" ".join(n.split()) in text for n in need)
            c.evidence.append(f"{r['site']}: atribucion y frase de no responsabilidad de GLO-30 {'presentes' if ok else 'AUSENTES'} en report.pdf (texto extraido)")
            if not ok:
                c.status = FAIL
    c.evidence.append("Pendiente (manual): abogado revisa el texto legal del informe antes de mostrarlo a terceros.")
    return c


def check_9(ctx: Ctx) -> Criterion:
    c = Criterion(9, "Tiempo y computo reales por informe, incluido el video", PASS)
    machine = (ctx.metrics or {}).get("machine") or {}
    c.evidence.append(f"Maquina: {machine.get('processor') or machine.get('machine')}, {machine.get('cpu_count')} hilos, {machine.get('ram_gb')} GB RAM, "
                      f"{machine.get('platform')}; WebGL del video: {machine.get('webgl_mode_video')}")
    rows = _sites(ctx)
    if ctx.metrics is None or not rows:
        c.status = FAIL
        c.evidence.append("sin metrics.json")
        return c
    for r in rows:
        r1 = r["run1"]
        if r1.get("status") != "ok":
            c.status = FAIL
            c.evidence.append(f"{r['site']}: sin corrida valida ({r1.get('status')})")
            continue
        s = r1.get("stages_s") or {}
        v = r.get("video") or {}
        missing = [n for n in ("fetch_dem", "compute_terrain", "build_glb", "render_pdf", "total") if s.get(n) is None]
        if v.get("status") != "ok":
            missing.append("video")
        if r1.get("peak_rss_mb") is None:
            missing.append("memoria")
        if missing:
            c.status = FAIL
        c.evidence.append(f"{r['site']}: pipeline+GLB+PDF+visor {_f(s.get('total'), 1)} s (corrida 2: {_f((r['run2'].get('stages_s') or {}).get('total'), 1)} s; "
                          f"GLB {_f(s.get('build_glb'), 1)} s, PDF {_f(s.get('render_pdf'), 1)} s, red/DEM {_f(s.get('fetch_dem'), 1)}+{_f(s.get('context'), 1)} s); "
                          f"memoria pico {_f(r1.get('peak_rss_mb'), 0)} MB; video {_f(v.get('wall_s'), 0)} s de pared ({v.get('frames')} fotogramas, {v.get('gl')}); "
                          f"GLB {_f((r1.get('glb_bytes') or 0) / 1024, 0)} KB, PDF {r1.get('pdf_pages')} pags / {_f((r1.get('pdf_bytes') or 0) / 1024, 0)} KB"
                          + (f"  FALTA MEDIR: {missing}" if missing else ""))
    sw = ctx.out / "video_software_gl.json"  # medicion aparte: mismo video con WebGL por software (sin GPU)
    if sw.exists():
        m = json.loads(sw.read_text(encoding="utf-8"))
        c.evidence.append(f"Video con WebGL por software (sin GPU), sitio {m.get('site')}: {_f(m.get('wall_s'), 0)} s de pared, {m.get('frames')} fotogramas, "
                          f"{m.get('gl')}, sin otra carga en la maquina")
    ok = [r for r in rows if r["run1"].get("status") == "ok"]
    if ok:
        tot = [r["run1"]["stages_s"].get("total") or 0 for r in ok]
        vid = [(r.get("video") or {}).get("wall_s") for r in ok if (r.get("video") or {}).get("wall_s")]
        c.evidence.append(f"Promedio por informe (sin video): {sum(tot) / len(tot):.1f} s"
                          + (f"; video: {sum(vid) / len(vid):.0f} s ({sum(vid) / len(vid) / 60:.1f} min) -> el video domina el costo" if vid else ""))
    return c


CHECKS = (check_1, check_2, check_3, check_4, check_5, check_6, check_7, check_8, check_9)


def evaluate(ctx: Ctx) -> list[Criterion]:
    ctx.keys = ctx.keys or default_keys()
    if ctx.metrics is None:
        ctx.metrics = load_metrics(ctx.out)
    ctx.pytest_runner = ctx.pytest_runner or run_pytest
    ctx.ffprobe_duration = ctx.ffprobe_duration or ffprobe_duration
    ctx.pdf_text = ctx.pdf_text or pdf_text
    return [fn(ctx) for fn in CHECKS]


def render_table(results: list[Criterion]) -> str:
    lines = ["CRITERIOS DE ACEPTACION DEL POC (SPEC-PRODUCTO-v0, sec. 8)", ""]
    for c in results:
        lines.append(f"[{c.status}] {c.n}. {c.title}")
        lines += [f"      - {e}" for e in c.evidence]
        lines.append("")
    counts = {s: sum(c.status == s for c in results) for s in (PASS, FAIL, MANUAL, BLOCKED)}
    lines.append("RESUMEN: " + "  ".join(f"{s}={n}" for s, n in counts.items()))
    lines.append("El POC pasa solo si los 9 criterios se cumplen; MANUAL y BLOQUEADO_CREDENCIALES son pendientes, no fallas.")
    return "\n".join(lines)


def exit_code(results: list[Criterion]) -> int:
    """Distinto de 0 solo si hay algun FAIL (BLOQUEADO_CREDENCIALES y MANUAL no son fallas)."""
    return 1 if any(c.status == FAIL for c in results) else 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    results = evaluate(Ctx())
    print(render_table(results))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
