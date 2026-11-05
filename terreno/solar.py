"""Asoleamiento por relieve (spec sec. 3 #7): perfil de horizonte desde el centroide del predio y horas de sol directo.

Solo se modela el RELIEVE que captura el DSM dentro de la ventana de trabajo del pipeline (bbox del poligono +- 3 px,
~100-250 m alrededor del predio): NO incluye cerros lejanos ni edificios vecinos y NO es un estudio de sombras de
proyecto. Las horas son un maximo teorico sin nubosidad (sol geometrico, sin refraccion). Todo sale al manifiesto
(``results.solar``); el informe solo lee de ahi.
"""
from __future__ import annotations

import datetime as _dt
import math

import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from shapely.geometry import Polygon

from terreno.asserts import valid_mask
from terreno.errors import RejectRequest
from terreno.geometry import centroid_zone
from terreno.reproject import Raster

SUN_DATES = (
    ("equinoccio de marzo", "2026-03-20"),
    ("solsticio de junio", "2026-06-21"),
    ("equinoccio de septiembre", "2026-09-22"),
    ("solsticio de diciembre", "2026-12-21"),
)
HORIZON_STEP_DEG = 10
RAY_STEP_M = 15.0
TIME_STEP_MIN = 5
SOLAR_SCOPE = (
    "solo el relieve que captura el DSM dentro de la ventana de trabajo alrededor del predio; sin cerros lejanos, sin "
    "edificios vecinos, sin nubosidad; no es un estudio de sombras de proyecto"
)


def declination_deg(date_iso: str) -> float:
    """Declinacion solar (Spencer 1971), grados; precision ~0,3 grados, suficiente para screening."""
    n = _dt.date.fromisoformat(date_iso).timetuple().tm_yday
    b = 2.0 * math.pi * (n - 1) / 365.0
    d = (0.006918 - 0.399912 * math.cos(b) + 0.070257 * math.sin(b) - 0.006758 * math.cos(2 * b)
         + 0.000907 * math.sin(2 * b) - 0.002697 * math.cos(3 * b) + 0.00148 * math.sin(3 * b))
    return math.degrees(d)


def sun_position(lat_deg: float, date_iso: str, solar_hour: float) -> tuple[float, float]:
    """(elevacion, azimut desde el norte en sentido horario) en grados, para una hora SOLAR local (12 = mediodia solar)."""
    phi, dec = math.radians(lat_deg), math.radians(declination_deg(date_iso))
    h = math.radians(15.0 * (solar_hour - 12.0))
    sin_el = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(h)
    el = math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))
    az = math.degrees(math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(dec) * math.cos(phi))) + 180.0
    return el, az % 360.0


def _bilinear(arr, valid, t, x, y):
    """Valor bilineal en (x, y) UTM sobre centros de celda; NaN si algun vecino es invalido o cae fuera."""
    cf = (np.asarray(x) - t.c) / t.a - 0.5
    rf = (np.asarray(y) - t.f) / t.e - 0.5
    n_rows, n_cols = arr.shape
    c0, r0 = np.floor(cf).astype(int), np.floor(rf).astype(int)
    out = np.full(np.shape(cf), np.nan)
    inside = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < n_cols) & (r0 + 1 < n_rows)
    c0i, r0i = np.clip(c0, 0, n_cols - 2), np.clip(r0, 0, n_rows - 2)
    fc, fr = cf - c0i, rf - r0i
    v = (arr[r0i, c0i] * (1 - fr) * (1 - fc) + arr[r0i, c0i + 1] * (1 - fr) * fc
         + arr[r0i + 1, c0i] * fr * (1 - fc) + arr[r0i + 1, c0i + 1] * fr * fc)
    ok = inside & valid[r0i, c0i] & valid[r0i, c0i + 1] & valid[r0i + 1, c0i] & valid[r0i + 1, c0i + 1]
    out[ok] = v[ok]
    return out


bilinear_at = _bilinear  # API publica para las figuras del informe


def horizon_profile(arr, valid, t, x0, y0, z0, bounds):
    """Angulo de elevacion maximo del relieve visto desde (x0, y0, z0), cada HORIZON_STEP_DEG; y largo de cada rayo (m)."""
    left, bottom, right, top = bounds
    angles, lengths = [], []
    for az in range(0, 360, HORIZON_STEP_DEG):
        dx, dy = math.sin(math.radians(az)), math.cos(math.radians(az))
        lim = []
        if dx > 1e-12:
            lim.append((right - x0) / dx)
        elif dx < -1e-12:
            lim.append((left - x0) / dx)
        if dy > 1e-12:
            lim.append((top - y0) / dy)
        elif dy < -1e-12:
            lim.append((bottom - y0) / dy)
        dmax = max(0.0, min(lim)) - t.a  # una celda de seguridad: la bilineal necesita vecinos
        lengths.append(max(dmax, 0.0))
        if dmax < RAY_STEP_M:
            angles.append(float("nan"))
            continue
        d = np.arange(RAY_STEP_M, dmax + 1e-9, RAY_STEP_M)
        z = _bilinear(arr, valid, t, x0 + dx * d, y0 + dy * d)
        ang = np.degrees(np.arctan2(z - z0, d))
        ang = ang[np.isfinite(ang)]
        angles.append(float(ang.max()) if ang.size else float("nan"))
    return angles, lengths


def _horizon_at(horizon: list[float], az: float) -> float:
    """Interpolacion circular del horizonte; el sol solo se bloquea por horizonte > 0."""
    s = az / HORIZON_STEP_DEG
    i0 = int(math.floor(s)) % len(horizon)
    i1 = (i0 + 1) % len(horizon)
    f = s - math.floor(s)
    return max(0.0, (1 - f) * horizon[i0] + f * horizon[i1])


def compute_solar(r: Raster, poly: Polygon) -> dict:
    """Dict para ``results.solar`` del manifiesto. Nunca lanza: si no se puede, status='no_disponible' con el motivo."""
    try:
        epsg = centroid_zone(poly)
        fwd = Transformer.from_crs(4326, epsg, always_xy=True)
        poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
        with rasterio.open(r.path) as src:
            arr = src.read(1).astype("float64")
            valid = valid_mask(src.read(1).astype("float32"), src.nodata)
            t, bounds = src.transform, tuple(src.bounds)
        x0, y0 = poly_utm.centroid.x, poly_utm.centroid.y
        z0 = float(_bilinear(arr, valid, t, np.array([x0]), np.array([y0]))[0])
        if not math.isfinite(z0):
            return {"status": "no_disponible", "reason": "el centroide del predio cae en una celda sin dato"}
        angles, lengths = horizon_profile(arr, valid, t, x0, y0, z0, bounds)
        if not any(math.isfinite(a) for a in angles):
            return {"status": "no_disponible", "reason": "la ventana de trabajo es demasiado chica para trazar el horizonte"}
        # rayos sin muestras (nan): se rellenan con el vecino mas cercano para no inventar un horizonte despejado
        good = [i for i, a in enumerate(angles) if math.isfinite(a)]
        for i, a in enumerate(angles):
            if not math.isfinite(a):
                j = min(good, key=lambda k: min((k - i) % len(angles), (i - k) % len(angles)))
                angles[i] = angles[j]
        lat = poly.centroid.y
        dates = {}
        for label, iso in SUN_DATES:
            hours = np.arange(0.0, 24.0, TIME_STEP_MIN / 60.0)
            up = direct = 0
            noon_el = sun_position(lat, iso, 12.0)[0]
            for h in hours:
                el, az = sun_position(lat, iso, float(h) + TIME_STEP_MIN / 120.0)  # centro del intervalo
                if el > 0.0:
                    up += 1
                    if el > _horizon_at(angles, az):
                        direct += 1
            dates[iso] = {
                "label": label,
                "day_length_h": round(up * TIME_STEP_MIN / 60.0, 2),
                "direct_sun_h": round(direct * TIME_STEP_MIN / 60.0, 2),
                "noon_elevation_deg": round(noon_el, 2),
            }
        return {
            "status": "ok",
            "method": "horizonte por trazado de rayos sobre el DSM (rayos cada 10 grados, paso 15 m) y sol geometrico "
                      "(Spencer 1971) muestreado cada 5 min en hora solar local; sin refraccion ni nubosidad",
            "scope": SOLAR_SCOPE,
            "latitude": round(lat, 5),
            "viewpoint": "centroide del predio, a nivel del suelo (DSM)",
            "viewpoint_elevation_m": round(z0, 2),
            "horizon_step_deg": HORIZON_STEP_DEG,
            "horizon_deg": [round(a, 2) for a in angles],
            "ray_length_m": {"min": round(min(lengths), 1), "max": round(max(lengths), 1)},
            "dates": dates,
        }
    except (RejectRequest, rasterio.errors.RasterioError, ValueError) as exc:  # no se adivina: se declara que no hay dato
        reason = getattr(exc, "reason", str(exc))
        return {"status": "no_disponible", "reason": f"no se pudo calcular el asoleamiento: {reason}"}
