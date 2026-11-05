"""Elige 16 lotes de prueba RM ADICIONALES para validar la regla A/B/C (data/sites_validacion.json). Solo datos publicos.

Por que: con los 8 pilotos el sello de confianza del dato tiene un sitio en A y uno en C; no se puede decidir si ordena el
error. Aqui se eligen sitios por ESTRATOS FIJADOS ANTES de medir nada (pendiente y cobertura, nunca el error), para que
haya varios en cada nivel y la prueba no dependa de lo que salga.

Datos (todos publicos): comunas DPA 2023 (``datos_publicos/dpa2023_rm.gpkg``), Copernicus GLO-30 (pendiente y elevacion en
malla UTM de 30 m, igual que ``elegir_pilotos_rm``) y ESA WorldCover 10 m v200 (% de arbolado + construido, promediado a la
misma malla). Los lotes son cuadrados de prueba, NO predios reales.

ESTRATOS (cada uno: comunas, superficie, rango de pendiente media, rango de dosel)
* A (6): terreno llano y sin dosel: pendiente 0-12 %, dosel < 8 %, 3-6 ha (>= 25 pixeles).
* B (4): pendiente 12-22 %, dosel 0-50 %, 2-6 ha.
* C (6): pendiente 30-60 %, 3-8 ha.
Un estrato "A" no garantiza nivel A: el sello tambien depende del desacuerdo con AW3D30, vacios y HEM. Eso es lo que se mide.

REGLA PREFIJADA para evaluar (tools/evaluar_regla_validacion.py): la regla "discrimina" si el RMSE medio contra ICESat-2/GEDI
es no decreciente A <= B <= C con al menos 3 sitios en cada nivel Y la correlacion de rangos entre el nivel (A=0, B=1, C=2) y
el RMSE de cada sitio es >= 0,5. Si no se cumple, se informa tal cual; no se ajustan cortes sobre estos mismos sitios.

Seleccion por estrato: grilla UTM de 60 m con desfase sorteado (SEED fijo); lote + 50 m dentro de la union de comunas; se toma
el candidato valido mas cercano al centroide de los candidatos validos (desempate: menor x, luego menor y).

Uso:  python tools/elegir_sitios_validacion.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import rasterio
import shapely
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject
from shapely.geometry import box, shape
from shapely.ops import unary_union

POC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(POC_DIR))
from terreno.worldcover import BASE_URL, CANOPY_CLASSES, tile_name, tile_url  # noqa: E402
from tools.elegir_pilotos_rm import (  # noqa: E402
    CACHE_DIR, DEM_BOX, EPSG, MARGEN_COMUNA_M, PASO_M, VENTANA_MIN_M, _a_utm, _poligono_4326, area_geodesica_m2,
    cargar_comunas, cuadrado_utm, integral, pendiente_glo30, ventana_media)

SEED = 20261031
OUT_PATH = POC_DIR / "data" / "sites_validacion.json"
FUENTE_POLIGONO = "lote de prueba estándar (datos públicos)"

# key, nombre, comunas, zona (lon0, lat0, lon1, lat1) o None, area_m2, pendiente (min, max) %, dosel (min, max) %
ESTRATOS: list[dict] = [
    dict(key="v01_llano_pudahuel", estrato="A", comunas=("Pudahuel",), zona=None, area_m2=40_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v02_llano_lampa", estrato="A", comunas=("Lampa",), zona=None, area_m2=50_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v03_llano_padre_hurtado", estrato="A", comunas=("Padre Hurtado", "Peñaflor"), zona=None, area_m2=40_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v04_llano_buin", estrato="A", comunas=("Buin",), zona=None, area_m2=50_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v05_llano_paine", estrato="A", comunas=("Paine",), zona=None, area_m2=60_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v06_llano_talagante", estrato="A", comunas=("Talagante", "Calera de Tango"), zona=None, area_m2=40_000.0, pend=(0, 12), dosel=(0, 8)),
    dict(key="v07_media_colina", estrato="B", comunas=("Colina",), zona=(-70.75, -33.25, -70.55, -33.05), area_m2=40_000.0, pend=(12, 22), dosel=(0, 50)),
    dict(key="v08_media_pirque", estrato="B", comunas=("Pirque",), zona=None, area_m2=30_000.0, pend=(12, 22), dosel=(0, 50)),
    dict(key="v09_media_la_florida", estrato="B", comunas=("La Florida", "Peñalolén"), zona=None, area_m2=20_000.0, pend=(12, 22), dosel=(0, 50)),
    dict(key="v10_media_san_jose", estrato="B", comunas=("San José de Maipo",), zona=None, area_m2=30_000.0, pend=(12, 22), dosel=(0, 50)),
    dict(key="v11_cerro_lo_barnechea", estrato="C", comunas=("Lo Barnechea",), zona=None, area_m2=50_000.0, pend=(30, 60), dosel=(0, 100)),
    dict(key="v12_cerro_las_condes", estrato="C", comunas=("Las Condes",), zona=None, area_m2=50_000.0, pend=(30, 60), dosel=(0, 100)),
    dict(key="v13_cerro_san_jose", estrato="C", comunas=("San José de Maipo",), zona=None, area_m2=80_000.0, pend=(30, 60), dosel=(0, 100)),
    dict(key="v14_cerro_pirque", estrato="C", comunas=("Pirque",), zona=None, area_m2=60_000.0, pend=(30, 60), dosel=(0, 100)),
    dict(key="v15_cerro_penalolen", estrato="C", comunas=("Peñalolén", "La Reina"), zona=None, area_m2=30_000.0, pend=(30, 60), dosel=(0, 100)),
    dict(key="v16_cerro_til_til", estrato="C", comunas=("Tiltil",), zona=None, area_m2=60_000.0, pend=(30, 60), dosel=(0, 100)),
]


def dosel_30m(shape_hw: tuple[int, int], tf, epsg: int) -> np.ndarray:
    """% de arbolado + construido (ESA WorldCover 10 m) promediado a la malla UTM de 30 m del DEM. Se guarda en cache."""
    cache = CACHE_DIR / "dosel30.npy"
    if cache.exists():
        arr = np.load(cache)
        if arr.shape == shape_hw:
            return arr
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        with rasterio.open(tile_url(BASE_URL, tile_name(-36, -72))) as src:
            w = src.window(*DEM_BOX).round_offsets().round_lengths()
            cls = src.read(1, window=w)
            stf = src.window_transform(w)
    binary = np.isin(cls, CANOPY_CLASSES).astype("float32")
    dst = np.full(shape_hw, np.nan, dtype="float32")
    reproject(source=binary, destination=dst, src_transform=stf, src_crs="EPSG:4326", dst_transform=tf,
              dst_crs=f"EPSG:{epsg}", resampling=Resampling.average, dst_nodata=np.nan)
    dst *= 100.0
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(cache, dst)
    return dst


def elegir(spec: dict, slope: np.ndarray, dosel: np.ndarray, tf, zona, *, seed: int):
    """Candidato valido mas central del estrato, o None. Funcion pura (probada con arreglos sinteticos)."""
    rng = random.Random(seed)
    ox, oy = rng.uniform(0, PASO_M), rng.uniform(0, PASO_M)
    minx, miny, maxx, maxy = zona.bounds
    xs, ys = np.meshgrid(np.arange(minx + ox, maxx, PASO_M), np.arange(miny + oy, maxy, PASO_M))
    cx, cy = xs.ravel(), ys.ravel()
    lado = math.sqrt(spec["area_m2"])
    m = lado / 2.0 + MARGEN_COMUNA_M
    shapely.prepare(zona)
    ok = shapely.contains(zona, shapely.box(cx - m, cy - m, cx + m, cy + m))
    idx = np.flatnonzero(ok)
    if idx.size == 0:
        return None
    ms = ventana_media(integral(slope), tf, cx[idx], cy[idx], lado)
    md = ventana_media(integral(dosel), tf, cx[idx], cy[idx], lado)
    good = (ms >= spec["pend"][0]) & (ms <= spec["pend"][1]) & (md >= spec["dosel"][0]) & (md <= spec["dosel"][1])
    idx, ms, md = idx[good], ms[good], md[good]
    if idx.size == 0:
        return None
    vx, vy = cx[idx], cy[idx]
    k = int(np.lexsort((vy, vx, np.hypot(vx - vx.mean(), vy - vy.mean())))[0])
    return {"cx": float(vx[k]), "cy": float(vy[k]), "pendiente_ventana_pct": float(ms[k]), "dosel_ventana_pct": float(md[k]),
            "n_candidatos_validos": int(idx.size), "ventana_m": max(lado, VENTANA_MIN_M)}


def main() -> int:
    comunas = cargar_comunas()
    comunas_utm = {k: _a_utm(g) for k, g in comunas.items()}
    slope, elev, tf, epsg = pendiente_glo30()
    if epsg != EPSG:
        raise SystemExit(f"se esperaba UTM {EPSG}, el raster esta en {epsg}")
    dosel = dosel_30m(slope.shape, tf, epsg)
    sites: dict = {}
    for spec in ESTRATOS:
        faltan = [c for c in spec["comunas"] if c not in comunas_utm]
        if faltan:
            raise SystemExit(f"{spec['key']}: comuna(s) no encontradas en la DPA 2023: {faltan}")
        zona = unary_union([comunas_utm[c] for c in spec["comunas"]])
        if spec["zona"] is not None:
            zona = zona.intersection(_a_utm(box(*spec["zona"]).segmentize(0.001)))
        sel = elegir(spec, slope, dosel, tf, zona, seed=SEED)
        if sel is None:
            print(f"{spec['key']}: NINGUN candidato cumple el estrato ({spec['pend']} %, dosel {spec['dosel']} %)")
            continue
        sq = cuadrado_utm(sel["cx"], sel["cy"], spec["area_m2"])
        poly = _poligono_4326(sq)
        lote = shape(poly)
        comuna_ref = next((n for n, g in comunas.items() if g.contains(lote.centroid)), None)
        inside = geometry_mask([sq], out_shape=slope.shape, transform=tf, all_touched=False, invert=True)
        sv = slope[inside & np.isfinite(slope)]
        sites[spec["key"]] = {
            "nombre": spec["key"], "estrato": spec["estrato"], "comuna_referencial": comuna_ref, "polygon": poly,
            "area_m2": round(area_geodesica_m2(lote), 1), "fuente_poligono": FUENTE_POLIGONO,
            "seleccion": {"regla": "tools/elegir_sitios_validacion.py (DPA 2023, GLO-30, ESA WorldCover; sin capas privadas)",
                          "seed": SEED, "comunas_zona": list(spec["comunas"]), "rango_pendiente_pct": list(spec["pend"]),
                          "rango_dosel_pct": list(spec["dosel"]), "pendiente_ventana_pct": round(sel["pendiente_ventana_pct"], 2),
                          "dosel_ventana_pct": round(sel["dosel_ventana_pct"], 2), "n_pixeles_lote": int(sv.size),
                          "n_candidatos_validos": sel["n_candidatos_validos"]},
        }
        print(f"{spec['key']:26s} {spec['estrato']} {str(comuna_ref):18s} {sites[spec['key']]['area_m2']:8.0f} m2 "
              f"pend={sel['pendiente_ventana_pct']:5.1f}% dosel={sel['dosel_ventana_pct']:5.1f}% n_valid={sel['n_candidatos_validos']}")
    OUT_PATH.write_text(json.dumps(sites, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("escrito", OUT_PATH, f"({len(sites)} de {len(ESTRATOS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
