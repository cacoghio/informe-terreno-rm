import json
from pathlib import Path

import pytest
from pyproj import Geod
from shapely.geometry import Polygon, shape

from terreno.errors import RejectRequest
from terreno.geometry import (
    SITES,
    centroid_zone,
    load_polygon,
    utm_epsg_for,
    utm_zone_info,
)

GEOD = Geod(ellps="WGS84")


def box(lon0, lat0, lon1, lat1):
    return {
        "type": "Polygon",
        "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
    }


def area_m2(poly):
    return abs(GEOD.geometry_area_perimeter(poly)[0])


def test_utm_zone_boundary():
    assert utm_epsg_for(-72.0001, -33.0) == 32718
    assert utm_epsg_for(-72.0, -33.0) == 32719
    assert utm_epsg_for(-71.9999, -33.0) == 32719


def test_utm_zone_extremes_inside_chile_box():
    assert utm_epsg_for(-76.0, -17.0) == 32718
    assert utm_epsg_for(-66.0, -56.0) == 32719


def test_outside_chile_rejected():
    with pytest.raises(RejectRequest) as e:
        utm_epsg_for(10.0, 50.0)
    assert e.value.reason
    with pytest.raises(RejectRequest):
        load_polygon(box(10.0, 50.0, 10.002, 50.002))


@pytest.mark.parametrize("lon,lat", [(float("nan"), -33.0), (-70.0, float("inf")), (-76.01, -33.0), (-65.99, -33.0), (-70.0, -16.99), (-70.0, -56.01)])
def test_zone_rejects_nonfinite_and_out_of_box(lon, lat):
    with pytest.raises(RejectRequest):
        utm_epsg_for(lon, lat)


def test_polygon_too_large_rejected():
    # ~0.03 deg x 0.03 deg a -33 => ~ 2.8 km x 3.3 km ~ 9.3 km2
    g = box(-70.60, -33.45, -70.57, -33.42)
    assert area_m2(shape(g)) > 5_000_000
    with pytest.raises(RejectRequest) as e:
        load_polygon(g)
    assert "5 km" in e.value.reason


def test_polygon_just_under_limit_accepted():
    # ~0.02 x 0.02 deg => ~1.9 x 2.2 km ~ 4.1 km2
    g = box(-70.60, -33.45, -70.58, -33.43)
    assert 3_000_000 < area_m2(shape(g)) < 5_000_000
    assert isinstance(load_polygon(g), Polygon)


def test_inverted_or_empty_polygon_rejected():
    empty = {"type": "Polygon", "coordinates": []}
    with pytest.raises(RejectRequest):
        load_polygon(empty)
    # ejes invertidos (lat, lon): cae fuera de Chile
    swapped = {"type": "Polygon", "coordinates": [[[-33.45, -70.60], [-33.45, -70.59], [-33.44, -70.59], [-33.44, -70.60], [-33.45, -70.60]]]}
    with pytest.raises(RejectRequest):
        load_polygon(swapped)
    # anillo sin cerrar
    unclosed = {"type": "Polygon", "coordinates": [[[-70.60, -33.45], [-70.59, -33.45], [-70.59, -33.44], [-70.60, -33.44]]]}
    with pytest.raises(RejectRequest) as e:
        load_polygon(unclosed)
    assert "cerr" in e.value.reason
    # area cero (todos los puntos colineales)
    flat = {"type": "Polygon", "coordinates": [[[-70.60, -33.45], [-70.59, -33.45], [-70.58, -33.45], [-70.60, -33.45]]]}
    with pytest.raises(RejectRequest):
        load_polygon(flat)
    # autointersecta (corbata)
    bow = {"type": "Polygon", "coordinates": [[[-70.60, -33.45], [-70.59, -33.44], [-70.59, -33.45], [-70.60, -33.44], [-70.60, -33.45]]]}
    with pytest.raises(RejectRequest):
        load_polygon(bow)
    # coordenadas no finitas
    nan = box(-70.60, -33.45, float("nan"), -33.44)
    with pytest.raises(RejectRequest):
        load_polygon(nan)


@pytest.mark.parametrize("bad", [None, [], "texto", {"type": "Point", "coordinates": [-70.0, -33.0]}, {"type": "FeatureCollection", "features": []}, {"type": "Polygon"}])
def test_malformed_geojson_rejected(bad):
    with pytest.raises(RejectRequest):
        load_polygon(bad)


def test_multipolygon_rejected():
    mp = {"type": "MultiPolygon", "coordinates": [box(-70.6, -33.45, -70.59, -33.44)["coordinates"]]}
    with pytest.raises(RejectRequest):
        load_polygon(mp)


def test_feature_wrapper_and_clockwise_accepted_and_z_dropped():
    g = {"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon",
        "coordinates": [[[-70.60, -33.45, 0.0], [-70.60, -33.44, 0.0], [-70.59, -33.44, 0.0], [-70.59, -33.45, 0.0], [-70.60, -33.45, 0.0]]]}}
    p = load_polygon(g)
    assert not p.has_z
    assert p.exterior.is_ccw


def test_polygon_crossing_72w_has_single_declared_zone():
    # caja que cruza -72: centroide en -72.0001 -> 18S; en -71.9999 -> 19S
    west = load_polygon(box(-72.0020, -33.0010, -71.9982, -32.9990))  # centroide -72.0001
    east = load_polygon(box(-72.0018, -33.0010, -71.9980, -32.9990))  # centroide -71.9999
    assert west.bounds[0] < -72.0 < west.bounds[2]
    assert centroid_zone(west) == 32718
    assert centroid_zone(east) == 32719
    info = utm_zone_info(west)
    assert info["epsg"] == 32718
    assert info["cruza_limite_de_zona"] is True
    assert "centroide" in info["regla"]
    assert utm_zone_info(load_polygon(box(-70.60, -33.45, -70.59, -33.44)))["cruza_limite_de_zona"] is False


def test_sites_json_has_eight_rm_pilots():
    assert set(SITES) == {
        "rm01_plano_urbano", "rm02_plano_periurbano", "rm03_piedemonte_oriente", "rm04_piedemonte_alto",
        "rm05_piedemonte_sur", "rm06_cerro_isla_urbano", "rm07_lomaje_norte", "rm08_precordillera",
    }
    for key, s in SITES.items():
        assert {"nombre", "polygon", "fuente_poligono", "verdad_de_terreno"} <= set(s), key
        assert s["fuente_poligono"] == "lote de prueba estándar (datos públicos)", key
        p = load_polygon(s["polygon"])  # todos pasan la validacion
        assert centroid_zone(p) == 32719  # la RM esta toda en 19S


def test_rm_pilots_are_square_test_lots_inside_the_rm():
    for key, s in SITES.items():
        p = load_polygon(s["polygon"])
        a = area_m2(p)
        assert 600 <= a <= 90_000, (key, a)
        c = p.centroid
        assert -71.7 < c.x < -69.8 and -34.3 < c.y < -32.9, key
        assert abs(a - s["area_m2"]) / a < 0.01, key
