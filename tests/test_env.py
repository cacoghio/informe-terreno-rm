import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from terreno.config import GDAL_BIN, RECIPE_VERSION, run_gdal


def test_gdaldem_runs():
    # gdaldem --version prints the banner on stdout but exits 1 (usage error): check=False
    out = run_gdal("gdaldem", "--version", check=False)
    assert "GDAL 3.8" in out


def test_gdal_contour_runs():
    out = run_gdal("gdal_contour", "--version", check=False)
    assert "GDAL 3.8" in out


def test_gdal_failure_raises_runtime_error():
    with pytest.raises(RuntimeError):
        run_gdal("gdalinfo", "no_existe.tif")


def test_gdal_bin_and_recipe_version():
    assert GDAL_BIN.exists()
    assert RECIPE_VERSION == "0.1.0"


def test_rasterio_opens_cog_header(tmp_path):
    p = tmp_path / "syn.tif"
    data = np.arange(9, dtype="float32").reshape(3, 3)
    with rasterio.open(
        p, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(-70.0, -33.0, 0.001, 0.001),
    ) as dst:
        dst.write(data, 1)
    with rasterio.open(p) as src:
        assert src.crs.to_epsg() == 4326
        assert src.read(1)[2, 2] == 8.0


def test_gdal_env_points_to_qgis_data_dirs():
    from terreno.config import _gdal_env

    env = _gdal_env()
    assert env["GDAL_DATA"].replace("\\", "/").endswith("share/gdal")
    assert env["PROJ_DATA"].replace("\\", "/").endswith("share/proj")
    assert (GDAL_BIN.parent / "apps" / "gdal" / "share" / "gdal").is_dir()


def test_gdal_contour_gpkg_emits_no_gdal_data_warning(tmp_path):
    import subprocess

    from terreno.config import _gdal_env
    from tests.fixtures.rasters import utm_ramp

    r = utm_ramp(tmp_path / "a.tif", slope_x=0.1)
    p = subprocess.run(
        [str(GDAL_BIN / "gdal_contour.exe"), "-i", "10", "-f", "GPKG", str(r.path), str(tmp_path / "c.gpkg")],
        capture_output=True, text=True, env=_gdal_env(),
    )
    assert p.returncode == 0 and "GDAL_DATA" not in p.stderr, p.stderr


# --- AUD-1 M8: la salida de GDAL es UTF-8 aunque el locale de Windows sea cp1252 ---

def test_gdal_error_with_non_ascii_path_is_a_runtimeerror_not_a_unicodedecodeerror():
    with pytest.raises(RuntimeError) as ei:
        run_gdal("gdalinfo", "no_existe_ÍÑú.tif")
    assert "no_existe" in str(ei.value)


def test_run_gdal_decodes_utf8_and_never_raises_on_undecodable_bytes(monkeypatch):
    import subprocess

    import terreno.config as cfg

    class P:
        returncode = 1
        stdout = "ruta C:/Datos/Íñ.tif ".encode("utf-8") + b"\xff\xfe"
        stderr = "ERROR 1: ".encode("utf-8") + "Í".encode("utf-8") + b"\x8d\xff"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    with pytest.raises(RuntimeError) as ei:
        cfg.run_gdal("gdalinfo", "x.tif")
    assert "Í" in str(ei.value)
    P.returncode = 0
    out = cfg.run_gdal("gdalinfo", "x.tif")
    assert "Íñ.tif" in out and isinstance(out, str)
