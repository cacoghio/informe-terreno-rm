"""Corridas reales del agente (Task 13, paso 5): `python run_agent_live.py rm01|fuera_de_chile`.

Usa el login de Claude Code de esta maquina (no lee ni fija claves). Escribe en out/agente_<caso>/ y imprime un JSON con lo que hizo el agente.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from shapely.geometry import box, mapping

from terreno.agent import run_agent
from terreno.geometry import SITES

ROOT = Path(__file__).resolve().parent


def request_for(case: str) -> tuple[str, str]:
    if case == "rm01":
        poly = SITES["rm01_plano_urbano"]["polygon"]
        txt = ("Necesito el informe preliminar de terreno de un lote de prueba en Ñuñoa (plano urbano, RM). "
               f"Poligono del lote (GeoJSON, EPSG:4326, lon/lat): {json.dumps(poly)}")
        return txt, "Ñuñoa (lote de prueba RM-01)"
    if case == "fuera_de_chile":
        poly = json.loads(json.dumps(mapping(box(-58.4000, -34.6000, -58.3970, -34.5970))))  # Buenos Aires, Argentina
        txt = ("Necesito el informe preliminar de terreno de este predio en Buenos Aires. "
               f"Poligono (GeoJSON, EPSG:4326, lon/lat): {json.dumps(poly)}")
        return txt, "Buenos Aires"
    raise SystemExit("caso desconocido: rm01 | fuera_de_chile")


if __name__ == "__main__":
    case = sys.argv[1]
    request, site = request_for(case)
    res = run_agent(request, ROOT / "out" / f"agente_{case}", site_name=site)
    d = {k: (str(v) if isinstance(v, Path) else v) for k, v in res.__dict__.items()}
    print(json.dumps(d, indent=2, ensure_ascii=False, default=str))
