"""Genera carpetas de salida de MUESTRA (sinteticas) con el layout exacto del contrato visor <-> build_glb.

    poc/.venv/Scripts/python poc/viewer/sample/make_sample.py [out_root]

Cada carpeta contiene terreno.glb, predio.geojson, layers/{pendiente,orientacion,curvas,sombreado}.png y manifest.json.
El manifest.json NO se inventa: sale de ``terreno.pipeline.run_pipeline`` (Tasks 2-8) con un DEM sintetico inyectado
(sin red); solo se le agrega la clave opcional ``sample_notice`` para que el visor rotule que es una muestra sintetica.
La malla/capas usan una ventana de contexto mayor que el predio (el panorama del visor), calculada con la misma funcion
analitica del DEM sintetico.

Sitios:
  conclusivo    ladera ~ 3 km, predio de ~ 400 m (100 px de 30 m) -> nivel B, capas completas.
  inconclusivo  lote urbano chico (~ 6 px) -> nivel C, sin capa de pendiente y orientacion transparente.
"""
from __future__ import annotations

import json
import warnings
import shutil
import sys
from pathlib import Path

import numpy as np
import rasterio
import trimesh
from PIL import Image, ImageDraw
from pyproj import Transformer
from rasterio.transform import Affine
from shapely.geometry import box, mapping
from shapely.ops import transform as shp_transform

warnings.filterwarnings("ignore", category=DeprecationWarning)
HERE = Path(__file__).resolve().parent
POC = HERE.parents[1]
sys.path.insert(0, str(POC))

from terreno.fetch import DemResult  # noqa: E402
from terreno.layers import AuxStats  # noqa: E402
from terreno.pipeline import run_pipeline  # noqa: E402

LEGEND = json.loads((HERE.parent / "legend.json").read_text(encoding="utf-8"))
PIXEL_M = 30.0
RES_DEG = 1.0 / 3600.0


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


# ---------------------------------------------------------------- DEM sintetico (funcion analitica en metros locales)
def _field(e, n, *, seed):
    rng = np.random.default_rng(seed)
    z = np.zeros_like(e, dtype="float64")
    for _ in range(6):
        k = rng.uniform(2 * np.pi / 1500, 2 * np.pi / 420)
        th = rng.uniform(0, 2 * np.pi)
        z += rng.uniform(3, 9) * np.sin(k * (e * np.cos(th) + n * np.sin(th)) + rng.uniform(0, 2 * np.pi))
    return z


def dem_ladera(e, n):
    """Ladera de cordillera: sube al NE, un espolon y una quebrada suave; ~ 25-45 % de pendiente en el predio."""
    spur = 120.0 * np.exp(-(((e - 650.0) / 700.0) ** 2 + ((n - 520.0) / 800.0) ** 2))
    ramp = 0.10 * n + 0.04 * e
    gully = -22.0 * np.exp(-((e + 150.0 - 0.25 * n) / 120.0) ** 2)
    return 812.0 + ramp + spur + gully + _field(e, n, seed=11)


def dem_urbano(e, n):
    return 605.0 + 0.010 * n - 0.006 * e + 0.8 * np.sin(e / 190.0) * np.cos(n / 240.0) + 0.5 * _field(e, n, seed=5) / 6.0


SITES = {
    "conclusivo": dict(
        nombre="Ladera sintética (muestra)", lonlat=(-70.5000, -33.3800), half_deg=0.0018, ctx_m=3000.0, fn=dem_ladera,
    ),
    "inconclusivo": dict(
        nombre="Lote urbano sintético (muestra)", lonlat=(-70.6000, -33.4500), half_deg=0.00028, ctx_m=1500.0, fn=dem_urbano,
    ),
}


# ---------------------------------------------------------------- pipeline real con DEM sintetico inyectado
def _write_dem_4326(path: Path, lon0, lat0, fn, to_local, n=150):
    left, top = lon0 - n / 2 * RES_DEG, lat0 + n / 2 * RES_DEG
    cx = left + (np.arange(n) + 0.5) * RES_DEG
    cy = top - (np.arange(n) + 0.5) * RES_DEG
    lon, lat = np.meshgrid(cx, cy)
    e, nn = to_local(lon, lat)
    arr = fn(e, nn).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32", crs="EPSG:4326",
                       transform=Affine(RES_DEG, 0, left, 0, -RES_DEG, top), nodata=float("nan")) as dst:
        dst.write(arr, 1)


def run_synthetic_pipeline(spec: dict, work: Path):
    lon0, lat0 = spec["lonlat"]
    h = spec["half_deg"]
    poly = box(lon0 - h, lat0 - h * 1.15, lon0 + h, lat0 + h * 1.15)
    epsg = 32719 if lon0 >= -72 else 32718
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    c_utm = shp_transform(fwd.transform, poly).centroid
    ox, oy = c_utm.x, c_utm.y

    def to_local(lon, lat):
        x, y = fwd.transform(lon, lat)
        return x - ox, y - oy

    def fetch_glo(p, cache_dir):
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "dem_sintetico.tif"
        _write_dem_4326(path, lon0, lat0, spec["fn"], to_local)
        return DemResult(path=path, tiles=["sintetico"], source_url="https://copernicus-dem-30m.s3.amazonaws.com",
                         acquired="2026-10-23", nodata_fraction=0.0, tile_urls=["sintetico"], source="glo30")

    def no_aux(p, dem):
        return AuxStats(n_pixels=0, void_water_fill_pct=0.0, hem_p90_m=1.0, available=True, note="sintetico")

    res = run_pipeline(poly, work, sources=("glo30",), fetch={"glo30": fetch_glo}, fetch_aux=no_aux,
                       cache_dir=work / "cache", now=lambda: "2026-10-23T00:00:00+00:00")
    return res, poly, (ox, oy, epsg, fwd)


# ---------------------------------------------------------------- capas (numpy + Pillow)
def horn(z, cell):
    zp = np.pad(z, 1, mode="edge")
    a, b, c = zp[:-2, :-2], zp[:-2, 1:-1], zp[:-2, 2:]
    d, f = zp[1:-1, :-2], zp[1:-1, 2:]
    g, h_, i = zp[2:, :-2], zp[2:, 1:-1], zp[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * cell)  # este
    dzdy = ((a + 2 * b + c) - (g + 2 * h_ + i)) / (8 * cell)  # norte (fila 0 = norte)
    return dzdx, dzdy


def build_layers(z30: np.ndarray, out: Path, *, with_slope: bool, with_aspect_visible: bool):
    ny, nx = z30.shape
    up = int(min(8, 1400 // max(nx, ny)))
    W, H = nx * up, ny * up
    zf = np.asarray(Image.fromarray(z30.astype("float32"), "F").resize((W, H), Image.BICUBIC), dtype="float64")
    cell = PIXEL_M / up
    dzdx, dzdy = horn(zf, cell)
    slope = np.hypot(dzdx, dzdy) * 100.0
    layers = out / "layers"
    layers.mkdir(parents=True, exist_ok=True)

    if with_slope:
        cols = [_hex(c) for c in LEGEND["pendiente"]["colores"]]
        cls = np.digitize(slope, [5, 15, 30])
        rgba = np.zeros((H, W, 4), "uint8")
        for k, col in enumerate(cols):
            rgba[cls == k] = (*col, 205)
        Image.fromarray(rgba, "RGBA").save(layers / "pendiente.png", optimize=True)

    # orientacion (azimut de bajada, 8 sectores); <3 % -> transparente. 'visible' False = totalmente transparente
    az = (np.degrees(np.arctan2(-dzdx, -dzdy)) + 360.0) % 360.0
    sector = (np.floor((az + 22.5) / 45.0) % 8).astype(int)
    cols = [_hex(c) for c in LEGEND["orientacion"]["colores"]]
    rgba = np.zeros((H, W, 4), "uint8")
    if with_aspect_visible:
        for k, col in enumerate(cols):
            rgba[(sector == k) & (slope >= 3.0)] = (*col, 205)
    Image.fromarray(rgba, "RGBA").save(layers / "orientacion.png", optimize=True)

    # curvas (equidistancia 10 m, cada 5 una de indice) sobre la malla de 30 m
    import contourpy

    step = LEGEND["curvas"]["equidistancia_m"]
    ink = _hex(LEGEND["curvas"]["color"])
    img = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    xs = (np.arange(nx) + 0.5) * up * 2
    ys = (np.arange(ny) + 0.5) * up * 2
    gen = contourpy.contour_generator(xs, ys, z30.astype("float64"))
    lo, hi = np.floor(z30.min() / step) * step, np.ceil(z30.max() / step) * step
    for lev in np.arange(lo, hi + step, step):
        idx = int(round(lev / step)) % LEGEND["curvas"]["indice_cada"] == 0
        for line in gen.lines(lev):
            if len(line) >= 2:
                dr.line([tuple(p) for p in line], fill=(*ink, 235 if idx else 150), width=4 if idx else 2, joint="curve")
    img.resize((W, H), Image.LANCZOS).save(layers / "curvas.png", optimize=True)

    # sombreado (acimut 315, altura 45)
    azl, alt = np.radians(LEGEND["sombreado"]["azimut_deg"]), np.radians(LEGEND["sombreado"]["altura_deg"])
    s = np.arctan(np.hypot(dzdx, dzdy))
    asp = np.arctan2(dzdy, -dzdx)
    hs = np.sin(alt) * np.cos(s) + np.cos(alt) * np.sin(s) * np.cos(azl - asp)
    hs = np.clip(hs, 0, 1)
    # solo oscurece las laderas en sombra; las iluminadas quedan transparentes (no tapa los colores de la base)
    a = ((1.0 - hs) * 215).astype("uint8")
    rgba = np.dstack([np.zeros_like(a), np.zeros_like(a), np.zeros_like(a), a])
    Image.fromarray(rgba, "RGBA").save(layers / "sombreado.png", optimize=True)


# ---------------------------------------------------------------- GLB
def build_mesh(z30: np.ndarray, x0: float, y1: float, *, exaggeration: float, extras: dict) -> trimesh.Trimesh:
    """z30[fila 0 = norte]; x0 = este (m locales) del centro del pixel [0,0]; y1 = norte (m locales) de la fila 0."""
    ny, nx = z30.shape
    xs = x0 + np.arange(nx) * PIXEL_M
    ns = y1 - np.arange(ny) * PIXEL_M  # norte de cada fila
    X, N = np.meshgrid(xs, ns)
    emin = float(z30.min())
    Y = (z30 - emin) * exaggeration
    verts = np.column_stack([X.ravel(), Y.ravel(), -N.ravel()])
    u = (X - xs[0]) / (xs[-1] - xs[0])
    v = 1.0 - (N - ns[-1]) / (ns[0] - ns[-1])
    uv = np.column_stack([u.ravel(), v.ravel()])
    ii, jj = np.meshgrid(np.arange(nx - 1), np.arange(ny - 1))
    a = (jj * nx + ii).ravel()
    b = a + 1
    c = a + nx
    d = c + 1
    faces = np.vstack([np.column_stack([a, c, b]), np.column_stack([b, c, d])])
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
    assert (mesh.face_normals[:, 1] > 0).all(), "normales hacia abajo: orden de caras equivocado"
    mesh.metadata.update(extras)
    return mesh


def make_site(name: str, out_root: Path, *, exaggeration: float = 1.5) -> Path:
    spec = SITES[name]
    out = out_root / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work = out_root / f"_work_{name}"
    res, poly, (ox, oy, epsg, fwd) = run_synthetic_pipeline(spec, work)

    # ventana de contexto (panorama) en metros locales, alineada a la malla de 30 m
    half = spec["ctx_m"] / 2.0
    nx = ny = int(round(spec["ctx_m"] / PIXEL_M)) + 1
    x0, y1 = -half, half
    e = x0 + np.arange(nx) * PIXEL_M
    n = y1 - np.arange(ny) * PIXEL_M
    E, Nn = np.meshgrid(e, n)
    z30 = spec["fn"](E, Nn).astype("float32")
    emin, emax = float(z30.min()), float(z30.max())
    bbox = [float(e[0]), float(n[-1]), float(e[-1]), float(n[0])]
    extras = {"exaggeration": exaggeration, "vertical_unit": "m", "datum": "EGM2008", "source": "Copernicus GLO-30",
              "pixel_m": int(PIXEL_M), "bbox_local_m": bbox, "elevation_min": emin, "elevation_max": emax}
    mesh = build_mesh(z30, x0, y1, exaggeration=exaggeration, extras=extras)
    glb = trimesh.Scene(mesh).export(file_type="glb")
    (out / "terreno.glb").write_bytes(glb)

    # predio en metros locales (x este, y norte)
    poly_utm = shp_transform(fwd.transform, poly)
    local = shp_transform(lambda x, y, z=None: (np.asarray(x) - ox, np.asarray(y) - oy), poly_utm)
    geo = {"type": "Feature", "properties": {"crs": f"local m (origen = centroide, UTM {epsg})"}, "geometry": mapping(local)}
    (out / "predio.geojson").write_text(json.dumps(geo, indent=1), encoding="utf-8")

    inconc = bool(res.manifest["results"]["terrain"].get("inconclusive"))
    build_layers(z30, out, with_slope=not inconc, with_aspect_visible=not inconc)

    manifest = json.loads(json.dumps(res.manifest))
    manifest["sample_notice"] = {
        "sintetico": True, "nombre": spec["nombre"],
        "texto": "Muestra sintetica generada por viewer/sample/make_sample.py: el relieve no es de un lugar real.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return out


def main(argv=None) -> None:
    out_root = Path((argv or sys.argv[1:] or [HERE / "out"])[0])
    for name in SITES:
        p = make_site(name, out_root)
        m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        t = m["results"]["terrain"]
        print(f"{name}: nivel {m['confidence']['overall']} n_pixels={t['n_pixels']} inconcluyente={t.get('inconclusive')} "
              f"-> {p}")


if __name__ == "__main__":
    main()
