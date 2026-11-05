"""build_glb: malla GLB del terreno + predio.geojson + capas PNG para el visor (Task 9).

Contrato con el visor (binding):
* ``terreno.glb``: una malla en metros locales, origen en el centroide del poligono, glTF Y-up:
  x = este, y = (elevacion - elevation_min) * exageracion, z = -norte.
  UV: u = (x - xmin)/(xmax - xmin), v = 1 - (norte - ymin)/(ymax - ymin)  (convencion glTF, origen arriba-izquierda).
  ``extras`` (escena y malla): exaggeration, vertical_unit, datum, source, pixel_m, bbox_local_m [xmin, ymin, xmax, ymax]
  (este/norte, entre centros de celda), elevation_min, elevation_max, context_m (ventana de contexto pedida, None = todo el raster).
* ``predio.geojson``: Feature Polygon en las MISMAS coordenadas locales (x = este, y = norte).
* ``layers/{pendiente,orientacion,curvas,sombreado}.png`` (RGBA, cubren exactamente ``bbox_local_m``, fila 0 = norte) y
  ``layers/layers.json`` (disponibilidad de cada capa y motivo si no se dibuja).
Los vertices son las celdas reales de 30 m (sin suavizar ni interpolar: la suavidad aparente que el dato no tiene).
La malla y las capas cubren la ventana de contexto: bbox del predio +- ``context_m`` (1500 m por defecto) recortada al raster
recibido, para que el visor y el video tengan "panorama del sector" (spec sec. 6). Las estadisticas del informe NO salen de la
ventana de contexto sino del raster de trabajo (manifiesto).
"""
from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
import shapely
import trimesh
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon, mapping, shape
from trimesh.visual import TextureVisuals
from trimesh.visual.material import PBRMaterial

from terreno.asserts import (
    assert_crs,
    assert_meters,
    assert_nodata_below,
    assert_plausible_range,
    assert_polygon_inside,
    valid_mask,
)
from terreno.errors import RejectRequest
from terreno.geometry import centroid_zone
from terreno.overlays import (
    aspect_layer,
    contour_layer,
    hillshade_layer,
    layer_size,
    read_gpkg_lines,
    save_layer,
    slope_layer,
)
from terreno.reproject import MAX_NODATA_FRACTION, Raster, _assert_geographic_polygon
from terreno.terrain import TerrainResult, compute_terrain

DEFAULT_EXAGGERATION = 1.5
DEFAULT_CONTEXT_M = 1500.0  # ventana de contexto alrededor del bbox del predio
MAX_CONTEXT_M = 20_000.0
MAX_EXAGGERATION = 10.0
DEFAULT_MAX_VERTICES = 250_000
GLB_MAX_BYTES = 25 * 1024 * 1024  # limite por archivo de Cloudflare Pages
GLB_NAME = "terreno.glb"
PREDIO_NAME = "predio.geojson"
LAYER_NAMES = ("pendiente", "orientacion", "curvas", "sombreado")
LAYERS_INDEX = "layers.json"


@dataclass
class GlbInfo:
    path: Path
    vertices: int
    size_bytes: int
    exaggeration: float
    faces: int = 0
    decimation_step: float = 1.0  # filas originales / filas conservadas (1 = sin decimar)
    bbox_local_m: list = field(default_factory=list)
    elevation_min: float = 0.0
    elevation_max: float = 0.0
    layers: dict = field(default_factory=dict)
    predio_path: Path | None = None


def _check_exaggeration(e) -> float:
    if isinstance(e, bool) or not isinstance(e, (int, float)) or not math.isfinite(e) or not (0.0 < e <= MAX_EXAGGERATION):
        raise RejectRequest(f"exageracion vertical invalida: {e!r} (se exige un numero finito en (0, {MAX_EXAGGERATION:g}])")
    return float(e)


def _pick(n: int, m: int) -> np.ndarray:
    """m indices de 0..n-1 espaciados lo mas parejo posible (siempre incluye ambos bordes): celdas reales, sin promediar."""
    if m >= n:
        return np.arange(n)
    return np.unique(np.rint(np.linspace(0, n - 1, m)).astype(int))


def _budget_shape(n_rows: int, n_cols: int, budget: int) -> tuple[int, int]:
    """(filas, columnas) a conservar para no pasar de ``budget`` vertices, repartiendo el recorte en proporcion."""
    if n_rows * n_cols <= budget:
        return n_rows, n_cols
    if budget < 4:
        raise RejectRequest(f"max_vertices={budget} es demasiado bajo para una malla de al menos 2x2 vertices")
    s = math.sqrt(budget / (n_rows * n_cols))
    nr, nc = max(2, int(n_rows * s)), max(2, int(n_cols * s))
    while len(_pick(n_rows, nr)) * len(_pick(n_cols, nc)) > budget:
        if nr >= nc and nr > 2:
            nr -= 1
        elif nc > 2:
            nc -= 1
        else:
            raise RejectRequest(f"max_vertices={budget} es demasiado bajo para una malla de al menos 2x2 vertices")
    return nr, nc


def _vertex_normals(verts: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Normales por vertice (promedio ponderado por area) en numpy: trimesh cae a un bucle lentisimo sin scipy."""
    a, b, c = (verts[tri[:, i]] for i in range(3))
    fn = np.cross(b - a, c - a)
    acc = np.zeros_like(verts)
    for k in range(3):
        for col in range(3):
            acc[:, col] += np.bincount(tri[:, k], weights=fn[:, col], minlength=len(verts))
    ln = np.linalg.norm(acc, axis=1, keepdims=True)
    ln[ln == 0] = 1.0
    return acc / ln


def _grid_mesh(z_full, valid_full, east, north, cx, cy, zmin, exaggeration, shape_rc):
    rows, cols = _pick(len(north), shape_rc[0]), _pick(len(east), shape_rc[1])
    z = z_full[np.ix_(rows, cols)]
    ok = valid_full[np.ix_(rows, cols)]
    nr, nc = len(rows), len(cols)
    X = np.broadcast_to((east[cols] - cx)[None, :], (nr, nc))
    NN = np.broadcast_to((north[rows] - cy)[:, None], (nr, nc))
    Y = np.where(ok, (np.where(ok, z, zmin) - zmin) * exaggeration, 0.0)
    verts = np.column_stack([X.ravel(), Y.ravel(), (-NN).ravel()]).astype("float64")
    idx = np.arange(nr * nc).reshape(nr, nc)
    a, b, c, d = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    # CCW visto desde +y (x = este a la derecha, norte arriba): (a, c, b) y (b, c, d)
    tri = np.concatenate([np.stack([a, c, b], -1).reshape(-1, 3), np.stack([b, c, d], -1).reshape(-1, 3)])
    okf = ok.ravel()
    tri = tri[okf[tri].all(axis=1)]
    return verts, tri, okf


def _check_context(c) -> float | None:
    if c is None:
        return None
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or not (0.0 < c <= MAX_CONTEXT_M):
        raise RejectRequest(f"context_m invalido: {c!r} (se exige None o un numero finito en (0, {MAX_CONTEXT_M:g}] metros)")
    return float(c)


def _context_window(east, north, bounds_utm, context_m, pixel_m) -> tuple[slice, slice]:
    """(filas, columnas) cuyo centro cae a <= context_m (+ media celda) del bbox del predio; None = todo el raster."""
    if context_m is None:
        return slice(0, len(north)), slice(0, len(east))
    minx, miny, maxx, maxy = bounds_utm
    pad = context_m + pixel_m / 2.0
    cols = np.flatnonzero((east >= minx - pad) & (east <= maxx + pad))
    rows = np.flatnonzero((north >= miny - pad) & (north <= maxy + pad))
    if len(cols) < 2 or len(rows) < 2:
        raise RejectRequest("la ventana de contexto tiene menos de 2x2 celdas")
    return slice(int(rows[0]), int(rows[-1]) + 1), slice(int(cols[0]), int(cols[-1]) + 1)


def _extras(exaggeration, bbox, zmin, zmax, pixel_m, context_m=None) -> dict:
    return {
        "exaggeration": float(exaggeration), "vertical_unit": "m", "datum": "EGM2008", "source": "Copernicus GLO-30",
        "pixel_m": int(pixel_m) if float(pixel_m).is_integer() else float(pixel_m),
        "bbox_local_m": [round(float(v), 4) for v in bbox], "elevation_min": round(float(zmin), 4),
        "elevation_max": round(float(zmax), 4), "context_m": context_m,
    }


def _export(verts, tri, bbox, extras, texture: Path | None) -> tuple[bytes, int, int]:
    xmin, ymin, xmax, ymax = bbox
    keep = np.zeros(len(verts), dtype=bool)
    keep[tri.ravel()] = True
    remap = np.cumsum(keep) - 1
    v = verts[keep]
    f = remap[tri]
    north = -v[:, 2]
    u = (v[:, 0] - xmin) / (xmax - xmin)
    # trimesh invierte v al exportar (convencion OpenGL -> glTF): pasamos v_gl = (norte-ymin)/(ymax-ymin)
    vv = (north - ymin) / (ymax - ymin)
    if texture is not None:
        try:
            img = Image.open(texture)
            img.load()
        except Exception as exc:
            raise RejectRequest(f"no se puede abrir la textura {texture}: {exc}")
        material = PBRMaterial(baseColorTexture=img, metallicFactor=0.0, roughnessFactor=1.0, doubleSided=True)
    else:
        material = PBRMaterial(baseColorFactor=[200, 200, 200, 255], metallicFactor=0.0, roughnessFactor=1.0, doubleSided=True)
    mesh = trimesh.Trimesh(vertices=v, faces=f, process=False)
    mesh.visual = TextureVisuals(uv=np.column_stack([u, vv]), material=material)
    mesh.vertex_normals = _vertex_normals(v, f)  # se exportan como NORMAL
    mesh.metadata.clear()
    mesh.metadata.update(extras)
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="terreno", geom_name="terreno")
    scene.metadata.clear()
    scene.metadata.update(extras)
    return bytes(scene.export(file_type="glb")), len(v), len(f)


def _write_predio(poly_utm: Polygon, cx: float, cy: float, out: Path, epsg: int) -> Path:
    local = shapely.transform(poly_utm, lambda c: np.column_stack([c[:, 0] - cx, c[:, 1] - cy]))
    local = shapely.set_precision(local, 0.001)  # mm: salida estable
    feat = {
        "type": "Feature",
        "properties": {"units": "m", "x": "este", "y": "norte", "origin": "centroide del predio", "utm_epsg": epsg},
        "geometry": mapping(local),
    }
    path = out / PREDIO_NAME
    path.write_text(json.dumps(feat, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _read_float(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with rasterio.open(path) as src:
        a = src.read(1).astype("float32")
        nd = src.nodata
    return a, valid_mask(a, nd)


def build_glb(
    r: Raster,
    poly: Polygon,
    out: Path,
    exaggeration: float = DEFAULT_EXAGGERATION,
    max_vertices: int = DEFAULT_MAX_VERTICES,
    *,
    terrain: TerrainResult | None = None,
    omit_point_slope: bool = False,
    texture: Path | None = None,
    context_m: float | None = DEFAULT_CONTEXT_M,
) -> GlbInfo:
    """Escribe ``out/terreno.glb``, ``out/predio.geojson`` y ``out/layers/*.png`` (ver docstring del modulo).

    ``poly`` en EPSG:4326 (como en el resto del pipeline). ``terrain`` aporta pendiente/orientacion/sombreado/curvas ya
    calculados; si es None se calcula con compute_terrain. ``omit_point_slope`` (nivel C) deja en blanco pendiente y
    orientacion igual que un predio no concluyente. ``texture`` opcional (p. ej. Sentinel-2) va como color base.
    ``context_m``: malla y capas cubren el bbox del predio +- ``context_m`` recortado a ``r`` (None = todo ``r``); para que
    haya contexto, ``r`` debe ser mas grande que el raster de trabajo (el pipeline entrega un raster de contexto aparte).
    """
    exaggeration = _check_exaggeration(exaggeration)
    context_m = _check_context(context_m)
    if isinstance(max_vertices, bool) or not isinstance(max_vertices, int) or max_vertices < 4:
        raise RejectRequest(f"max_vertices invalido: {max_vertices!r}")
    if not isinstance(poly, Polygon) or poly.is_empty:
        raise RejectRequest("poligono invalido para build_glb")
    _assert_geographic_polygon(poly)
    epsg = centroid_zone(poly)
    assert_crs(r, epsg)
    assert_meters(r)
    assert_nodata_below(r, MAX_NODATA_FRACTION)
    assert_plausible_range(r)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    with rasterio.open(r.path) as src:
        z_full = src.read(1).astype("float32")
        nodata, t, bounds = src.nodata, src.transform, tuple(src.bounds)
    if z_full.shape[0] < 2 or z_full.shape[1] < 2:
        raise RejectRequest("el raster es demasiado pequeno para una malla (minimo 2x2 celdas)")
    valid_full = valid_mask(z_full, nodata)
    if not valid_full.any():
        raise RejectRequest("el raster no tiene celdas con dato")
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    assert_polygon_inside(poly_utm, bounds)
    cx, cy = poly_utm.centroid.x, poly_utm.centroid.y

    east = t.c + (np.arange(z_full.shape[1]) + 0.5) * t.a
    north = t.f + (np.arange(z_full.shape[0]) + 0.5) * t.e
    win = _context_window(east, north, poly_utm.bounds, context_m, r.pixel_m)  # (filas, columnas)
    z_full, valid_full, east, north = z_full[win], valid_full[win], east[win[1]], north[win[0]]
    n_rows, n_cols = z_full.shape
    if not valid_full.any():
        raise RejectRequest("la ventana de contexto no tiene celdas con dato")
    zmin, zmax = float(z_full[valid_full].min()), float(z_full[valid_full].max())
    bbox = [float(east[0] - cx), float(north[-1] - cy), float(east[-1] - cx), float(north[0] - cy)]
    extras = _extras(exaggeration, bbox, zmin, zmax, r.pixel_m, context_m)

    budget = max_vertices
    while True:
        shape_rc = _budget_shape(n_rows, n_cols, budget)
        verts, tri, _ = _grid_mesh(z_full, valid_full, east, north, cx, cy, zmin, exaggeration, shape_rc)
        if len(tri) == 0:
            raise RejectRequest("la malla no tiene caras validas (demasiado nodata)")
        blob, nv, nf = _export(verts, tri, bbox, extras, texture)
        if len(blob) <= GLB_MAX_BYTES:
            break
        budget = int(budget * 0.8)  # decimar mas hasta caber en 25 MiB (si no cabe, _budget_shape rechaza)
    step = round(max(n_rows / shape_rc[0], n_cols / shape_rc[1]), 3)
    glb_path = out / GLB_NAME
    glb_path.write_bytes(blob)
    predio_path = _write_predio(poly_utm, cx, cy, out, epsg)

    layers = _write_layers(r, poly, out / "layers", terrain, omit_point_slope, bbox, east, north, valid_full, win)
    return GlbInfo(
        path=glb_path, vertices=nv, size_bytes=len(blob), exaggeration=exaggeration, faces=nf, decimation_step=step,
        bbox_local_m=bbox, elevation_min=zmin, elevation_max=zmax, layers=layers, predio_path=predio_path,
    )


def _write_layers(r, poly, layer_dir: Path, terrain, omit_point_slope, bbox, east, north, valid_full, win) -> dict:
    layer_dir.mkdir(parents=True, exist_ok=True)
    if terrain is None:
        # sin TerrainResult previo: se calcula en un directorio temporal (los PNG ya contienen todo lo que hace falta)
        with tempfile.TemporaryDirectory() as td:
            terrain = compute_terrain(r, poly, out_dir=Path(td))
            return _write_layers(r, poly, layer_dir, terrain, omit_point_slope, bbox, east, north, valid_full, win)
    for p in (terrain.slope_path, terrain.aspect_path, terrain.hillshade_path, terrain.contours_path):
        if not Path(p).exists():
            raise RejectRequest(f"falta el archivo de trabajo del terreno: {p}")
    n_rows, n_cols = valid_full.shape
    w, h = layer_size(n_cols, n_rows)
    slope, s_ok = (a[win] for a in _read_float(terrain.slope_path))  # mismas filas/columnas que la malla
    aspect, a_ok = (a[win] for a in _read_float(terrain.aspect_path))
    hill, h_ok = (a[win] for a in _read_float(terrain.hillshade_path))
    if slope.shape != valid_full.shape:
        raise RejectRequest("las capas de terreno no tienen la misma malla que el raster del GLB")
    st = terrain.stats

    slope_reason = None
    if st.get("inconclusive"):
        slope_reason = "no_concluyente"
    elif omit_point_slope:
        slope_reason = "no_concluyente"  # nivel C: la spec omite las cifras de pendiente puntuales
    aspect_reason = slope_reason
    if aspect_reason is None and st.get("aspect_status") != "ok":
        aspect_reason = st.get("aspect_status") or "sin_orientacion_predominante"

    info: dict = {}
    save_layer(slope_layer(slope, s_ok, w, h, available=slope_reason is None), layer_dir / "pendiente.png")
    info["pendiente"] = {"available": slope_reason is None, "reason": slope_reason}
    save_layer(aspect_layer(aspect, slope, s_ok & a_ok, w, h, available=aspect_reason is None), layer_dir / "orientacion.png")
    info["orientacion"] = {"available": aspect_reason is None, "reason": aspect_reason}
    lines = read_gpkg_lines(terrain.contours_path)
    save_layer(contour_layer(lines, east_first=float(east[0]), east_last=float(east[-1]), north_first=float(north[0]),
                             north_last=float(north[-1]), w=w, h=h), layer_dir / "curvas.png")
    info["curvas"] = {"available": True, "reason": None, "interval_m": st.get("contour_interval_m"),
                      "n_lines": len(lines), "indicative": True}
    save_layer(hillshade_layer(hill, h_ok, w, h), layer_dir / "sombreado.png")
    info["sombreado"] = {"available": True, "reason": None}
    index = {
        "bbox_local_m": [round(float(v), 4) for v in bbox], "size_px": [w, h], "orientation": "fila 0 = norte; columna 0 = oeste",
        **info,
    }
    (layer_dir / LAYERS_INDEX).write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return info


def build_glb_hook(manifest: dict, out_dir: Path, *, exaggeration: float = DEFAULT_EXAGGERATION) -> Path:
    """Gancho de ``run_pipeline(hooks={"build_glb": ...})``: reconstruye todo desde el manifiesto (rutas de trabajo).

    Si el pipeline dejo un raster de contexto (``volatile.paths.context_raster_utm``) la malla y las capas se construyen
    sobre el (bbox del predio +- 1500 m) con pendiente/orientacion/sombreado/curvas calculados sobre ese mismo raster; las
    estadisticas (inconcluso, orientacion, equidistancia) siguen siendo las del manifiesto, del raster de trabajo.
    """
    paths = manifest.get("volatile", {}).get("paths", {})
    need = ("raster_utm", "slope", "aspect", "hillshade", "contours")
    missing = [k for k in need if not paths.get(k) or not Path(paths[k]).exists()]
    if missing:
        raise RejectRequest(f"faltan archivos de trabajo del manifiesto para construir el GLB (raster/capas): {missing}")
    poly = shape(manifest["inputs"]["polygon"])
    epsg, pixel_m = int(manifest["grid"]["epsg"]), float(manifest["grid"]["pixel_m"])
    stats = manifest["results"]["terrain"]
    omit = bool(manifest["confidence"].get("omit_point_slope"))
    ctx = paths.get("context_raster_utm")
    if ctx and Path(ctx).exists():
        r = Raster(path=Path(ctx), epsg=epsg, pixel_m=pixel_m, nodata=float("nan"))
        ctx_terrain = compute_terrain(r, poly, out_dir=Path(out_dir) / "work" / "context")
        terrain = TerrainResult(slope_path=ctx_terrain.slope_path, aspect_path=ctx_terrain.aspect_path,
                                hillshade_path=ctx_terrain.hillshade_path, contours_path=ctx_terrain.contours_path, stats=stats)
        context_m = manifest.get("volatile", {}).get("context", {}).get("requested_m") or DEFAULT_CONTEXT_M
    else:
        r = Raster(path=Path(paths["raster_utm"]), epsg=epsg, pixel_m=pixel_m, nodata=float("nan"))
        terrain = TerrainResult(slope_path=Path(paths["slope"]), aspect_path=Path(paths["aspect"]),
                                hillshade_path=Path(paths["hillshade"]), contours_path=Path(paths["contours"]), stats=stats)
        context_m = None  # sin raster de contexto: ventana de trabajo (bbox del predio +- 3 pixeles)
    info = build_glb(r, poly, Path(out_dir), exaggeration, terrain=terrain, omit_point_slope=omit, context_m=context_m)
    return info.path
