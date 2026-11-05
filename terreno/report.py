"""render_pdf: informe de terreno de 14 secciones (spec sec. 3) en espanol, con ReportLab + matplotlib (Task 10).

* TODO numero que se escribe en texto sale del manifiesto (``report_fields`` y los campos del propio manifiesto); el
  manifiesto debe estar integro (``result_hash`` verificado) y el PDF no se genera junto a un ``rejection.json``.
* Las figuras dibujan los rasters de trabajo cuyas rutas guarda el manifiesto; si faltan, la figura se reemplaza por un
  aviso ("figura no disponible"), nunca por un dibujo inventado.
* Los textos fijos (descargo, "Que este informe no cubre", atribuciones, aviso de resolucion) viven en ``texts_es.py``.
* Predio no concluyente o nivel C: ni pendiente puntual, ni orientacion, ni puntaje de relieve, ni perfiles en ninguna parte.
"""
from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
from PIL import Image as PILImage
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfgen import canvas as rl_canvas

from terreno import texts_es as T
from terreno.errors import RejectRequest
from terreno.glb import DEFAULT_EXAGGERATION
from terreno.manifest import hash_manifest, report_fields
from terreno.palette import SLOPE_CLASS_KEYS, SLOPE_CLASS_LABELS
from terreno.pipeline import REJECTION_NAME
from terreno.report_figs import (
    fig_3d,
    fig_contours,
    fig_diff,
    fig_overview,
    fig_profiles,
    fig_rose,
    fig_slope_bars,
    fig_slope_map,
    fig_sun,
    load_ctx,
)

REPORT_NAME = "report.pdf"
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
FRAME_W = PAGE_W - 2 * MARGIN
TOL = T.TOLERANCIA_VERTICAL

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#52606d")
RULE = colors.HexColor("#cbd2d9")
PANEL = colors.HexColor("#f3f5f7")
ACCENT = colors.HexColor("#0b6e99")
LEVEL_COLORS = {"A": colors.HexColor("#2e7d32"), "B": colors.HexColor("#b26a00"), "C": colors.HexColor("#b71c1c")}

ASPECT_NAMES = {"N": "norte", "NE": "noreste", "E": "este", "SE": "sureste", "S": "sur", "SO": "suroeste", "O": "oeste",
                "NO": "noroeste"}
FACTOR_NAMES = {
    "pixels": ("Píxeles de 30 m dentro del predio", "{} px", 0),
    "elev_range_m": ("Rango de elevación (frente al error ≈ 2 m)", "{} m", 1),
    "slope_mean_pct": ("Pendiente del predio (factor de confianza)", "{} %", 1),
    "disagreement_rms_m": ("Desacuerdo GLO-30 vs AW3D30 (RMS centrado)", "{} m", 2),
    "canopy_pct": ("Cobertura de dosel o construido", "{} %", 1),
    "void_water_pct": ("Vacíos, rellenos y agua (EDM/FLM/WBM)", "{} %", 1),
    "hem_p90_m": ("Incertidumbre propia de GLO-30 (HEM, percentil 90)", "{} m", 1),
}
SECTION_NAMES = {"elevacion": "Elevación", "pendiente": "Pendiente", "orientacion": "Orientación",
                 "comparacion_fuentes": "Comparación de fuentes"}

_FONTS_DONE = False


def fmt_es(x, nd: int = 1) -> str:
    """Numero al estilo es-CL: coma decimal y punto de miles."""
    s = f"{abs(float(x)):,.{nd}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    neg = float(x) < 0 and any(ch in "123456789" for ch in s)
    return ("-" if neg else "") + s


def _fonts():
    global _FONTS_DONE
    if _FONTS_DONE:
        return
    d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("DV", str(d / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DV-B", str(d / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("DV-I", str(d / "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFont(TTFont("DV-BI", str(d / "DejaVuSans-BoldOblique.ttf")))
    pdfmetrics.registerFont(TTFont("DVM", str(d / "DejaVuSansMono.ttf")))
    pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-B", italic="DV-I", boldItalic="DV-BI")
    _FONTS_DONE = True


def _styles() -> dict:
    base = dict(fontName="DV", fontSize=9, leading=12.6, textColor=INK, alignment=TA_LEFT)
    return {
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", **{**base, "fontSize": 7.6, "leading": 10.2, "textColor": MUTED}),
        "cap": ParagraphStyle("cap", **{**base, "fontName": "DV-I", "fontSize": 7.8, "leading": 10.4, "textColor": MUTED,
                                         "spaceBefore": 2, "spaceAfter": 6}),
        "h1": ParagraphStyle("h1", **{**base, "fontName": "DV-B", "fontSize": 13, "leading": 16, "spaceBefore": 12,
                                       "spaceAfter": 5, "keepWithNext": 1}),
        "h2": ParagraphStyle("h2", **{**base, "fontName": "DV-B", "fontSize": 9.6, "leading": 12.5, "spaceBefore": 7,
                                       "spaceAfter": 3, "keepWithNext": 1}),
        "title": ParagraphStyle("title", **{**base, "fontName": "DV-B", "fontSize": 21, "leading": 25, "spaceAfter": 2}),
        "sub": ParagraphStyle("sub", **{**base, "fontSize": 10, "leading": 13.5, "textColor": MUTED, "spaceAfter": 6}),
        "bullet": ParagraphStyle("bullet", **{**base, "leftIndent": 10, "bulletIndent": 0, "spaceAfter": 2}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": 8.4, "leading": 11}),
        "cellb": ParagraphStyle("cellb", **{**base, "fontName": "DV-B", "fontSize": 8.4, "leading": 11}),
        "mono": ParagraphStyle("mono", **{**base, "fontName": "DVM", "fontSize": 6.6, "leading": 8.6}),
        "note": ParagraphStyle("note", **{**base, "fontSize": 8.4, "leading": 11.4, "textColor": INK}),
        "alert": ParagraphStyle("alert", **{**base, "fontName": "DV-B", "fontSize": 9.4, "leading": 12.6,
                                             "textColor": colors.white}),
        "badge": ParagraphStyle("badge", **{**base, "fontName": "DV-B", "fontSize": 34, "leading": 38,
                                             "textColor": colors.white, "alignment": 1}),
        "badgesub": ParagraphStyle("badgesub", **{**base, "fontSize": 7, "leading": 9, "textColor": colors.white, "alignment": 1}),
    }


class _NumberedCanvas(rl_canvas.Canvas):
    """Pie de pagina en CADA hoja: fuente, resolucion real, nivel y 'hoja i de N' (principio 3 de la spec)."""

    footer_text = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
        self.setFont("DV", 7)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, 10 * mm, self.footer_text)
        self.drawRightString(PAGE_W - MARGIN, 10 * mm, f"hoja {self._pageNumber} de {total}")


# ---------------------------------------------------------------- helpers de contenido

def _p(text, style):
    return Paragraph(text, style)


def _esc(s) -> str:
    return escape(str(s))


def _img(png: bytes, width_mm: float) -> Image:
    w, h = PILImage.open(io.BytesIO(png)).size
    return Image(io.BytesIO(png), width=width_mm * mm, height=width_mm * mm * h / w)


def _missing(S, what: str):
    return _p(f"<i>Figura no disponible: {_esc(what)}.</i>", S["small"])


def _kv_table(S, rows, col=(52 * mm, FRAME_W - 52 * mm)):
    data = [[_p(_esc(k), S["cellb"]), _p(v if isinstance(v, str) else _esc(v), S["cell"])] for k, v in rows]
    t = Table(data, colWidths=col)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _grid_table(S, header, rows, widths):
    data = [[_p(_esc(h), S["cellb"]) for h in header]] + [
        [c if not isinstance(c, str) else _p(c, S["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _alert(S, text, color):
    t = Table([[_p(text, S["alert"])]], colWidths=[FRAME_W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), color), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _utm_label(epsg: int) -> str:
    return {32718: "UTM 18S", 32719: "UTM 19S"}.get(epsg, f"EPSG:{epsg}")


# ---------------------------------------------------------------- validacion

def _validate(manifest: dict, out_dir: Path) -> None:
    if (out_dir / REJECTION_NAME).exists():
        raise RejectRequest(f"el pipeline rechazó este pedido ({REJECTION_NAME}): no se genera informe")
    try:
        for k in ("recipe", "inputs", "grid", "results", "confidence", "volatile", "result_hash"):
            manifest[k]
        manifest["results"]["terrain"]["n_pixels"]
        manifest["confidence"]["overall"]
        next(d for d in manifest["inputs"]["datasets"] if d["role"] == "base")
        manifest["inputs"]["polygon"], manifest["inputs"]["polygon_area_m2"], manifest["grid"]["pixel_m"]
    except (KeyError, StopIteration, TypeError) as exc:
        raise RejectRequest(f"manifiesto incompleto o inválido: falta {exc}")
    if manifest["confidence"]["overall"] not in ("A", "B", "C"):
        raise RejectRequest("manifiesto inválido: nivel de confianza fuera de A/B/C")
    if hash_manifest(manifest) != manifest["result_hash"]:
        raise RejectRequest("manifiesto alterado: el result_hash no coincide con su contenido")


# ---------------------------------------------------------------- secciones

def _title_and_quality(S, manifest, f, site_name):
    st = []
    base = next(d for d in manifest["inputs"]["datasets"] if d["role"] == "base")
    acquired = manifest["volatile"].get("acquired", {}).get(base["source"], "no registrada")
    epoch = base.get("acquisition_epoch", "no registrada")
    st.append(_p(_esc(T.TITULO), S["title"]))
    sub = f"{_esc(site_name)} · {_esc(T.SUBTITULO)}" if site_name else _esc(T.SUBTITULO)
    st.append(_p(sub, S["sub"]))
    level = f["nivel"]
    badge = Table([[_p(level, S["badge"])], [_p("NIVEL", S["badgesub"])]], colWidths=[24 * mm])
    badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LEVEL_COLORS[level]), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    lines = [
        _p("<b>Calidad del dato</b> (antes de los resultados)", S["cellb"]),
        _p(f"Fuente: {_esc(f['fuente'])}", S["body"]),
        _p(f"Resolución real del dato: <b>{fmt_es(f['resolucion_m'], 0)} m</b>", S["body"]),
        _p(f"Nivel de confianza: <b>{level}</b>", S["body"]),
        _p(f"Píxeles de 30 m dentro del predio: <b>{fmt_es(f['n_pixeles'], 0)}</b>", S["body"]),
        _p(f"Fecha de descarga: {_esc(acquired)} · época de adquisición del dato: {_esc(epoch)} · "
           f"datum vertical {_esc(f['datum_vertical'])}", S["body"]),
    ]
    box = Table([[badge, lines]], colWidths=[28 * mm, FRAME_W - 28 * mm])
    box.setStyle(TableStyle([("BACKGROUND", (1, 0), (1, 0), PANEL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (1, 0), (1, 0), 10), ("TOPPADDING", (1, 0), (1, 0), 6),
                             ("BOTTOMPADDING", (1, 0), (1, 0), 6), ("BOX", (0, 0), (-1, -1), 0.6, RULE)]))
    st += [box, Spacer(1, 5), _p(_esc(T.AVISO_RESOLUCION), S["note"]), Spacer(1, 4)]
    if f["revision_humana"]:
        st += [_alert(S, _esc(T.REVISION_HUMANA_C), LEVEL_COLORS["C"]), Spacer(1, 4)]
    if f["pendiente"]["estado"] == "no_concluyente":
        why = (f"El predio tiene {fmt_es(f['n_pixeles'], 0)} píxeles de 30 m (menos de 9)"
               if manifest["results"]["terrain"].get("inconclusive") else "El nivel de confianza es C")
        st += [_p(f"<b>Pendiente y orientación: {T.NO_CONCLUYENTE}.</b> {why}; el informe no entrega cifras de pendiente "
                  "puntuales ni orientación.", S["note"]), Spacer(1, 4)]
    desc = Table([[[_p(_esc(T.DESCARGO), S["note"]), Spacer(1, 2), _p(_esc(T.FILTRO_DE_ESCRITORIO), S["note"]),
                    Spacer(1, 2), _p(_esc(T.BORRADOR_LEGAL), S["small"])]]], colWidths=[FRAME_W])
    desc.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    st.append(desc)
    return st


def _sec_ficha(S, manifest, f, ctx, client_fields, sentinel2_year):
    inp = manifest["inputs"]
    area = float(inp["polygon_area_m2"])
    lon, lat = inp["centroid_lonlat"]
    epsg = manifest["grid"]["epsg"]
    rows = [
        ("Superficie del polígono", f"{fmt_es(area, 0)} m² ({fmt_es(area / 10000.0, 3)} ha)"),
        ("Centroide (lon, lat)", f"{fmt_es(lon, 5)}°, {fmt_es(lat, 5)}° (WGS 84)"),
        ("CRS de trabajo", f"{manifest['grid']['crs']} · WGS 84 / {_utm_label(epsg)} (zona del centroide)"),
        ("Datum vertical", _esc(f["datum_vertical"])),
    ]
    for k, v in (client_fields or {}).items():
        rows.append((str(k), _esc(v)))
    st = [_p("1. Ficha del terreno", S["h1"]), _kv_table(S, rows), _p(_esc(T.SUPERFICIE_NOTA), S["cap"])]
    png = fig_overview(ctx) if ctx is not None else None
    if png:
        st += [_img(png, 118), _p("Sombreado del relieve (GLO-30, 30 m) con el polígono del predio. "
                                  + ("" if sentinel2_year else _esc(T.SIN_SENTINEL2)), S["cap"])]
    else:
        st.append(_missing(S, "no se encontró el ráster de trabajo del manifiesto"))
    return st


def _sec_calidad(S, manifest, f):
    base = next(d for d in manifest["inputs"]["datasets"] if d["role"] == "base")
    c = manifest["confidence"]
    acquired = manifest["volatile"].get("acquired", {}).get(base["source"], "no registrada")
    st = [_p("2. Calidad del dato y nivel de confianza (A/B/C)", S["h1"])]
    st.append(_kv_table(S, [
        ("Fuente", _esc(f["fuente"])), ("Versión del dato", _esc(base["version"])),
        ("Resolución real", f"{fmt_es(f['resolucion_m'], 0)} m (píxel ≈ {fmt_es(f['resolucion_m'] ** 2, 0)} m²)"),
        ("Fecha de descarga", _esc(acquired)), ("Época de adquisición", _esc(base.get("acquisition_epoch", "no registrada"))),
        ("Origen", _esc(base["url"])),
        ("Píxeles dentro del predio", fmt_es(f["n_pixeles"], 0)), ("Nivel de confianza", f"<b>{f['nivel']}</b>"),
    ] + ([("Sellos", " · ".join(f"{_esc(T.SELLO_NAMES.get(k, k))}: <b>{_esc(v)}</b>" for k, v in c["by_seal"].items()))]
         if c.get("by_seal") else [])))
    st.append(Spacer(1, 6))
    rows = []
    inconcl = f["pendiente"]["estado"] == "no_concluyente"
    for key, (name, unit, nd) in FACTOR_NAMES.items():
        lvl = c["by_factor"].get(key, "sin_dato")
        v = c["values"].get(key)
        if key == "slope_mean_pct" and inconcl:
            val, lvl_txt = f"{T.NO_CONCLUYENTE} (no se informa)", lvl if lvl != "sin_dato" else "sin dato"
        elif v is None and lvl == "no_disponible":
            val, lvl_txt = "no disponible (la lectura falló)", "no disponible (tope B)"
        elif v is None:
            val = "no medido en esta corrida" if key == "canopy_pct" else "sin dato"
            lvl_txt = "sin dato (tope B)"
        else:
            val, lvl_txt = unit.format(fmt_es(v, nd)), lvl
        rows.append([name, val, f"<b>{_esc(lvl_txt)}</b>"])
    st.append(_grid_table(S, ["Factor", "Valor", "Nivel"], rows, [84 * mm, 56 * mm, FRAME_W - 140 * mm]))
    secs = ", ".join(f"{SECTION_NAMES.get(k, k)}: {v}" for k, v in c["by_section"].items())
    st += [Spacer(1, 4), _p(f"Nivel por sección: {_esc(secs)}.", S["body"])]
    lim = [FACTOR_NAMES[k][0] for k in c.get("limiting_factors", []) if k in FACTOR_NAMES]
    if lim:
        st.append(_p("Factores que fijan el nivel: " + _esc("; ".join(lim)) + ".", S["body"]))
    lost = [FACTOR_NAMES[k][0] for k in c.get("unavailable_factors", []) if k in FACTOR_NAMES]
    if lost:
        notes = []
        if any(k != "canopy_pct" for k in c.get("unavailable_factors", [])):
            notes.append("la capa auxiliar de Copernicus GLO-30 (" + manifest["results"]["aux_layers"].get("note", "") + ")")
        if "canopy_pct" in c.get("unavailable_factors", []):
            notes.append("la cobertura ESA WorldCover (" + (manifest["results"].get("worldcover") or {}).get("note", "") + ")")
        st.append(_p("<b>No disponible:</b> " + _esc("; ".join(lost)) + ". Se pudo medir, pero no se pudo leer "
                     + _esc(" ni ".join(notes)) + " tras reintentar. El nivel no puede pasar de B y la revisión humana "
                     "es obligatoria antes de entregar.", S["body"]))
    st += [_p(_esc(T.REGLA_CONFIANZA), S["cap"])]
    return st


def _sec_elevacion(S, f):
    e = f["elevacion"]
    st = [_p("3. Elevación", S["h1"])]
    if e["elev_min"] is None:
        return st + [_p("Sin datos de elevación dentro del predio.", S["body"])]
    rows = [
        ["Mínima", f"{fmt_es(e['elev_min'], 1)} m ({TOL})"], ["Máxima", f"{fmt_es(e['elev_max'], 1)} m ({TOL})"],
        ["Media", f"{fmt_es(e['elev_mean'], 1)} m ({TOL})"], ["Rango dentro del predio", f"{fmt_es(e['elev_range'], 1)} m"],
    ]
    st.append(_grid_table(S, ["Dentro del predio", f"Cota en m s. n. m. ({f['datum_vertical'].split(' ')[0]})"], rows,
                          [70 * mm, 70 * mm]))
    st += [Spacer(1, 4), _p(_esc(T.LE90_NOTA), S["body"])]
    if e["elevation_within_noise"]:
        st.append(_p("<b>" + _esc(T.RUIDO_DEL_DATO) + "</b> El rango es menor que ~4 m (el doble del error relativo medido "
                     "en la RM): no se interpreta como relieve.",
                     S["body"]))
    return st


def _sec_pendiente(S, manifest, f, ctx):
    st = [_p("4. Pendiente por clases gruesas", S["h1"])]
    pend = f["pendiente"]
    if pend["estado"] == "no_concluyente":
        why = ("el predio tiene menos de 9 píxeles de 30 m (≈3×3)" if manifest["results"]["terrain"].get("inconclusive")
               else "el nivel de confianza es C (la spec omite las cifras de pendiente puntuales)")
        return st + [_p(f"<b>{T.NO_CONCLUYENTE.capitalize()}</b>: {why}. No se muestran clases, mapa ni cifras de pendiente.",
                        S["body"])]
    cls = pend["clases_pct"]
    rows = [[lab, f"{fmt_es(cls[k], 1)} %"] for k, lab in zip(SLOPE_CLASS_KEYS, SLOPE_CLASS_LABELS)]
    left = [_grid_table(S, ["Clase de pendiente", "% del predio"], rows, [38 * mm, 28 * mm]), Spacer(1, 5),
            _p(f"Pendiente media: <b>{fmt_es(pend['media_pct'], 1)} %</b> ({fmt_es(pend['media_grados'], 1)}°)", S["body"])]
    bars = _img(fig_slope_bars(cls), 84)
    t = Table([[left, bars]], colWidths=[72 * mm, FRAME_W - 72 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    st.append(t)
    png = fig_slope_map(ctx) if ctx is not None else None
    st.append(KeepTogether([_img(png, 96), _p("Mapa de pendiente por clases (píxeles reales de 30 m, sin suavizar).", S["cap"])])
              if png else _missing(S, "no se encontró el ráster de pendiente"))
    st.append(_p("Rangos, no cifras puntuales. " + _esc(T.PENDIENTE_REFERENCIA), S["small"]))
    return st


def _sec_orientacion(S, f):
    st = [_p("5. Orientación (aspect)", S["h1"])]
    o = f["orientacion"]
    if o["estado"] == "no_concluyente":
        return st + [_p(f"<b>{T.NO_CONCLUYENTE.capitalize()}</b>: no se informa orientación.", S["body"])]
    if o["estado"] != "ok":
        return st + [_p("<b>Sin orientación predominante</b>: en terreno casi plano (pendiente media bajo 3 %) la orientación "
                        "es ruido del dato.", S["body"])]
    name = ASPECT_NAMES[o["dominante"]]
    left = [_p(f"Orientación predominante: <b>{_esc(o['dominante'])}</b> ({name})", S["body"]),
            _p("Dirección hacia la que baja el terreno; la rosa reparte los píxeles del predio por sector de 45°.", S["small"])]
    t = Table([[left, _img(fig_rose(o["rosa"], o["dominante"]), 58)]], colWidths=[FRAME_W - 60 * mm, 60 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return st + [t]


def _sec_puntaje(S, manifest, f):
    st = [_p("6. Puntaje de complejidad del relieve", S["h1"])]
    if f["puntaje_relieve"] is None:
        return st + [_p(f"<b>{T.NO_CONCLUYENTE.capitalize()}</b>: no se informa puntaje.", S["body"])]
    hyp = manifest["results"]["terrain"].get("relief_score_hypothesis", {}).get("formula", "")
    st += [_p(f"Puntaje: <b>{f['puntaje_relieve']} de 7</b> (escala de 1 a 7)", S["body"]), _p(_esc(T.PUNTAJE_ALCANCE), S["cap"])]
    if hyp:
        st.append(_p("Fórmula (hipótesis): " + _esc(hyp), S["small"]))
    return st


def _sec_sol(S, manifest, f):
    st = [_p("7. Asoleamiento y sombra por relieve", S["h1"])]
    s = f["asoleamiento"]
    if s.get("status") != "ok":
        return st + [_p(f"No disponible: {_esc(s.get('reason', 'no se calculó'))}.", S["body"])]
    st.append(KeepTogether([_img(fig_sun(s), FRAME_W / mm),
                            _p("Trayectoria solar por estación (hora solar) sobre el perfil de horizonte del relieve del DSM "
                               "alrededor del predio.", S["cap"])]))
    rows = [[v["label"].capitalize(), iso, f"{fmt_es(v['day_length_h'], 1)} h", f"{fmt_es(v['direct_sun_h'], 1)} h",
             f"{fmt_es(v['noon_elevation_deg'], 0)}°"] for iso, v in s["dates"].items()]
    st.append(_grid_table(S, ["Fecha de referencia", "Día", "Día con sol sobre el horizonte plano",
                              "Sol directo con el relieve de la ventana", "Elevación al mediodía"], rows,
                          [42 * mm, 24 * mm, 34 * mm, 42 * mm, FRAME_W - 142 * mm]))
    st += [Spacer(1, 4), _p(_esc(T.ASOLEAMIENTO_ALCANCE), S["body"]),
           _p(f"Alcance del cálculo: rayos de {fmt_es(s['ray_length_m']['min'], 0)} a {fmt_es(s['ray_length_m']['max'], 0)} m "
              "desde el centroide; sin refracción ni nubosidad (máximos teóricos).", S["small"])]
    return st


def _sec_perfiles(S, f, ctx):
    st = [_p("8. Perfiles de elevación", S["h1"])]
    if f["pendiente"]["estado"] == "no_concluyente":
        return st + [_p("Perfiles no incluidos: con el predio «no concluyente» un perfil de 30 m insinuaría una pendiente que "
                        "el informe no afirma.", S["body"])]
    png = fig_profiles(ctx) if ctx is not None else None
    if not png:
        return st + [_missing(S, "no se encontró el ráster de trabajo para trazar los cortes")]
    return st + [KeepTogether([_img(png, FRAME_W / mm),
                               _p(f"Cortes sur-norte y oeste-este por el centroide. La banda naranja es la incertidumbre vertical "
                                  f"({TOL}, LE90); variaciones dentro de la banda no son relieve demostrable.", S["cap"])])]


def _sec_curvas(S, f, ctx):
    st = [_p("9. Curvas de nivel indicativas", S["h1"])]
    interval = f["curvas"]["intervalo_m"]
    st.append(_p(f"<b>{_esc(T.CURVAS_INDICATIVAS)}</b> Con una equidistancia de {fmt_es(interval, 0)} m, "
                 f"calculada con GDAL sobre el DEM de 30 m. {_esc(T.CURVAS_SIN_DESCARGA)}", S["body"]))
    png, n = (fig_contours(ctx, float(interval)) if ctx is not None else (None, 0))
    if png and n:
        st.append(KeepTogether([_img(png, 100), _p("Curvas de nivel cada 10 m (más gruesas cada 50 m) sobre el sombreado. "
                                                   "Indicativas, no para diseño.", S["cap"])]))
    elif ctx is not None and ctx.lines is not None:
        st.append(_p("No hay curvas que mostrar: el desnivel de la ventana de trabajo es menor que la equidistancia.", S["body"]))
    else:
        st.append(_missing(S, "no se encontraron las curvas de trabajo"))
    return st


def _sec_hidrico(S, f):
    st = [_p("10. Contexto hídrico de ladera", S["h1"])]
    o = f["orientacion"]
    if o["estado"] == "ok":
        d = o["dominante"]
        st.append(_p(f"A escala de ladera el terreno baja hacia el {ASPECT_NAMES[d]} ({_esc(d)}); ese es el sentido general "
                     "del escurrimiento superficial.", S["body"]))
    elif o["estado"] == "no_concluyente":
        st.append(_p(f"<b>{T.NO_CONCLUYENTE.capitalize()}</b>: no se informa dirección de escurrimiento.", S["body"]))
    else:
        st.append(_p("Sin dirección predominante de escurrimiento a escala de ladera (terreno casi plano).", S["body"]))
    return st + [_p(_esc(T.HIDRICO_ALCANCE), S["cap"])]


def _sec_comparacion(S, manifest, f, ctx):
    st = [_p("11. Comparación de fuentes", S["h1"])]
    cm = manifest["results"]["comparison"]
    c = manifest["confidence"]
    if f["comparacion"] is None:
        return st + [_p(f"No disponible: {_esc(cm.get('reason', 'sin comparación'))}.", S["body"]),
                     _p("Sin la comparación el factor de desacuerdo queda «sin dato» y el nivel no puede ser mejor que B.", S["body"])]
    k = f["comparacion"]
    inconcl = f["pendiente"]["estado"] == "no_concluyente"
    rows = [("Diferencia RMS (AW3D30 − GLO-30)", f"{fmt_es(k['rms_diff_m'], 2)} m"),
            ("Diferencia media con signo", f"{fmt_es(k['mean_diff_m'], 2)} m"),
            ("Datums alineados (EGM96 → EGM2008)", "sí" if k["datum_aligned"] else "no"),
            ("Alcance de la estadística", "predio" if k["stats_scope"] != "entorno" else "entorno del predio (el predio es muy chico)")]
    if not inconcl:
        rows.insert(2, ("Diferencia media de pendiente", f"{fmt_es(k['slope_diff_mean_pct'], 1)} puntos porcentuales"))
    st += [_kv_table(S, rows), Spacer(1, 5)]
    lvl = c["by_factor"].get("disagreement_rms_m")
    if not k["datum_aligned"]:
        st.append(_p("Los datums no pudieron alinearse: la diferencia incluye el sesgo de datum y no se usa para el nivel.", S["body"]))
    elif lvl in ("B", "C"):
        st.append(_p(f"<b>La diferencia entre fuentes es alta</b> (factor en nivel {lvl}); el nivel de confianza baja por esta causa.", S["body"]))
    else:
        st.append(_p("La diferencia entre fuentes es baja (factor en nivel A).", S["body"]))
    png = fig_diff(ctx) if ctx is not None else None
    st.append(KeepTogether([_img(png, 100), _p("Mapa de desacuerdo AW3D30 − GLO-30 (m).", S["cap"])]) if png
              else _missing(S, "no se encontró el mapa de diferencia de trabajo"))
    st.append(_p(_esc(T.NOTA_AW3D30), S["small"]))
    return st


def _sec_visor(S, ctx, viewer_url, video_url, exaggeration):
    st = [_p("12. Visor 3D y video", S["h1"])]
    ex = fmt_es(exaggeration, 1)
    rows = [("Visor 3D interactivo", _esc(viewer_url) if viewer_url else "enlace no publicado en esta corrida"),
            ("Video de 30-60 s", _esc(video_url) if video_url else "enlace no publicado en esta corrida")]
    st.append(_kv_table(S, rows))
    st.append(Spacer(1, 4))
    png = fig_3d(ctx, exaggeration) if ctx is not None else None
    cap = (f"Vista 3D cualitativa del terreno (los ejes no llevan cifras; las cotas del predio están en la sección 3). "
           f"<b>Exageración vertical ×{ex}</b>: las alturas se dibujan {ex} veces mayores que las reales. "
           "El visor muestra el mismo rótulo de forma permanente y permite ajustarlo.")
    st.append(KeepTogether([_img(png, 115), _p(cap, S["cap"])]) if png else
              _p(cap + " (Vista no disponible: falta el ráster de trabajo.)", S["cap"]))
    return st


def _sec_no_cubre(S):
    st = [_p("13. " + _esc(T.NO_CUBRE_TITULO), S["h1"])]
    for item in T.NO_CUBRE_ITEMS:
        st.append(Paragraph(_esc(item), S["bullet"], bulletText="•"))
    return st


def _sec_anexo(S, manifest, sentinel2_year, comparison_ok):
    st = [_p("14. Anexo de trazabilidad", S["h1"])]
    acq = manifest["volatile"].get("acquired", {})
    for d in manifest["inputs"]["datasets"]:
        rows = [("Rol", "dato base" if d["role"] == "base" else "comparación"), ("Dataset", _esc(d["name"])),
                ("Versión", _esc(d["version"])), ("Fecha de descarga", _esc(acq.get(d["source"], "no registrada"))),
                ("Época de adquisición", _esc(d.get("acquisition_epoch", "no registrada")) + " — "
                 + _esc(d.get("acquisition_epoch_note", ""))),
                ("Enlace", _esc(d["url"])), ("Tiles", _esc(", ".join(d.get("tiles", [])))),
                ("CRS nativo · datum vertical", f"{_esc(d['native_crs'])} · {_esc(d['vertical_datum'])}"),
                ("Fracción nodata", fmt_es(100 * float(d["nodata_fraction"]), 2) + " %"),
                ("Huella SHA-256 del ráster", f"<font name='DVM' size='6.6'>{_esc(d['array_sha256'])}</font>")]
        st += [_p(f"Dataset: {_esc(d['source'])}", S["h2"]), _kv_table(S, rows)]
    g = manifest["grid"]
    r = manifest["recipe"]
    rows = [("CRS de trabajo", _esc(g["crs"])), ("Tamaño de píxel", f"{fmt_es(g['pixel_m'], 0)} m · remuestreo {_esc(g['resampling'])}"),
            ("Datum vertical", _esc(g["vertical_datum"])), ("Regla de zona UTM", _esc(manifest["inputs"]["utm_zone_rule"])),
            ("Versión de la receta", _esc(r["version"])),
            ("Hash de la receta", f"<font name='DVM' size='6.6'>{_esc(r['hash'])}</font>"),
            ("Hash de las entradas", f"<font name='DVM' size='6.6'>{_esc(manifest['inputs']['hash'])}</font>"),
            ("Hash de los resultados", f"<font name='DVM' size='6.6'>{_esc(manifest['result_hash'])}</font>"),
            ("Hash de las reglas de confianza", f"<font name='DVM' size='6.6'>{_esc(r['confidence_rules_hash'])}</font>")]
    tools = r["tools"]
    rows.append(("Versiones de software", _esc("; ".join(f"{k} {v}" for k, v in tools.items()))))
    if manifest["volatile"].get("generated_at"):
        rows.append(("Generado", _esc(manifest["volatile"]["generated_at"])))
    st += [_p("Procesamiento", S["h2"]), _kv_table(S, rows)]
    st += [_p("Hipótesis declaradas", S["h2"])]
    for h in r["hypotheses"]:
        st.append(Paragraph(_esc(h), S["bullet"], bulletText="•"))
    st += [_p(_esc(T.ATRIBUCION_TITULO), S["h2"]), _p(_esc(T.ATRIBUCION_GLO30), S["body"]), Spacer(1, 3),
           _p(_esc(T.NO_RESPONSABILIDAD_GLO30), S["body"]), Spacer(1, 3), _p(_esc(T.NOTA_GLO30_MODIFICADO), S["small"])]
    if comparison_ok:
        st += [Spacer(1, 3), _p(_esc(T.NOTA_AW3D30), S["small"])]
    if (manifest["results"].get("worldcover") or {}).get("status") == "ok":
        st += [Spacer(1, 3), _p(_esc(T.ATRIBUCION_WORLDCOVER), S["small"])]
    if sentinel2_year:
        st += [Spacer(1, 3), _p(_esc(T.SENTINEL2_TEMPLATE.format(year=sentinel2_year)), S["body"])]
    st += [Spacer(1, 3), _p(_esc(T.NO_AVAL), S["small"])]
    return st


def _glue(section: list) -> list:
    """El titulo de una seccion nunca queda solo al pie de una hoja: se pega al primer bloque que le sigue."""
    return [KeepTogether(section[:2])] + section[2:] if len(section) > 1 else section


# ---------------------------------------------------------------- API

def render_pdf(
    manifest: dict,
    out: Path,
    *,
    site_name: str | None = None,
    client_fields: dict | None = None,
    viewer_url: str | None = None,
    video_url: str | None = None,
    sentinel2_year: int | None = None,
    exaggeration: float = DEFAULT_EXAGGERATION,
) -> Path:
    """Escribe el informe y devuelve la ruta. ``out``: archivo ``.pdf`` o carpeta (se escribe ``report.pdf`` dentro).

    Rechaza (RejectRequest, sin escribir nada) si el manifiesto esta incompleto o alterado o si junto a ``out`` hay un
    ``rejection.json``. ``sentinel2_year``: solo si el visor/GLB usa una textura Sentinel-2 (activa la atribucion).
    """
    out = Path(out)
    pdf_path = out if out.suffix.lower() == ".pdf" else out / REPORT_NAME
    _validate(manifest, pdf_path.parent)
    _fonts()
    S = _styles()
    f = report_fields(manifest)
    ctx = load_ctx(manifest)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    class Canvas(_NumberedCanvas):
        footer_text = (f"Informe preliminar de terreno · Copernicus GLO-30 · resolución real {fmt_es(f['resolucion_m'], 0)} m · "
                       f"nivel {f['nivel']}")

    story = []
    story += _title_and_quality(S, manifest, f, site_name)
    story += _sec_ficha(S, manifest, f, ctx, client_fields, sentinel2_year)
    story.append(PageBreak())  # los resultados empiezan despues de la hoja 1 (spec sec. 3 #2)
    story += _glue(_sec_calidad(S, manifest, f))
    story += _glue(_sec_elevacion(S, f))
    story += _glue(_sec_pendiente(S, manifest, f, ctx))
    story += _glue(_sec_orientacion(S, f))
    story += _glue(_sec_puntaje(S, manifest, f))
    story += _glue(_sec_sol(S, manifest, f))
    story += _glue(_sec_perfiles(S, f, ctx))
    story += _glue(_sec_curvas(S, f, ctx))
    story += _glue(_sec_hidrico(S, f))
    story += _glue(_sec_comparacion(S, manifest, f, ctx))
    story += _glue(_sec_visor(S, ctx, viewer_url, video_url, float(exaggeration)))
    story += _glue(_sec_no_cubre(S))
    story += _sec_anexo(S, manifest, sentinel2_year, f["comparacion"] is not None)

    old = rl_config.invariant
    rl_config.invariant = 1  # PDF reproducible: sin fecha de creacion ni ids aleatorios
    try:
        doc = SimpleDocTemplate(
            str(pdf_path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm, bottomMargin=20 * mm,
            title=T.TITULO, author="terreno (POC)", subject=f"Nivel {f['nivel']} · resolución real 30 m", invariant=1,
        )
        doc.build(story, canvasmaker=Canvas)
    finally:
        rl_config.invariant = old
    return pdf_path


def render_pdf_hook(manifest: dict, out_dir: Path) -> Path:
    """Gancho de ``run_pipeline(hooks={"render_pdf": ...})``: solo recibe el manifiesto y la carpeta de salida."""
    return render_pdf(manifest, Path(out_dir) / REPORT_NAME)
