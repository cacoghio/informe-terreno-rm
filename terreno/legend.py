"""viewer/legend.json se GENERA desde terreno/palette.py (unica fuente de colores).

Las capas PNG de build_glb se pintan con palette.py; la leyenda del visor dibuja viewer/legend.json. Para que la leyenda no
mienta, legend.json no se edita a mano: ``python -m terreno.legend`` lo regenera y ``--check`` falla si difiere.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from terreno import palette
from terreno.overlays import MAJOR_CONTOUR_EVERY_M
from terreno.terrain import CONTOUR_INTERVAL_M

LEGEND_PATH = Path(__file__).resolve().parent.parent / "viewer" / "legend.json"
NOTE = ("GENERADO por `python -m terreno.legend` desde terreno/palette.py (unica fuente de colores). No editar a mano: "
        "tests/test_legend.py compara esta leyenda con la paleta de las capas PNG.")


def build_legend() -> dict:
    return {
        "_nota": NOTE,
        "pendiente": {
            "clases": [lab.replace("< ", "<").replace("> ", ">") for lab in palette.SLOPE_CLASS_LABELS],
            "colores": [palette.hex_of(palette.SLOPE_COLORS[k]) for k in palette.SLOPE_CLASS_KEYS],
        },
        "orientacion": {
            "sectores": list(palette.ASPECT_LABELS),
            "colores": [palette.hex_of(palette.ASPECT_COLORS[k]) for k in palette.ASPECT_LABELS],
        },
        "curvas": {
            "color": palette.hex_of(palette.CONTOUR_RGBA[:3]),
            "equidistancia_m": CONTOUR_INTERVAL_M,
            "indice_cada": int(round(MAJOR_CONTOUR_EVERY_M / CONTOUR_INTERVAL_M)),
        },
        "sombreado": {"azimut_deg": 315, "altura_deg": 45},  # valores por defecto de gdaldem hillshade
    }


def render(legend: dict) -> str:
    """Un bloque por linea (listas cortas en linea): diff legible."""
    rows = [f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}" for k, v in legend.items()]
    return "{\n" + ",\n".join(rows) + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="no escribe: falla (codigo 1) si el archivo difiere de la paleta")
    ap.add_argument("--path", type=Path, default=LEGEND_PATH)
    a = ap.parse_args(argv)
    want = build_legend()
    if a.check:
        try:
            have = json.loads(a.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"no se puede leer {a.path}: {exc}", file=sys.stderr)
            return 1
        if have != want:
            print(f"{a.path} difiere de terreno/palette.py; ejecutar `python -m terreno.legend`", file=sys.stderr)
            return 1
        return 0
    a.path.write_text(render(want), encoding="utf-8")
    print(f"escrito {a.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
