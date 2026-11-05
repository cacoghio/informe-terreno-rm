"""Geometria de entrada: zona UTM por regla explicita, validacion de poligonos y sitios piloto."""
from __future__ import annotations

import json
import math
from pathlib import Path

from pyproj import Geod
from shapely import force_2d
from shapely.geometry import Polygon, shape
from shapely.geometry.polygon import orient

from terreno.errors import RejectRequest

# Caja de Chile continental (aproximacion gruesa, ver LEDGER): lon [-76,-66], lat [-56,-17].
LON_MIN, LON_MAX = -76.0, -66.0
LAT_MIN, LAT_MAX = -56.0, -17.0
ZONE_SPLIT_LON = -72.0  # lon < -72 -> 32718 (18S); lon >= -72 -> 32719 (19S)
MAX_AREA_M2 = 5_000_000.0  # 5 km2

_GEOD = Geod(ellps="WGS84")
_SITES_PATH = Path(__file__).resolve().parent.parent / "data" / "sites.json"


def utm_epsg_for(lon: float, lat: float) -> int:
    """Zona UTM WGS84 para un punto: regla fija, nunca elegida por el agente."""
    try:
        lon_f, lat_f = float(lon), float(lat)
    except (TypeError, ValueError):
        raise RejectRequest("coordenadas no numericas")
    if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
        raise RejectRequest("coordenadas no finitas")
    if not (LON_MIN <= lon_f <= LON_MAX and LAT_MIN <= lat_f <= LAT_MAX):
        raise RejectRequest(
            f"punto fuera de Chile continental (lon {lon_f}, lat {lat_f}; "
            f"se acepta lon [{LON_MIN}, {LON_MAX}], lat [{LAT_MIN}, {LAT_MAX}])"
        )
    return 32718 if lon_f < ZONE_SPLIT_LON else 32719


def _extract_geometry(geojson: dict) -> dict:
    if not isinstance(geojson, dict):
        raise RejectRequest("GeoJSON invalido: se esperaba un objeto")
    t = geojson.get("type")
    if t == "Feature":
        geom = geojson.get("geometry")
        if not isinstance(geom, dict):
            raise RejectRequest("Feature sin geometria")
        return _extract_geometry(geom)
    if t == "Polygon":
        return geojson
    if t == "MultiPolygon":
        raise RejectRequest("solo se acepta un Polygon unico (recibido MultiPolygon)")
    raise RejectRequest(f"tipo de geometria no soportado: {t!r} (se esperaba Polygon)")


def load_polygon(geojson: dict) -> Polygon:
    """Valida un GeoJSON (EPSG:4326) y devuelve un Polygon 2D con anillo exterior antihorario."""
    geom = _extract_geometry(geojson)
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or len(coords) == 0:
        raise RejectRequest("poligono vacio (sin coordenadas)")

    for ring in coords:
        if not isinstance(ring, list) or len(ring) < 4:
            raise RejectRequest("anillo con menos de 4 posiciones")
        for pos in ring:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                raise RejectRequest("posicion mal formada")
            try:
                vals = [float(v) for v in pos]
            except (TypeError, ValueError):
                raise RejectRequest("coordenada no numerica")
            if not all(math.isfinite(v) for v in vals):
                raise RejectRequest("coordenada no finita")
        if list(ring[0][:2]) != list(ring[-1][:2]):
            raise RejectRequest("el anillo no esta cerrado (primer punto != ultimo)")

    try:
        poly = shape({"type": "Polygon", "coordinates": coords})
    except Exception as exc:  # shapely lanza varios tipos
        raise RejectRequest(f"poligono no construible: {exc}")
    if poly.is_empty:
        raise RejectRequest("poligono vacio")
    if poly.has_z:
        poly = force_2d(poly)
    if not poly.is_valid:
        raise RejectRequest("poligono invalido (autointerseccion o anillos degenerados)")
    if poly.area == 0.0:
        raise RejectRequest("poligono de area cero")

    minx, miny, maxx, maxy = poly.bounds
    # Las esquinas del bbox deben estar dentro de la caja de Chile (cubre ejes invertidos lat/lon).
    for x, y in ((minx, miny), (maxx, maxy)):
        utm_epsg_for(x, y)

    area = abs(_GEOD.geometry_area_perimeter(poly)[0])
    if area <= 0.0:
        raise RejectRequest("poligono de area cero")
    if area > MAX_AREA_M2:
        raise RejectRequest(
            f"poligono demasiado grande: {area / 1e6:.2f} km2 (maximo 5 km2)"
        )
    return orient(poly, sign=1.0)


def centroid_zone(poly: Polygon) -> int:
    """Zona UTM del centroide. Regla unica incluso si el poligono cruza 72 O."""
    c = poly.centroid
    return utm_epsg_for(c.x, c.y)


def utm_zone_info(poly: Polygon) -> dict:
    """Declaracion de zona para el manifiesto (revision enfocada 1)."""
    minx, _, maxx, _ = poly.bounds
    return {
        "epsg": centroid_zone(poly),
        "regla": "zona del centroide: lon < -72 -> 32718 (UTM 18S); lon >= -72 -> 32719 (UTM 19S)",
        "cruza_limite_de_zona": bool(minx < ZONE_SPLIT_LON < maxx),
    }


def _load_sites() -> dict[str, dict]:
    with open(_SITES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


SITES: dict[str, dict] = _load_sites()
