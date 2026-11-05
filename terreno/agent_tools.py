"""Capa de agente (Task 13, spec sec. 7): lista CERRADA de diez tools con entrada y salida JSON validadas por esquema.

Principio: el agente no calcula, no elige el CRS ni las formulas y no escribe cifras. Selecciona y secuencia recetas ya
versionadas; cada tool llama a las funciones del pipeline (las mismas que ``run_pipeline``) y el texto del informe sale del
manifiesto. Sin Bash, sin edicion de archivos, sin web: ninguna tool recibe rutas, URL ni CRS.

* ``TOOLS[nombre](sesion, args) -> dict``: valida ``args`` contra ``SCHEMAS[nombre]`` (``additionalProperties: false``),
  ejecuta, valida la salida contra ``OUTPUT_SCHEMAS[nombre]``. Nunca lanza: devuelve ``status`` (ok, no_disponible,
  entrada_invalida, orden_invalido, rechazado, detenido, error).
* Un ``RejectRequest`` de cualquier etapa de calculo es un RECHAZO: se registra en ``rejection.json`` y el pipeline queda
  detenido (ninguna otra tool corre). ``compare_sources`` es la unica etapa opcional (como en run_pipeline).
* ``decide_tool_use`` es la politica de ``can_use_tool``: niega por defecto; permite solo ``mcp__terreno__<una de las diez>``
  con entrada valida y sin campos tipo ruta/URL/CRS.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jsonschema

from terreno.compare import compare_sources as _compare_sources
from terreno.compare import fetch_aw3d30
from terreno.confidence import factors_from_results, score_confidence as _score_confidence
from terreno.errors import RejectRequest
from terreno.fetch import fetch_dem as _fetch_dem
from terreno.geometry import centroid_zone, load_polygon
from terreno.glb import DEFAULT_EXAGGERATION, GLB_NAME, LAYERS_INDEX, PREDIO_NAME, build_glb_hook
from terreno.layers import AUX_KINDS, AuxStats, fetch_aux_stats
from terreno.worldcover import CanopyStats
from terreno.manifest import MANIFEST_NAME
from terreno.pipeline import DEFAULT_CONTEXT_M, REJECTION_NAME, _context_raster, write_run_manifest
from terreno.publish import DATA_SUBDIR, PUBLISH_DIR, VIEWER_DIR, publish_viewer as _publish_viewer
from terreno.report import REPORT_NAME, render_pdf as _render_pdf
from terreno.reproject import reproject as _reproject
from terreno.solar import compute_solar
from terreno.terrain import compute_terrain as _compute_terrain

MCP_SERVER_NAME = "terreno"
MCP_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
TOOL_NAMES = ("fetch_dem", "reproject", "compute_terrain", "compare_sources", "score_confidence", "build_glb",
              "render_pdf", "publish_viewer", "reject_request", "request_human_review")
HUMAN_REVIEW_NAME = "REVISION_HUMANA_REQUERIDA.json"
AUDIT_NAME = "agent_log.json"
MIN_EXAGGERATION, MAX_EXAGGERATION = 1.0, 5.0  # rango del deslizador del visor; el valor siempre queda rotulado
FORBIDDEN_KEYS = re.compile(r"^(path|paths|file|files|file_path|filepath|filename|dir|directory|out_dir|out|output|cwd|url|uri|"
                            r"epsg|crs|srs|utm|utm_zone|zone|pixel_m|resampling|datum|formula|command|cmd|code)$", re.I)

# ---------------------------------------------------------------------------------------------- esquemas


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    s: dict = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


_REASON = {"type": "string", "minLength": 3, "maxLength": 500}
_POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3}
_POLYGON = {
    "type": "object", "additionalProperties": False, "required": ["type", "coordinates"],
    "properties": {"type": {"const": "Polygon"},
                   "coordinates": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 4, "items": _POINT}}},
}
_LEVEL = {"enum": ["A", "B", "C"]}

SCHEMAS: dict[str, dict] = {
    "fetch_dem": _obj({"polygon": _POLYGON, "source": {"enum": ["glo30"]}}, ["polygon"]),
    "reproject": _obj(),  # sin parametros: la zona UTM sale del centroide, por codigo
    "compute_terrain": _obj(),
    "compare_sources": _obj(),
    "score_confidence": _obj(),
    "build_glb": _obj({"exaggeration": {"type": "number", "minimum": MIN_EXAGGERATION, "maximum": MAX_EXAGGERATION}}),
    "render_pdf": _obj(),  # el texto sale del manifiesto; el agente no aporta ni siquiera el nombre del sitio
    "publish_viewer": _obj(),
    "reject_request": _obj({"reason": _REASON}, ["reason"]),
    "request_human_review": _obj({"reason": _REASON}, ["reason"]),
}

OUTPUT_SCHEMAS: dict[str, dict] = {
    "fetch_dem": _obj({"status": {"const": "ok"}, "source": {"type": "string"}, "tiles": {"type": "array", "items": {"type": "string"}},
                       "acquired": {"type": "string"}, "nodata_fraction": {"type": "number"}}, ["status"]),
    "reproject": _obj({"status": {"const": "ok"}, "epsg": {"enum": [32718, 32719]}, "pixel_m": {"type": "number"},
                       "rows": {"type": "integer"}, "cols": {"type": "integer"}}, ["status", "epsg"]),
    "compute_terrain": _obj({"status": {"const": "ok"}, "n_pixels": {"type": "integer"}, "inconclusive": {"type": "boolean"},
                             "solar_status": {"type": "string"}}, ["status"]),
    "compare_sources": _obj({"status": {"enum": ["ok", "no_disponible"]}, "reason": {"type": "string"}}, ["status"]),
    "score_confidence": _obj({"status": {"const": "ok"}, "overall": _LEVEL, "human_review_required": {"type": "boolean"},
                              "omit_point_slope": {"type": "boolean"}, "result_hash": {"type": "string"},
                              "context_status": {"type": "string"}}, ["status", "overall", "human_review_required"]),
    "build_glb": _obj({"status": {"const": "ok"}, "exaggeration": {"type": "number"}, "files": {"type": "array", "items": {"type": "string"}},
                       "size_bytes": {"type": "integer"}}, ["status"]),
    "render_pdf": _obj({"status": {"const": "ok"}, "file": {"type": "string"}, "size_bytes": {"type": "integer"}}, ["status"]),
    "publish_viewer": _obj({"status": {"const": "ok"}, "directory": {"type": "string"}, "viewer_entry": {"type": "string"},
                            "files": {"type": "integer"}}, ["status"]),
    "reject_request": _obj({"status": {"const": "rechazado"}, "reason": {"type": "string"}, "stage": {"type": "string"},
                            "removed": {"type": "array", "items": {"type": "string"}}}, ["status", "reason"]),
    "request_human_review": _obj({"status": {"const": "revision_humana_registrada"}, "flag_file": {"type": "string"}}, ["status"]),
}

ERROR_STATUSES = ("entrada_invalida", "orden_invalido", "rechazado", "detenido", "error")
ERROR_SCHEMA = _obj({"status": {"enum": list(ERROR_STATUSES)}, "reason": {"type": "string"}, "stage": {"type": "string"}},
                    ["status", "reason"])

_VALIDATORS = {n: jsonschema.Draft202012Validator(s) for n, s in SCHEMAS.items()}
_OUT_VALIDATORS = {n: jsonschema.Draft202012Validator(s) for n, s in OUTPUT_SCHEMAS.items()}
_ERR_VALIDATOR = jsonschema.Draft202012Validator(ERROR_SCHEMA)


def _errors(validator, instance) -> str:
    errs = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    out = []
    for e in errs[:6]:
        where = "/".join(str(p) for p in e.absolute_path) or "(raiz)"
        if e.validator == "additionalProperties":
            extra = sorted(set(e.instance) - set(e.schema.get("properties", {})))
            out.append(f"{where}: campos no permitidos {extra}")
        else:
            out.append(f"{where}: {e.message[:140]}")
    return "; ".join(out)


# ---------------------------------------------------------------------------------------------- politica can_use_tool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    message: str = ""


def _forbidden_keys(obj, depth=0) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict) and depth < 8:
        for k, v in obj.items():
            if isinstance(k, str) and FORBIDDEN_KEYS.match(k):
                found.append(k)
            found += _forbidden_keys(v, depth + 1)
    elif isinstance(obj, list) and depth < 8:
        for v in obj[:50]:
            found += _forbidden_keys(v, depth + 1)
    return found


def decide_tool_use(tool_name: str, tool_input) -> Decision:
    """Politica de ``can_use_tool``: deniega todo salvo las diez tools del servidor ``terreno`` con entrada valida."""
    if not isinstance(tool_name, str) or not tool_name.startswith(MCP_PREFIX) or tool_name[len(MCP_PREFIX):] not in TOOL_NAMES:
        return Decision(False, "Herramienta no permitida: solo existen las diez tools del servidor 'terreno'. "
                               "No hay shell, archivos ni web; si el pedido no se puede cumplir con ellas, usa reject_request.")
    name = tool_name[len(MCP_PREFIX):]
    if not isinstance(tool_input, dict):
        return Decision(False, "La entrada de la tool debe ser un objeto JSON.")
    bad = _forbidden_keys(tool_input)
    if bad:
        return Decision(False, f"Campos no permitidos (rutas, URL, CRS o formulas los fija el codigo, no el agente): {sorted(set(bad))}")
    err = _errors(_VALIDATORS[name], tool_input)
    if err:
        return Decision(False, f"Entrada invalida para {name}: {err}")
    return Decision(True)


# ---------------------------------------------------------------------------------------------- sesion


class AgentSession:
    """Estado de UNA corrida del agente sobre UNA carpeta de salida. Los resultados intermedios viven aqui (en proceso);
    el agente solo ve los diccionarios JSON que devuelven las tools."""

    OWN_OUTPUTS = (MANIFEST_NAME, GLB_NAME, PREDIO_NAME)

    def __init__(self, out_dir: Path, *, fetch: dict[str, Callable] | None = None, fetch_aux: Callable | None = None,
                 fetch_canopy: Callable | None = None, shift_fn: Callable | None = None, now: Callable[[], str] | None = None, site_name: str | None = None,
                 cache_dir: Path | None = None, viewer_dir: Path = VIEWER_DIR, context_m: float | None = DEFAULT_CONTEXT_M):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for stale in (MANIFEST_NAME, REJECTION_NAME, HUMAN_REVIEW_NAME, AUDIT_NAME):  # una corrida no hereda la anterior
            (self.out_dir / stale).unlink(missing_ok=True)
        self.fetchers = {"glo30": _fetch_dem, "aw3d30": fetch_aw3d30, **(fetch or {})}
        self.fetch_aux = fetch_aux or fetch_aux_stats
        self.fetch_canopy = fetch_canopy  # None = dosel sin dato por diseno (igual que run_pipeline)
        self.canopy: CanopyStats | None = None
        self.shift_fn, self.now, self.site_name = shift_fn, now, site_name
        self.viewer_dir, self.context_m = Path(viewer_dir), context_m
        self.cache = Path(cache_dir) if cache_dir is not None else self.out_dir / "cache"
        self.work = self.out_dir / "work"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        # estado de la corrida
        self.poly = None
        self.epsg: int | None = None
        self.dem = self.raster = self.terrain = self.solar = None
        self.dem_b = self.comparison = None
        self.comparison_unavailable: str | None = None
        self.aux = self.confidence = self.manifest = None
        self.context_raster, self.context_info = None, {"status": "no_solicitado", "requested_m": None}
        self.exaggeration = DEFAULT_EXAGGERATION
        self.done: set[str] = set()
        self.last_ok: str | None = None  # ultima etapa completada (para registrar en que punto se rechazo)
        self.elapsed: dict[str, float] = {}
        self.rejected: dict | None = None
        self.human_review = False
        self.audit: list[dict] = []
        self.forbidden_attempts: list[dict] = []
        self.calls: list[dict] = []

    # --- registro
    def log_permission(self, tool: str, allowed: bool, message: str = "") -> None:
        ev = {"tool": tool, "allowed": allowed, "message": message, "t": round(time.time(), 3)}
        self.audit.append(ev)
        if not allowed:
            self.forbidden_attempts.append({"tool": tool, "message": message})
        self.write_audit()

    def write_audit(self) -> None:
        (self.out_dir / AUDIT_NAME).write_text(json.dumps(
            {"permissions": self.audit, "calls": self.calls, "forbidden_attempts": self.forbidden_attempts,
             "rejected": self.rejected, "human_review": self.human_review}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # --- rechazo
    def stop(self, stage: str, reason: str, by: str) -> list[str]:
        """Registra el rechazo (rejection.json) y retira lo generado por esta corrida: no queda informe a medias."""
        removed = []
        for f in self.OWN_OUTPUTS:
            p = self.out_dir / f
            if p.exists():
                p.unlink()
                removed.append(f)
        layers = self.out_dir / "layers"
        if layers.is_dir():
            for p in layers.iterdir():
                if p.is_file() and (p.suffix == ".png" or p.name == LAYERS_INDEX):
                    p.unlink()
            removed.append("layers/")
            if not any(layers.iterdir()):
                layers.rmdir()
        self.rejected = {"rejected": True, "stage": stage, "reason": reason, "by": by}
        (self.out_dir / REJECTION_NAME).write_text(json.dumps({**self.rejected, "removed": removed}, indent=2, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        self.write_audit()
        return removed

    # --- cierre (codigo, no agente)
    def finalize(self) -> None:
        """Si la regla de confianza exige revision humana y el agente no la registro, la escribe el anfitrion."""
        if self.rejected or self.confidence is None or not self.confidence.human_review_required or self.human_review:
            self.write_audit()
            return
        self._write_flag("nivel C: la regla de confianza exige revision humana antes de entregar", "regla de confianza (codigo)")
        self.write_audit()

    def _write_flag(self, reason: str, by: str) -> None:
        (self.out_dir / HUMAN_REVIEW_NAME).write_text(json.dumps({
            "required": True, "reason": reason, "by": by, "level": self.confidence.overall if self.confidence else None,
            "result_hash": self.manifest["result_hash"] if self.manifest else None,
            "nota": "Revision humana obligatoria antes de entregar este informe (spec sec. 4 y 7).",
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.human_review = True


# ---------------------------------------------------------------------------------------------- handlers
# Cada handler recibe (sesion, args validados) y devuelve el dict de salida; RejectRequest = rechazo del pedido.


def _timed(s: AgentSession, name: str, fn, *a, **k):
    t0 = time.perf_counter()
    try:
        return fn(*a, **k)
    finally:
        s.elapsed[name] = round(time.perf_counter() - t0, 3)


def _h_fetch_dem(s: AgentSession, args: dict) -> dict:
    poly = _timed(s, "validar_poligono", load_polygon, args["polygon"])
    s.epsg = centroid_zone(poly)
    s.poly = poly
    s.dem = _timed(s, "fetch_dem", s.fetchers["glo30"], poly, s.cache)
    return {"status": "ok", "source": s.dem.source, "tiles": list(s.dem.tiles), "acquired": s.dem.acquired,
            "nodata_fraction": float(s.dem.nodata_fraction)}


def _h_reproject(s: AgentSession, args: dict) -> dict:
    import rasterio

    s.raster = _timed(s, "reproject", _reproject, s.dem, s.poly)
    with rasterio.open(s.raster.path) as r:
        rows, cols = r.height, r.width
    return {"status": "ok", "epsg": s.raster.epsg, "pixel_m": float(s.raster.pixel_m), "rows": rows, "cols": cols}


def _h_compute_terrain(s: AgentSession, args: dict) -> dict:
    s.terrain = _timed(s, "compute_terrain", _compute_terrain, s.raster, s.poly, out_dir=s.work)
    s.solar = _timed(s, "solar", compute_solar, s.raster, s.poly)
    st = s.terrain.stats
    return {"status": "ok", "n_pixels": int(st["n_pixels"]), "inconclusive": bool(st.get("inconclusive")),
            "solar_status": str(s.solar.get("status", "ok")) if isinstance(s.solar, dict) else "ok"}


def _h_compare_sources(s: AgentSession, args: dict) -> dict:
    try:
        s.dem_b = _timed(s, "fetch_aw3d30", s.fetchers["aw3d30"], s.poly, s.cache)
        raster_b = _timed(s, "reproject_aw3d30", _reproject, s.dem_b, s.poly)
        s.comparison = _timed(s, "compare_sources", _compare_sources, s.raster, raster_b, s.poly,
                              a_datum=s.dem.vertical_datum, b_datum=s.dem_b.vertical_datum, shift_fn=s.shift_fn, out_dir=s.work)
    except RejectRequest as exc:  # etapa opcional, como en run_pipeline: sin comparacion el nivel no pasa de B
        s.comparison, s.dem_b, s.comparison_unavailable = None, None, exc.reason
        return {"status": "no_disponible", "reason": exc.reason}
    return {"status": "ok"}


def _h_score_confidence(s: AgentSession, args: dict) -> dict:
    try:
        s.aux = _timed(s, "aux_layers", s.fetch_aux, s.poly, s.dem)
    except RejectRequest as exc:
        s.aux = AuxStats(n_pixels=0, void_water_fill_pct=None, hem_p90_m=None, available=False, note=exc.reason,
                         unavailable=list(AUX_KINDS))
    s.canopy = None
    if s.fetch_canopy is not None:
        try:
            s.canopy = _timed(s, "worldcover", s.fetch_canopy, s.poly)
        except RejectRequest as exc:
            s.canopy = CanopyStats(n_pixels=0, canopy_pct=None, available=False, note=exc.reason)
    lost = s.aux.unavailable_factors() + (["canopy_pct"] if s.canopy is not None and not s.canopy.available else [])
    s.confidence = _timed(s, "score_confidence", lambda: _score_confidence(
        factors_from_results(s.terrain.stats, s.comparison, s.aux, canopy_pct=None, canopy=s.canopy), unavailable=lost))
    if s.context_m is not None:
        try:
            s.context_raster, s.context_info = _timed(s, "context", _context_raster, s.fetchers["glo30"], s.poly, s.cache,
                                                      float(s.context_m))
        except (RejectRequest, OSError) as exc:
            s.context_info = {"status": "no_disponible", "requested_m": float(s.context_m),
                              "reason": exc.reason if isinstance(exc, RejectRequest) else f"{type(exc).__name__}: {exc}"}
    s.manifest, _ = write_run_manifest(
        s.out_dir, poly=s.poly, epsg=s.epsg, dem=s.dem, raster=s.raster, terrain=s.terrain, solar=s.solar,
        comparison=s.comparison, comparison_unavailable=s.comparison_unavailable or ("aw3d30 no disponible" if s.dem_b is None else None),
        dem_b=s.dem_b, aux=s.aux, validation=None, n_footprints=0, confidence=s.confidence, context_raster=s.context_raster,
        context_info=s.context_info, elapsed=s.elapsed, generated_at=(s.now() if s.now else None), canopy=s.canopy)
    c = s.confidence
    return {"status": "ok", "overall": c.overall, "human_review_required": bool(c.human_review_required),
            "omit_point_slope": bool(getattr(c, "omit_point_slope", False)), "result_hash": s.manifest["result_hash"],
            "context_status": str(s.context_info.get("status"))}


def _h_build_glb(s: AgentSession, args: dict) -> dict:
    s.exaggeration = float(args.get("exaggeration", DEFAULT_EXAGGERATION))
    path = _timed(s, "build_glb", build_glb_hook, s.manifest, s.out_dir, exaggeration=s.exaggeration)
    files = [GLB_NAME, PREDIO_NAME] + sorted(f"layers/{p.name}" for p in (s.out_dir / "layers").iterdir())
    return {"status": "ok", "exaggeration": s.exaggeration, "files": files, "size_bytes": Path(path).stat().st_size}


def _h_render_pdf(s: AgentSession, args: dict) -> dict:
    pdf = _timed(s, "render_pdf", _render_pdf, s.manifest, s.out_dir / REPORT_NAME, site_name=s.site_name,
                 exaggeration=s.exaggeration)
    return {"status": "ok", "file": REPORT_NAME, "size_bytes": Path(pdf).stat().st_size}


def _h_publish_viewer(s: AgentSession, args: dict) -> dict:
    pub = _timed(s, "publish_viewer", _publish_viewer, s.manifest, s.out_dir, viewer_dir=s.viewer_dir)
    n = sum(1 for p in pub.rglob("*") if p.is_file())
    return {"status": "ok", "directory": PUBLISH_DIR, "viewer_entry": f"{PUBLISH_DIR}/index.html", "files": n}


def _h_reject_request(s: AgentSession, args: dict) -> dict:
    removed = s.stop(s.last_ok or "entrada", args["reason"], "agente")
    return {"status": "rechazado", "reason": args["reason"], "stage": s.rejected["stage"], "removed": removed}


def _h_request_human_review(s: AgentSession, args: dict) -> dict:
    s._write_flag(args["reason"], "agente")
    s.write_audit()
    return {"status": "revision_humana_registrada", "flag_file": HUMAN_REVIEW_NAME}


_HANDLERS: dict[str, Callable[[AgentSession, dict], dict]] = {
    "fetch_dem": _h_fetch_dem, "reproject": _h_reproject, "compute_terrain": _h_compute_terrain,
    "compare_sources": _h_compare_sources, "score_confidence": _h_score_confidence, "build_glb": _h_build_glb,
    "render_pdf": _h_render_pdf, "publish_viewer": _h_publish_viewer, "reject_request": _h_reject_request,
    "request_human_review": _h_request_human_review,
}

# prerrequisitos (pasos que deben estar hechos) y, para las tools de calculo, el paso que se marca al terminar
_REQUIRES = {
    "fetch_dem": (), "reproject": ("fetch_dem",), "compute_terrain": ("reproject",), "compare_sources": ("compute_terrain",),
    # AUD-1 I2: la comparacion debe haberse INTENTADO (ok o no_disponible) antes de puntuar; si no, el factor de desacuerdo
    # quedaria sin_dato por omision del agente y un C (RMS > 6 m) saldria como B.
    "score_confidence": ("compute_terrain", "compare_sources"), "build_glb": ("score_confidence",), "render_pdf": ("score_confidence",),
    "publish_viewer": ("build_glb",), "reject_request": (), "request_human_review": ("score_confidence",),
}
_ONCE = ("fetch_dem", "reproject", "compute_terrain", "compare_sources", "score_confidence", "build_glb", "render_pdf",
         "publish_viewer")


def _error(status: str, reason: str, **extra) -> dict:
    return {"status": status, "reason": reason, **extra}


def _dispatch(name: str, s: AgentSession, args) -> dict:
    t0 = time.perf_counter()
    out = _dispatch_inner(name, s, args)
    s.calls.append({"tool": name, "status": out.get("status"), "seconds": round(time.perf_counter() - t0, 3)})
    s.write_audit()
    return out


def _dispatch_inner(name: str, s: AgentSession, args) -> dict:
    if not isinstance(args, dict):
        return _error("entrada_invalida", "la entrada debe ser un objeto JSON")
    err = _errors(_VALIDATORS[name], args)
    if err:
        return _error("entrada_invalida", err)
    if s.rejected is not None:
        return _error("detenido", f"pipeline detenido: solicitud rechazada ({s.rejected['reason']}). No se ejecutan mas tools.")
    missing = [r for r in _REQUIRES[name] if r not in s.done]
    if missing:
        return _error("orden_invalido", f"antes de {name} debe ejecutarse: {', '.join(missing)}")
    if name in _ONCE and name in s.done:
        return _error("orden_invalido", f"{name} ya se ejecuto en esta corrida")
    if name == "reject_request" and "render_pdf" in s.done:
        return _error("orden_invalido", "el informe ya se genero: ya no se puede rechazar; usa request_human_review")
    try:
        out = _HANDLERS[name](s, args)
    except RejectRequest as exc:
        s.stop(name, exc.reason, f"tool:{name}")
        return _error("rechazado", exc.reason, stage=name)
    except Exception as exc:  # fallo interno (red, disco): no es un rechazo del sitio; la tool puede reintentarse
        return _error("error", f"{type(exc).__name__}: {str(exc)[:300]}")
    errs = _errors(_OUT_VALIDATORS[name], out) if isinstance(out, dict) else "la salida no es un objeto"
    if errs:
        return _error("error", f"salida de {name} no cumple su esquema: {errs}")
    if name in _ONCE:
        s.done.add(name)
        s.last_ok = name
    return out


def _make_tool(name: str) -> Callable[[AgentSession, Any], dict]:
    def tool(session: AgentSession, args) -> dict:
        return _dispatch(name, session, args)

    tool.__name__ = name
    tool.__doc__ = f"Tool '{name}': entrada validada por SCHEMAS['{name}'], salida por OUTPUT_SCHEMAS['{name}']."
    return tool


TOOLS: dict[str, Callable[[AgentSession, Any], dict]] = {n: _make_tool(n) for n in TOOL_NAMES}

DESCRIPTIONS = {
    "fetch_dem": "Paso 1. Descarga el DEM Copernicus GLO-30 del poligono. Entrada: el poligono GeoJSON copiado EXACTAMENTE del pedido. "
                 "Rechaza si esta fuera de Chile, es demasiado grande o no hay tile publicado.",
    "reproject": "Paso 2. Reproyecta a UTM 30 m. Sin parametros: la zona (18S/19S) la fija el codigo desde el centroide.",
    "compute_terrain": "Paso 3. Pendiente, orientacion, sombreado, curvas y horizonte solar. Sin parametros.",
    "compare_sources": "Paso 4 (obligatorio intentarlo). Compara con AW3D30. Si la fuente no esta disponible devuelve no_disponible: "
                       "no es un rechazo, pero score_confidence no corre hasta que este paso se haya intentado.",
    "score_confidence": "Paso 5. Aplica la regla de confianza A/B/C y escribe el manifiesto. Devuelve el nivel y si la revision humana es obligatoria.",
    "build_glb": "Paso 6. Malla 3D (GLB), predio y capas PNG con ventana de contexto. Parametro opcional: exageracion vertical 1-5 (siempre rotulada).",
    "render_pdf": "Paso 7. Informe PDF de 14 secciones. Todo el texto sale del manifiesto. Sin parametros.",
    "publish_viewer": "Paso 8. Copia el visor estatico y los datos a la carpeta 'publicado'. Sin parametros.",
    "reject_request": "Rechaza el pedido (queda registrado y detiene todo). Usar si el pedido no es un poligono valido en Chile "
                      "o no se puede cumplir con estas tools. Solo antes de render_pdf.",
    "request_human_review": "Registra una bandera visible de revision humana obligatoria con un motivo breve. "
                            "Obligatoria si score_confidence devolvio human_review_required=true.",
}


def build_sdk_tools(session: AgentSession) -> list:
    """Las diez tools como tools MCP en proceso del Claude Agent SDK (cada una delega en ``TOOLS``)."""
    from claude_agent_sdk import tool

    def wrap(name: str):
        @tool(name, DESCRIPTIONS[name], SCHEMAS[name])
        async def _t(args: dict) -> dict:
            out = TOOLS[name](session, args)
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                    "is_error": out.get("status") in ("entrada_invalida", "orden_invalido", "detenido", "error")}

        return _t

    return [wrap(n) for n in TOOL_NAMES]
