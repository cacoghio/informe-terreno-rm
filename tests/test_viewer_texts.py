"""Los textos fijos del visor (viewer/texts.js) deben coincidir con los de la spec sec. 5 y, si ya existe, con terreno/texts_es.py."""
import importlib
import re
from pathlib import Path

import pytest

TEXTS_JS = Path(__file__).resolve().parent.parent / "viewer" / "texts.js"

ATTRIBUTION = (
    "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved"
)
LIABILITY = (
    "The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability "
    "for any use of the Copernicus WorldDEM-30"
)
DISCLAIMER_START = "Evaluación preliminar basada en datos públicos. Los resultados deben validarse con levantamientos detallados"

_STR = r"'(?:[^'\\]|\\.)*'"


def _js_consts() -> dict[str, str]:
    """Extrae `export const NAME = 'a' + 'b';` (solo cadenas simples concatenadas)."""
    src = TEXTS_JS.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(rf"export const (\w+) =\s*((?:{_STR}\s*\+?\s*)+);", src):
        parts = re.findall(_STR, m.group(2))
        out[m.group(1)] = "".join(p[1:-1] for p in parts).replace("\\'", "'")
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(".")


def test_js_texts_match_spec_literals():
    c = _js_consts()
    assert c["GLO30_ATTRIBUTION"] == ATTRIBUTION
    assert c["GLO30_LIABILITY"] == LIABILITY
    assert c["DISCLAIMER"].startswith(DISCLAIMER_START)
    assert "30 m" in c["RESOLUTION_NOTICE"] and "900 m²" in c["RESOLUTION_NOTICE"]


def test_js_texts_match_texts_es_when_available():
    try:
        mod = importlib.import_module("terreno.texts_es")
    except ModuleNotFoundError:
        pytest.skip("terreno/texts_es.py todavia no existe (Task 10)")
    values: list[str] = []

    def collect(v):
        if isinstance(v, str):
            values.append(_norm(v))
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                collect(x)

    for name in dir(mod):
        if not name.startswith("_"):
            collect(getattr(mod, name))
    c = _js_consts()
    for key in ("GLO30_ATTRIBUTION", "GLO30_LIABILITY", "DISCLAIMER", "RESOLUTION_NOTICE", "NO_ENDORSEMENT", "DESK_SCREEN"):
        assert _norm(c[key]) in values, f"{key} difiere de terreno/texts_es.py"
