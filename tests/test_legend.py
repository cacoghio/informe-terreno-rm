"""La leyenda del visor (viewer/legend.json) y las capas PNG de build_glb (terreno/palette.py) usan los mismos colores."""
import json
import subprocess
import sys
from pathlib import Path

from terreno import legend as legend_mod
from terreno import palette

ROOT = Path(__file__).resolve().parent.parent
LEGEND_PATH = ROOT / "viewer" / "legend.json"


def load():
    return json.loads(LEGEND_PATH.read_text(encoding="utf-8"))


def norm(label: str) -> str:
    return label.replace(" ", "")


def test_slope_legend_colours_equal_palette_class_by_class():
    leg = load()["pendiente"]
    assert [norm(c) for c in leg["clases"]] == [norm(c) for c in palette.SLOPE_CLASS_LABELS]
    for key, label, hexcol in zip(palette.SLOPE_CLASS_KEYS, leg["clases"], leg["colores"]):
        assert hexcol.lower() == palette.hex_of(palette.SLOPE_COLORS[key]), (key, label)


def test_aspect_legend_colours_equal_palette_sector_by_sector():
    leg = load()["orientacion"]
    assert leg["sectores"] == list(palette.ASPECT_LABELS)
    for label, hexcol in zip(leg["sectores"], leg["colores"]):
        assert hexcol.lower() == palette.hex_of(palette.ASPECT_COLORS[label]), label


def test_contour_legend_colour_and_interval_equal_pipeline():
    from terreno.overlays import MAJOR_CONTOUR_EVERY_M
    from terreno.terrain import CONTOUR_INTERVAL_M

    c = load()["curvas"]
    assert c["color"].lower() == palette.hex_of(palette.CONTOUR_RGBA[:3])
    assert c["equidistancia_m"] == CONTOUR_INTERVAL_M
    assert c["indice_cada"] * c["equidistancia_m"] == MAJOR_CONTOUR_EVERY_M


def test_legend_json_is_exactly_what_the_generator_writes():
    """palette.py es la unica fuente: legend.json se regenera con `python -m terreno.legend`, no se edita a mano."""
    assert load() == legend_mod.build_legend()


def test_generator_cli_check_mode_passes_and_detects_drift(tmp_path):
    ok = subprocess.run([sys.executable, "-m", "terreno.legend", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    drift = tmp_path / "legend.json"
    drift.write_text(LEGEND_PATH.read_text(encoding="utf-8").replace("#a6d96a", "#000000"), encoding="utf-8")
    bad = subprocess.run([sys.executable, "-m", "terreno.legend", "--check", "--path", str(drift)], cwd=ROOT,
                         capture_output=True, text=True)
    assert bad.returncode != 0
