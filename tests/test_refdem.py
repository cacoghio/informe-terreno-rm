"""refdem: DSM de referencia fino (nubes de puntos Pleiades/SPOT de OpenTopography) SOLO para validacion interna.

Las nubes son sinteticas (LAS/LAZ minimos escritos con laspy en tmp_path); no se descarga nada.
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import laspy
import numpy as np
import pytest
import rasterio
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, mapping

from terreno import refdem
from terreno.errors import RejectRequest
from terreno.refdem import (
    compare_to_reference,
    rasterize_point_cloud,
    reference_dsm,
)
from terreno.reproject import Raster
from tests.fixtures.rasters import write_raster

X0, Y0 = 350000.0, 6290000.0


def plane(x, y):
    return 100.0 + 0.1 * (x - X0) + 0.05 * (y - Y0)


def write_las(path, x, y, z, *, epsg=32719, classification=None, with_crs=True):
    header = laspy.LasHeader(point_format=2, version="1.2")
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([300000.0, 6200000.0, 0.0])
    if with_crs:
        header.add_crs(CRS.from_epsg(epsg))
    las = laspy.LasData(header)
    las.x = np.asarray(x, dtype="float64")
    las.y = np.asarray(y, dtype="float64")
    las.z = np.asarray(z, dtype="float64")
    if classification is not None:
        las.classification = np.asarray(classification, dtype="uint8")
    las.write(str(path))
    return Path(path)


def grid_points(x0, x1, y0, y1, step, fn=plane):
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    X, Y = np.meshgrid(xs, ys)
    return X.ravel(), Y.ravel(), fn(X.ravel(), Y.ravel())


def read(r: Raster):
    with rasterio.open(r.path) as src:
        return src.read(1), src.transform, src.tags(), src.crs.to_epsg()


# --- rasterize_point_cloud ---

def test_median_per_cell_on_a_plane(tmp_path):
    x, y, z = grid_points(X0, X0 + 100, Y0, Y0 + 100, 1.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    r = rasterize_point_cloud(laz, (X0, Y0, X0 + 60, Y0 + 60), 5.0, "median", out_dir=tmp_path / "out")
    arr, tf, _, epsg = read(r)
    assert isinstance(r, Raster) and r.epsg == 32719 and r.pixel_m == 5.0 and epsg == 32719
    assert arr.shape == (12, 12)
    assert (tf.a, tf.e, tf.c, tf.f) == (5.0, -5.0, X0, Y0 + 60)
    # mediana de un plano muestreado simetricamente = valor en el centro de la celda
    cx = X0 + (np.arange(12) + 0.5) * 5.0
    cy = Y0 + 60 - (np.arange(12) + 0.5) * 5.0
    expected = plane(cx[None, :], cy[:, None])
    assert np.allclose(arr, expected, atol=0.02)


def test_grid_snaps_outward_to_multiples_of_res(tmp_path):
    x, y, z = grid_points(X0, X0 + 100, Y0, Y0 + 100, 1.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    r = rasterize_point_cloud(laz, (X0 + 3, Y0 + 1, X0 + 58, Y0 + 57), 5.0, out_dir=tmp_path / "out")
    arr, tf, _, _ = read(r)
    assert (tf.c, tf.f) == (X0, Y0 + 60)
    assert arr.shape == (12, 12)


def test_empty_cells_are_nan_and_points_outside_bbox_ignored(tmp_path):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    x = np.append(x, X0 + 500.0)  # lejos de la ventana: no debe contaminar nada
    y = np.append(y, Y0 + 500.0)
    z = np.append(z, 9999.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    r = rasterize_point_cloud(laz, (X0, Y0, X0 + 60, Y0 + 60), 5.0, out_dir=tmp_path / "out")
    arr, _, _, _ = read(r)
    assert np.isfinite(arr).sum() == 36  # solo la esquina de 30 x 30 m = 6 x 6 celdas
    assert np.nanmax(arr) < 200
    assert np.isnan(arr[0, 11])  # esquina NE sin puntos


def test_max_stat_catches_a_spike_and_median_does_not(tmp_path):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    x, y, z = np.append(x, X0 + 2.5), np.append(y, Y0 + 2.5), np.append(z, 500.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    bb = (X0, Y0, X0 + 30, Y0 + 30)
    med = read(rasterize_point_cloud(laz, bb, 5.0, "median", out_dir=tmp_path / "o1"))[0]
    mx = read(rasterize_point_cloud(laz, bb, 5.0, "max", out_dir=tmp_path / "o2"))[0]
    assert med[5, 0] == pytest.approx(plane(X0 + 2.5, Y0 + 2.5), abs=0.1)
    assert mx[5, 0] == pytest.approx(500.0, abs=0.01)  # fila 5 = la ultima (sur), columna 0


def test_noise_classes_are_dropped(tmp_path):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    cls = np.full(x.shape, 2)
    x, y, z = np.append(x, X0 + 2.5), np.append(y, Y0 + 2.5), np.append(z, 9999.0)
    cls = np.append(cls, 7)  # clase 7 = ruido (LAStools lasnoise)
    laz = write_las(tmp_path / "a.las", x, y, z, classification=cls)
    r = rasterize_point_cloud(laz, (X0, Y0, X0 + 30, Y0 + 30), 5.0, "max", out_dir=tmp_path / "out")
    assert np.nanmax(read(r)[0]) < 200


def test_chunked_reading_gives_the_same_raster(tmp_path):
    x, y, z = grid_points(X0, X0 + 60, Y0, Y0 + 60, 1.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    bb = (X0, Y0, X0 + 60, Y0 + 60)
    a = read(rasterize_point_cloud(laz, bb, 5.0, out_dir=tmp_path / "o1", chunk_size=10_000_000))[0]
    b = read(rasterize_point_cloud(laz, bb, 5.0, out_dir=tmp_path / "o2", chunk_size=777))[0]
    assert np.array_equal(a, b, equal_nan=True)


def test_reads_compressed_laz(tmp_path):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    laz = write_las(tmp_path / "a.laz", x, y, z)
    r = rasterize_point_cloud(laz, (X0, Y0, X0 + 30, Y0 + 30), 5.0, out_dir=tmp_path / "out")
    assert np.isfinite(read(r)[0]).all()


def test_wrong_or_missing_crs_is_rejected(tmp_path):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    bad = write_las(tmp_path / "b.las", x, y, z, epsg=32718)
    with pytest.raises(RejectRequest, match="CRS"):
        rasterize_point_cloud(bad, (X0, Y0, X0 + 30, Y0 + 30), 5.0, out_dir=tmp_path / "o")
    none = write_las(tmp_path / "c.las", x, y, z, with_crs=False)
    with pytest.raises(RejectRequest, match="CRS"):
        rasterize_point_cloud(none, (X0, Y0, X0 + 30, Y0 + 30), 5.0, out_dir=tmp_path / "o")


@pytest.mark.parametrize("kw", [
    {"res_m": 0.0}, {"res_m": -5.0}, {"res_m": float("nan")}, {"res_m": 5.0, "stat": "mean"},
    {"res_m": 5.0, "bbox": (X0 + 10, Y0, X0, Y0 + 10)}, {"res_m": 5.0, "bbox": (X0, Y0, float("inf"), Y0 + 10)},
])
def test_bad_arguments_rejected(tmp_path, kw):
    x, y, z = grid_points(X0, X0 + 30, Y0, Y0 + 30, 1.0)
    laz = write_las(tmp_path / "a.las", x, y, z)
    kw = dict(kw)
    bbox = kw.pop("bbox", (X0, Y0, X0 + 30, Y0 + 30))
    with pytest.raises(RejectRequest):
        rasterize_point_cloud(laz, bbox, kw.pop("res_m"), kw.pop("stat", "median"), out_dir=tmp_path / "o")


def test_missing_file_is_rejected_with_download_hint(tmp_path):
    with pytest.raises(RejectRequest, match="descarg"):
        rasterize_point_cloud(tmp_path / "nope.laz", (X0, Y0, X0 + 30, Y0 + 30), 5.0, out_dir=tmp_path / "o")


# --- reference_dsm ---

def site_at(cx, cy, half=150.0):
    to_ll = Transformer.from_crs(32719, 4326, always_xy=True)
    pts = [to_ll.transform(cx + dx, cy + dy) for dx, dy in
           [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]]
    return {"polygon": json.loads(json.dumps(mapping(Polygon(pts))))}


@pytest.fixture()
def cloud(tmp_path):
    x, y, z = grid_points(X0, X0 + 1500, Y0, Y0 + 1500, 2.5)
    return write_las(tmp_path / "pleiades.las", x, y, z)


def test_reference_dsm_inside_footprint(tmp_path, cloud):
    sites = {"s": site_at(X0 + 750, Y0 + 750)}
    r = reference_dsm("s", "pleiades", 5.0, laz_path=cloud, cache_dir=tmp_path / "c", sites=sites,
                      context_m=300.0, vertical="orthometric")
    assert isinstance(r, Raster) and r.epsg == 32719 and r.pixel_m == 5.0
    arr, tf, tags, _ = read(r)
    assert tf.c % 30 == 0 and tf.f % 30 == 0  # alineada a la malla de 30 m de GLO-30
    assert np.isfinite(arr).mean() > 0.99
    assert tags["VERTICAL_DATUM"] == "EGM2008"
    assert tags["SOURCE"] == "pleiades"


def test_reference_dsm_outside_footprint_is_none(tmp_path, cloud):
    sites = {"far": site_at(X0 + 20000, Y0 + 750)}
    assert reference_dsm("far", "pleiades", 5.0, laz_path=cloud, cache_dir=tmp_path / "c", sites=sites,
                         context_m=300.0, vertical="orthometric") is None


def test_reference_dsm_inside_header_box_but_without_points_is_none(tmp_path):
    xa, ya, za = grid_points(X0, X0 + 300, Y0, Y0 + 1500, 2.5)
    xb, yb, zb = grid_points(X0 + 1200, X0 + 1500, Y0, Y0 + 1500, 2.5)
    laz = write_las(tmp_path / "strip.las", np.r_[xa, xb], np.r_[ya, yb], np.r_[za, zb])
    sites = {"hole": site_at(X0 + 750, Y0 + 750)}  # dentro de la caja del encabezado, sin puntos
    assert reference_dsm("hole", "pleiades", 5.0, laz_path=laz, cache_dir=tmp_path / "c", sites=sites,
                         context_m=300.0, vertical="orthometric") is None


def test_reference_dsm_converts_ellipsoidal_heights_with_the_geoid(tmp_path, cloud):
    sites = {"s": site_at(X0 + 750, Y0 + 750)}
    calls = []

    def geoid(lon, lat):
        calls.append(1)
        return np.full(np.shape(lon), 28.0)

    ell = reference_dsm("s", "pleiades", 5.0, laz_path=cloud, cache_dir=tmp_path / "c1", sites=sites,
                        context_m=300.0, vertical="ellipsoidal", geoid_fn=geoid)
    ort = reference_dsm("s", "pleiades", 5.0, laz_path=cloud, cache_dir=tmp_path / "c2", sites=sites,
                        context_m=300.0, vertical="orthometric")
    a, b = read(ell)[0], read(ort)[0]
    assert calls
    assert np.nanmedian(b - a) == pytest.approx(28.0, abs=1e-3)
    assert read(ell)[2]["VERTICAL_INPUT"] == "ellipsoidal"


def test_reference_dsm_is_cached(tmp_path, cloud, monkeypatch):
    sites = {"s": site_at(X0 + 750, Y0 + 750)}
    kw = dict(laz_path=cloud, cache_dir=tmp_path / "c", sites=sites, context_m=300.0, vertical="orthometric")
    first = reference_dsm("s", "pleiades", 5.0, **kw)

    def boom(*a, **k):
        raise AssertionError("no debe releer la nube")

    monkeypatch.setattr(refdem, "_collect_points", boom)
    again = reference_dsm("s", "pleiades", 5.0, **kw)
    assert again.path == first.path


def test_reference_dsm_rejects_unknown_site_source_and_vertical(tmp_path, cloud):
    sites = {"s": site_at(X0 + 750, Y0 + 750)}
    kw = dict(laz_path=cloud, cache_dir=tmp_path / "c", sites=sites, context_m=300.0)
    with pytest.raises(RejectRequest, match="sitio"):
        reference_dsm("nope", "pleiades", 5.0, vertical="orthometric", **kw)
    with pytest.raises(RejectRequest, match="fuente"):
        reference_dsm("s", "lidar", 5.0, vertical="orthometric", **kw)
    with pytest.raises(RejectRequest, match="vertical"):
        reference_dsm("s", "pleiades", 5.0, vertical="tide", **kw)


def test_datasets_table_documents_licence_and_files():
    assert refdem.DATASETS["pleiades"]["file"] == "pleiades_denoised_UTM.laz"
    assert refdem.DATASETS["spot"]["file"] == "spot_point_cloud_UTM.laz"
    for d in refdem.DATASETS.values():
        assert "no comercial" in d["licencia"].lower() or "non-commercial" in d["licencia"].lower()
        assert d["doi"].startswith("10.5069/")


# --- compare_to_reference ---

TOP, RES = Y0 + 1800.0, 30.0
N30 = 60  # 1800 m


def ll_poly(c0, r0, c1, r1):
    to_ll = Transformer.from_crs(32719, 4326, always_xy=True)
    x0, x1 = X0 + c0 * RES, X0 + c1 * RES
    y0, y1 = TOP - r1 * RES, TOP - r0 * RES
    return Polygon([to_ll.transform(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]])


LOT = ll_poly(25, 25, 35, 35)  # 10 x 10 celdas de 30 m


def fine_ref(tmp_path, fn, res=5.0, name="ref.tif"):
    n = int(N30 * RES / res)
    xs = X0 + (np.arange(n) + 0.5) * res
    ys = TOP - (np.arange(n) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    return write_raster(tmp_path / name, fn(X, Y), epsg=32719, res=res, left=X0, top=TOP)


def coarse(tmp_path, fn, name="glo.tif"):
    xs = X0 + (np.arange(N30) + 0.5) * RES
    ys = TOP - (np.arange(N30) + 0.5) * RES
    X, Y = np.meshgrid(xs, ys)
    return write_raster(tmp_path / name, fn(X, Y), epsg=32719, res=RES, left=X0, top=TOP)


def test_constant_offset_metrics(tmp_path):
    ref = fine_ref(tmp_path, plane)
    glo = coarse(tmp_path, lambda x, y: plane(x, y) + 2.0)
    out = compare_to_reference(glo, ref, LOT, context_m=300.0)
    json.dumps(out)  # serializable
    c = out["context"]
    assert c["n"] > 400
    assert c["bias_m"] == pytest.approx(2.0, abs=1e-3)
    assert c["mae_m"] == pytest.approx(2.0, abs=1e-3)
    assert c["rmse_m"] == pytest.approx(2.0, abs=1e-3)
    assert c["nmad_m"] == pytest.approx(0.0, abs=1e-3)
    assert c["p90_abs_m"] == pytest.approx(2.0, abs=1e-3)
    assert c["slope"]["mae_deg"] == pytest.approx(0.0, abs=1e-3)
    assert out["convencion"].startswith("sesgo = ")
    assert out["lote"]["n"] == 100 and out["lote"]["bias_m"] == pytest.approx(2.0, abs=1e-3)


def test_nmad_is_robust_and_rmse_is_not(tmp_path):
    ref = fine_ref(tmp_path, plane)

    def glo_fn(x, y):
        z = plane(x, y)
        spike = (np.abs(x - (X0 + 885)) < 45) & (np.abs(y - (TOP - 885)) < 45)  # 3 x 3 celdas
        return z + np.where(spike, 80.0, 0.0)

    glo = coarse(tmp_path, glo_fn)
    c = compare_to_reference(glo, ref, LOT, context_m=300.0)["context"]
    assert c["nmad_m"] == pytest.approx(0.0, abs=1e-3)
    assert c["rmse_m"] > 5.0


def test_slope_error_and_slope_classes(tmp_path):
    ref = fine_ref(tmp_path, lambda x, y: 100.0 + 0.10 * (x - X0))       # 10 % = 5.71 deg
    glo = coarse(tmp_path, lambda x, y: 100.0 + 0.05 * (x - X0))         # 5 % = 2.86 deg
    c = compare_to_reference(glo, ref, LOT, context_m=300.0)["context"]
    assert c["slope"]["bias_deg"] == pytest.approx(math.degrees(math.atan(0.05)) - math.degrees(math.atan(0.10)), abs=0.01)
    assert c["slope"]["mae_deg"] == pytest.approx(abs(c["slope"]["bias_deg"]), abs=1e-6)
    cls = c["by_slope_class"]
    assert set(cls) == {"<5", "5-15", "15-30", ">30"}
    assert cls["5-15"]["n"] > 400 and cls["<5"]["n"] == 0 and cls["15-30"]["n"] == 0 and cls[">30"]["n"] == 0
    assert cls["<5"]["bias_m"] is None


def test_slope_classes_split_by_reference_slope(tmp_path):
    # mitad oeste plana (0 %), mitad este a 40 %
    def fn(x, y):
        return 100.0 + np.where(x > X0 + 900, 0.40 * (x - (X0 + 900)), 0.0)
    ref = fine_ref(tmp_path, fn)
    glo = coarse(tmp_path, lambda x, y: fn(x, y) + 1.0)
    c = compare_to_reference(glo, ref, LOT, context_m=600.0)["context"]["by_slope_class"]
    assert c["<5"]["n"] > 100 and c[">30"]["n"] > 100
    assert c["<5"]["bias_m"] == pytest.approx(1.0, abs=0.05)
    assert c[">30"]["bias_m"] == pytest.approx(1.0, abs=0.05)
    assert c["5-15"]["n"] + c["15-30"]["n"] < 100


def test_native_comparison_shows_detail_lost_at_30_m(tmp_path):
    amp, wl = 2.0, 30.0  # ondulacion de 30 m de longitud de onda: un periodo exacto por celda, invisible a 30 m
    rough = lambda x, y: plane(x, y) + amp * np.sin(2 * np.pi * (x - X0) / wl)
    ref = fine_ref(tmp_path, rough)
    glo = coarse(tmp_path, plane)
    out = compare_to_reference(glo, ref, LOT, context_m=300.0)
    c = out["context"]
    assert c["rmse_m"] < 0.05                                 # a 30 m casi no hay error
    nat = c["nativo"]
    assert nat["res_m"] == 5.0
    assert nat["rmse_m"] == pytest.approx(amp / math.sqrt(2), abs=0.1)   # a 5 m si aparece
    assert nat["rmse_m"] > 20 * c["rmse_m"]
    assert c["detalle_perdido_30m_rmse_m"] == pytest.approx(amp / math.sqrt(2), abs=0.1)
    assert out["lote"]["nativo"]["rmse_m"] == pytest.approx(amp / math.sqrt(2), abs=0.15)


def test_lot_with_fewer_than_nine_pixels_is_none(tmp_path):
    ref = fine_ref(tmp_path, plane)
    glo = coarse(tmp_path, plane)
    small = ll_poly(30, 30, 32, 32)  # 2 x 2 = 4 celdas
    out = compare_to_reference(glo, ref, small, context_m=300.0)
    assert out["lote"] is None and out["lote_n_pixeles"] == 4
    assert out["context"]["n"] > 100


def test_context_false_only_measures_the_lot(tmp_path):
    ref = fine_ref(tmp_path, plane)
    glo = coarse(tmp_path, lambda x, y: plane(x, y) + 1.0)
    out = compare_to_reference(glo, ref, LOT, context=False)
    assert out["context"] is None
    assert out["lote"]["bias_m"] == pytest.approx(1.0, abs=1e-3)


def test_partial_reference_coverage_uses_only_covered_cells(tmp_path):
    def fn(x, y):
        return np.where(x < X0 + 900, plane(x, y), np.nan)
    ref = fine_ref(tmp_path, fn)
    glo = coarse(tmp_path, lambda x, y: plane(x, y) + 1.0)
    out = compare_to_reference(glo, ref, LOT, context_m=600.0)
    full = (2 * 600 // 30 + 10) ** 2
    assert 0 < out["context"]["n"] < full
    assert out["context"]["cobertura_ref"] < 0.9
    assert out["context"]["bias_m"] == pytest.approx(1.0, abs=1e-3)


def test_no_overlap_is_rejected(tmp_path):
    ref = fine_ref(tmp_path, lambda x, y: np.full_like(x, np.nan))
    glo = coarse(tmp_path, plane)
    with pytest.raises(RejectRequest):
        compare_to_reference(glo, ref, LOT, context_m=300.0)


def test_misaligned_reference_grid_is_rejected(tmp_path):
    n = 360
    arr = np.full((n, n), 100.0)
    ref = write_raster(tmp_path / "off.tif", arr, epsg=32719, res=5.0, left=X0 + 2.0, top=TOP)
    glo = coarse(tmp_path, plane)
    with pytest.raises(RejectRequest, match="alinea"):
        compare_to_reference(glo, ref, LOT, context_m=300.0)


def test_epsg_mismatch_is_rejected(tmp_path):
    ref = fine_ref(tmp_path, plane)
    xs = np.zeros((N30, N30))
    glo = write_raster(tmp_path / "g.tif", xs, epsg=32718, res=RES, left=X0, top=TOP)
    with pytest.raises(RejectRequest, match="CRS"):
        compare_to_reference(glo, ref, LOT, context_m=300.0)


def test_diff_map_png_is_written_where_asked(tmp_path):
    ref = fine_ref(tmp_path, plane)
    glo = coarse(tmp_path, lambda x, y: plane(x, y) + 1.0)
    png = refdem.render_diff_map(glo, ref, LOT, tmp_path / "maps" / "d.png", context_m=300.0, title="t")
    assert png == tmp_path / "maps" / "d.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --- cortafuegos: la referencia nunca llega a un entregable ---

POC = Path(refdem.__file__).resolve().parent.parent
PUBLISHING = {"publish", "report", "report_figs", "glb", "video", "hooks", "agent", "agent_tools", "pipeline", "layers",
              "overlays", "legend", "manifest"}


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names |= {f"{mod}.{a.name}" for a in node.names}
    return names


def test_refdem_does_not_import_any_publishing_module():
    imported = _imports(POC / "terreno" / "refdem.py")
    leaked = {m for m in imported if any(m == f"terreno.{p}" or m.startswith(f"terreno.{p}.") for p in PUBLISHING)}
    assert not leaked, f"refdem importa modulos que producen entregables: {leaked}"


def test_no_publishing_module_imports_refdem():
    for p in (POC / "terreno").glob("*.py"):
        if p.name == "refdem.py":
            continue
        src = p.read_text(encoding="utf-8")
        assert "refdem" not in src, f"{p.name} menciona refdem: la referencia no puede entrar al pipeline de entregables"
    for p in (POC / "viewer").glob("*.js"):
        assert "refdem" not in p.read_text(encoding="utf-8")


def test_refdem_functions_only_write_inside_the_given_cache(tmp_path, monkeypatch):
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    x, y, z = grid_points(X0, X0 + 1500, Y0, Y0 + 1500, 2.5)
    (tmp_path / "src").mkdir()
    laz = write_las(tmp_path / "src" / "p.las", x, y, z)
    cache = tmp_path / "cache"
    sites = {"s": site_at(X0 + 750, Y0 + 750)}
    ref = reference_dsm("s", "pleiades", 5.0, laz_path=laz, cache_dir=cache, sites=sites, context_m=300.0,
                        vertical="orthometric")
    assert Path(ref.path).resolve().is_relative_to(cache.resolve())
    glo = coarse(tmp_path, plane)
    compare_to_reference(glo, ref, LOT, context_m=300.0)
    assert not list(work.rglob("*"))  # el cwd quedo vacio
    produced = {p.name.lower() for p in tmp_path.rglob("*")}
    assert not ({"publicado", "report.pdf", "terreno.glb", "video.mp4", "predio.geojson"} & produced)
    assert not [p for p in tmp_path.rglob("*") if p.suffix.lower() in {".pdf", ".glb", ".mp4"}]


def test_default_cache_is_gitignored_and_not_a_deliverable_folder():
    d = refdem.DEFAULT_CACHE.resolve()
    assert d == (POC / "cache" / "refdem").resolve()
    assert "cache/" in (POC / ".gitignore").read_text()
    assert not ({"out", "out_run2", "publicado"} & set(d.parts))


# --- CLI ---

def test_cli_parser_and_runner(tmp_path, monkeypatch):
    p = refdem.build_parser()
    a = p.parse_args(["--site", "rm04_piedemonte_alto", "--source", "pleiades"])
    assert (a.site, a.source, a.res) == ("rm04_piedemonte_alto", "pleiades", None)
    seen = {}

    def fake(site, source, res, out_dir, *, aw3d30=True, **kw):
        seen.update(site=site, source=source, out_dir=out_dir, aw3d30=aw3d30)
        return {"site": site, "source": source, "estado": "ok"}

    monkeypatch.setattr(refdem, "analyze_site", fake)
    rc = refdem.main(["--site", "rm04_piedemonte_alto", "--source", "pleiades", "--out-dir", str(tmp_path), "--no-aw3d30"])
    assert rc == 0 and seen["aw3d30"] is False and seen["source"] == "pleiades"
    saved = json.loads((tmp_path / "error_vs_referencia_fina_rm04_piedemonte_alto_pleiades.json").read_text(encoding="utf-8"))
    assert saved["estado"] == "ok"
    assert refdem.main(["--site", "no_existe", "--source", "spot", "--out-dir", str(tmp_path)]) != 0


# --- evidencia del datum vertical ---

def test_datum_evidence_picks_ellipsoidal_when_the_offset_matches_the_geoid():
    e = refdem.datum_evidence(median_glo_minus_cloud_m=-28.3, geoid_n_m=28.33)
    assert e["veredicto"] == "elipsoidal"
    assert e["mediana_nube_menos_glo30_m"] == pytest.approx(28.3)
    assert e["residual_si_elipsoidal_m"] == pytest.approx(-0.03, abs=1e-6)
    assert e["residual_si_ortometrica_m"] == pytest.approx(28.3)


def test_datum_evidence_picks_orthometric_when_there_is_no_offset():
    assert refdem.datum_evidence(median_glo_minus_cloud_m=0.4, geoid_n_m=28.3)["veredicto"] == "ortometrica"


def test_datum_evidence_is_inconclusive_where_the_geoid_is_small():
    assert refdem.datum_evidence(median_glo_minus_cloud_m=-1.0, geoid_n_m=2.0)["veredicto"] == "no_concluyente"
