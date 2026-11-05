"""Elige los 8 lotes de prueba estandar de la Region Metropolitana SOLO con datos publicos y escribe data/sites.json.

Datos usados (todos publicos): comunas DPA 2023 (SUBDERE, ``datos_publicos/dpa2023_rm.gpkg``), Copernicus GLO-30
(via ``terreno.fetch`` / ``terreno.reproject`` y ``gdaldem slope``, igual que ``terreno.terrain``) y las huellas de las
dos nubes de referencia de OpenTopography (SPOT 2014 y Pleiades 2016) tomadas del catalogo publico
https://portal.opentopography.org/API/otCatalog (se guardan en ``datos_publicos/huellas_referencia_ot.json``).
Ninguna capa privada: los lotes son cuadrados abstractos de prueba, NO predios reales.

REGLA DE SELECCION (por piloto, determinista)
1. Candidatos: centros de una grilla UTM 19S de ``PASO_M`` = 60 m dentro de la zona del piloto (comunas nombradas por
   la DPA 2023, y si el piloto lo indica, un cuadro lon/lat de interes). La grilla se desplaza con un desfase
   (x, y) sorteado con ``random.Random(SEED)`` (SEED fijo) para que los centros no coincidan con bordes de pixel.
2. El lote es un cuadrado alineado a UTM de la superficie pedida. Es valido si:
   a) el lote mas ``MARGEN_COMUNA_M`` (50 m) queda dentro de la union de las comunas nombradas;
   b) el lote mas ``MARGEN_REF_M`` (300 m) queda dentro de la huella de cada referencia exigida (SPOT, Pleiades);
   c) la pendiente media GLO-30 (Horn, % sobre malla UTM de 30 m) en la ventana centrada en el lote cae en el rango
      del tipo de terreno. La ventana es el lote, con un minimo de ``VENTANA_MIN_M`` = 150 m de lado (5 x 5 pixeles),
      porque un lote urbano de ~1.000 m2 es ~1 pixel y no es estadistica por si solo;
   d) si el tipo exige un rango de elevacion (precordillera), la elevacion media GLO-30 de la ventana cae en el rango.
3. Se elige el candidato valido MAS CENTRAL: el mas cercano al centroide del conjunto de candidatos validos (empate:
   menor x, luego menor y). Si un piloto declara referencia de respaldo (piloto 5: Pleiades si cabe, si no SPOT) y no
   hay candidato valido con todas las referencias, se repite con las de respaldo.
4. Salida: ``data/sites.json`` (8 pilotos, EPSG:4326) con comuna referencial (DPA), tipo de terreno, banderas de
   cobertura de referencia y los numeros de la seleccion. Reproducible: mismas entradas + mismo SEED = mismo archivo.

Limites declarados: la vegetacion del piloto 8 no se verifica (GLO-30 es un DSM; no se usa ninguna capa de cobertura
vegetal); ``icesat2_gedi: true`` significa que el sensor cubre la zona, no que haya huellas dentro del lote (eso lo mide
``terreno.truth`` en la corrida).

Uso:  python tools/elegir_pilotos_rm.py [--refrescar-huellas]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import Polygon, box, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

POC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(POC_DIR))
from terreno.config import run_gdal  # noqa: E402

ROOT = POC_DIR.parent
DPA_GPKG = ROOT / "datos_publicos" / "dpa2023_rm.gpkg"
HUELLAS_JSON = ROOT / "datos_publicos" / "huellas_referencia_ot.json"
SITES_PATH = POC_DIR / "data" / "sites.json"
CACHE_DIR = POC_DIR / "cache" / "pilotos_rm"

EPSG = 32719  # toda la RM esta en UTM 19S
SEED = 20261030
PASO_M = 60.0
VENTANA_MIN_M = 150.0
MARGEN_COMUNA_M = 50.0
MARGEN_REF_M = 300.0
FUENTE_POLIGONO = "lote de prueba estándar (datos públicos)"
OT_CATALOG = "https://portal.opentopography.org/API/otCatalog"
OT_IDS = {"pleiades": "OTDS.022021.32719.1", "spot": "OTDS.022021.32719.2"}
# Caja de busqueda del DEM: 1 grado de GLO-30 (un tile) que contiene todas las zonas de interes
DEM_BOX = (-70.995, -33.995, -70.005, -33.005)

# Pilotos. ``pendiente_pct`` y ``elev_m`` son rangos (min, max) en % y m; ``zona`` es un cuadro (lon0, lat0, lon1, lat1)
# aproximado de la zona de interes dentro de las comunas (coordenadas geograficas publicas, no de predios).
PILOTOS: list[dict] = [
    dict(key="rm01_plano_urbano", nombre="RM-01 Plano urbano consolidado", tipo_terreno="plano urbano consolidado",
         comunas=("Ñuñoa", "Providencia"), zona=None, area_m2=800.0, pendiente_pct=(0.0, 3.0), elev_m=None,
         referencias=("spot",), ref_fallback=None),
    dict(key="rm02_plano_periurbano", nombre="RM-02 Plano periurbano poniente", tipo_terreno="plano periurbano poniente",
         comunas=("Maipú", "Pudahuel"), zona=None, area_m2=15_000.0, pendiente_pct=(0.0, 3.0), elev_m=None,
         referencias=("spot",), ref_fallback=None),
    dict(key="rm03_piedemonte_oriente", nombre="RM-03 Piedemonte oriente", tipo_terreno="piedemonte oriente",
         comunas=("Peñalolén", "La Reina"), zona=None, area_m2=10_000.0, pendiente_pct=(10.0, 20.0), elev_m=None,
         referencias=("pleiades", "spot"), ref_fallback=None),
    dict(key="rm04_piedemonte_alto", nombre="RM-04 Piedemonte alto", tipo_terreno="piedemonte alto",
         comunas=("Lo Barnechea", "Las Condes"), zona=None, area_m2=60_000.0, pendiente_pct=(20.0, 35.0), elev_m=None,
         referencias=("pleiades", "spot"), ref_fallback=None),
    dict(key="rm05_piedemonte_sur", nombre="RM-05 Piedemonte sur", tipo_terreno="piedemonte sur",
         comunas=("Puente Alto", "La Florida"), zona=None, area_m2=18_000.0, pendiente_pct=(8.0, 25.0), elev_m=None,
         referencias=("pleiades", "spot"), ref_fallback=("spot",)),
    dict(key="rm06_cerro_isla_urbano", nombre="RM-06 Cerro isla urbano", tipo_terreno="cerro isla urbano",
         comunas=("Renca", "Huechuraba"), zona=None, area_m2=1_000.0, pendiente_pct=(25.0, 50.0), elev_m=None,
         referencias=("spot",), ref_fallback=None),
    dict(key="rm07_lomaje_norte", nombre="RM-07 Lomaje norte (Chicureo)", tipo_terreno="lomaje norte",
         comunas=("Colina",), zona=(-70.70, -33.35, -70.55, -33.20), area_m2=80_000.0, pendiente_pct=(8.0, 20.0),
         elev_m=None, referencias=(), ref_fallback=None),
    dict(key="rm08_precordillera", nombre="RM-08 Precordillera con vegetación", tipo_terreno="precordillera con vegetación",
         comunas=("Pirque", "San José de Maipo"), zona=(-70.62, -33.75, -70.35, -33.50), area_m2=70_000.0,
         pendiente_pct=(25.0, 55.0), elev_m=(800.0, 1500.0), referencias=(), ref_fallback=None),
]

_GEOD = Geod(ellps="WGS84")
_FWD = Transformer.from_crs(4326, EPSG, always_xy=True)
_INV = Transformer.from_crs(EPSG, 4326, always_xy=True)


# ----------------------------------------------------------------------------------------------------------------------
# funciones puras (probadas en tests/test_elegir_pilotos_rm.py)
# ----------------------------------------------------------------------------------------------------------------------

def cuadrado_utm(cx: float, cy: float, area_m2: float) -> Polygon:
    h = math.sqrt(area_m2) / 2.0
    return box(cx - h, cy - h, cx + h, cy + h)


def area_geodesica_m2(poly: Polygon) -> float:
    return abs(_GEOD.geometry_area_perimeter(poly)[0])


def integral(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Imagenes integrales (suma y conteo de pixeles validos) para medias de ventana en O(1)."""
    ok = np.isfinite(arr)
    vals = np.where(ok, arr, 0.0).astype("float64")
    pad = ((1, 0), (1, 0))
    return (np.pad(vals.cumsum(0).cumsum(1), pad), np.pad(ok.astype("float64").cumsum(0).cumsum(1), pad))


def ventana_media(sat: tuple[np.ndarray, np.ndarray], tf, cx, cy, lado):
    """Media de los pixeles cuyo centro cae en el cuadrado centrado en (cx, cy) de lado max(lado, VENTANA_MIN_M).

    ``cx``, ``cy`` pueden ser escalares o arreglos. NaN si la ventana no tiene pixeles validos."""
    vals, cnt = sat
    h, w = cnt.shape[0] - 1, cnt.shape[1] - 1
    half = max(float(lado), VENTANA_MIN_M) / 2.0
    cx, cy = np.asarray(cx, dtype="float64"), np.asarray(cy, dtype="float64")
    c0 = np.ceil((cx - half - tf.c) / tf.a - 0.5)
    c1 = np.floor((cx + half - tf.c) / tf.a - 0.5)
    r0 = np.ceil((tf.f - (cy + half)) / -tf.e - 0.5)
    r1 = np.floor((tf.f - (cy - half)) / -tf.e - 0.5)
    c0, c1 = np.clip(c0, 0, w).astype(int), np.clip(c1 + 1, 0, w).astype(int)
    r0, r1 = np.clip(r0, 0, h).astype(int), np.clip(r1 + 1, 0, h).astype(int)

    def tot(s):
        return s[r1, c1] - s[r0, c1] - s[r1, c0] + s[r0, c0]

    n = tot(cnt)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(n > 0, tot(vals) / np.where(n > 0, n, 1.0), np.nan)
    return float(out) if out.ndim == 0 else out


def elegir_candidato(spec: dict, slope: np.ndarray, elev: np.ndarray, tf, zona, huellas: dict, *, seed: int):
    """Candidato valido mas central segun la regla del modulo, o None. ``zona`` y ``huellas`` en UTM."""
    rng = random.Random(seed)
    ox, oy = rng.uniform(0, PASO_M), rng.uniform(0, PASO_M)
    minx, miny, maxx, maxy = zona.bounds
    xs, ys = np.meshgrid(np.arange(minx + ox, maxx, PASO_M), np.arange(miny + oy, maxy, PASO_M))
    cx, cy = xs.ravel(), ys.ravel()
    lado = math.sqrt(spec["area_m2"])
    half = lado / 2.0

    ok = np.ones(cx.shape, dtype=bool)
    shapely.prepare(zona)
    m = half + MARGEN_COMUNA_M
    ok &= shapely.contains(zona, shapely.box(cx - m, cy - m, cx + m, cy + m))
    for ref in spec["referencias"]:
        h = huellas.get(ref)
        if h is None:
            return None
        shapely.prepare(h)
        m = half + MARGEN_REF_M
        idx = np.flatnonzero(ok)
        ok[idx] = shapely.contains(h, shapely.box(cx[idx] - m, cy[idx] - m, cx[idx] + m, cy[idx] + m))

    idx = np.flatnonzero(ok)
    if idx.size == 0:
        return None
    ms = ventana_media(integral(slope), tf, cx[idx], cy[idx], lado)
    lo, hi = spec["pendiente_pct"]
    good = (ms >= lo) & (ms <= hi)
    me = None
    if spec["elev_m"] is not None:
        me = ventana_media(integral(elev), tf, cx[idx], cy[idx], lado)
        good &= (me >= spec["elev_m"][0]) & (me <= spec["elev_m"][1])
    idx, ms = idx[good], ms[good]
    if idx.size == 0:
        return None
    if me is not None:
        me = me[good]
    vx, vy = cx[idx], cy[idx]
    mx, my = vx.mean(), vy.mean()
    order = np.lexsort((vy, vx, np.hypot(vx - mx, vy - my)))
    k = int(order[0])
    out = {
        "cx": float(vx[k]), "cy": float(vy[k]), "pendiente_media_ventana_pct": float(ms[k]),
        "n_candidatos_validos": int(idx.size), "ventana_m": max(lado, VENTANA_MIN_M),
    }
    if me is not None:
        out["elev_media_ventana_m"] = float(me[k])
    return out


def seleccionar(spec: dict, slope, elev, tf, zona, huellas: dict, *, seed: int):
    """``elegir_candidato`` con la referencia de respaldo del piloto (si la declara)."""
    r = elegir_candidato(spec, slope, elev, tf, zona, huellas, seed=seed)
    usadas, fallback = list(spec["referencias"]), False
    if r is None and spec.get("ref_fallback"):
        r = elegir_candidato(spec | {"referencias": tuple(spec["ref_fallback"])}, slope, elev, tf, zona, huellas, seed=seed)
        usadas, fallback = list(spec["ref_fallback"]), True
    if r is None:
        return None
    return r | {"referencias_usadas": usadas, "uso_fallback": fallback}


# ----------------------------------------------------------------------------------------------------------------------
# datos publicos
# ----------------------------------------------------------------------------------------------------------------------

def _a_utm(geom):
    return shapely.transform(geom, lambda c: np.column_stack(_FWD.transform(c[:, 0], c[:, 1])))


def cargar_comunas() -> dict:
    """{nombre: geometria 4326} de las comunas DPA 2023 de la RM."""
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "c.geojson"
        run_gdal("ogr2ogr", "-f", "GeoJSON", str(dst), str(DPA_GPKG), "comunas_rm")
        fc = json.loads(dst.read_text(encoding="utf-8"))
    return {f["properties"]["COMUNA"]: shape(f["geometry"]) for f in fc["features"]}


def descargar_huellas() -> dict:
    """Huellas de SPOT 2014 y Pleiades 2016 desde el catalogo publico de OpenTopography (sin credenciales)."""
    import requests

    params = dict(productFormat="PointCloud", minx=-71.2, miny=-33.8, maxx=-70.2, maxy=-33.0, detail="true",
                  outputFormat="json", include_federated="true")
    r = requests.get(OT_CATALOG, params=params, timeout=60)
    r.raise_for_status()
    out: dict = {"fuente": OT_CATALOG, "consulta": params, "huellas": {}}
    for item in r.json()["Datasets"]:
        ds = item["Dataset"]
        ident = ds["identifier"]["value"]
        for nombre, oid in OT_IDS.items():
            if ident == oid:
                out["huellas"][nombre] = {
                    "opentopography_id": oid, "titulo": ds["name"], "doi_url": ds["url"],
                    "fecha_catalogo": ds["dateCreated"],
                    "geometry": ds["spatialCoverage"]["geo"]["geojson"]["features"][0]["geometry"],
                }
    faltan = set(OT_IDS) - set(out["huellas"])
    if faltan:
        raise SystemExit(f"el catalogo de OpenTopography no devolvio: {sorted(faltan)}")
    return out


def cargar_huellas(refrescar: bool = False) -> dict:
    """{'spot': geom4326, 'pleiades': geom4326}; descarga y guarda en datos_publicos si no estan o si se pide."""
    if refrescar or not HUELLAS_JSON.exists():
        HUELLAS_JSON.write_text(json.dumps(descargar_huellas(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    data = json.loads(HUELLAS_JSON.read_text(encoding="utf-8"))["huellas"]
    return {k: shape(v["geometry"]) for k, v in data.items()}


def pendiente_glo30():
    """(pendiente %, elevacion m, geotransform, epsg) de GLO-30 en una caja de 1 grado, malla UTM de 30 m."""
    from terreno.fetch import fetch_dem
    from terreno.reproject import reproject

    poly = box(*DEM_BOX)
    dem = fetch_dem(poly, CACHE_DIR)
    r = reproject(dem, poly, margin_px=3)
    slope_p = Path(r.path).with_name(Path(r.path).stem + "_slope.tif")
    if slope_p.exists():
        slope_p.unlink()
    run_gdal("gdaldem", "slope", str(r.path), str(slope_p), "-p", "-of", "GTiff")
    with rasterio.open(r.path) as s, rasterio.open(slope_p) as sl:
        elev, slope, tf = s.read(1).astype("float32"), sl.read(1).astype("float32"), s.transform
        nd = sl.nodata
    if nd is not None and not math.isnan(nd):
        slope[slope == np.float32(nd)] = np.nan
    return slope, elev, tf, r.epsg


# ----------------------------------------------------------------------------------------------------------------------
# construccion de sites.json
# ----------------------------------------------------------------------------------------------------------------------

def _poligono_4326(sq_utm: Polygon) -> dict:
    pts = [[round(v, 7) for v in _INV.transform(x, y)] for x, y in sq_utm.exterior.coords]
    ring = orient(Polygon(pts), sign=1.0)
    return {"type": "Polygon", "coordinates": [[list(c) for c in ring.exterior.coords]]}


def construir_sites(refrescar_huellas: bool = False) -> dict:
    comunas = cargar_comunas()
    huellas4326 = cargar_huellas(refrescar_huellas)
    huellas = {k: _a_utm(g) for k, g in huellas4326.items()}
    comunas_utm = {k: _a_utm(g) for k, g in comunas.items()}
    slope, elev, tf, epsg = pendiente_glo30()
    if epsg != EPSG:
        raise SystemExit(f"se esperaba UTM {EPSG}, el raster esta en {epsg}")
    sat_slope = integral(slope)

    sites: dict = {}
    for spec in PILOTOS:
        zona = unary_union([comunas_utm[c] for c in spec["comunas"]])
        if spec["zona"] is not None:
            lon0, lat0, lon1, lat1 = spec["zona"]
            zona = zona.intersection(_a_utm(box(lon0, lat0, lon1, lat1).segmentize(0.001)))
        sel = seleccionar(spec, slope, elev, tf, zona, huellas, seed=SEED)
        if sel is None:
            raise SystemExit(f"{spec['key']}: ningun candidato cumple la regla (revisar rangos o zona)")
        sq = cuadrado_utm(sel["cx"], sel["cy"], spec["area_m2"])
        poly4326 = _poligono_4326(sq)
        lote = shape(poly4326)
        c = lote.centroid
        comuna_ref = next((n for n, g in comunas.items() if g.contains(c)), None)
        cobertura = {
            "spot": bool(huellas4326["spot"].contains(lote)),
            "pleiades": bool(huellas4326["pleiades"].contains(lote)),
            "icesat2_gedi": True,
        }
        # media de pendiente sobre los pixeles que caen dentro del lote (como terreno.compute_terrain); None si < 9 px
        from rasterio.features import geometry_mask
        inside = geometry_mask([sq], out_shape=slope.shape, transform=tf, all_touched=False, invert=True)
        sv = slope[inside & np.isfinite(slope)]
        sites[spec["key"]] = {
            "nombre": spec["nombre"],
            "comuna_referencial": comuna_ref,
            "tipo_terreno": spec["tipo_terreno"],
            "polygon": poly4326,
            "area_m2": round(area_geodesica_m2(lote), 1),
            "fuente_poligono": FUENTE_POLIGONO,
            "cobertura_referencia": cobertura,
            "seleccion": {
                "regla": "tools/elegir_pilotos_rm.py (datos públicos: DPA 2023, GLO-30, huellas OpenTopography)",
                "seed": SEED, "paso_grilla_m": PASO_M,
                "comunas_zona": list(spec["comunas"]),
                "rango_pendiente_objetivo_pct": list(spec["pendiente_pct"]),
                "pendiente_media_ventana_pct": round(sel["pendiente_media_ventana_pct"], 2),
                "ventana_m": sel["ventana_m"],
                "pendiente_media_lote_pct": round(float(sv.mean()), 2) if sv.size >= 9 else None,
                "n_pixeles_lote": int(sv.size),
                "elev_media_ventana_m": round(sel["elev_media_ventana_m"], 1) if "elev_media_ventana_m" in sel else None,
                "referencias_exigidas": list(sel["referencias_usadas"]),
                "uso_referencia_de_respaldo": sel["uso_fallback"],
                "n_candidatos_validos": sel["n_candidatos_validos"],
            },
            "verdad_de_terreno": {
                "elevacion_medida": None,
                "nota": "Lote de prueba estándar sin cotas propias; la elevación viene de DEM externo, ICESat-2/GEDI y, "
                        "donde hay cobertura, de las nubes de referencia SPOT/Pléiades (solo validación interna).",
            },
        }
    return sites


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refrescar-huellas", action="store_true", help="vuelve a consultar el catalogo de OpenTopography")
    a = ap.parse_args(argv)
    sites = construir_sites(a.refrescar_huellas)
    SITES_PATH.write_text(json.dumps(sites, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("escrito", SITES_PATH)
    for k, s in sites.items():
        q = s["seleccion"]
        print(f"{k:26s} {s['comuna_referencial']:18s} {s['area_m2']:9.0f} m2  pend_ventana={q['pendiente_media_ventana_pct']:5.1f}%  "
              f"cob={s['cobertura_referencia']}  n_valid={q['n_candidatos_validos']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
