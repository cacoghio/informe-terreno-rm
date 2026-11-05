"""Colores y etiquetas compartidos por el GLB (capas PNG), el visor y el PDF: una sola fuente para que coincidan."""
from __future__ import annotations

SLOPE_CLASS_KEYS = ("<5", "5-15", "15-30", ">30")
SLOPE_CLASS_LABELS = ("< 5 %", "5-15 %", "15-30 %", "> 30 %")
SLOPE_COLORS = {  # RGB; verde -> rojo, legibles en escala de grises por luminosidad decreciente
    "<5": (166, 217, 106),
    "5-15": (255, 237, 111),
    "15-30": (253, 141, 60),
    ">30": (189, 0, 38),
}
ASPECT_LABELS = ("N", "NE", "E", "SE", "S", "SO", "O", "NO")
ASPECT_COLORS = {  # ciclico: el sector N y el NO son vecinos
    "N": (59, 91, 219),
    "NE": (28, 154, 201),
    "E": (47, 158, 109),
    "SE": (143, 191, 63),
    "S": (230, 194, 41),
    "SO": (240, 140, 42),
    "O": (224, 69, 46),
    "NO": (162, 63, 181),
}
CONTOUR_RGBA = (74, 48, 24, 235)
LAYER_ALPHA = 190  # pendiente / orientacion: semitransparentes sobre el terreno


def hex_of(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(rgb)
