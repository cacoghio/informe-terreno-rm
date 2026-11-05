import json
import os

import h5py
import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Polygon, box

from terreno.errors import RejectRequest
from terreno.truth import (
    ATL08_FILL,
    Footprint,
    GranuleRef,
    ValidationResult,
    assert_geoid_function,
    cmr_search_granules,
    ellipsoid_to_orthometric,
    fetch_footprints,
    read_atl08,
    read_gedi,
    validate,
)
from tests.fixtures.rasters import utm_ramp

LEFT, TOP, RES, N = 350000.0, 6298000.0, 30.0, 20
TO_LL = Transformer.from_crs(32719, 4326, always_xy=True)
TO_UTM = Transformer.from_crs(4326, 32719, always_xy=True)


def raster(tmp_path, **kw):
    return utm_ramp(tmp_path / "dem.tif", n=N, res=RES, left=LEFT, top=TOP, **kw)


def fps_on(r, n, *, add=0.0, source="atl08", flag=1, slope_x=0.1, base=600.0, x0=2.5, dx=0.5):
    """Huellas sobre el plano de `raster`: z = base + slope_x*(x-LEFT); h_orto = z + add."""
    out = []
    for i in range(n):
        x = LEFT + (x0 + dx * i) * RES
        y = TOP - (6.0 + 0.3 * (i % 5)) * RES
        lon, lat = TO_LL.transform(x, y)
        z = base + slope_x * (x - LEFT)
        out.append(Footprint(lon=lon, lat=lat, h_ellipsoid_m=z + add + 27.0, h_orthometric_m=z + add,
                             source=source, date="2024-01-01", quality_flag=flag))
    return out


# --- validate ---

def test_validate_known_bias(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    res = validate(r, fps_on(r, 20, add=3.0))  # huellas = raster + 3 m
    assert isinstance(res, ValidationResult)
    assert res.status == "ok" and res.n == 20
    assert res.bias_m == pytest.approx(-3.0, abs=1e-3)  # sesgo = raster - verdad
    assert res.mae_m == pytest.approx(3.0, abs=1e-3)
    assert res.rmse_m == pytest.approx(3.0, abs=1e-3)


def test_fewer_than_10_footprints_insufficient(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    res = validate(r, fps_on(r, 9, add=3.0))
    assert res.status == "validacion_insuficiente"
    assert res.n == 9
    assert res.bias_m is None and res.mae_m is None and res.rmse_m is None  # no se calcula


def test_zero_footprints_insufficient_not_crash(tmp_path):
    r = raster(tmp_path)
    res = validate(r, [])
    assert res.status == "validacion_insuficiente" and res.n == 0
    assert res.rmse_m is None


def test_bad_quality_footprints_excluded(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    good = fps_on(r, 12, add=1.0)
    bad = fps_on(r, 30, add=500.0, flag=0)  # error enorme, pero con mala calidad
    res = validate(r, good + bad)
    assert res.n == 12 and res.n_excluded_quality == 30
    assert res.bias_m == pytest.approx(-1.0, abs=1e-3)
    assert res.rmse_m == pytest.approx(1.0, abs=1e-3)


def test_only_bad_quality_is_insufficient(tmp_path):
    r = raster(tmp_path)
    res = validate(r, fps_on(r, 50, flag=0))
    assert res.status == "validacion_insuficiente" and res.n == 0 and res.n_excluded_quality == 50


def test_footprints_outside_raster_counted_not_used(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    inside = fps_on(r, 10, add=2.0)
    far_lon, far_lat = TO_LL.transform(LEFT + 50000.0, TOP - 50000.0)
    outside = [Footprint(far_lon, far_lat, 900.0, 870.0, "gedi", "2024-01-01", 1)] * 5
    res = validate(r, inside + outside)
    assert res.n == 10 and res.n_outside_raster == 5
    assert res.status == "ok"


def test_high_slope_footprints_kept_and_reported_separately(tmp_path):
    flat = raster(tmp_path, slope_x=0.0)
    steep = utm_ramp(tmp_path / "steep.tif", n=N, res=RES, left=LEFT, top=TOP, slope_x=0.3)  # 30 %

    res_steep = validate(steep, fps_on(steep, 14, add=4.0, slope_x=0.3))
    assert res_steep.status == "ok" and res_steep.n == 14  # NO se descartan
    assert res_steep.by_slope[">=15"]["n"] == 14
    assert res_steep.by_slope[">=15"]["bias_m"] == pytest.approx(-4.0, abs=1e-3)
    assert res_steep.by_slope["<15"]["n"] == 0
    res_flat = validate(flat, fps_on(flat, 14, add=1.0, slope_x=0.0))
    assert res_flat.by_slope["<15"]["n"] == 14 and res_flat.by_slope[">=15"]["n"] == 0


def test_by_source_counts(tmp_path):
    r = raster(tmp_path, slope_x=0.1)
    res = validate(r, fps_on(r, 12, source="atl08") + fps_on(r, 5, source="gedi", x0=6.0))
    assert res.by_source["atl08"]["n"] == 12 and res.by_source["gedi"]["n"] == 5
    assert res.by_source["gedi"]["status"] == "validacion_insuficiente"


# --- conversion elipsoide -> EGM2008 ---

def test_geoid_conversion_known_point():
    """Punto con ondulacion conocida: N = 27.2 m -> H = h - N (tolerancia 0.5 m del plan)."""
    geoid = lambda lon, lat: np.where(np.abs(np.asarray(lat)) < 1.0, 17.2, 27.2)  # mini malla: 2 nodos de control
    H = ellipsoid_to_orthometric(np.array([-70.6]), np.array([-33.45]), np.array([700.0]), geoid_fn=geoid)
    assert H[0] == pytest.approx(700.0 - 27.2, abs=0.5)


def test_geoid_function_asserted_against_control_points():
    good = lambda lon, lat: np.where(np.abs(lat) < 1.0, 17.2, 27.2)
    assert_geoid_function(good)  # no lanza
    wrong = lambda lon, lat: np.full(np.shape(lon), 0.0)  # ballpark / sin malla -> N = 0
    with pytest.raises(RejectRequest, match="geoide"):
        assert_geoid_function(wrong)
    with pytest.raises(RejectRequest, match="geoide"):
        ellipsoid_to_orthometric(np.array([-70.6]), np.array([-33.45]), np.array([700.0]), geoid_fn=wrong)


def test_geoid_nonfinite_rejected():
    bad = lambda lon, lat: np.full(np.shape(lon), np.nan)
    with pytest.raises(RejectRequest, match="geoide"):
        assert_geoid_function(bad)


# --- lectores HDF5 (archivos sinteticos con la estructura del diccionario de datos) ---

LON0, LAT0 = -70.6, -33.45
BBOX = (LON0 - 0.01, LAT0 - 0.01, LON0 + 0.01, LAT0 + 0.01)


def write_atl08(path, n_in=6, n_out=4):
    """gt1l con n_in segmentos dentro del bbox y n_out fuera; gt1r vacio; gt2l con 1 segmento malo."""
    with h5py.File(path, "w") as f:
        def beam(name, lats, lons, h, unc, **flags):
            g = f.create_group(f"{name}/land_segments")
            g["latitude"] = np.asarray(lats, dtype="float64")
            g["longitude"] = np.asarray(lons, dtype="float64")
            k = len(lats)
            for key, default in (("layer_flag", 0), ("msw_flag", 0), ("segment_watermask", 0), ("night_flag", 1)):
                g[key] = np.asarray(flags.get(key, [default] * k), dtype="int8")
            t = g.create_group("terrain")
            t["h_te_best_fit"] = np.asarray(h, dtype="float32")
            t["h_te_uncertainty"] = np.asarray(unc, dtype="float32")
            # Diccionario ATL08 v7: INTEGER_1(5, :) (en h5py (N, 5)); -1 = no_photon_data_within_geosegment,
            # 0 = no_terrain_photons_within_geosegment, 1 = terrain_photons_present_within_geosegment.
            # Un segmento bueno tiene 1 en sus cinco subsegmentos de 20 m.
            t["subset_te_flag"] = np.asarray(flags.get("subset_te_flag", np.ones((k, 5))), dtype="int8")
            t["n_te_photons"] = np.full(k, 50, dtype="int32")

        k = n_in + n_out
        lats = [LAT0 + 0.001 * i for i in range(n_in)] + [LAT0 + 0.5 + i for i in range(n_out)]
        lons = [LON0 + 0.0005 * i for i in range(n_in)] + [LON0 + 0.5 for _ in range(n_out)]
        beam("gt1l", lats, lons, [700.0 + i for i in range(k)], [0.5] * k)
        beam("gt1r", [LAT0 + 2.0], [LON0], [800.0], [0.5])  # sin segmentos en el bbox
        # gt2l: 1 valido, 1 con relleno INVALID_R4B, 1 con nube, 1 con agua, 1 con incertidumbre alta
        beam("gt2l", [LAT0 + 0.002 * i for i in range(5)], [LON0] * 5,
             [650.0, ATL08_FILL, 650.0, 650.0, 650.0], [0.4, 0.4, 0.4, 0.4, 40.0],
             layer_flag=[0, 0, 1, 0, 0], segment_watermask=[0, 0, 0, 1, 0])
    return path


def test_read_atl08_filters_by_bbox_and_flags(tmp_path):
    p = write_atl08(tmp_path / "ATL08.h5")
    with h5py.File(p, "r") as f:
        rows = read_atl08(f, BBOX)
    gt1l = [r for r in rows if r["beam"] == "gt1l"]
    assert len(gt1l) == 6 and all(r["quality_flag"] == 1 for r in gt1l)
    assert {r["beam"] for r in rows} == {"gt1l", "gt2l"}
    gt2l = [r for r in rows if r["beam"] == "gt2l"]
    assert [r["quality_flag"] for r in gt2l] == [1, 0, 0, 0, 0]  # relleno, nube, agua, incertidumbre>umbral
    assert gt1l[0]["h_ellipsoid_m"] == pytest.approx(700.0)
    assert all(BBOX[0] <= r["lon"] <= BBOX[2] and BBOX[1] <= r["lat"] <= BBOX[3] for r in rows)


def test_read_atl08_accepts_2d_flags_like_real_granules(tmp_path):
    """Granulos reales: subset_te_flag viene como (N, 5), un valor por subsegmento de 20 m (v7: 1 = hay fotones de
    terreno, 0 = no_terrain_photons, -1 = no_photon_data). El segmento de 100 m es bueno solo si los CINCO valen 1."""
    p = write_atl08(tmp_path / "ATL08.h5")
    with h5py.File(p, "r+") as f:
        name = "gt1l/land_segments/terrain/subset_te_flag"
        n = f[name].shape[0]
        arr = np.ones((n, 5), dtype="int8")
        assert f[name].shape == (n, 5)
        arr[1, 3] = 0   # un subsegmento sin fotones de terreno invalida el segmento de 100 m
        arr[2, 0] = -1  # un subsegmento sin datos de fotones tambien
        del f[name]
        f[name] = arr
    with h5py.File(p, "r") as f:
        rows = read_atl08(f, BBOX)
    gt1l = [r for r in rows if r["beam"] == "gt1l"]
    assert [r["quality_flag"] for r in gt1l] == [1, 0, 0, 1, 1, 1]


def test_read_atl08_segment_with_all_five_subsegments_present_is_good(tmp_path):
    """Valor real de un granulo v7: [1, 1, 1, 1, 1] debe PASAR (antes el filtro exigia 0 y descartaba todo ATL08)."""
    p = write_atl08(tmp_path / "ATL08.h5")
    with h5py.File(p, "r") as f:
        assert f["gt1l/land_segments/terrain/subset_te_flag"].shape[1] == 5
        assert (f["gt1l/land_segments/terrain/subset_te_flag"][:] == 1).all()
        rows = read_atl08(f, BBOX)
    assert sum(r["quality_flag"] for r in rows if r["beam"] == "gt1l") == 6


def test_read_atl08_all_zero_subsegments_is_bad(tmp_path):
    """[0, 0, 0, 0, 0] = no_terrain_photons_within_geosegment: NO es un segmento bueno."""
    p = write_atl08(tmp_path / "ATL08.h5")
    with h5py.File(p, "r+") as f:
        name = "gt1l/land_segments/terrain/subset_te_flag"
        n = f[name].shape[0]
        del f[name]
        f[name] = np.zeros((n, 5), dtype="int8")
    with h5py.File(p, "r") as f:
        rows = read_atl08(f, BBOX)
    assert sum(r["quality_flag"] for r in rows if r["beam"] == "gt1l") == 0


def write_gedi(path):
    with h5py.File(path, "w") as f:
        for beam, off in (("BEAM0101", 0.0), ("BEAM1000", 0.003)):
            g = f.create_group(beam)
            k = 6
            g["lat_lowestmode"] = np.array([LAT0 + off + 0.001 * i for i in range(k)])
            g["lon_lowestmode"] = np.array([LON0 + off] * k)
            g["elev_lowestmode"] = np.array([700.0, 701.0, 702.0, 703.0, 704.0, 705.0], dtype="float32")
            g["quality_flag"] = np.array([1, 1, 0, 1, 1, 1], dtype="uint8")
            g["degrade_flag"] = np.array([0, 0, 0, 3, 0, 0], dtype="uint8")
            g["elevation_bias_flag"] = np.array([0, 0, 0, 0, 1, 0], dtype="uint8")
            g["sensitivity"] = np.array([0.95] * k, dtype="float32")
        h = f.create_group("BEAM0000")  # fuera del bbox
        h["lat_lowestmode"] = np.array([LAT0 + 3.0])
        h["lon_lowestmode"] = np.array([LON0])
        h["elev_lowestmode"] = np.array([900.0], dtype="float32")
        h["quality_flag"] = np.array([1], dtype="uint8")
        h["degrade_flag"] = np.array([0], dtype="uint8")
    return path


def test_read_gedi_filters_by_bbox_and_flags(tmp_path):
    p = write_gedi(tmp_path / "GEDI02_A.h5")
    with h5py.File(p, "r") as f:
        rows = read_gedi(f, BBOX)
    assert len(rows) == 12 and {r["beam"] for r in rows} == {"BEAM0101", "BEAM1000"}
    # quality_flag==1 y degrade_flag==0 y elevation_bias_flag==0
    assert [r["quality_flag"] for r in rows if r["beam"] == "BEAM0101"] == [1, 1, 0, 0, 0, 1]


def test_read_gedi_rejects_gross_returns_far_from_its_own_tandemx_dem(tmp_path):
    """Caso real (Nunoa, granulo O08246 de 2020-05-27): 65 huellas con quality_flag = 1 quedaron ~300 m sobre el
    suelo (rebote en nube). GEDI trae su propio DEM TanDEM-X (``digital_elevation_model``, elipsoide WGS84): mas de
    100 m de diferencia es un error grueso y la huella no es buena."""
    p = write_gedi(tmp_path / "GEDI02_A.h5")
    with h5py.File(p, "r+") as f:
        f["BEAM0101/elev_lowestmode"][1] = 1001.0  # +300 m: nube
        f["BEAM0101/digital_elevation_model"] = np.array([698.0, 701.0, 702.0, 703.0, 704.0, 606.0], dtype="float32")
        # ultima: 705 - 606 = 99 m -> todavia se acepta (el corte es solo para errores groseros)
    with h5py.File(p, "r") as f:
        rows = read_gedi(f, BBOX)
    assert [r["quality_flag"] for r in rows if r["beam"] == "BEAM0101"] == [1, 0, 0, 0, 0, 1]
    # sin el campo (BEAM1000) se aplican solo las demas reglas
    assert [r["quality_flag"] for r in rows if r["beam"] == "BEAM1000"] == [1, 1, 0, 0, 0, 1]


def test_read_gedi_rejects_footprints_with_non_finite_tandemx_dem(tmp_path):
    p = write_gedi(tmp_path / "GEDI02_A.h5")
    with h5py.File(p, "r+") as f:
        f["BEAM0101/digital_elevation_model"] = np.array([700.0, -999999.0, 702.0, 703.0, 704.0, np.nan],
                                                          dtype="float32")
    with h5py.File(p, "r") as f:
        rows = read_gedi(f, BBOX)
    assert [r["quality_flag"] for r in rows if r["beam"] == "BEAM0101"] == [1, 0, 0, 0, 0, 0]


# --- CMR (busqueda publica, sin credenciales) ---

CMR_JSON = {"feed": {"entry": [
    {"producer_granule_id": "ATL08_20220107115418_02471413_007_01.h5", "time_start": "2022-01-07T11:54:17.662Z",
     "links": [
         {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#", "href": "s3://nsidc/ATL08.h5"},
         {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
          "href": "https://data.nsidc.earthdatacloud.nasa.gov/x/ATL08_20220107115418_02471413_007_01.h5"},
         {"rel": "http://esipfed.org/ns/fedsearch/1.1/metadata#", "href": "https://x/ATL08.xml"}]},
    {"producer_granule_id": "ATL08_B.h5", "time_start": "2022-04-08T00:00:00.000Z", "links": []},
]}}


class FakeCmr:
    def __init__(self):
        self.calls = []

    def __call__(self, url, params, timeout):
        self.calls.append(dict(params))
        page = int(params.get("page_num", 1))

        class R:
            status_code = 200

            def json(self_):
                return CMR_JSON if page == 1 else {"feed": {"entry": []}}

        return R()


def test_cmr_search_parses_granules_and_paginates():
    g = FakeCmr()
    refs = cmr_search_granules("ATL08", "007", (-70.61, -33.46, -70.59, -33.44), getter=g)
    assert len(refs) == 1  # el 2o no tiene enlace https .h5
    assert refs[0].url.startswith("https://data.nsidc.earthdatacloud.nasa.gov/") and refs[0].url.endswith(".h5")
    assert refs[0].date == "2022-01-07"
    assert g.calls[0]["short_name"] == "ATL08" and g.calls[0]["version"] == "007"
    assert g.calls[0]["bounding_box"] == "-70.61,-33.46,-70.59,-33.44"
    assert len(g.calls) == 2


# --- fetch_footprints de punta a punta, sin red ---

def poly_site():
    return box(LON0 - 0.0005, LAT0 - 0.0005, LON0 + 0.0005, LAT0 + 0.0005)


GEOID = lambda lon, lat: np.where(np.abs(np.asarray(lat)) < 1.0, 17.2, 27.2)


def make_env(tmp_path):
    atl = write_atl08(tmp_path / "ATL08.h5")
    gedi = write_gedi(tmp_path / "GEDI.h5")
    refs = {
        "ATL08": [GranuleRef(url=str(atl), date="2022-01-07", granule_id="ATL08_x")],
        "GEDI02_A": [GranuleRef(url=str(gedi), date="2023-05-02", granule_id="GEDI02_A_x")],
    }
    searched = []

    def search(short_name, version, bbox, **kw):
        searched.append((short_name, version, bbox))
        return refs[short_name]

    opened = []

    def opener(url):
        opened.append(url)
        return h5py.File(url, "r")

    return search, opener, searched, opened


def test_fetch_footprints_end_to_end(tmp_path):
    search, opener, searched, opened = make_env(tmp_path)
    fps = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=opener, geoid_fn=GEOID)
    assert {s[0] for s in searched} == {"ATL08", "GEDI02_A"}
    bb = searched[0][2]
    assert bb[0] < LON0 - 0.0005 - 0.008 and bb[2] > LON0 + 0.0005 + 0.008  # ~1000 m de buffer
    assert all(isinstance(f, Footprint) for f in fps)
    srcs = {f.source for f in fps}
    assert srcs == {"atl08", "gedi"}
    atl = [f for f in fps if f.source == "atl08" and f.quality_flag == 1]
    assert atl and all(f.h_orthometric_m == pytest.approx(f.h_ellipsoid_m - 27.2) for f in atl)
    assert {f.date for f in fps if f.source == "atl08"} == {"2022-01-07"}
    assert {f.date for f in fps if f.source == "gedi"} == {"2023-05-02"}
    assert any(f.quality_flag == 0 for f in fps)  # los malos se devuelven marcados; validate() los excluye


def test_fetch_footprints_uses_cache(tmp_path):
    search, opener, searched, opened = make_env(tmp_path)
    fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=opener, geoid_fn=GEOID)
    n = len(opened)
    again = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=opener, geoid_fn=GEOID)
    assert len(opened) == n and again


def test_fetch_footprints_rejects_wrong_geoid_function(tmp_path):
    search, opener, *_ = make_env(tmp_path)
    with pytest.raises(RejectRequest, match="geoide"):
        fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=opener,
                         geoid_fn=lambda lon, lat: np.zeros(np.shape(lon)))


def test_fetch_footprints_no_granules_is_empty_not_error(tmp_path):
    fps = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=lambda *a, **k: [],
                           opener=lambda u: None, geoid_fn=GEOID)
    assert fps == []


def test_fetch_footprints_outside_chile_rejected(tmp_path):
    far = Polygon([(10, 50), (10.001, 50), (10.001, 50.001), (10, 50.001), (10, 50)])
    with pytest.raises(RejectRequest):
        fetch_footprints(far, 1000.0, tmp_path / "cache", search=lambda *a, **k: [], opener=None, geoid_fn=GEOID)


def test_default_opener_without_credentials_rejected(tmp_path, monkeypatch):
    """Sin ~/_netrc (Windows) ni EARTHDATA_USERNAME/PASSWORD: rechazo claro, no se piden ni inventan credenciales."""
    monkeypatch.setenv("NETRC", str(tmp_path / "no_existe_netrc"))
    monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
    monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
    from terreno.truth import open_granule

    with pytest.raises(RejectRequest, match="Earthdata"):
        open_granule("https://data.nsidc.earthdatacloud.nasa.gov/x.h5")


# --- pruebas vivas (excluidas por defecto) ---

def _earthdata_ok() -> bool:
    try:
        import earthaccess

        for strat in ("environment", "netrc"):
            try:
                if earthaccess.login(strategy=strat, persist=False).authenticated:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


@pytest.mark.network
def test_live_geoid_control_points():
    """Mallas de geoide de PROJ (red): EGM2008 N(0,0)~17.2 m y N(Nunoa)~27.2 m, a +-0.5 m."""
    from terreno.truth import egm2008_undulation_pyproj

    assert_geoid_function(egm2008_undulation_pyproj)
    n = egm2008_undulation_pyproj(np.array([-70.6]), np.array([-33.45]))
    assert n[0] == pytest.approx(27.17, abs=0.5)


@pytest.mark.network
def test_live_cmr_finds_atl08_granules_over_santiago():
    """La busqueda CMR es publica (sin credenciales)."""
    refs = cmr_search_granules("ATL08", "007", (-70.62, -33.46, -70.58, -33.44), start="2022-01-01", end="2022-12-31")
    assert refs and all(r.url.endswith(".h5") for r in refs)


@pytest.mark.network
def test_live_footprints_rm03(tmp_path):
    if not _earthdata_ok():
        pytest.skip("faltan credenciales NASA Earthdata (en Windows ~/_netrc o variable NETRC, o EARTHDATA_USERNAME/"
                    "EARTHDATA_PASSWORD); las crea el fundador, este test no las pide ni las inventa")
    from terreno.geometry import SITES, load_polygon

    poly = load_polygon(SITES["rm03_piedemonte_oriente"]["polygon"])
    fps = fetch_footprints(poly, 1000.0, tmp_path, start="2023-01-01", end="2023-12-31", max_granules=3)
    assert all(-34.0 < f.lat < -33.0 for f in fps)


class _Http502(Exception):
    """Imita aiohttp.ClientResponseError de un 502 del servidor de NASA."""

    status = 502


def test_fetch_footprints_retries_transient_server_errors(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    search, real_opener, searched, opened = make_env(tmp_path)
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _Http502("502 Bad Gateway")
        return real_opener(url)

    fps = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=flaky, geoid_fn=GEOID)
    assert fps and calls["n"] >= 3


def test_fetch_footprints_gives_up_after_three_attempts_with_a_clear_rejection(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    search, _, _, _ = make_env(tmp_path)
    calls = {"n": 0}

    def always_502(url):
        calls["n"] += 1
        raise _Http502("502 Bad Gateway")

    with pytest.raises(RejectRequest, match="no disponible tras 3 intentos"):
        fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=always_502, geoid_fn=GEOID)
    assert calls["n"] == 3


def test_fetch_footprints_does_not_retry_a_client_error(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    search, _, _, _ = make_env(tmp_path)
    calls = {"n": 0}

    class Http401(Exception):
        status = 401

    def unauthorized(url):
        calls["n"] += 1
        raise Http401("401")

    with pytest.raises(Http401):
        fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=unauthorized, geoid_fn=GEOID)
    assert calls["n"] == 1


# --- AUD-1 I5: reintentos tambien para OSError de h5py y errores de cliente aiohttp/fsspec (sin imports duros) ---

def _flaky_opener_factory(tmp_path, exc_factory, fail_times=2):
    search, real_opener, _, _ = make_env(tmp_path)
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc_factory()
        return real_opener(url)

    return search, flaky, calls


def _lazy_class(name, module, base=Exception, **attrs):
    return type(name, (base,), {"__module__": module, **attrs})


def test_h5py_oserror_during_remote_read_is_retried(tmp_path, monkeypatch):
    """Un 502 a mitad de la lectura llega como OSError de h5py (callback de lectura sin excepcion original)."""
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    search, flaky, calls = _flaky_opener_factory(
        tmp_path, lambda: OSError("Can't read data (file read failed: time = ..., callback failed)"))
    fps = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=flaky, geoid_fn=GEOID)
    assert fps and calls["n"] >= 3


def test_oserror_raised_by_the_reader_is_retried(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    atl = write_atl08(tmp_path / "ATL08.h5")
    calls = {"n": 0}

    def reader(f, bbox):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Unable to synchronously read data (callback failed)")
        return read_atl08(f, bbox)

    rows = truth._read_granule(lambda u: h5py.File(u, "r"), reader, str(atl), BBOX)
    assert calls["n"] == 2 and rows


def test_aiohttp_style_client_errors_are_retried_without_importing_aiohttp(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    Disconnected = _lazy_class("ServerDisconnectedError", "aiohttp.client_exceptions")
    search, flaky, calls = _flaky_opener_factory(tmp_path, lambda: Disconnected("Server disconnected"))
    fps = fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=flaky, geoid_fn=GEOID)
    assert fps and calls["n"] >= 3


def test_fsspec_style_errors_are_retried(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    FsTimeout = _lazy_class("FSTimeoutError", "fsspec.exceptions")
    search, flaky, calls = _flaky_opener_factory(tmp_path, lambda: FsTimeout())
    assert fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=flaky, geoid_fn=GEOID)
    assert calls["n"] >= 3


def test_retry_walks_the_cause_chain_and_still_never_retries_401_403(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: None)
    Http403 = _lazy_class("ClientResponseError", "aiohttp.client_exceptions", status=403)
    Http502 = _lazy_class("ClientResponseError", "aiohttp.client_exceptions", status=502)

    def wrapped(inner):
        def make():
            try:
                raise inner("x")
            except Exception as e:
                try:
                    raise OSError("Can't read data (callback failed)") from e
                except OSError as o:
                    return o
        return make

    search, flaky, calls = _flaky_opener_factory(tmp_path, wrapped(Http502))
    assert fetch_footprints(poly_site(), 1000.0, tmp_path / "cache", search=search, opener=flaky, geoid_fn=GEOID)
    assert calls["n"] >= 3

    search, denied, calls403 = _flaky_opener_factory(tmp_path, wrapped(Http403), fail_times=99)
    with pytest.raises(OSError):
        fetch_footprints(poly_site(), 1000.0, tmp_path / "cache2", search=search, opener=denied, geoid_fn=GEOID)
    assert calls403["n"] == 1  # 403 en la cadena de causas: sin reintentos


def test_missing_local_file_is_not_retried(tmp_path, monkeypatch):
    import terreno.truth as truth

    monkeypatch.setattr(truth.time, "sleep", lambda s: pytest.fail("no debe esperar"))
    calls = {"n": 0}

    def opener(url):
        calls["n"] += 1
        raise FileNotFoundError(url)

    with pytest.raises(FileNotFoundError):
        truth._read_granule(opener, read_atl08, "x.h5", BBOX)
    assert calls["n"] == 1


# --- AUD-1 M1: clave de cache con version de reglas y escritura atomica ---

def test_cache_key_includes_quality_rules_version(tmp_path, monkeypatch):
    """Cambiar QUALITY_RULES_VERSION invalida las huellas cacheadas (con su quality_flag viejo)."""
    import terreno.truth as truth

    search, opener, searched, opened = make_env(tmp_path)
    cache = tmp_path / "cache"
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    n1 = len(opened)
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    assert len(opened) == n1  # misma version -> cache
    monkeypatch.setattr(truth, "QUALITY_RULES_VERSION", "otra-version")
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    assert len(opened) == 2 * n1  # otra version -> se relee


def test_changing_gedi_rules_rereads_only_gedi_granules(tmp_path, monkeypatch):
    """Una regla nueva de GEDI no obliga a releer ATL08 (cada granulo cuesta minutos de red)."""
    import terreno.truth as truth

    search, opener, searched, opened = make_env(tmp_path)
    cache = tmp_path / "cache"
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    opened.clear()
    monkeypatch.setitem(truth.SOURCE_RULES_VERSION, "gedi", "otra-version")
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    assert opened and all("GEDI" in u for u in opened)


def test_cache_write_is_atomic_and_a_truncated_cache_file_is_not_fatal(tmp_path, monkeypatch):
    import terreno.truth as truth

    search, opener, searched, opened = make_env(tmp_path)
    cache = tmp_path / "cache"
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(truth.os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real_replace(a, b))[1])
    fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    assert replaced and all(a.endswith(".part") and b.endswith(".json") for a, b in replaced)
    assert not list((cache / "truth").glob("*.part"))
    # un JSON truncado (corrida interrumpida de una version vieja) se vuelve a leer, no revienta
    victim = sorted((cache / "truth").glob("*.json"))[0]
    victim.write_text('[{"lon": -70.', encoding="utf-8")
    n = len(opened)
    assert fetch_footprints(poly_site(), 1000.0, cache, search=search, opener=opener, geoid_fn=GEOID)
    assert len(opened) == n + 1
