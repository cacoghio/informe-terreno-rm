import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np
import pytest
import rasterio
import trimesh
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape

from terreno.errors import RejectRequest
from terreno.glb import DEFAULT_EXAGGERATION, GLB_MAX_BYTES, GlbInfo, build_glb, build_glb_hook
from terreno.manifest import read_manifest, result_hash
from terreno.palette import ASPECT_COLORS, SLOPE_COLORS
from terreno.pipeline import run_pipeline
from tests.fixtures.rasters import utm_ramp, write_raster
from terreno.fetch import DemResult
from tests.test_pipeline import LAT0, LON0, POLY, TINY, aux_ok, fetchers, ramp_fn, SHIFT
from tests.test_reproject import make_dem
from tests.test_terrain import LEFT, N, RES, TOP, ll_poly, ramp

MiB = 1024 * 1024


# ---------- lector independiente de GLB (no usa trimesh): verifica el contrato byte a byte ----------

def parse_glb(path):
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack("<4sII", data[:12])
    assert magic == b"glTF" and version == 2 and length == len(data)
    off, js, binc = 12, None, b""
    while off < length:
        clen, ctype = struct.unpack("<I4s", data[off:off + 8])
        chunk = data[off + 8:off + 8 + clen]
        if ctype == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == b"BIN\x00":
            binc = chunk
        off += 8 + clen
    return js, binc


_CT = {5126: "<f4", 5125: "<u4", 5123: "<u2", 5121: "u1"}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}


def accessor(gltf, binc, idx):
    acc = gltf["accessors"][idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    dt, nc = np.dtype(_CT[acc["componentType"]]), _NC[acc["type"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or dt.itemsize * nc
    assert stride == dt.itemsize * nc  # sin intercalado
    a = np.frombuffer(binc, dtype=dt, count=acc["count"] * nc, offset=start)
    return a.reshape(acc["count"], nc) if nc > 1 else a


def mesh_arrays(path):
    gltf, binc = parse_glb(path)
    assert len(gltf["meshes"]) == 1 and len(gltf["meshes"][0]["primitives"]) == 1
    prim = gltf["meshes"][0]["primitives"][0]
    pos = accessor(gltf, binc, prim["attributes"]["POSITION"])
    uv = accessor(gltf, binc, prim["attributes"]["TEXCOORD_0"])
    idx = accessor(gltf, binc, prim["indices"])
    return gltf, pos.astype("float64"), uv.astype("float64"), idx.reshape(-1, 3)


def data_digest(path) -> str:
    """Hash de los DATOS del GLB (posiciones, uv, indices, extras), no de los bytes del archivo."""
    gltf, pos, uv, idx = mesh_arrays(path)
    h = hashlib.sha256()
    for a in (pos, uv, idx.astype("int64")):
        h.update(np.ascontiguousarray(a).tobytes())
    h.update(json.dumps(gltf["scenes"][0].get("extras"), sort_keys=True).encode())
    return h.hexdigest()


def png_digest(path) -> str:
    a = np.asarray(Image.open(path).convert("RGBA"))
    return hashlib.sha256(a.tobytes() + str(a.shape).encode()).hexdigest()


# ---------- construccion ----------

def make(tmp_path, n=N, name="o", **kw):
    r = utm_ramp(tmp_path / "dem.tif", n=n, res=RES, left=LEFT, top=TOP, slope_x=kw.pop("slope_x", 0.1),
                 slope_y=kw.pop("slope_y", 0.05), base=600.0)
    poly = kw.pop("poly", None) or ll_poly(n // 2 - 3, n // 2 - 3, n // 2 + 3, n // 2 + 3)
    return r, poly, tmp_path / name


def test_glb_loads_with_trimesh(tmp_path):
    r, poly, out = make(tmp_path)
    info = build_glb(r, poly, out)
    assert isinstance(info, GlbInfo) and info.path == out / "terreno.glb" and info.path.exists()
    sc = trimesh.load(info.path)
    geoms = list(sc.geometry.values())
    assert len(geoms) == 1
    m = geoms[0]
    assert len(m.vertices) == info.vertices == N * N and len(m.faces) == 2 * (N - 1) ** 2
    assert info.size_bytes == info.path.stat().st_size
    assert info.exaggeration == DEFAULT_EXAGGERATION == 1.5


def test_glb_vertex_count_within_limit(tmp_path):
    r, poly, out = make(tmp_path, n=120)  # 14.400 vertices
    full = build_glb(r, poly, out / "a", context_m=None)
    assert full.vertices == 120 * 120
    small = build_glb(r, poly, out / "b", max_vertices=3000, context_m=None)
    assert small.vertices <= 3000 and small.vertices < full.vertices and small.decimation_step > 1
    _, pos, uv, idx = mesh_arrays(small.path)
    assert len(pos) == small.vertices
    # la decimacion no cambia la extension: las capas PNG y las UV siguen alineadas
    assert small.bbox_local_m == pytest.approx(full.bbox_local_m)
    assert idx.max() < len(pos)


def test_glb_size_under_25MiB(tmp_path):
    r, poly, out = make(tmp_path, n=520, slope_x=0.02, slope_y=0.02)  # 270.400 vertices -> se decima a <= 250.000
    info = build_glb(r, poly, out, context_m=None)  # ráster completo: 270.400 celdas
    assert 200_000 < info.vertices <= 250_000  # aprovecha el presupuesto, no decima de mas
    assert info.size_bytes < 25 * MiB == GLB_MAX_BYTES
    gltf = parse_glb(info.path)[0]
    assert "NORMAL" in gltf["meshes"][0]["primitives"][0]["attributes"]
    print(f"GLB de {info.vertices} vertices: {info.size_bytes / MiB:.2f} MiB")


def test_exaggeration_stored_in_extras(tmp_path):
    r, poly, out = make(tmp_path)
    build_glb(r, poly, out, exaggeration=2.5)
    gltf, _ = parse_glb(out / "terreno.glb")
    ex = gltf["scenes"][0]["extras"]
    assert ex["exaggeration"] == 2.5 and ex["vertical_unit"] == "m" and ex["datum"] == "EGM2008"
    assert ex["source"] == "Copernicus GLO-30" and ex["pixel_m"] == 30
    assert len(ex["bbox_local_m"]) == 4 and ex["elevation_min"] < ex["elevation_max"]
    assert gltf["meshes"][0]["extras"]["exaggeration"] == 2.5  # tambien en la malla


def test_heights_scale_with_exaggeration(tmp_path):
    r, poly, out = make(tmp_path)
    build_glb(r, poly, out / "a", exaggeration=1.0)
    build_glb(r, poly, out / "b", exaggeration=3.0)
    ya = mesh_arrays(out / "a" / "terreno.glb")[1][:, 1]
    yb = mesh_arrays(out / "b" / "terreno.glb")[1][:, 1]
    assert ya.min() == pytest.approx(0.0, abs=1e-4) and yb.min() == pytest.approx(0.0, abs=1e-4)
    assert yb.max() == pytest.approx(3.0 * ya.max(), rel=1e-5)
    ex = parse_glb(out / "a" / "terreno.glb")[0]["scenes"][0]["extras"]
    assert ya.max() == pytest.approx(ex["elevation_max"] - ex["elevation_min"], rel=1e-5)
    # x, z no dependen de la exageracion
    assert np.allclose(mesh_arrays(out / "a" / "terreno.glb")[1][:, [0, 2]], mesh_arrays(out / "b" / "terreno.glb")[1][:, [0, 2]])


def test_axes_origin_and_uv_contract(tmp_path):
    # pico aislado en una celda conocida: fija x = este, z = -norte y las UV
    n = 12
    z = np.full((n, n), 100.0)
    z[2, 9] = 200.0  # fila 2 (hacia el norte), columna 9 (hacia el este)
    r = write_raster(tmp_path / "spike.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    poly = ll_poly(4, 4, 8, 8)
    out = tmp_path / "o"
    info = build_glb(r, poly, out, exaggeration=2.0)
    gltf, pos, uv, idx = mesh_arrays(out / "terreno.glb")
    to_utm = Transformer.from_crs(4326, 32719, always_xy=True)
    cx, cy = shape({"type": "Polygon", "coordinates": [list(poly.exterior.coords)]}).centroid.coords[0]
    cxu, cyu = to_utm.transform(cx, cy)
    k = int(np.argmax(pos[:, 1]))
    east = LEFT + 9.5 * RES
    north = TOP - 2.5 * RES
    assert pos[k, 0] == pytest.approx(east - cxu, abs=1e-2)  # x = este
    assert pos[k, 2] == pytest.approx(-(north - cyu), abs=1e-2)  # z = -norte
    assert pos[k, 1] == pytest.approx(100.0 * 2.0, abs=1e-3)  # (200 - 100) * 2
    xmin, ymin, xmax, ymax = info.bbox_local_m
    assert gltf["scenes"][0]["extras"]["bbox_local_m"] == pytest.approx([xmin, ymin, xmax, ymax])
    assert xmin == pytest.approx(LEFT + 0.5 * RES - cxu, abs=1e-2) and xmax == pytest.approx(LEFT + 11.5 * RES - cxu, abs=1e-2)
    assert ymax == pytest.approx(TOP - 0.5 * RES - cyu, abs=1e-2) and ymin == pytest.approx(TOP - 11.5 * RES - cyu, abs=1e-2)
    # UV exactas del contrato: u=(x-xmin)/(xmax-xmin), v=1-(norte-ymin)/(ymax-ymin)
    nrt = -pos[:, 2]
    assert np.allclose(uv[:, 0], (pos[:, 0] - xmin) / (xmax - xmin), atol=1e-5)
    assert np.allclose(uv[:, 1], 1.0 - (nrt - ymin) / (ymax - ymin), atol=1e-5)
    assert 0.0 <= uv.min() and uv.max() <= 1.0
    # el vertice mas al norte-oeste tiene (u, v) = (0, 0); el sur-este (1, 1)
    nw = int(np.argmin(pos[:, 0] + pos[:, 2]))
    assert uv[nw] == pytest.approx([0.0, 0.0], abs=1e-6)
    # caras hacia arriba (+y): normales con componente y positiva
    a, b, c = (pos[idx[:, i]] for i in range(3))
    normals = np.cross(b - a, c - a)
    assert (normals[:, 1] >= 0).all() and (normals[:, 1] > 0).any()


def test_predio_geojson_in_local_metres(tmp_path):
    r, poly, out = make(tmp_path)
    info = build_glb(r, poly, out)
    gj = json.loads((out / "predio.geojson").read_text(encoding="utf-8"))
    assert gj["type"] == "Feature" and gj["geometry"]["type"] == "Polygon"
    ring = np.array(gj["geometry"]["coordinates"][0])
    assert np.allclose(ring[0], ring[-1])
    # el predio mide ~ 6 x 6 pixeles de 30 m = 180 m: en metros, no en grados
    assert np.ptp(ring[:, 0]) == pytest.approx(180.0, abs=2.0) and np.ptp(ring[:, 1]) == pytest.approx(180.0, abs=2.0)
    # centrado en el origen (el origen es el centroide del predio) y dentro de la extension de la malla
    assert abs(ring[:-1, 0].mean()) < 1.0 and abs(ring[:-1, 1].mean()) < 1.0
    xmin, ymin, xmax, ymax = info.bbox_local_m
    assert ring[:, 0].min() >= xmin and ring[:, 0].max() <= xmax and ring[:, 1].min() >= ymin and ring[:, 1].max() <= ymax


# ---------- capas PNG ----------

def png(out, name):
    return np.asarray(Image.open(out / "layers" / f"{name}.png").convert("RGBA"))


def test_layers_exist_rgba_and_same_aspect_as_extent(tmp_path):
    r, poly, out = make(tmp_path)
    info = build_glb(r, poly, out)
    xmin, ymin, xmax, ymax = info.bbox_local_m
    for name in ("pendiente", "orientacion", "curvas", "sombreado"):
        im = Image.open(out / "layers" / f"{name}.png")
        assert im.mode == "RGBA", name
        assert im.width / im.height == pytest.approx((xmax - xmin) / (ymax - ymin), rel=0.02), name
    # sombreado: gris y opaco; curvas: casi todo transparente con lineas finas
    s = png(out, "sombreado")
    assert (s[..., 0] == s[..., 1]).all() and (s[..., 1] == s[..., 2]).all() and (s[..., 3] > 0).mean() > 0.9
    c = png(out, "curvas")
    assert 0.0 < (c[..., 3] > 0).mean() < 0.15


def test_slope_layer_classes_and_north_up_orientation(tmp_path):
    # mitad norte (filas 0-9) con 50 % de pendiente en x; mitad sur plana: arriba de la imagen = norte
    n = 20
    xs = LEFT + (np.arange(n) + 0.5) * RES
    z = np.tile(600.0 + 0.5 * (xs - LEFT), (n, 1))
    z[10:, :] = 600.0
    r = write_raster(tmp_path / "d.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    out = tmp_path / "o"
    build_glb(r, ll_poly(6, 6, 14, 14), out)
    p = png(out, "pendiente")
    top_px = p[p.shape[0] // 8, p.shape[1] // 2]
    bot_px = p[7 * p.shape[0] // 8, p.shape[1] // 2]
    assert tuple(top_px[:3]) == SLOPE_COLORS[">30"] and top_px[3] > 0  # norte, 50 % -> >30 %
    assert tuple(bot_px[:3]) == SLOPE_COLORS["<5"] and bot_px[3] > 0  # sur, plano
    # orientacion: la pendiente baja hacia el oeste (z crece al este) -> sector O en el norte; plano -> transparente
    o = png(out, "orientacion")
    assert tuple(o[o.shape[0] // 8, o.shape[1] // 2][:3]) == ASPECT_COLORS["O"]
    assert o[7 * o.shape[0] // 8, o.shape[1] // 2][3] == 0


def test_inconclusive_site_has_no_slope_or_aspect_layer(tmp_path):
    out = tmp_path / "o"
    r = run_pipeline(TINY, out, fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "c",
                     hooks={"build_glb": build_glb_hook})
    assert r.terrain.stats["inconclusive"] is True
    for name in ("pendiente", "orientacion"):
        assert (png(out, name)[..., 3] == 0).all(), name  # nunca se inventan
    layers = json.loads((out / "layers" / "layers.json").read_text(encoding="utf-8"))
    assert layers["pendiente"]["available"] is False and layers["orientacion"]["available"] is False
    assert layers["pendiente"]["reason"] == "no_concluyente"
    assert layers["sombreado"]["available"] is True and layers["curvas"]["available"] is True
    # el sombreado y el terreno si se entregan
    assert (out / "terreno.glb").exists() and (out / "predio.geojson").exists()


def test_flat_site_has_no_orientation_layer(tmp_path):
    z = np.full((N, N), 600.0)
    z[:, 0::2] += 0.5  # rugosidad < 3 % de pendiente: sin orientacion predominante
    r = write_raster(tmp_path / "d.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    out = tmp_path / "o"
    build_glb(r, ll_poly(5, 5, 15, 15), out)
    assert (png(out, "orientacion")[..., 3] == 0).all()
    layers = json.loads((out / "layers" / "layers.json").read_text(encoding="utf-8"))
    assert layers["orientacion"]["available"] is False and layers["orientacion"]["reason"] == "sin_orientacion_predominante"


def test_omit_point_slope_level_C_skips_slope_layers(tmp_path):
    r, poly, out = make(tmp_path)
    build_glb(r, poly, out, omit_point_slope=True)
    assert (png(out, "pendiente")[..., 3] == 0).all() and (png(out, "orientacion")[..., 3] == 0).all()


# ---------- rechazos ----------

@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), 11.0])
def test_bad_exaggeration_rejected(tmp_path, bad):
    r, poly, out = make(tmp_path)
    with pytest.raises(RejectRequest, match="exageraci"):
        build_glb(r, poly, out, exaggeration=bad)
    assert not (out / "terreno.glb").exists()


def test_wrong_crs_polygon_outside_and_tiny_vertex_budget_rejected(tmp_path):
    r, poly, out = make(tmp_path)
    with pytest.raises(RejectRequest):
        build_glb(r, ll_poly(40, 40, 50, 50), out)  # poligono fuera del raster
    with pytest.raises(RejectRequest, match="max_vertices"):
        build_glb(r, poly, out, max_vertices=3)
    r32718 = utm_ramp(tmp_path / "d2.tif", epsg=32718, n=N, res=RES, left=LEFT, top=TOP)
    with pytest.raises(RejectRequest):
        build_glb(r32718, poly, out)  # el raster debe estar en la zona del centroide


def test_nodata_holes_do_not_create_faces(tmp_path):
    z = np.full((N, N), 600.0) + np.arange(N)[None, :] * 3.0
    z[8:11, 8:11] = np.nan
    r = write_raster(tmp_path / "d.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    out = tmp_path / "o"
    info = build_glb(r, ll_poly(4, 4, 16, 16), out)
    _, pos, uv, idx = mesh_arrays(out / "terreno.glb")
    assert info.vertices == len(pos) == N * N - 9
    assert np.isfinite(pos).all() and idx.max() < len(pos)


# ---------- integracion con el pipeline y determinismo ----------

def run_with_glb(tmp_path, name):
    return run_pipeline(POLY, tmp_path / name, fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                        cache_dir=tmp_path / f"c_{name}", hooks={"build_glb": build_glb_hook})


def test_hook_produces_contract_files_from_manifest_only(tmp_path):
    r = run_with_glb(tmp_path, "a")
    out = tmp_path / "a"
    assert r.outputs["build_glb"] == out / "terreno.glb"
    for f in ("terreno.glb", "predio.geojson", "manifest.json", "layers/pendiente.png", "layers/orientacion.png",
              "layers/curvas.png", "layers/sombreado.png"):
        assert (out / f).exists(), f
    # la exageracion y el origen son los del contrato; el hook no cambio el manifiesto ni su hash
    assert parse_glb(out / "terreno.glb")[0]["scenes"][0]["extras"]["exaggeration"] == 1.5
    assert result_hash(out) == r.result_hash == read_manifest(out)["result_hash"]


def test_two_runs_same_result_hash_and_same_glb_and_layer_data(tmp_path):
    a, b = run_with_glb(tmp_path, "a"), run_with_glb(tmp_path, "b")
    assert a.result_hash == b.result_hash == result_hash(tmp_path / "a") == result_hash(tmp_path / "b")
    assert data_digest(tmp_path / "a" / "terreno.glb") == data_digest(tmp_path / "b" / "terreno.glb")
    for n in ("pendiente", "orientacion", "curvas", "sombreado"):
        assert png_digest(tmp_path / "a" / "layers" / f"{n}.png") == png_digest(tmp_path / "b" / "layers" / f"{n}.png"), n
    assert (tmp_path / "a" / "predio.geojson").read_text() == (tmp_path / "b" / "predio.geojson").read_text()


def test_hook_rejects_when_work_files_are_gone(tmp_path):
    r = run_with_glb(tmp_path, "a")
    m = json.loads(json.dumps(r.manifest))
    m["volatile"]["paths"]["raster_utm"] = str(tmp_path / "no_existe.tif")
    with pytest.raises(RejectRequest, match="raster"):
        build_glb_hook(m, tmp_path / "x")


# ---------- ventana de contexto (Task 13, gap 1) ----------

def predio_bbox(out):
    ring = np.array(json.loads((out / "predio.geojson").read_text(encoding="utf-8"))["geometry"]["coordinates"][0])
    return ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max()


def test_glb_bbox_contains_polygon_with_at_least_1000_m_margin(tmp_path):
    n = 200  # 6 km x 6 km: el ráster permite 1500 m a cada lado de un predio de 180 m
    r, poly, out = make(tmp_path, n=n, slope_x=0.02, slope_y=0.01)
    info = build_glb(r, poly, out)
    xmin, ymin, xmax, ymax = info.bbox_local_m
    px0, py0, px1, py1 = predio_bbox(out)
    for margin in (px0 - xmin, xmax - px1, py0 - ymin, ymax - py1):
        assert 1000.0 <= margin <= 1500.0 + 30.0, margin  # contexto por defecto: bbox del predio +- 1500 m (celdas enteras)
    assert info.vertices < n * n  # el ráster de 200x200 se recorta a la ventana
    ex = parse_glb(info.path)[0]["scenes"][0]["extras"]
    assert ex["bbox_local_m"] == pytest.approx(info.bbox_local_m)
    assert ex["context_m"] == 1500.0


def test_context_window_is_clipped_to_the_raster(tmp_path):
    n = 200
    r = utm_ramp(tmp_path / "dem.tif", n=n, res=RES, left=LEFT, top=TOP, slope_x=0.02, base=600.0)
    poly = ll_poly(20, 100, 26, 106)  # cerca del borde oeste: sólo 600 m de ráster a ese lado
    out = tmp_path / "o"
    info = build_glb(r, poly, out)
    xmin, ymin, xmax, ymax = info.bbox_local_m
    px0, _, px1, _ = predio_bbox(out)
    assert px0 - xmin == pytest.approx(20 * RES - RES / 2, abs=1.0)  # llega hasta el centro de la 1a celda del raster
    assert xmax - px1 >= 1000.0  # al este sí hay contexto completo


def test_context_none_keeps_the_whole_raster_and_bad_values_rejected(tmp_path):
    r, poly, out = make(tmp_path, n=100)
    assert build_glb(r, poly, out / "a", context_m=None).vertices == 100 * 100
    assert parse_glb(out / "a" / "terreno.glb")[0]["scenes"][0]["extras"]["context_m"] is None
    for bad in (0, -5.0, float("nan"), float("inf"), True, "1500"):
        with pytest.raises(RejectRequest, match="context_m"):
            build_glb(r, poly, out / "b", context_m=bad)


def test_context_layers_cover_exactly_the_cropped_bbox(tmp_path):
    # mitad norte con 50 % de pendiente, mitad sur plana; la ventana recortada debe seguir alineada (fila 0 = norte)
    n = 150
    xs = LEFT + (np.arange(n) + 0.5) * RES
    z = np.tile(600.0 + 0.5 * (xs - LEFT), (n, 1))
    z[75:, :] = 600.0
    r = write_raster(tmp_path / "d.tif", z, epsg=32719, res=RES, left=LEFT, top=TOP)
    poly = ll_poly(70, 72, 80, 78)  # el predio cruza el límite norte/sur
    out = tmp_path / "o"
    info = build_glb(r, poly, out, context_m=600.0)  # 20 celdas de contexto
    xmin, ymin, xmax, ymax = info.bbox_local_m
    assert (xmax - xmin) < n * RES / 2 and info.vertices < n * n
    p = png(out, "pendiente")
    assert p.shape[1] / p.shape[0] == pytest.approx((xmax - xmin) / (ymax - ymin), rel=0.02)
    first_row = 72 - 20  # primera fila del raster dentro de la ventana (centros a <= 600 m + media celda del bbox del predio)
    rows = (78 + 20) - first_row
    y_of = lambda row: int((row - first_row) / (rows - 1) * (p.shape[0] - 1))  # fila 0 de la imagen = norte
    assert tuple(p[y_of(65), p.shape[1] // 2][:3]) == SLOPE_COLORS[">30"]  # norte del límite
    assert tuple(p[y_of(85), p.shape[1] // 2][:3]) == SLOPE_COLORS["<5"]  # sur del límite
    ex = parse_glb(info.path)[0]["scenes"][0]["extras"]
    assert ex["context_m"] == 600.0


def test_hook_uses_context_raster_when_present(tmp_path):
    out = tmp_path / "a"
    r = run_pipeline(POLY, out, fetch=big_fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT, cache_dir=tmp_path / "c",
                     hooks={"build_glb": build_glb_hook})
    info = parse_glb(out / "terreno.glb")[0]["scenes"][0]["extras"]
    xmin, ymin, xmax, ymax = info["bbox_local_m"]
    px0, py0, px1, py1 = predio_bbox(out)
    for margin in (px0 - xmin, xmax - px1, py0 - ymin, ymax - py1):
        assert 1000.0 <= margin <= 1560.0, margin
    assert info["context_m"] == 1500.0
    # el contexto no toca los números del informe: mismo hash que sin contexto
    r2 = run_pipeline(POLY, tmp_path / "b", fetch=big_fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                      cache_dir=tmp_path / "c2", context_m=None, hooks={"build_glb": build_glb_hook})
    assert r.result_hash == r2.result_hash
    ex2 = parse_glb(tmp_path / "b" / "terreno.glb")[0]["scenes"][0]["extras"]
    bb2 = ex2["bbox_local_m"]
    assert (bb2[2] - bb2[0]) < (xmax - xmin)  # sin contexto: sólo bbox del predio +- 3 píxeles
    # pendiente/orientación del predio siguen las del manifiesto (no las de la ventana grande)
    assert r.manifest["results"]["terrain"] == r2.manifest["results"]["terrain"]


def big_fetchers(tmp_path):
    """DEM sintético de ~6 km (200 arcseg) para que el ráster permita 1500 m de contexto."""
    f = fetchers(tmp_path)

    def glo(poly, cache_dir):
        d = make_dem(cache_dir, LON0, LAT0, n=200, fn=ramp_fn(0.0), name="glo.tif")
        return DemResult(path=d.path, tiles=["Copernicus_DSM_COG_10_S34_00_W071_00_DEM"], source_url="https://x", acquired="2026-10-23",
                         nodata_fraction=0.0, tile_urls=["https://example/t.tif"], source="glo30")

    return {**f, "glo30": glo}
