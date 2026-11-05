"""Capas PNG RGBA del visor (pendiente por clases, orientacion, curvas, sombreado) y lector de curvas GPKG.

Todas las capas cubren EXACTAMENTE ``bbox_local_m`` de la malla (extension entre centros de celda, primera fila = norte),
asi que el mapeo UV de la malla las alinea sin transformaciones. Las capas categoricas (pendiente, orientacion) se
remuestrean por vecino mas cercano: se ve el pixel real de 30 m, sin suavizar. Si la pendiente no es concluyente (o el
nivel es C) la capa de pendiente/orientacion se escribe totalmente transparente: nunca se inventa.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import numpy as np
import shapely
from PIL import Image, ImageDraw

from terreno.palette import (
    ASPECT_COLORS,
    ASPECT_LABELS,
    CONTOUR_RGBA,
    LAYER_ALPHA,
    SLOPE_CLASS_KEYS,
    SLOPE_COLORS,
)

MAX_LAYER_PX = 2048
MAJOR_CONTOUR_EVERY_M = 50.0  # curvas indice (mas gruesas) cada 50 m
FLAT_SLOPE_PCT = 3.0


def layer_size(n_cols: int, n_rows: int) -> tuple[int, int]:
    """(ancho, alto) en pixeles de la capa: (n-1) intervalos entre centros por factor entero k (4..32)."""
    span = max(n_cols - 1, n_rows - 1, 1)
    k = int(max(4, min(32, MAX_LAYER_PX // span)))
    return (n_cols - 1) * k, (n_rows - 1) * k


def _coords(w: int, h: int, n_cols: int, n_rows: int) -> tuple[np.ndarray, np.ndarray]:
    """Coordenadas (en indices de celda, centros enteros) de cada pixel de la capa. Fila 0 = norte."""
    cf = (np.arange(w) + 0.5) / w * (n_cols - 1)
    rf = (np.arange(h) + 0.5) / h * (n_rows - 1)
    return cf, rf


def sample_nearest(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    n_rows, n_cols = arr.shape
    cf, rf = _coords(w, h, n_cols, n_rows)
    ci = np.clip(np.floor(cf + 0.5).astype(int), 0, n_cols - 1)
    ri = np.clip(np.floor(rf + 0.5).astype(int), 0, n_rows - 1)
    return arr[np.ix_(ri, ci)]


def sample_bilinear(arr: np.ndarray, valid: np.ndarray, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Bilineal enmascarado: promedia solo vecinos validos. Devuelve (valores, mascara de validez)."""
    n_rows, n_cols = arr.shape
    cf, rf = _coords(w, h, n_cols, n_rows)
    c0 = np.clip(np.floor(cf).astype(int), 0, n_cols - 2)
    r0 = np.clip(np.floor(rf).astype(int), 0, n_rows - 2)
    fc = (cf - c0)[None, :]
    fr = (rf - r0)[:, None]
    a = np.where(valid, arr, 0.0).astype("float64")
    m = valid.astype("float64")
    num = np.zeros((h, w))
    den = np.zeros((h, w))
    for dr, wr in ((0, 1 - fr), (1, fr)):
        for dc, wc in ((0, 1 - fc), (1, fc)):
            wt = wr * wc
            num += wt * a[np.ix_(r0 + dr, c0 + dc)]
            den += wt * m[np.ix_(r0 + dr, c0 + dc)]
    ok = den > 1e-9
    return np.where(ok, num / np.where(ok, den, 1.0), 0.0), ok


def _save(rgba: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba.astype("uint8"), "RGBA").save(path, format="PNG", optimize=False, compress_level=6)
    return path


def slope_class_index(slope_pct: np.ndarray) -> np.ndarray:
    """0..3 con los mismos intervalos cerrados a la izquierda que compute_terrain: [0,5) [5,15) [15,30) [30,inf)."""
    return np.digitize(slope_pct, [5.0, 15.0, 30.0])


def slope_layer(slope: np.ndarray, valid: np.ndarray, w: int, h: int, *, available: bool) -> np.ndarray:
    rgba = np.zeros((h, w, 4), dtype="uint8")
    if not available:
        return rgba
    s = sample_nearest(np.where(valid, slope, 0.0), w, h)
    ok = sample_nearest(valid, w, h)
    cls = slope_class_index(s)
    for i, key in enumerate(SLOPE_CLASS_KEYS):
        sel = ok & (cls == i)
        rgba[sel, :3] = SLOPE_COLORS[key]
    rgba[ok, 3] = LAYER_ALPHA
    return rgba


def aspect_sector_index(aspect_deg: np.ndarray) -> np.ndarray:
    return np.floor(((aspect_deg + 22.5) % 360.0) / 45.0).astype(int) % 8


def aspect_layer(aspect: np.ndarray, slope: np.ndarray, valid: np.ndarray, w: int, h: int, *, available: bool) -> np.ndarray:
    rgba = np.zeros((h, w, 4), dtype="uint8")
    if not available:
        return rgba
    a = sample_nearest(np.where(valid, aspect, 0.0), w, h)
    s = sample_nearest(np.where(valid, slope, 0.0), w, h)
    ok = sample_nearest(valid, w, h) & (s >= FLAT_SLOPE_PCT)  # bajo 3 % la orientacion es ruido: transparente
    sec = aspect_sector_index(a)
    for i, label in enumerate(ASPECT_LABELS):
        rgba[ok & (sec == i), :3] = ASPECT_COLORS[label]
    rgba[ok, 3] = LAYER_ALPHA
    return rgba


def hillshade_layer(hill: np.ndarray, valid: np.ndarray, w: int, h: int) -> np.ndarray:
    v, ok = sample_bilinear(hill, valid, w, h)
    g = np.clip(np.rint(v), 0, 255).astype("uint8")
    rgba = np.zeros((h, w, 4), dtype="uint8")
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = np.where(ok, g, 0)
    rgba[..., 3] = np.where(ok, 255, 0)
    return rgba


# ---- curvas: GPKG de gdal_contour -> lineas ----

def read_gpkg_lines(path: Path, layer: str = "contour") -> list[tuple[float, np.ndarray]]:
    """[(elevacion, Nx2 array de coordenadas UTM)], leyendo el GPKG con sqlite3 (cabecera GPKG + WKB)."""
    con = sqlite3.connect(str(path))
    try:
        gcol = con.execute("select column_name from gpkg_geometry_columns where table_name=?", (layer,)).fetchone()
        if gcol is None:
            return []
        rows = con.execute(f'select elev, "{gcol[0]}" from "{layer}" order by elev, fid').fetchall()
    finally:
        con.close()
    out: list[tuple[float, np.ndarray]] = []
    for elev, blob in rows:
        blob = bytes(blob)
        if blob[:2] != b"GP":
            continue
        flags = blob[3]
        env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
        g = shapely.from_wkb(blob[8 + env:])
        parts = list(g.geoms) if hasattr(g, "geoms") else [g]
        for p in parts:
            c = np.asarray(p.coords, dtype="float64")
            if len(c) >= 2:
                out.append((float(elev), c[:, :2]))
    return out


def contour_layer(lines, *, east_first: float, east_last: float, north_first: float,
                  north_last: float, w: int, h: int, major_every_m: float = MAJOR_CONTOUR_EVERY_M) -> np.ndarray:
    """Dibuja las curvas (UTM) sobre un lienzo transparente que cubre [east_first, east_last] x [north_last, north_first]."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sx = w / (east_last - east_first)
    sy = h / (north_first - north_last)
    thin = max(1, w // 700)
    for elev, c in lines:
        px = (c[:, 0] - east_first) * sx
        py = (north_first - c[:, 1]) * sy
        wid = thin + 1 if abs(elev / major_every_m - round(elev / major_every_m)) < 1e-9 else thin
        d.line([(float(a), float(b)) for a, b in zip(px, py)], fill=CONTOUR_RGBA, width=wid)
    return np.asarray(img)


def save_layer(rgba: np.ndarray, path: Path) -> Path:
    return _save(rgba, path)
