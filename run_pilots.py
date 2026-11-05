"""Corrida piloto (Task 14): los ocho sitios RM de data/sites.json x 2 corridas, con GLB, PDF y visor publicado.

    python run_pilots.py                 # 8 sitios x 2 corridas -> out/<sitio>/ y out_run2/<sitio>/, out/metrics.{json,csv}
    python run_pilots.py --video         # ademas, el video de cada sitio (secuencial, un Chromium a la vez)
    python run_pilots.py --skip-runs --video   # solo los videos, sobre las carpetas ya generadas
    python run_pilots.py --live          # ademas AW3D30 (OpenTopography) e ICESat-2/GEDI (Earthdata) + Tier 2

Credenciales: ``OPENTOPOGRAPHY_API_KEY`` (env) y Earthdata (``~/_netrc`` o ``EARTHDATA_USERNAME``/``EARTHDATA_PASSWORD``). Este
script solo averigua SI existen; nunca imprime, copia ni guarda su valor. Sin ellas, los pasos que las necesitan quedan
``BLOQUEADO_CREDENCIALES`` con el dato exacto que falta, y todo lo demas se ejecuta igual.

Cada corrida se hace en un subproceso propio, asi la memoria pico y el tiempo de cada una son independientes (la memoria
medida es la del proceso Python; los procesos GDAL y Chromium hijos no se suman).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from terreno.errors import RejectRequest  # noqa: E402
from terreno.geometry import SITES, load_polygon  # noqa: E402
from terreno.hooks import default_hooks  # noqa: E402
from terreno.pipeline import REJECTION_NAME, run_pipeline  # noqa: E402
from terreno.publish import publish_viewer  # noqa: E402
from terreno.reproject import Raster  # noqa: E402

OUT = ROOT / "out"
OUT2 = ROOT / "out_run2"
# Huellas ICESat-2/GEDI: insumo externo fijo (granulos publicados). Se comparte entre corridas y sitios; la clave de
# cache incluye URL del granulo, bbox y version de las reglas de calidad (truth.QUALITY_RULES_VERSION).
SHARED_TRUTH_CACHE = ROOT / "cache" / "truth_shared"
# AW3D30 tambien es un insumo externo fijo, y OpenTopography permite solo 50 llamadas/24 h: la cache vive fuera de out/
# (run_site borra out/<sitio> en cada corrida). Clave = bbox pedido (compare.fetch_aw3d30).
SHARED_AW3D30_CACHE = ROOT / "cache" / "aw3d30_shared"
METRICS_JSON, METRICS_CSV = "metrics.json", "metrics.csv"
FACTORS = ("pixels", "elev_range_m", "slope_mean_pct", "disagreement_rms_m", "canopy_pct", "void_water_pct", "hem_p90_m")
STAGE_COLUMNS = ("validar_poligono", "fetch_dem", "reproject", "compute_terrain", "solar", "fetch_aw3d30", "reproject_aw3d30",
                 "compare_sources", "aux_layers", "ground_truth", "validate", "score_confidence", "context", "build_glb",
                 "render_pdf", "publish_viewer", "total")
MISSING_OPENTOPO = "falta la variable de entorno OPENTOPOGRAPHY_API_KEY (cuenta gratuita en opentopography.org)"
MISSING_EARTHDATA = ("faltan credenciales de NASA Earthdata: archivo %USERPROFILE%\\_netrc con la linea 'machine urs.earthdata.nasa.gov' "
                     "o variables EARTHDATA_USERNAME y EARTHDATA_PASSWORD")
BLOCKED = "BLOQUEADO_CREDENCIALES"


# ------------------------------------------------------------------ entorno

def credential_state(env: dict | None = None, home: Path | None = None) -> dict:
    """Existencia (no valores) de las credenciales. Nunca devuelve ni imprime el contenido."""
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    opentopo = bool(env.get("OPENTOPOGRAPHY_API_KEY", "").strip())
    earthdata = bool(env.get("EARTHDATA_USERNAME", "").strip() and env.get("EARTHDATA_PASSWORD", "").strip())
    if not earthdata:
        candidates = [home / "_netrc"]
        if env.get("NETRC"):
            candidates.append(Path(env["NETRC"]))
        for p in candidates:
            try:
                if p.is_file() and "urs.earthdata.nasa.gov" in p.read_text(encoding="utf-8", errors="ignore"):
                    earthdata = True
                    break
            except OSError:
                pass
    return {"opentopography": opentopo, "earthdata": earthdata,
            "missing": {"opentopography": None if opentopo else MISSING_OPENTOPO,
                        "earthdata": None if earthdata else MISSING_EARTHDATA}}


def peak_rss_mb() -> float | None:
    """Memoria pico (working set) de ESTE proceso, en MB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return None
        return round(pmc.PeakWorkingSetSize / 1024 ** 2, 1)
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(v / 1024 / (1024 if sys.platform == "darwin" else 1), 1)
    except Exception:
        return None


def machine_info() -> dict:
    ram = None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEM()
        m.dwLength = ctypes.sizeof(MEM)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            ram = round(m.ullTotalPhys / 1024 ** 3, 1)
    return {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "ram_gb": ram, "python": platform.python_version(),
            "webgl_mode_video": os.environ.get("TERRENO_GL", "software")}


# ------------------------------------------------------------------ una corrida

def _pdf_pages(path: Path) -> int | None:
    exe = shutil.which("pdfinfo")
    if not exe or not Path(path).exists():
        return None
    out = subprocess.run([exe, str(path)], capture_output=True, text=True, errors="replace").stdout
    m = re.search(r"Pages:\s+(\d+)", out)
    return int(m.group(1)) if m else None


def _missing(creds: dict, which: str) -> str:
    return (creds.get("missing") or {}).get(which) or (MISSING_OPENTOPO if which == "opentopography" else MISSING_EARTHDATA)


def _aw3d30_block(manifest: dict, sources: tuple, live: bool, creds: dict) -> dict:
    if "aw3d30" in sources:  # solo ocurre con --live y la clave presente
        c = manifest["results"]["comparison"]
        if c.get("status") != "ok":
            return {"status": "no_disponible", "detail": c.get("reason", "")}
        return {"status": "ejecutado", "rms_diff_m": c["rms_diff_m"], "mean_diff_m": c["mean_diff_m"],
                "slope_diff_mean_pct": c["slope_diff_mean_pct"], "datum_aligned": c["datum_aligned"],
                "stats_scope": c["stats_scope"], "n_pixels": c.get("n_pixels"), "detail": ""}
    if not creds.get("opentopography"):
        return {"status": BLOCKED, "detail": _missing(creds, "opentopography")}
    return {"status": "no_solicitado", "detail": "no se pidio --live"}


def fetch_aw3d30_shared(poly, cache_dir, *, margin_m: float = 0.0):
    """AW3D30 del lote o del contexto en SHARED_AW3D30_CACHE (ignora la cache por corrida, que se borra)."""
    from terreno import compare
    return compare.fetch_aw3d30(poly, SHARED_AW3D30_CACHE, margin_m=margin_m)


def run_site(key: str, poly, out_dir: Path, *, live: bool = False, creds: dict | None = None, fetch: dict | None = None,
             fetch_aux=None, fetch_canopy=None, shift_fn=None, footprints_fn=None, now=None,
             max_granules: int | None = None) -> dict:
    """Una corrida completa de un sitio (pipeline + GLB + PDF + visor publicado) y sus metricas. Nunca lanza por un rechazo."""
    creds = creds if creds is not None else credential_state()
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)  # los entregables deben salir de ESTA corrida, no de una anterior
    out_dir.mkdir(parents=True)

    sources = ("glo30", "aw3d30") if (live and creds.get("opentopography")) else ("glo30",)
    use_truth = bool(live and creds.get("earthdata"))
    captured: list = []

    def ground_truth(poly_, cache):
        if footprints_fn is not None:
            fps = footprints_fn(poly_, cache)
        else:
            from terreno import truth
            fps = truth.fetch_footprints(poly_, 1000.0, SHARED_TRUTH_CACHE, max_granules=max_granules)
        captured.extend(fps)
        return fps

    stages: dict[str, float] = {}

    def timed(name, fn):
        def wrapper(manifest, out):
            t0 = time.perf_counter()
            try:
                return fn(manifest, out)
            finally:
                stages[name] = round(time.perf_counter() - t0, 3)
        return wrapper

    hooks = {name: timed(name, fn) for name, fn in {**default_hooks(), "publish_viewer": publish_viewer}.items()}
    kw = dict(sources=sources, hooks=hooks, now=now, ground_truth=ground_truth if use_truth else None)
    if fetch is not None:
        kw["fetch"] = fetch
    elif "aw3d30" in sources:
        kw["fetch"] = {"aw3d30": fetch_aw3d30_shared}
    if fetch_aux is not None:
        kw["fetch_aux"] = fetch_aux
    if fetch_canopy is not None:
        kw["fetch_canopy"] = fetch_canopy
    elif fetch is None:  # corrida real: dosel/construido desde ESA WorldCover (con DEM inyectados, sin red)
        from terreno.worldcover import fetch_canopy_stats
        kw["fetch_canopy"] = fetch_canopy_stats
    if shift_fn is not None:
        kw["shift_fn"] = shift_fn

    metrics: dict = {"site": key, "live": live, "sources": list(sources)}
    t0 = time.perf_counter()
    try:
        res = run_pipeline(poly, out_dir, cache_dir=out_dir / "cache", **kw)
    except RejectRequest as exc:
        stages["total"] = round(time.perf_counter() - t0, 3)
        rej = out_dir / REJECTION_NAME
        stage = json.loads(rej.read_text(encoding="utf-8")).get("stage") if rej.exists() else None
        metrics.update(status="rechazado", reason=exc.reason, stage=stage, result_hash=None, stages_s=stages,
                       peak_rss_mb=peak_rss_mb())
        return _finish(metrics, out_dir)
    except Exception as exc:  # noqa: BLE001 - un error inesperado se registra, no tumba a los otros sitios
        stages["total"] = round(time.perf_counter() - t0, 3)
        metrics.update(status="error", reason=f"{type(exc).__name__}: {exc}", stage=None, result_hash=None, stages_s=stages,
                       peak_rss_mb=peak_rss_mb())
        return _finish(metrics, out_dir)
    stages["total"] = round(time.perf_counter() - t0, 3)
    stages.update({k: v for k, v in res.manifest["volatile"]["elapsed_s"].items()})

    m = res.manifest
    t = m["results"]["terrain"]
    conf = res.confidence
    glb = out_dir / "terreno.glb"
    pdf = out_dir / "report.pdf"
    metrics.update(
        status="ok", reason=None, stage=None, result_hash=res.result_hash, level=conf.overall,
        human_review_required=bool(conf.human_review_required), omit_point_slope=bool(conf.omit_point_slope),
        factors={k: conf.by_factor.get(k) for k in FACTORS}, factor_values={k: conf.values.get(k) for k in FACTORS},
        limiting_factors=list(conf.limiting_factors), by_section=dict(conf.by_section), seals=dict(conf.by_seal),
        canopy_context_pct=(m["results"].get("worldcover") or {}).get("context_pct"),
        n_pixels=t["n_pixels"], inconclusive=bool(t.get("inconclusive")), elev_min=t.get("elev_min"),
        elev_max=t.get("elev_max"), elev_range_m=t.get("elev_range"), slope_mean_pct=t.get("slope_mean_pct"),
        stages_s=stages, peak_rss_mb=peak_rss_mb(),
        glb_bytes=glb.stat().st_size if glb.exists() else None, pdf_bytes=pdf.stat().st_size if pdf.exists() else None,
        pdf_pages=_pdf_pages(pdf), published=(out_dir / "publicado" / "index.html").exists(),
        context_status=m["volatile"].get("context", {}).get("status"),
        aw3d30=_aw3d30_block(m, sources, live, creds),
    )
    metrics["truth"], metrics["tier2"] = _truth_blocks(res, m, captured, live, creds, out_dir)
    return _finish(metrics, out_dir)


def _truth_blocks(res, manifest: dict, captured: list, live: bool, creds: dict, out_dir: Path) -> tuple[dict, dict]:
    if not creds.get("earthdata"):
        blocked = {"status": BLOCKED, "detail": _missing(creds, "earthdata")}
        return dict(blocked), dict(blocked)
    if not live:
        pending = {"status": "no_solicitado", "detail": "no se pidio --live"}
        return dict(pending), dict(pending)
    v = manifest["results"].get("ground_truth_validation") or {}
    truth = {"status": v.get("status", "no_disponible"), "n_footprints": v.get("n_footprints_total", len(captured)),
             "n": v.get("n"), "bias_m": v.get("bias_m"), "mae_m": v.get("mae_m"), "rmse_m": v.get("rmse_m"),
             "n_excluded_quality": v.get("n_excluded_quality"), "n_outside_raster": v.get("n_outside_raster"),
             "by_source": v.get("by_source"), "by_slope": v.get("by_slope"), "detail": v.get("reason", "")}
    # AUD-1 I1: GLO-30 y AW3D30 se puntuaron en el pipeline con la MISMA politica de raster (contexto) y el MISMO conjunto
    # de huellas; aqui solo se transcribe lo que quedo en el manifiesto.
    comp = res.comparison
    vb = v.get("aw3d30")
    if vb and vb.get("status") in ("ok", "validacion_insuficiente"):
        truth["aw3d30"] = {"status": vb["status"], "n": vb.get("n"), "bias_m": vb.get("bias_m"), "mae_m": vb.get("mae_m"),
                           "rmse_m": vb.get("rmse_m"), "datum_aligned": vb.get("datum_aligned"), "by_slope": vb.get("by_slope"),
                           "same_footprints_as_glo30": True}
    elif vb:
        truth["aw3d30"] = {"status": "no_disponible", "detail": vb.get("reason", "")}
    elif comp is None:
        truth["aw3d30"] = {"status": "no_disponible", "detail": "no hay comparacion AW3D30 en esta corrida"}
    else:
        truth["aw3d30"] = {"status": "no_disponible", "detail": "AW3D30 no se puntuo contra las huellas"}
    ref_raster = res.context_raster if res.context_raster is not None else res.raster  # misma politica que validate
    from terreno.manifest import report_fields
    from terreno.ranking import reference_slopes
    truth["reference_slope"] = reference_slopes(ref_raster, captured)  # criterio 6: huellas vs pendiente media de GLO-30
    pend = report_fields(manifest)["pendiente"]
    truth["reference_slope"]["report_lot_slope_pct"] = pend.get("media_pct") if pend.get("estado") == "ok" else None
    if captured:
        (out_dir / "footprints.json").write_text(
            json.dumps([asdict(f) for f in captured], ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    from terreno.bias_correct import evaluate_bias_correction
    return truth, asdict(evaluate_bias_correction(ref_raster, captured))


def _finish(metrics: dict, out_dir: Path) -> dict:
    (Path(out_dir) / "run_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metrics


# ------------------------------------------------------------------ resumen y archivos

def summarize(key: str, site: dict, run1: dict, run2: dict) -> dict:
    h1, h2 = run1.get("result_hash"), run2.get("result_hash")
    return {"site": key, "nombre": site.get("nombre"), "fuente_poligono": site.get("fuente_poligono") or site.get("fuente_polígono"),
            "hash_match": (h1 == h2) if (h1 and h2) else None, "run1": run1, "run2": run2}


def _flat(row: dict) -> dict:
    r1, r2 = row["run1"], row["run2"]
    flat = {"site": row["site"], "fuente_poligono": row.get("fuente_poligono"), "status": r1.get("status"),
            "status_run2": r2.get("status"), "reject_stage": r1.get("stage"), "reject_reason": r1.get("reason"),
            "level": r1.get("level"), "human_review": r1.get("human_review_required"),
            "n_pixels": r1.get("n_pixels"), "elev_range_m": r1.get("elev_range_m"), "slope_mean_pct": r1.get("slope_mean_pct"),
            "limiting_factors": ";".join(r1.get("limiting_factors") or []),
            "hash_run1": r1.get("result_hash"), "hash_run2": r2.get("result_hash"), "hash_match": row.get("hash_match")}
    for f in FACTORS:
        flat[f"factor_{f}"] = (r1.get("factors") or {}).get(f)
    for s in STAGE_COLUMNS:
        flat[f"t_{s}_s"] = (r1.get("stages_s") or {}).get(s)
    flat["t_total_run2_s"] = (r2.get("stages_s") or {}).get("total")
    flat["peak_rss_mb"] = r1.get("peak_rss_mb")
    flat["peak_rss_mb_run2"] = r2.get("peak_rss_mb")
    flat.update(glb_bytes=r1.get("glb_bytes"), pdf_pages=r1.get("pdf_pages"), pdf_bytes=r1.get("pdf_bytes"),
                published=r1.get("published"))
    aw, tr, t2 = r1.get("aw3d30") or {}, r1.get("truth") or {}, r1.get("tier2") or {}
    flat.update(aw3d30_status=aw.get("status"), aw3d30_rms_diff_m=aw.get("rms_diff_m"), aw3d30_mean_diff_m=aw.get("mean_diff_m"),
                aw3d30_detail=aw.get("detail"), truth_status=tr.get("status"), truth_n=tr.get("n_footprints"),
                truth_bias_m=tr.get("bias_m"), truth_mae_m=tr.get("mae_m"), truth_rmse_m=tr.get("rmse_m"),
                truth_detail=tr.get("detail"), tier2_status=t2.get("status"), tier2_n_train=t2.get("n_train"),
                tier2_n_test=t2.get("n_test"), tier2_rmse_raw_m=t2.get("rmse_raw_m"),
                tier2_rmse_corrected_m=t2.get("rmse_corrected_m"), tier2_reduction_pct=t2.get("reduction_pct"))
    v = row.get("video") or {}
    flat.update(video_status=v.get("status"), video_wall_s=v.get("wall_s"), video_frames=v.get("frames"),
                video_bytes=v.get("bytes"), video_seconds=v.get("seconds"), video_gl=v.get("gl"))
    return flat


def write_metrics(rows: list[dict], out_dir: Path, *, machine: dict, credentials: dict) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    creds = {k: credentials.get(k) for k in ("opentopography", "earthdata")}
    jpath = out_dir / METRICS_JSON
    jpath.write_text(json.dumps({"machine": machine, "credentials": creds, "sites": rows}, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    flats = [_flat(r) for r in rows]
    cols: list[str] = []
    for f in flats:
        cols.extend(c for c in f if c not in cols)
    cpath = out_dir / METRICS_CSV
    with cpath.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(flats)
    return jpath, cpath


# ------------------------------------------------------------------ video

def viewer_site_url(base: str, out_dir: Path) -> str:
    """URL del visor (servido desde la raiz de poc/) para una carpeta de salida real, sin datos personales."""
    rel = "/" + Path(out_dir).resolve().relative_to(ROOT).as_posix()
    return f"{base.rstrip('/')}/viewer/index.html?site={urllib.parse.quote(rel, safe='/-_.')}"


def render_site_video(out_dir: Path, *, seconds: int = 45, fps: int = 12) -> dict:
    """Renderiza out_dir/video.mp4 con el visor sobre los archivos reales del sitio; mide el tiempo de pared."""
    from terreno.video import render_video, serve_viewer
    out_dir = Path(out_dir)
    target = out_dir / "video.mp4"
    t0 = time.perf_counter()
    try:
        with serve_viewer(root=ROOT) as base:
            render_video(viewer_site_url(base, out_dir), target, seconds, fps=fps)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "wall_s": round(time.perf_counter() - t0, 1)}
    wall = round(time.perf_counter() - t0, 1)
    stats = json.loads(target.with_suffix(".render.json").read_text(encoding="utf-8"))
    return {"status": "ok", "wall_s": wall, "seconds": seconds, "fps": fps, "frames": stats.get("frames"), "gl": stats.get("gl"),
            "load_s": round(stats.get("load_ms", 0) / 1000, 1), "capture_s": round(stats.get("capture_ms", 0) / 1000, 1),
            "bytes": target.stat().st_size, "problems": stats.get("problems", [])}


# ------------------------------------------------------------------ CLI

def _spawn_worker(key: str, out_dir: Path, live: bool, max_granules: int | None) -> dict:
    result = out_dir.parent / f".{key}_{out_dir.parent.name}_run.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", key, str(out_dir), str(result)]
    if live:
        cmd.append("--live")
    if max_granules:
        cmd += ["--max-granules", str(max_granules)]
    proc = subprocess.run(cmd, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not result.exists():
        return {"site": key, "status": "error", "reason": f"worker fallo ({proc.returncode}): {(proc.stderr or proc.stdout)[-600:]}",
                "stage": None, "result_hash": None, "stages_s": {}}
    data = json.loads(result.read_text(encoding="utf-8"))
    result.unlink()
    return data


def _worker(key: str, out_dir: str, result: str, live: bool, max_granules: int | None) -> int:
    poly = load_polygon(SITES[key]["polygon"])
    m = run_site(key, poly, Path(out_dir), live=live, max_granules=max_granules)
    Path(result).write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="AW3D30 + ICESat-2/GEDI + Tier 2 (necesita credenciales)")
    ap.add_argument("--video", action="store_true", help="renderizar el video de cada sitio (lento; secuencial)")
    ap.add_argument("--skip-runs", action="store_true", help="no correr el pipeline; usar out/metrics.json existente")
    ap.add_argument("--sites", default="", help="lista separada por comas (por defecto, los ocho)")
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--max-granules", type=int, default=None, help="tope de granulos ATL08/GEDI por producto (prueba)")
    ap.add_argument("--worker", nargs=3, metavar=("SITE", "OUT_DIR", "RESULT_JSON"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.worker:
        return _worker(a.worker[0], a.worker[1], a.worker[2], a.live, a.max_granules)

    keys = [k for k in (a.sites.split(",") if a.sites else list(SITES)) if k]
    unknown = [k for k in keys if k not in SITES]
    if unknown:
        print(f"sitio desconocido: {unknown}; validos: {list(SITES)}")
        return 2
    creds = credential_state()
    print(f"Credenciales: OpenTopography={'si' if creds['opentopography'] else 'NO'} "
          f"Earthdata={'si' if creds['earthdata'] else 'NO'}  modo={'--live' if a.live else 'sin --live'}")
    if a.live:
        for k, v in creds["missing"].items():
            if v:
                print(f"  {BLOCKED} ({k}): {v}")

    mpath = OUT / METRICS_JSON
    rows: list[dict] = []
    if a.skip_runs:
        rows = json.loads(mpath.read_text(encoding="utf-8"))["sites"]
    else:
        for k in keys:
            runs = []
            for n, base in ((1, OUT), (2, OUT2)):
                t0 = time.perf_counter()
                m = _spawn_worker(k, base / k, a.live, a.max_granules)
                runs.append(m)
                print(f"[{k}] corrida {n}: {m['status']} nivel={m.get('level')} hash={str(m.get('result_hash'))[:12]} "
                      f"{time.perf_counter() - t0:.1f}s" + (f"  ({m.get('reason')})" if m['status'] != 'ok' else ""), flush=True)
            rows.append(summarize(k, SITES[k], runs[0], runs[1]))
            print(f"[{k}] hashes iguales: {rows[-1]['hash_match']}", flush=True)
        write_metrics(rows, OUT, machine=machine_info(), credentials=creds)

    if a.video:
        by_key = {r["site"]: r for r in rows}
        for k in keys:
            if k not in by_key or by_key[k]["run1"].get("status") != "ok":
                print(f"[{k}] sin video: el sitio no tiene resultado")
                continue
            print(f"[{k}] renderizando video ({a.seconds}s x {a.fps} fps, GL={os.environ.get('TERRENO_GL', 'software')}) ...", flush=True)
            by_key[k]["video"] = render_site_video(OUT / k, seconds=a.seconds, fps=a.fps)
            v = by_key[k]["video"]
            print(f"[{k}] video: {v['status']} {v.get('wall_s')}s " + (v.get("reason") or ""), flush=True)
            write_metrics(rows, OUT, machine=machine_info(), credentials=creds)  # se guarda tras cada video
    bad = [r["site"] for r in rows if r["run1"].get("status") == "error" or r.get("hash_match") is False]
    print(f"Listo. Metricas: {OUT / METRICS_CSV}" + (f"  PROBLEMAS: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
