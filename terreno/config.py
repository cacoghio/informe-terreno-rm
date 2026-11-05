"""Configuracion global: ruta de GDAL CLI, version de receta y ejecutor de GDAL."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

RECIPE_VERSION = "0.1.0"

GDAL_BIN: Path = Path(
    os.environ.get("TERRENO_GDAL_BIN", r"C:\Program Files\QGIS 3.36.1\bin")
)


def _gdal_env() -> dict[str, str]:
    env = os.environ.copy()
    extra = [str(GDAL_BIN)]
    qgis_bin = GDAL_BIN.parent / "apps" / "qgis" / "bin"
    if qgis_bin.exists():
        extra.append(str(qgis_bin))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    # Datos de GDAL/PROJ de QGIS (evita "GDAL_DATA is not defined" y mezclas con el PROJ de las ruedas de Python).
    gdal_data = GDAL_BIN.parent / "apps" / "gdal" / "share" / "gdal"
    proj_data = GDAL_BIN.parent / "share" / "proj"
    if gdal_data.is_dir():
        env["GDAL_DATA"] = str(gdal_data)
    if proj_data.is_dir():
        env["PROJ_DATA"] = str(proj_data)
        env["PROJ_LIB"] = str(proj_data)
    return env


def run_gdal(tool: str, *args: str, check: bool = True) -> str:
    """Ejecuta ``<GDAL_BIN>/<tool>.exe`` y devuelve stdout.

    Lanza RuntimeError con stderr si el codigo de salida != 0 (salvo check=False,
    usado solo para ``--version`` de gdaldem/gdal_contour, que imprimen el banner
    en stdout pero salen con codigo 1).
    """
    exe = GDAL_BIN / f"{tool}.exe"
    # GDAL escribe en UTF-8; ``text=True`` decodificaria con el locale de Windows (cp1252) en modo estricto y una ruta con
    # "Í" daria UnicodeDecodeError en vez del RuntimeError con el stderr (AUD-1 M8).
    proc = subprocess.run(
        [str(exe), *map(str, args)],
        capture_output=True, env=_gdal_env(),
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{tool} fallo (codigo {proc.returncode}): {stderr.strip()}"
        )
    return stdout
