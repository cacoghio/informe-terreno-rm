"""sample_ground_truth: huellas ICESat-2 ATL08 / GEDI L2A como verdad de terreno independiente (POC/validacion).

Ruta de descarga elegida (verificada en Windows, ver LEDGER):
* BUSQUEDA: CMR publico (https://cmr.earthdata.nasa.gov/search/granules.json) con ``requests``; no necesita credenciales.
* LECTURA: ``earthaccess`` (login por ``environment`` o ``netrc`` -- NUNCA interactivo) abre el granulo HDF5 con lecturas
  por rangos (fsspec) y ``h5py`` lee solo lo que cae dentro del bbox. En Windows earthaccess busca ``~/_netrc`` (o la
  variable ``NETRC``), no ``~/.netrc``. Sin credenciales: RejectRequest claro; este modulo no las pide ni las crea.
Alturas: ATL08 ``h_te_best_fit`` y GEDI ``elev_lowestmode`` estan sobre el elipsoide WGS84; se pasan a EGM2008 con las
mallas de geoide de PROJ y la conversion se ASIERTA contra puntos de control antes de usarse.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import Polygon

from terreno.errors import RejectRequest
from terreno.geometry import utm_epsg_for
from terreno.retry import is_transient

CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
ATL08_FILL = 3.4028235e38  # INVALID_R4B del diccionario de datos ATL08
ATL08_MAX_UNCERTAINTY_M = 15.0  # hipotesis de corte para h_te_uncertainty
MIN_FOOTPRINTS = 10  # < 10 huellas -> "validacion insuficiente" (spec sec. 8 criterio 4)
HIGH_SLOPE_PCT = 15.0  # huellas en pendiente >= 15 % se reportan aparte (no se descartan)
GEOID_TOL_M = 0.5
# (lon, lat, N EGM2008 esperado en m). (0,0): 17.2 m (EGM96 oficial 17.16, EGM2008 17.22); Nunoa: 27.17 m (valor de la malla
# EGM2008 de PROJ, verificado ademas contra EGM96 27.29 m).
GEOID_CONTROL_POINTS = ((0.0, 0.0, 17.2), (-70.6, -33.45, 27.2))
# Version de las reglas de calidad (quality_flag). Entra en la clave de la cache de huellas: si cambia una regla, las
# huellas ya cacheadas (con su quality_flag viejo) NO se reutilizan. Subirla en cada cambio de reglas.
# 2: subset_te_flag -> los cinco subsegmentos deben valer 1 (v7: 1 = terrain_photons_present); antes se exigia 0 (AUD-1 C1).
QUALITY_RULES_VERSION = "2"
# Version extra por fuente: cambia solo la clave de cache de esa fuente (vacia = sin reglas propias todavia).
# gedi "3": se descarta la huella a mas de GEDI_MAX_DEM_DIFF_M de su DEM TanDEM-X (rebotes en nube con quality_flag 1).
SOURCE_RULES_VERSION = {"atl08": "", "gedi": "3"}
# Corte para errores groseros, no para precision: el error esperado de un DEM de 30 m es 3-10 m, asi que 100 m no
# recorta la cola real (no es circular) y si saca nubes (~300 m en Nunoa, granulo O08246 2020-05-27).
GEDI_MAX_DEM_DIFF_M = 100.0
PRODUCTS = {"atl08": ("ATL08", "007"), "gedi": ("GEDI02_A", "002")}


@dataclass
class Footprint:
    lon: float
    lat: float
    h_ellipsoid_m: float
    h_orthometric_m: float
    source: str  # "atl08" | "gedi"
    date: str
    quality_flag: int  # 1 = bueno, 0 = malo (se devuelve marcado; validate() lo excluye)
    uncertainty_m: float | None = None
    beam: str = ""
    granule: str = ""


@dataclass
class GranuleRef:
    url: str
    date: str
    granule_id: str = ""


@dataclass
class ValidationResult:
    n: int
    bias_m: float | None
    mae_m: float | None
    rmse_m: float | None
    status: str  # "ok" | "validacion_insuficiente"
    n_excluded_quality: int = 0
    n_outside_raster: int = 0
    by_source: dict = field(default_factory=dict)
    by_slope: dict = field(default_factory=dict)
    high_slope_threshold_pct: float = HIGH_SLOPE_PCT
    min_footprints: int = MIN_FOOTPRINTS
    sign_convention: str = "sesgo = DEM - verdad (huellas)"


# --- geoide ---

def egm2008_undulation_pyproj(lon, lat) -> np.ndarray:
    """Ondulacion N del geoide EGM2008 (m) con la malla us_nga_egm08_25.tif de PROJ (red; lectura por rangos)."""
    from pyproj import network
    from pyproj.transformer import TransformerGroup

    prev = network.is_network_enabled()
    network.set_network_enabled(True)
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            group = TransformerGroup("EPSG:4979", "EPSG:4326+3855", always_xy=True)
        grid_based = [t for t in group.transformers if "ballpark" not in t.description.lower()]
        if not grid_based:
            raise RejectRequest("sin malla de geoide EGM2008 en PROJ (solo ballpark): no se puede convertir elipsoide a EGM2008")
        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        _, _, z = grid_based[0].transform(lon, lat, np.zeros_like(lon))
        return -np.asarray(z, dtype="float64")
    except RejectRequest:
        raise
    except Exception as exc:
        raise RejectRequest(f"fallo al aplicar la malla de geoide EGM2008: {exc}")
    finally:
        network.set_network_enabled(prev)


def assert_geoid_function(geoid_fn: Callable) -> None:
    """Asierta la funcion de geoide contra los puntos de control (tolerancia 0.5 m). RejectRequest si falla."""
    for lon, lat, expected in GEOID_CONTROL_POINTS:
        try:
            got = float(np.asarray(geoid_fn(np.array([lon]), np.array([lat])), dtype="float64").ravel()[0])
        except RejectRequest:
            raise
        except Exception as exc:
            raise RejectRequest(f"la funcion de geoide fallo en el punto de control ({lon}, {lat}): {exc}")
        if not math.isfinite(got) or abs(got - expected) > GEOID_TOL_M:
            raise RejectRequest(
                f"conversion de geoide no verificada: N({lon}, {lat}) = {got} m, se esperaba {expected} +- {GEOID_TOL_M} m"
            )


def ellipsoid_to_orthometric(lon, lat, h_ell, *, geoid_fn: Callable | None = None) -> np.ndarray:
    """H (EGM2008) = h (elipsoide WGS84) - N. La funcion de geoide se asierta antes de usarla."""
    geoid_fn = geoid_fn or egm2008_undulation_pyproj
    assert_geoid_function(geoid_fn)
    lon = np.asarray(lon, dtype="float64")
    lat = np.asarray(lat, dtype="float64")
    n = np.asarray(geoid_fn(lon, lat), dtype="float64")
    if not np.isfinite(n).all():
        raise RejectRequest("conversion de geoide: ondulacion no finita en alguna huella")
    return np.asarray(h_ell, dtype="float64") - n


# --- lectores HDF5 ---

def _bbox_idx(lat, lon, bbox):
    w, s, e, n = bbox
    return np.nonzero(np.isfinite(lat) & np.isfinite(lon) & (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n))[0]


def _take(group, name, idx, i0, i1, required=False):
    if name not in group:
        if required:
            raise RejectRequest(f"estructura inesperada del granulo: falta {group.name}/{name}")
        return None
    return np.asarray(group[name][i0:i1])[idx - i0]


def _rows_all(cond: np.ndarray) -> np.ndarray:
    """Reduce una condicion (N,) o (N, k) a (N,): True solo si se cumple en todas las columnas."""
    cond = np.asarray(cond)
    return cond if cond.ndim == 1 else cond.reshape(cond.shape[0], -1).all(axis=1)


def read_atl08(f, bbox) -> list[dict]:
    """Segmentos de 100 m de ATL08 (v7) dentro del bbox (w, s, e, n).

    quality_flag = 1 solo si: h_te_best_fit y h_te_uncertainty validos (no INVALID_R4B) y uncertainty <= 15 m,
    layer_flag == 0 (aire despejado, ATL09), msw_flag == 0, segment_watermask != 1 (agua) y subset_te_flag == 1 en
    los CINCO subsegmentos de 20 m. Diccionario ATL08 v7: subset_te_flag es INTEGER_1(5, :) con -1 = no_photon_data_
    within_geosegment, 0 = no_terrain_photons_within_geosegment, 1 = terrain_photons_present_within_geosegment, asi
    que "bueno" es 1 (terreno de >= 100 m de fotones) y cualquier 0 o -1 invalida el segmento.
    ``terrain_flg`` NO se usa: compara contra un DEM de referencia (circular).
    """
    rows: list[dict] = []
    for beam in sorted(k for k in f.keys() if re.fullmatch(r"gt[123][lr]", k)):
        ls = f[beam].get("land_segments")
        if ls is None or "latitude" not in ls or "longitude" not in ls:
            continue
        lat = np.asarray(ls["latitude"][:], dtype="float64")
        lon = np.asarray(ls["longitude"][:], dtype="float64")
        idx = _bbox_idx(lat, lon, bbox)
        if idx.size == 0:
            continue
        i0, i1 = int(idx.min()), int(idx.max()) + 1
        if "terrain" not in ls:
            raise RejectRequest(f"estructura inesperada de ATL08: falta {beam}/land_segments/terrain")
        t = ls["terrain"]
        h = _take(t, "h_te_best_fit", idx, i0, i1, required=True).astype("float64")
        unc = _take(t, "h_te_uncertainty", idx, i0, i1, required=True).astype("float64")
        ok = np.isfinite(h) & (h < 1e30) & np.isfinite(unc) & (unc < 1e30) & (unc <= ATL08_MAX_UNCERTAINTY_M)
        layer = _take(ls, "layer_flag", idx, i0, i1)
        msw = _take(ls, "msw_flag", idx, i0, i1)
        water = _take(ls, "segment_watermask", idx, i0, i1)
        sub = _take(t, "subset_te_flag", idx, i0, i1)
        # Los flags pueden venir como (N,) o (N, 5) (un valor por subsegmento de 20 m): el segmento de 100 m
        # solo es bueno si TODOS sus valores lo son.
        if layer is not None:
            ok &= _rows_all(layer == 0)
        if msw is not None:
            ok &= _rows_all(msw == 0)
        if water is not None:
            ok &= _rows_all(water != 1)
        if sub is not None:
            ok &= _rows_all(sub == 1)
        for k, i in enumerate(idx):
            rows.append({
                "lon": float(lon[i]), "lat": float(lat[i]), "h_ellipsoid_m": float(h[k]),
                "uncertainty_m": float(unc[k]) if unc[k] < 1e30 else None,
                "quality_flag": int(ok[k]), "beam": beam,
            })
    return rows


def read_gedi(f, bbox) -> list[dict]:
    """Huellas GEDI L2A dentro del bbox. quality_flag = 1 solo si quality_flag == 1, degrade_flag == 0 y
    elevation_bias_flag == 0 (si existe), con elev_lowestmode finito en [-1000, 25000] m y, si el granulo trae
    ``digital_elevation_model`` (TanDEM-X, elipsoide WGS84), a no mas de GEDI_MAX_DEM_DIFF_M de el."""
    rows: list[dict] = []
    for beam in sorted(k for k in f.keys() if re.fullmatch(r"BEAM\d{4}", k)):
        g = f[beam]
        if "lat_lowestmode" not in g or "lon_lowestmode" not in g or "elev_lowestmode" not in g:
            continue
        lat = np.asarray(g["lat_lowestmode"][:], dtype="float64")
        lon = np.asarray(g["lon_lowestmode"][:], dtype="float64")
        idx = _bbox_idx(lat, lon, bbox)
        if idx.size == 0:
            continue
        i0, i1 = int(idx.min()), int(idx.max()) + 1
        z = _take(g, "elev_lowestmode", idx, i0, i1, required=True).astype("float64")
        q = _take(g, "quality_flag", idx, i0, i1, required=True)
        deg = _take(g, "degrade_flag", idx, i0, i1, required=True)
        bias = _take(g, "elevation_bias_flag", idx, i0, i1)
        ok = np.isfinite(z) & (z > -1000.0) & (z < 25000.0) & (q == 1) & (deg == 0)
        if bias is not None:
            ok &= bias == 0
        dem = _take(g, "digital_elevation_model", idx, i0, i1)
        if dem is not None:
            dem = dem.astype("float64")
            with np.errstate(invalid="ignore"):
                ok &= np.isfinite(dem) & (dem > -1000.0) & (np.abs(z - dem) <= GEDI_MAX_DEM_DIFF_M)
        for k, i in enumerate(idx):
            rows.append({
                "lon": float(lon[i]), "lat": float(lat[i]), "h_ellipsoid_m": float(z[k]), "uncertainty_m": None,
                "quality_flag": int(ok[k]), "beam": beam,
            })
    return rows


# --- CMR y apertura de granulos ---

def _fmt(v: float) -> str:
    return repr(round(float(v), 6))


def _http_get(url, params, timeout):
    import requests

    return requests.get(url, params=params, timeout=timeout)


def cmr_search_granules(
    short_name: str,
    version: str,
    bbox: tuple[float, float, float, float],
    *,
    start: str | None = None,
    end: str | None = None,
    getter: Callable | None = None,
    page_size: int = 200,
    max_pages: int = 25,
    timeout: float = 60.0,
) -> list[GranuleRef]:
    """Granulos que intersectan el bbox (w, s, e, n), ordenados por fecha. Busqueda publica (sin credenciales)."""
    getter = getter or _http_get
    params = {
        "short_name": short_name, "version": version,
        "bounding_box": ",".join(_fmt(v) for v in bbox),
        "page_size": page_size, "sort_key": "start_date",
    }
    if start or end:
        params["temporal"] = f"{start + 'T00:00:00Z' if start else ''},{end + 'T23:59:59Z' if end else ''}"
    refs: list[GranuleRef] = []
    for page in range(1, max_pages + 1):
        params["page_num"] = page
        try:
            resp = getter(CMR_URL, params, timeout)
        except Exception as exc:
            raise RejectRequest(f"no se pudo consultar CMR: {exc}")
        if getattr(resp, "status_code", None) != 200:
            raise RejectRequest(f"CMR devolvio HTTP {getattr(resp, 'status_code', None)}")
        entries = resp.json().get("feed", {}).get("entry", [])
        if not entries:
            break
        for e in entries:
            url = next(
                (l["href"] for l in e.get("links", [])
                 if l.get("rel", "").endswith("/data#") and l.get("href", "").startswith("https://")
                 and l["href"].endswith(".h5")),
                None,
            )
            if url:
                refs.append(GranuleRef(url=url, date=str(e.get("time_start", ""))[:10],
                                       granule_id=e.get("producer_granule_id", "")))
    return refs


def open_granule(url: str):
    """Abre un granulo HDF5 (h5py.File). Ruta local -> directo; URL -> earthaccess (no interactivo)."""
    import h5py

    if not str(url).startswith("http"):
        return h5py.File(url, "r")
    import os

    import earthaccess
    from earthaccess.auth import netrc_path

    have_env = bool(os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"))
    if not have_env and not Path(netrc_path()).exists():
        raise RejectRequest(
            "faltan credenciales NASA Earthdata: defina EARTHDATA_USERNAME y EARTHDATA_PASSWORD, o cree el archivo "
            f"{netrc_path()} (en Windows earthaccess usa _netrc o la variable NETRC)"
        )
    auth = None
    for strat in ("environment", "netrc"):
        try:
            a = earthaccess.login(strategy=strat, persist=False)
            if a.authenticated:
                auth = a
                break
        except Exception:
            continue
    if auth is None:
        raise RejectRequest("no se pudo iniciar sesion en NASA Earthdata con las credenciales disponibles")
    files = earthaccess.open([url])
    if not files:
        raise RejectRequest(f"earthaccess no pudo abrir {url}")
    return h5py.File(files[0], "r")


# --- fetch_footprints ---

READ_ATTEMPTS = 3
RETRY_BACKOFF_S = (3.0, 8.0)  # espera tras el 1.er y el 2.o fallo transitorio


_is_transient = is_transient


def _read_granule(opener: Callable, reader: Callable, url: str, bbox) -> list[dict]:
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            fobj = opener(url)
            try:
                return reader(fobj, bbox)
            finally:
                close = getattr(fobj, "close", None)
                if close:
                    close()
        except RejectRequest:
            raise
        except Exception as exc:
            if not _is_transient(exc):
                raise
            if attempt == READ_ATTEMPTS:
                raise RejectRequest(
                    f"granulo no disponible tras {READ_ATTEMPTS} intentos: {url} ({type(exc).__name__}: {exc})") from exc
            time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S)) - 1])
    raise AssertionError("inalcanzable")  # pragma: no cover


def _cache_read(path: Path):
    """Filas cacheadas o None (no existe o JSON ilegible: se vuelve a leer el granulo, no se revienta)."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def _cache_write(path: Path, rows: list) -> None:
    """Escritura atomica: temporal en la misma carpeta + os.replace (nunca queda un JSON truncado)."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    os.replace(tmp, path)


def _bbox_with_buffer(poly: Polygon, buffer_m: float) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = poly.bounds
    lat_mid = (miny + maxy) / 2.0
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * max(math.cos(math.radians(lat_mid)), 0.1))
    return (minx - dlon, miny - dlat, maxx + dlon, maxy + dlat)


def fetch_footprints(
    poly: Polygon,
    buffer_m: float = 1000.0,
    cache_dir: Path | None = None,
    *,
    sources: tuple[str, ...] = ("atl08", "gedi"),
    search: Callable | None = None,
    opener: Callable | None = None,
    geoid_fn: Callable | None = None,
    start: str | None = None,
    end: str | None = None,
    max_granules: int | None = None,
) -> list[Footprint]:
    """Huellas ATL08 / GEDI dentro del poligono + ``buffer_m``, con altura ortometrica EGM2008 (asertada)."""
    if not isinstance(poly, Polygon) or poly.is_empty or not poly.is_valid:
        raise RejectRequest("poligono invalido para fetch_footprints")
    if not (buffer_m >= 0 and math.isfinite(buffer_m)):
        raise RejectRequest("buffer_m invalido")
    minx, miny, maxx, maxy = poly.bounds
    for x, y in ((minx, miny), (maxx, maxy)):
        utm_epsg_for(x, y)
    unknown = set(sources) - set(PRODUCTS)
    if unknown:
        raise RejectRequest(f"fuente de verdad desconocida: {sorted(unknown)}")
    bbox = _bbox_with_buffer(poly, buffer_m)
    search = search or cmr_search_granules
    opener = opener or open_granule
    cache = Path(cache_dir) if cache_dir is not None else None
    if cache is not None:
        (cache / "truth").mkdir(parents=True, exist_ok=True)

    raw: list[tuple[str, GranuleRef, dict]] = []
    for src in sources:
        short, version = PRODUCTS[src]
        kw = {}
        if start:
            kw["start"] = start
        if end:
            kw["end"] = end
        refs = search(short, version, bbox, **kw)
        if max_granules is not None:
            refs = refs[:max_granules]
        reader = read_atl08 if src == "atl08" else read_gedi
        for ref in refs:
            extra = SOURCE_RULES_VERSION.get(src, "")
            rules = f"{QUALITY_RULES_VERSION}/{src}{extra}" if extra else QUALITY_RULES_VERSION
            tag = hashlib.sha1(f"{rules}|{ref.url}|{[round(v, 6) for v in bbox]}".encode()).hexdigest()[:20]
            cpath = cache / "truth" / f"{src}_{tag}.json" if cache is not None else None
            rows = _cache_read(cpath) if cpath is not None else None
            if rows is None:
                rows = _read_granule(opener, reader, ref.url, bbox)
                if cpath is not None:
                    _cache_write(cpath, rows)
            raw.extend((src, ref, r) for r in rows)

    if not raw:
        return []
    lon = np.array([r["lon"] for _, _, r in raw])
    lat = np.array([r["lat"] for _, _, r in raw])
    h = np.array([r["h_ellipsoid_m"] for _, _, r in raw])
    H = ellipsoid_to_orthometric(lon, lat, h, geoid_fn=geoid_fn)
    out = []
    for (src, ref, r), Hi in zip(raw, H):
        out.append(Footprint(
            lon=r["lon"], lat=r["lat"], h_ellipsoid_m=r["h_ellipsoid_m"], h_orthometric_m=float(Hi), source=src,
            date=ref.date, quality_flag=int(r["quality_flag"]), uncertainty_m=r.get("uncertainty_m"),
            beam=r.get("beam", ""), granule=ref.granule_id,
        ))
    return out


# --- validacion ---

def _horn_slope_pct(arr: np.ndarray, res: float) -> np.ndarray:
    p = np.pad(arr.astype("float64"), 1, constant_values=np.nan)
    z1, z2, z3 = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    z4, z6 = p[1:-1, :-2], p[1:-1, 2:]
    z7, z8, z9 = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8.0 * res)
    dzdy = ((z7 + 2 * z8 + z9) - (z1 + 2 * z2 + z3)) / (8.0 * res)
    return 100.0 * np.hypot(dzdx, dzdy)


def _stats(diff: np.ndarray) -> dict:
    n = int(diff.size)
    if n < MIN_FOOTPRINTS:
        return {"n": n, "status": "validacion_insuficiente", "bias_m": None, "mae_m": None, "rmse_m": None}
    return {
        "n": n, "status": "ok", "bias_m": float(diff.mean()), "mae_m": float(np.abs(diff).mean()),
        "rmse_m": float(math.sqrt(float((diff ** 2).mean()))),
    }


@dataclass
class Residuals:
    """Residuos DEM - verdad de las huellas utilizables (calidad buena y dentro del raster, sin NaN en su celda)."""
    diffs: np.ndarray
    slopes: np.ndarray
    sources: np.ndarray
    used: list  # list[Footprint], en el mismo orden que ``diffs``
    n_outside_raster: int
    n_excluded_quality: int


def usable_ids(raster: "Raster", fps: list[Footprint]) -> set:  # noqa: F821
    """``id()`` de las huellas utilizables en ``raster`` (buena calidad, dentro y sin NaN en su celda). Sirve para
    puntuar dos DEM sobre el MISMO conjunto de huellas: ``restrict_to = usable_ids(a) & usable_ids(b)``."""
    return {id(f) for f in sample_residuals(raster, fps).used}


def sample_residuals(raster: "Raster", fps: list[Footprint], *, restrict_to: set | None = None) -> Residuals:  # noqa: F821
    """Muestrea el DEM (bilineal) en cada huella de buena calidad. Sesgo = DEM - verdad. Base de ``validate`` y de Tier 2.

    ``restrict_to``: conjunto de ``id(footprint)`` permitidos; las huellas buenas que no estan en el se cuentan en
    ``n_outside_raster`` (no pertenecen al conjunto comun) y no entran a los estadisticos.
    """
    good = [f for f in fps if f.quality_flag == 1]
    n_bad = len(fps) - len(good)
    with rasterio.open(raster.path) as src:
        arr = src.read(1).astype("float64")
        nodata = src.nodata
        tf = src.transform
        epsg = src.crs.to_epsg() if src.crs else None
    if nodata is not None and not math.isnan(nodata):
        arr[arr == nodata] = np.nan
    if epsg is None or epsg != raster.epsg:
        raise RejectRequest(f"CRS del raster ({epsg}) inconsistente con el declarado ({raster.epsg})")

    diffs, slopes, srcs, used = [], [], [], []
    n_out = 0
    if restrict_to is not None:
        n_out += sum(1 for f in good if id(f) not in restrict_to)
        good = [f for f in good if id(f) in restrict_to]
    if good:
        fwd = Transformer.from_crs(4326, epsg, always_xy=True)
        x, y = fwd.transform([f.lon for f in good], [f.lat for f in good])
        col = (np.asarray(x) - tf.c) / tf.a - 0.5
        row = (np.asarray(y) - tf.f) / tf.e - 0.5
        slope = _horn_slope_pct(arr, float(abs(tf.a)))
        h, w = arr.shape
        for f, cf, rf in zip(good, col, row):
            if not (0.0 <= cf <= w - 1 and 0.0 <= rf <= h - 1):
                n_out += 1
                continue
            c0, r0 = int(math.floor(cf)), int(math.floor(rf))
            c1, r1 = min(c0 + 1, w - 1), min(r0 + 1, h - 1)
            dc, dr = cf - c0, rf - r0
            z = np.array([arr[r0, c0], arr[r0, c1], arr[r1, c0], arr[r1, c1]])
            if not np.isfinite(z).all():
                n_out += 1
                continue
            val = (z[0] * (1 - dc) + z[1] * dc) * (1 - dr) + (z[2] * (1 - dc) + z[3] * dc) * dr
            diffs.append(val - f.h_orthometric_m)
            s = slope[int(round(rf)), int(round(cf))]
            slopes.append(float(s) if np.isfinite(s) else float("nan"))
            srcs.append(f.source)
            used.append(f)
    return Residuals(diffs=np.asarray(diffs, dtype="float64"), slopes=np.asarray(slopes, dtype="float64"),
                     sources=np.asarray(srcs), used=used, n_outside_raster=n_out, n_excluded_quality=n_bad)


def validate(raster: "Raster", fps: list[Footprint], *, restrict_to: set | None = None) -> ValidationResult:  # noqa: F821
    """Compara el DEM (muestreo bilineal) con las huellas de buena calidad. Sesgo = DEM - verdad.

    Con menos de 10 huellas utilizables NO se calculan estadisticos ("validacion_insuficiente").
    """
    res = sample_residuals(raster, fps, restrict_to=restrict_to)
    d, sl, sr = res.diffs, res.slopes, res.sources
    base = _stats(d)
    by_source = {s: _stats(d[sr == s]) for s in sorted(set(sr.tolist()) | {f.source for f in fps})}
    hi = np.isfinite(sl) & (sl >= HIGH_SLOPE_PCT)
    lo = np.isfinite(sl) & (sl < HIGH_SLOPE_PCT)
    by_slope = {"<15": _stats(d[lo]), ">=15": _stats(d[hi])}
    return ValidationResult(
        n=base["n"], bias_m=base["bias_m"], mae_m=base["mae_m"], rmse_m=base["rmse_m"], status=base["status"],
        n_excluded_quality=res.n_excluded_quality, n_outside_raster=res.n_outside_raster, by_source=by_source, by_slope=by_slope,
    )
