"""run_pipeline: poligono -> fetch -> reproject -> terrain -> (comparacion) -> confianza -> manifiesto.

Determinista: dos corridas con la misma entrada dan el mismo ``result_hash`` (las marcas de tiempo, la fecha de
descarga, las rutas y los tiempos viven en ``manifest["volatile"]`` y quedan fuera del hash).
Nada se adivina: si falla la fuente base o una asercion, se registra ``rejection.json`` y se relanza RejectRequest.
Ganchos para las tareas siguientes (todavia no implementadas): ``hooks`` con las claves de ``HOOK_ORDER``.
"""
from __future__ import annotations

import inspect
import json
import math
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import rasterio
from pyproj import Geod
from shapely.geometry import Polygon, mapping

from terreno.compare import ComparisonResult, compare_sources, fetch_aw3d30
from terreno.confidence import Confidence, factors_from_results, score_confidence
from terreno.errors import RejectRequest
from terreno.fetch import DemResult, fetch_dem
from terreno.geometry import centroid_zone, load_polygon
from terreno.layers import AUX_KINDS, AuxStats, fetch_aux_stats
from terreno.manifest import (
    MANIFEST_NAME,
    build_manifest,
    dataset_entry,
    raster_digest,
    result_hash,
    write_manifest,
)
from terreno.reproject import PIXEL_M, Raster, reproject
from terreno.solar import compute_solar
from terreno.worldcover import CanopyStats
from terreno.terrain import RELIEF_FORMULA, TerrainResult, compute_terrain
from terreno.truth import ValidationResult, usable_ids, validate

HOOK_ORDER = ("build_glb", "render_pdf", "publish_viewer", "render_video")  # Tasks 9-12; hooks(manifest, out_dir) -> Path|None
REJECTION_NAME = "rejection.json"
DEFAULT_SOURCES = ("glo30", "aw3d30")
DEFAULT_CONTEXT_M = 1500.0  # ventana de contexto del GLB/visor: bbox del predio +- 1500 m (solo vitrina, no entra al hash)
ARCSEC_M = 30.87  # metros de un arcseg de latitud (el de longitud es esto x cos(lat))
HYPOTHESES = [
    "regla de confianza A/B/C y sus umbrales (spec sec. 4)",
    "agrupacion de factores por seccion del informe",
    "puntaje de complejidad del relieve 1-7 (" + RELIEF_FORMULA + ")",
    "cortes de clases de pendiente <5, 5-15, 15-30, >30 %",
    "umbral de 'sin orientacion predominante' < 3 %",
    "equidistancia de curvas de 10 m",
    "horizonte y horas de sol directo solo con el relieve de la ventana de trabajo (sec. 3 #7)",
]


@dataclass
class RunResult:
    out_dir: Path
    manifest_path: Path
    manifest: dict
    result_hash: str
    confidence: Confidence
    terrain: TerrainResult
    raster: Raster
    comparison: ComparisonResult | None = None
    comparison_unavailable_reason: str | None = None
    aux: AuxStats | None = None
    validation: ValidationResult | None = None
    outputs: dict = field(default_factory=dict)
    context_raster: Raster | None = None  # ventana de contexto (+-1500 m): raster de referencia de toda verdad de terreno


def _reject(out_dir: Path, stage: str, exc: RejectRequest) -> None:
    (Path(out_dir) / REJECTION_NAME).write_text(
        json.dumps({"rejected": True, "stage": stage, "reason": exc.reason}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def _context_raster(fetcher: Callable, poly: Polygon, cache: Path, context_m: float) -> tuple[Raster, dict]:
    """DEM y raster UTM de la ventana de contexto (bbox del predio +- ``context_m``), aparte del raster de trabajo.

    Solo alimenta la malla/capas de la vitrina (GLB, visor, video): las cifras del informe salen del raster de trabajo, de
    modo que el hash de resultados no cambia. Usa una carpeta propia (los DEM del fetch se llaman igual) y copia tileList.txt.
    """
    ctx_cache = cache / "context"
    ctx_cache.mkdir(parents=True, exist_ok=True)
    tile_list = cache / "tileList.txt"
    if tile_list.exists() and not (ctx_cache / "tileList.txt").exists():
        shutil.copy2(tile_list, ctx_cache / "tileList.txt")
    lat = abs(poly.centroid.y)
    fetch_margin = math.ceil(context_m / (ARCSEC_M * math.cos(math.radians(lat)))) + 4  # pixeles de 1 arcseg
    kwargs = {"margin_px": fetch_margin} if "margin_px" in inspect.signature(fetcher).parameters else {}
    dem = fetcher(poly, ctx_cache, **kwargs)
    margin_px = math.ceil(context_m / PIXEL_M)
    raster = reproject(dem, poly, margin_px=margin_px)
    with rasterio.open(raster.path) as r:
        shape = [r.height, r.width]
    return raster, {"status": "ok", "requested_m": context_m, "margin_px": margin_px, "fetch_margin_px": fetch_margin,
                    "shape_rows_cols": shape, "fetch_accepts_margin": bool(kwargs)}


def _context_aw3d30(fetcher: Callable, poly: Polygon, cache: Path, context_m: float, context_raster: Raster,
                    context_info: dict, *, a_datum: str, shift_fn: Callable | None, work: Path) -> tuple[Raster, bool]:
    """AW3D30 sobre la MISMA ventana de contexto que GLO-30, ya en la malla y el datum de GLO-30.

    Solo para puntuar AW3D30 contra las huellas con la misma politica de raster que GLO-30 (AUD-1 I1). Devuelve
    (raster alineado, datum_aligned). Lanza RejectRequest/OSError si no se puede (el llamador lo registra).
    """
    ctx_cache = cache / "context"
    ctx_cache.mkdir(parents=True, exist_ok=True)
    kwargs = {"margin_m": float(context_m)} if "margin_m" in inspect.signature(fetcher).parameters else {}
    dem_b = fetcher(poly, ctx_cache, **kwargs)
    raster_b = reproject(dem_b, poly, margin_px=int(context_info["margin_px"]))
    cmp = compare_sources(context_raster, raster_b, poly, a_datum=a_datum, b_datum=dem_b.vertical_datum,
                          shift_fn=shift_fn, out_dir=work / "context_cmp")
    aligned = Raster(path=Path(cmp.extra["b_aligned_path"]), epsg=context_raster.epsg, pixel_m=context_raster.pixel_m,
                     nodata=float("nan"))
    return aligned, bool(cmp.datum_aligned)


def _validation_dict(v: ValidationResult, n_footprints: int) -> dict:
    d = asdict(v)
    d["n_footprints_total"] = n_footprints
    return d


def _comparison_dict(c: ComparisonResult) -> dict:
    d = asdict(c)
    d.pop("diff_map_path", None)
    d.pop("extra", None)
    d["status"] = "ok"
    return d


def write_run_manifest(
    out_dir: Path, *, poly: Polygon, epsg: int, dem: DemResult, raster: Raster, terrain: TerrainResult, solar: dict,
    comparison: ComparisonResult | None, comparison_unavailable: str | None, dem_b: DemResult | None,
    aux: AuxStats | None, validation: ValidationResult | None, n_footprints: int, confidence: Confidence,
    context_raster: Raster | None, context_info: dict, elapsed: dict, generated_at: str | None,
    validation_extra: dict | None = None, validation_unavailable: str | None = None,
    canopy: CanopyStats | None = None,
) -> tuple[dict, Path]:
    """Arma y escribe manifest.json con los resultados de las etapas. Lo usan run_pipeline y la capa de agente
    (agent_tools), para que ambos caminos produzcan exactamente el mismo manifiesto (y el mismo ``result_hash``)."""
    with rasterio.open(raster.path) as r:
        shape = (r.height, r.width)
    datasets = [dataset_entry("base", dem, array_sha256=raster_digest(dem.path))]
    acquired = {dem.source: dem.acquired}
    if dem_b is not None:
        datasets.append(dataset_entry("comparison", dem_b, array_sha256=raster_digest(dem_b.path)))
        acquired[dem_b.source] = dem_b.acquired
    comparison_dict = (_comparison_dict(comparison) if comparison is not None
                       else {"status": "no_disponible", "reason": comparison_unavailable or "aw3d30 no solicitado"})
    aux_dict = None
    if aux is not None:
        aux_dict = {k: v for k, v in asdict(aux).items() if k not in ("urls",)}
        aux_dict["status"] = "ok" if aux.available else "parcial_o_sin_dato"
    val_dict = None
    if validation is not None:
        val_dict = _validation_dict(validation, n_footprints)
        if validation_extra:
            val_dict.update(validation_extra)  # p. ej. {"aw3d30": {...}} sobre el MISMO conjunto de huellas
    elif validation_unavailable:  # la verdad de terreno es solo validacion: su fallo no rechaza el informe (AUD-1 I4)
        val_dict = {"status": "no_disponible", "reason": validation_unavailable}
    worldcover_dict = None
    if canopy is not None:
        worldcover_dict = {k: v for k, v in asdict(canopy).items()}
        worldcover_dict["status"] = "ok" if canopy.available else "no_disponible"
        worldcover_dict["source"] = "ESA WorldCover 10 m v200 (2021), CC BY 4.0"
    area = abs(Geod(ellps="WGS84").geometry_area_perimeter(poly)[0])
    c = poly.centroid
    paths = {"dem": str(dem.path), "raster_utm": str(raster.path), "slope": str(terrain.slope_path),
             "aspect": str(terrain.aspect_path), "hillshade": str(terrain.hillshade_path),
             "contours": str(terrain.contours_path)}
    if comparison is not None:
        paths["diff_map"] = str(comparison.diff_map_path)
    if context_raster is not None:
        paths["context_raster_utm"] = str(context_raster.path)
    manifest = build_manifest(
        poly_geojson=json.loads(json.dumps(mapping(poly))), polygon_area_m2=round(area, 3),
        centroid=(round(c.x, 9), round(c.y, 9)), epsg=epsg, pixel_m=raster.pixel_m, grid_shape=shape,
        datasets=datasets, acquired=acquired, terrain_stats=terrain.stats, comparison=comparison_dict, aux=aux_dict,
        validation=val_dict, confidence=asdict(confidence), hypotheses=HYPOTHESES, solar=solar,
        generated_at=generated_at, paths=paths, elapsed_s=elapsed, context=context_info, worldcover=worldcover_dict,
    )
    return manifest, write_manifest(manifest, out_dir)


def run_pipeline(
    poly: Polygon,
    out_dir: Path,
    *,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    fetch: dict[str, Callable] | None = None,
    fetch_aux: Callable | None = None,
    ground_truth: Callable | None = None,
    shift_fn: Callable | None = None,
    canopy_pct: float | None = None,
    fetch_canopy: Callable | None = None,
    hooks: dict[str, Callable] | None = None,
    cache_dir: Path | None = None,
    now: Callable[[], str] | None = None,
    context_m: float | None = DEFAULT_CONTEXT_M,
) -> RunResult:
    """Encadena las tareas 2-7. ``fetch`` mapea fuente -> callable(poly, cache_dir) -> DemResult (inyectable).

    ``context_m``: ventana de contexto para el GLB (None = sin contexto). Es un raster aparte (``volatile.paths.
    context_raster_utm``); si no se puede obtener la corrida sigue y queda ``volatile.context.status = no_disponible``.
    """
    if context_m is not None and (isinstance(context_m, bool) or not isinstance(context_m, (int, float))
                                  or not math.isfinite(context_m) or not (0 < context_m <= 20_000)):
        raise RejectRequest(f"context_m invalido: {context_m!r} (None o metros en (0, 20000])")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in (MANIFEST_NAME, REJECTION_NAME):  # una corrida rechazada no deja el resultado de otra anterior
        (out_dir / stale).unlink(missing_ok=True)
    hooks = hooks or {}
    unknown = set(hooks) - set(HOOK_ORDER)
    if unknown:
        raise RejectRequest(f"gancho desconocido: {sorted(unknown)}; validos: {list(HOOK_ORDER)}")
    if not sources or sources[0] != "glo30":
        raise RejectRequest("la fuente base debe ser 'glo30' (primera en 'sources')")
    fetchers = {"glo30": fetch_dem, "aw3d30": fetch_aw3d30, **(fetch or {})}
    bad = [s for s in sources if s not in fetchers]
    if bad:
        raise RejectRequest(f"fuente no soportada: {bad}")
    fetch_aux = fetch_aux or fetch_aux_stats
    cache = Path(cache_dir) if cache_dir is not None else out_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    elapsed: dict[str, float] = {}
    stage = "entrada"

    def timed(name, fn, *a, **k):
        nonlocal stage
        stage = name
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            elapsed[name] = round(time.perf_counter() - t0, 3)

    try:
        poly = timed("validar_poligono", lambda: load_polygon(json.loads(json.dumps(mapping(poly)))))
        epsg = centroid_zone(poly)

        dem = timed("fetch_dem", fetchers["glo30"], poly, cache)
        raster = timed("reproject", reproject, dem, poly)
        terrain = timed("compute_terrain", compute_terrain, raster, poly, out_dir=work)
        solar = timed("solar", compute_solar, raster, poly)

        comparison = None
        unavailable = None
        dem_b = None
        if "aw3d30" in sources:
            try:
                dem_b = timed("fetch_aw3d30", fetchers["aw3d30"], poly, cache)
                raster_b = timed("reproject_aw3d30", reproject, dem_b, poly)
                comparison = timed("compare_sources", compare_sources, raster, raster_b, poly,
                                   a_datum=dem.vertical_datum, b_datum=dem_b.vertical_datum,
                                   shift_fn=shift_fn, out_dir=work)
            except RejectRequest as exc:
                # La comparacion es opcional: sin ella el factor de desacuerdo queda sin dato (tope B). Se registra el motivo.
                comparison, unavailable = None, f"{stage}: {exc.reason}"

        aux = None
        try:
            aux = timed("aux_layers", fetch_aux, poly, dem)
        except RejectRequest as exc:
            aux = AuxStats(n_pixels=0, void_water_fill_pct=None, hem_p90_m=None, available=False, note=exc.reason,
                           unavailable=list(AUX_KINDS))

        canopy = None
        if fetch_canopy is not None:  # dosel/construido (ESA WorldCover): sin esta funcion el factor queda sin dato por diseno
            try:
                canopy = timed("worldcover", fetch_canopy, poly)
            except RejectRequest as exc:
                canopy = CanopyStats(n_pixels=0, canopy_pct=None, available=False, note=exc.reason)

        context_raster, context_info = None, {"status": "no_solicitado", "requested_m": None}
        if context_m is not None:
            try:
                context_raster, context_info = timed("context", _context_raster, fetchers["glo30"], poly, cache, float(context_m))
            except (RejectRequest, OSError, rasterio.errors.RasterioError) as exc:
                reason = exc.reason if isinstance(exc, RejectRequest) else f"{type(exc).__name__}: {exc}"
                context_info = {"status": "no_disponible", "requested_m": float(context_m), "reason": reason}

        validation, validation_extra, gt_unavailable, fps = None, None, None, []
        if ground_truth is not None:
            # La verdad de terreno es solo de validacion: si falla (CMR/Earthdata caidos, OSError de h5py...) el informe
            # sigue y el manifiesto dice 'no_disponible' con el motivo (AUD-1 I4).
            try:
                fps = timed("ground_truth", ground_truth, poly, cache)
                # Las huellas casi nunca caen dentro de un lote chico: TODA verdad de terreno se puntua contra el raster de
                # contexto (+-context_m, mismo GLO-30) y, si no hay contexto, contra el de trabajo. AW3D30 se puntua sobre
                # la misma ventana y SOLO sobre las huellas que ambos DEM pueden evaluar (AUD-1 I1).
                ref = context_raster if context_raster is not None else raster
                ref_b, b_aligned, restrict = None, None, None
                if "aw3d30" in sources:
                    if comparison is None:
                        validation_extra = {"aw3d30": {
                            "status": "no_disponible", "reason": f"sin comparacion AW3D30 en esta corrida ({unavailable})"}}
                    elif context_raster is None:
                        validation_extra = {"aw3d30": {
                            "status": "no_disponible",
                            "reason": "sin ventana de contexto: AW3D30 no se puede puntuar sobre las mismas huellas"}}
                    else:
                        try:
                            ref_b, b_aligned = timed("reference_aw3d30", _context_aw3d30, fetchers["aw3d30"], poly, cache,
                                                     float(context_m), context_raster, context_info,
                                                     a_datum=dem.vertical_datum, shift_fn=shift_fn, work=work)
                        except (RejectRequest, OSError, rasterio.errors.RasterioError) as exc:
                            why = exc.reason if isinstance(exc, RejectRequest) else type(exc).__name__
                            validation_extra = {"aw3d30": {
                                "status": "no_disponible", "reason": f"AW3D30 sobre la ventana de contexto: {why}"}}
                if ref_b is not None:
                    restrict = usable_ids(ref, fps) & usable_ids(ref_b, fps)
                validation = timed("validate", validate, ref, fps, restrict_to=restrict)
                if ref_b is not None:
                    vb = validate(ref_b, fps, restrict_to=restrict)
                    validation_extra = {"aw3d30": {**_validation_dict(vb, len(fps)), "datum_aligned": bool(b_aligned)}}
            except Exception as exc:  # noqa: BLE001 - etapa solo de validacion
                why = exc.reason if isinstance(exc, RejectRequest) else f"error inesperado ({type(exc).__name__})"
                validation, validation_extra, gt_unavailable = None, None, f"{stage}: {why}"

        lost = aux.unavailable_factors() + (["canopy_pct"] if canopy is not None and not canopy.available else [])
        confidence = timed("score_confidence", lambda: score_confidence(
            factors_from_results(terrain.stats, comparison, aux, canopy_pct=canopy_pct, canopy=canopy), unavailable=lost))
    except RejectRequest as exc:
        _reject(out_dir, stage, exc)
        raise
    except Exception as exc:  # noqa: BLE001 - nada falla en silencio: tambien lo inesperado deja rejection.json
        _reject(out_dir, stage, RejectRequest(f"error inesperado en {stage}: {type(exc).__name__}: {exc}"))
        raise

    outputs: dict = {}
    try:
        stage = "manifest"
        manifest, path = write_run_manifest(
            out_dir, poly=poly, epsg=epsg, dem=dem, raster=raster, terrain=terrain, solar=solar, comparison=comparison,
            comparison_unavailable=unavailable, dem_b=dem_b, aux=aux, validation=validation,
            n_footprints=(len(fps) if validation is not None else 0), confidence=confidence, context_raster=context_raster,
            context_info=context_info, elapsed=elapsed, generated_at=(now() if now else None),
            validation_extra=validation_extra, validation_unavailable=gt_unavailable, canopy=canopy)

        for name in HOOK_ORDER:  # ganchos de las Tasks 9-12 (GLB, PDF, visor, video): reciben solo el manifiesto
            if name in hooks:
                stage = f"hook:{name}"
                outputs[name] = hooks[name](manifest, out_dir)
    except Exception as exc:  # noqa: BLE001 - un entregable roto no deja un manifiesto huerfano junto a un informe a medias
        (out_dir / MANIFEST_NAME).unlink(missing_ok=True)
        _reject(out_dir, stage, exc if isinstance(exc, RejectRequest)
                else RejectRequest(f"error inesperado en {stage}: {type(exc).__name__}: {exc}"))
        raise
    return RunResult(
        out_dir=out_dir, manifest_path=path, manifest=manifest, result_hash=result_hash(out_dir),
        confidence=confidence, terrain=terrain, raster=raster, comparison=comparison,
        comparison_unavailable_reason=unavailable, aux=aux, validation=validation, outputs=outputs,
        context_raster=context_raster,
    )
