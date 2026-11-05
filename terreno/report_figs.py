"""Figuras del informe (matplotlib, backend Agg). Cada funcion devuelve bytes PNG o None si falta un dato de entrada.

Las figuras dibujan los rasters de trabajo cuyas rutas y huellas estan en el manifiesto (``volatile.paths``); los NUMEROS
que el informe escribe en texto salen solo del manifiesto (report.py). Coordenadas locales: metros desde el centroide
del predio (x = este, y = norte). Nunca se dibuja pendiente ni orientacion si el predio no es concluyente (lo decide report.py).
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import shapely  # noqa: E402
from matplotlib.colors import LightSource, ListedColormap  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely.geometry import LineString, shape  # noqa: E402

from terreno.asserts import valid_mask  # noqa: E402
from terreno.overlays import read_gpkg_lines  # noqa: E402
from terreno.palette import (  # noqa: E402
    ASPECT_COLORS,
    ASPECT_LABELS,
    SLOPE_CLASS_KEYS,
    SLOPE_CLASS_LABELS,
    SLOPE_COLORS,
)
from terreno.solar import SUN_DATES, bilinear_at, sun_position  # noqa: E402

INK = "#1f2933"
MUTED = "#52606d"
GRID = "#d9dee3"
ACCENT = "#0b6e99"
DPI = 170


def _rgb01(rgb):
    return tuple(c / 255.0 for c in rgb)


@dataclass
class Ctx:
    epsg: int
    cx: float
    cy: float
    poly_local: object
    dem: np.ndarray
    transform: object
    extent: tuple  # (left, right, bottom, top) en metros locales, bordes de celda
    hill: np.ndarray | None
    slope: np.ndarray | None
    lines: list | None
    diff: tuple | None  # (arr, extent)
    elev_min: float
    elev_max: float


def _read(path, nan_nodata=True):
    if not path or not Path(path).exists():
        return None
    with rasterio.open(path) as src:
        a = src.read(1).astype("float32")
        ok = valid_mask(a, src.nodata)
        a = np.where(ok, a, np.nan)
        return a, src.transform, tuple(src.bounds)


def load_ctx(manifest: dict) -> Ctx | None:
    paths = manifest.get("volatile", {}).get("paths", {})
    got = _read(paths.get("raster_utm"))
    if got is None:
        return None
    dem, t, (left, bottom, right, top) = got
    epsg = int(manifest["grid"]["epsg"])
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    poly = shape(manifest["inputs"]["polygon"])
    poly_utm = shapely.transform(poly, lambda c: np.column_stack(fwd.transform(c[:, 0], c[:, 1])))
    cx, cy = poly_utm.centroid.x, poly_utm.centroid.y
    poly_local = shapely.transform(poly_utm, lambda c: np.column_stack([c[:, 0] - cx, c[:, 1] - cy]))
    hill = _read(paths.get("hillshade"))
    slope = _read(paths.get("slope"))
    lines = None
    if paths.get("contours") and Path(paths["contours"]).exists():
        lines = [(e, np.column_stack([c[:, 0] - cx, c[:, 1] - cy])) for e, c in read_gpkg_lines(Path(paths["contours"]))]
    diff = None
    d = _read(paths.get("diff_map"))
    if d is not None:
        arr, dt, (dl, db, dr, dtp) = d
        diff = (arr, (dl - cx, dr - cx, db - cy, dtp - cy))
    return Ctx(
        epsg=epsg, cx=cx, cy=cy, poly_local=poly_local, dem=dem, transform=t,
        extent=(left - cx, right - cx, bottom - cy, top - cy),
        hill=hill[0] if hill else None, slope=slope[0] if slope else None, lines=lines, diff=diff,
        elev_min=float(np.nanmin(dem)), elev_max=float(np.nanmax(dem)),
    )


def _png(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, metadata={"Software": "terreno"})
    return buf.getvalue()


def _style(ax):
    ax.tick_params(colors=MUTED, labelsize=7)
    for s in ax.spines.values():
        s.set_color(GRID)


def _outline(ax, ctx: Ctx, label=True):
    x, y = ctx.poly_local.exterior.xy
    ax.plot(x, y, color="white", lw=3.6, solid_joinstyle="round", zorder=5)
    ax.plot(x, y, color=INK, lw=1.7, solid_joinstyle="round", zorder=6, label="Predio" if label else None)


def _scalebar(ax, ctx: Ctx):
    l, r, b, t = ctx.extent
    width = r - l
    nice = [10, 20, 50, 100, 200, 500, 1000, 2000]
    L = max([n for n in nice if n <= width * 0.3] or [nice[0]])
    x0, y0 = l + 0.04 * width, b + 0.05 * (t - b)
    ax.plot([x0, x0 + L], [y0, y0], color="white", lw=4.5, zorder=7, solid_capstyle="butt")
    ax.plot([x0, x0 + L], [y0, y0], color=INK, lw=2.2, zorder=8, solid_capstyle="butt")
    ax.text(x0 + L / 2, y0 + 0.015 * (t - b), f"{L:g} m", ha="center", va="bottom", fontsize=7.5, color=INK, zorder=9,
            bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85))
    ax.annotate("N", xy=(r - 0.06 * width, t - 0.03 * (t - b)), xytext=(r - 0.06 * width, t - 0.17 * (t - b)), ha="center",
                fontsize=8.5, color=INK, fontweight="bold", zorder=9,
                arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.4), bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.8))


def _hillshade_bg(ax, ctx: Ctx):
    if ctx.hill is not None:
        ax.imshow(ctx.hill, cmap="gray", vmin=0, vmax=255, extent=ctx.extent, origin="upper", interpolation="bilinear", zorder=1)
    else:
        ax.imshow(ctx.dem, cmap="Greys", extent=ctx.extent, origin="upper", interpolation="nearest", zorder=1)
    ax.set_xlim(ctx.extent[0], ctx.extent[1])
    ax.set_ylim(ctx.extent[2], ctx.extent[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color(GRID)


def fig_overview(ctx: Ctx) -> bytes:
    fig = Figure(figsize=(5.4, 4.3))
    ax = fig.subplots()
    _hillshade_bg(ax, ctx)
    _outline(ax, ctx)
    _scalebar(ax, ctx)
    ax.legend(loc="upper left", fontsize=7.5, frameon=True, framealpha=0.9)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    return _png(fig)


def fig_slope_map(ctx: Ctx) -> bytes | None:
    if ctx.slope is None:
        return None
    fig = Figure(figsize=(5.4, 4.6))
    ax = fig.subplots()
    _hillshade_bg(ax, ctx)
    cls = np.digitize(np.nan_to_num(ctx.slope, nan=0.0), [5.0, 15.0, 30.0]).astype(float)
    cls[~np.isfinite(ctx.slope)] = np.nan
    cmap = ListedColormap([_rgb01(SLOPE_COLORS[k]) for k in SLOPE_CLASS_KEYS])
    ax.imshow(np.ma.masked_invalid(cls), cmap=cmap, vmin=-0.5, vmax=3.5, extent=ctx.extent, origin="upper",
              interpolation="nearest", alpha=0.72, zorder=2)
    _outline(ax, ctx)
    _scalebar(ax, ctx)
    handles = [Patch(fc=_rgb01(SLOPE_COLORS[k]), ec=MUTED, label=lab) for k, lab in zip(SLOPE_CLASS_KEYS, SLOPE_CLASS_LABELS)]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=4, fontsize=8, frameon=False,
              title="Pendiente (píxeles de 30 m)", title_fontsize=8)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.14)
    return _png(fig)


def fig_slope_bars(classes: dict) -> bytes:
    fig = Figure(figsize=(3.6, 2.5))
    ax = fig.subplots()
    vals = [float(classes[k]) for k in SLOPE_CLASS_KEYS]
    y = np.arange(4)[::-1]
    ax.barh(y, vals, color=[_rgb01(SLOPE_COLORS[k]) for k in SLOPE_CLASS_KEYS], edgecolor=MUTED, height=0.62)
    for yi, v in zip(y, vals):
        ax.text(min(v + 2, 92), yi, f"{v:.0f} %".replace(".", ","), va="center", fontsize=8, color=INK)
    ax.set_yticks(y)
    ax.set_yticklabels(SLOPE_CLASS_LABELS, fontsize=8, color=INK)
    ax.set_xlim(0, 108)
    ax.set_xlabel("% del predio (píxeles de 30 m)", fontsize=8, color=MUTED)
    ax.xaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    _style(ax)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.97, bottom=0.2)
    return _png(fig)


def fig_rose(rose: list, dominant: str | None) -> bytes:
    fig = Figure(figsize=(3.4, 3.4))
    ax = fig.add_subplot(projection="polar")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    theta = np.deg2rad(np.arange(8) * 45.0)
    ax.bar(theta, rose, width=np.deg2rad(40), color=[_rgb01(ASPECT_COLORS[l]) for l in ASPECT_LABELS], edgecolor=MUTED, lw=0.7)
    ax.set_xticks(theta)
    ax.set_xticklabels(ASPECT_LABELS, fontsize=9, color=INK)
    top = max(max(rose) * 1.15, 10)
    ax.set_ylim(0, top)
    ax.set_yticks(np.linspace(0, top, 4)[1:])
    ax.set_yticklabels([f"{v:.0f} %" for v in np.linspace(0, top, 4)[1:]], fontsize=6.5, color=MUTED)
    ax.grid(color=GRID, lw=0.6)
    ax.set_title("Dirección hacia la que baja el terreno", fontsize=8, color=MUTED, pad=10)
    fig.subplots_adjust(left=0.1, right=0.9, top=0.84, bottom=0.1)
    return _png(fig)


def _profile(ctx: Ctx, axis: str, step=10.0):
    l, r, b, t = ctx.extent
    if axis == "ns":
        s = np.arange(b + step / 2, t, step)
        xs, ys = np.zeros_like(s), s
        line = LineString([(0, b), (0, t)])
    else:
        s = np.arange(l + step / 2, r, step)
        xs, ys = s, np.zeros_like(s)
        line = LineString([(l, 0), (r, 0)])
    valid = np.isfinite(ctx.dem)
    z = bilinear_at(np.nan_to_num(ctx.dem, nan=0.0).astype("float64"), valid, ctx.transform, xs + ctx.cx, ys + ctx.cy)
    inter = line.intersection(ctx.poly_local)
    spans = []
    geoms = list(inter.geoms) if hasattr(inter, "geoms") else [inter]
    for g in geoms:
        if g.is_empty or g.geom_type != "LineString":
            continue
        c = np.asarray(g.coords)
        spans.append((c[:, 1].min(), c[:, 1].max()) if axis == "ns" else (c[:, 0].min(), c[:, 0].max()))
    return s, z, spans


def fig_profiles(ctx: Ctx, tolerance_m: float = 4.0) -> bytes | None:
    fig = Figure(figsize=(6.9, 2.9))
    axes = fig.subplots(1, 2)
    any_data = False
    for ax, axis, title, xl in ((axes[0], "ns", "Perfil sur → norte", "distancia al centroide (m, + hacia el norte)"),
                                (axes[1], "ew", "Perfil oeste → este", "distancia al centroide (m, + hacia el este)")):
        s, z, spans = _profile(ctx, axis)
        ok = np.isfinite(z)
        if not ok.any():
            ax.text(0.5, 0.5, "sin datos a lo largo del corte", ha="center", va="center", transform=ax.transAxes, color=MUTED)
            continue
        any_data = True
        for a, bb in spans:
            ax.axvspan(a, bb, color=ACCENT, alpha=0.10, lw=0)
        ax.fill_between(s, z - tolerance_m, z + tolerance_m, where=ok, color="#e6a23c", alpha=0.35, lw=0,
                        label=f"incertidumbre vertical ±{tolerance_m:g} m (LE90)")
        ax.plot(s, z, color=INK, lw=1.6, label="elevación GLO-30 (30 m)")
        lo, hi = np.nanmin(z) - tolerance_m, np.nanmax(z) + tolerance_m
        pad = 0.08 * (hi - lo)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_title(title, fontsize=9, color=INK, loc="left")
        ax.set_xlabel(xl, fontsize=7.5, color=MUTED)
        ax.set_ylabel("m s. n. m. (EGM2008)", fontsize=7.5, color=MUTED)
        ax.grid(color=GRID, lw=0.6)
        _style(ax)
    if not any_data:
        return None
    h, lab = axes[0].get_legend_handles_labels()
    fig.legend(h + [Patch(fc=ACCENT, alpha=0.18, label="tramo dentro del predio")], lab + ["tramo dentro del predio"],
               loc="lower center", ncol=3, fontsize=7.5, frameon=False)
    fig.subplots_adjust(left=0.085, right=0.985, top=0.92, bottom=0.30, wspace=0.28)
    return _png(fig)


def fig_contours(ctx: Ctx, interval_m: float) -> tuple[bytes | None, int]:
    if ctx.lines is None:
        return None, 0
    fig = Figure(figsize=(5.4, 4.5))
    ax = fig.subplots()
    _hillshade_bg(ax, ctx)
    levels = sorted({e for e, _ in ctx.lines})
    for e, c in ctx.lines:
        major = abs(e / (5 * interval_m) - round(e / (5 * interval_m))) < 1e-9
        ax.plot(c[:, 0], c[:, 1], color="#7a4a1e", lw=1.35 if major else 0.8, zorder=3)
    labelled = 0
    for e, c in ctx.lines:
        if abs(e / (5 * interval_m) - round(e / (5 * interval_m))) < 1e-9 and len(c) >= 2 and labelled < 14:
            mid = c[len(c) // 2]
            ax.text(mid[0], mid[1], f"{e:g}", fontsize=6.5, color="#4a2d12", ha="center", va="center", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.8))
            labelled += 1
    _outline(ax, ctx)
    _scalebar(ax, ctx)
    ax.text(0.5, 0.012, "Curvas indicativas, no para diseño", transform=ax.transAxes, ha="center", va="bottom", fontsize=7.5,
            color="white", bbox=dict(boxstyle="round,pad=0.25", fc="#7a4a1e", ec="none", alpha=0.9), zorder=10)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    return _png(fig), len(levels)


def fig_diff(ctx: Ctx) -> bytes | None:
    if ctx.diff is None:
        return None
    arr, ext = ctx.diff
    fig = Figure(figsize=(5.4, 4.4))
    ax = fig.subplots()
    lim = max(1.0, float(np.nanmax(np.abs(arr)))) if np.isfinite(arr).any() else 1.0
    im = ax.imshow(np.ma.masked_invalid(arr), cmap="RdBu_r", vmin=-lim, vmax=lim, extent=ext, origin="upper",
                   interpolation="nearest")
    ax.set_xlim(ctx.extent[0], ctx.extent[1])
    ax.set_ylim(ctx.extent[2], ctx.extent[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    _outline(ax, ctx, label=False)
    _scalebar(ax, ctx)
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.02)
    cb.set_label("AW3D30 − GLO-30 (m), datums alineados", fontsize=7.5, color=MUTED)
    cb.ax.tick_params(labelsize=7, colors=MUTED)
    fig.subplots_adjust(left=0.01, right=0.93, top=0.99, bottom=0.01)
    return _png(fig)


def fig_sun(solar: dict) -> bytes:
    fig = Figure(figsize=(6.9, 3.1))
    ax = fig.subplots()
    step = solar["horizon_step_deg"]
    hz = list(solar["horizon_deg"])
    az = np.arange(0, 360, step)
    x = ((az + 180) % 360) - 180  # 0 = norte al centro
    order = np.argsort(x)
    xs, ys = x[order], np.maximum(np.array(hz)[order], 0.0)
    ax.fill_between(np.r_[xs, 180.0], np.r_[ys, ys[0]], 0, step="post", color="#9aa5b1", alpha=0.85, lw=0,
                    label="horizonte de relieve (ventana de trabajo)")
    palette = ["#1b9e77", "#d95f02", "#7570b3", "#e7298a"]
    for (label, iso), col in zip(SUN_DATES, palette):
        hrs = np.arange(0, 24, 0.05)
        pts = [sun_position(solar["latitude"], iso, float(h)) for h in hrs]
        el = np.array([p[0] for p in pts])
        a = np.array([((p[1] + 180) % 360) - 180 for p in pts])
        m = el > 0
        a, el = a[m], el[m]
        brk = np.where(np.abs(np.diff(a)) > 180)[0] + 1
        for i, (seg_a, seg_e) in enumerate(zip(np.split(a, brk), np.split(el, brk))):
            ax.plot(seg_a, seg_e, color=col, lw=1.8, label=label if i == 0 else None)
    ax.set_xlim(-180, 180)
    ax.set_ylim(0, 90)
    ax.set_xticks([-180, -90, 0, 90, 180])
    ax.set_xticklabels(["S", "O", "N", "E", "S"], fontsize=8, color=INK)
    ax.set_xlabel("azimut", fontsize=8, color=MUTED)
    ax.set_ylabel("elevación del sol (°)", fontsize=8, color=MUTED)
    ax.grid(color=GRID, lw=0.6)
    _style(ax)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=7.5, frameon=False)
    fig.subplots_adjust(left=0.07, right=0.985, top=0.97, bottom=0.34)
    return _png(fig)


def fig_3d(ctx: Ctx, exaggeration: float) -> bytes | None:
    z = ctx.dem
    n_rows, n_cols = z.shape
    if n_rows < 3 or n_cols < 3 or not np.isfinite(z).any():
        return None
    l, r, b, t = ctx.extent
    xs = np.linspace(l + (r - l) / n_cols / 2, r - (r - l) / n_cols / 2, n_cols)
    ys = np.linspace(t - (t - b) / n_rows / 2, b + (t - b) / n_rows / 2, n_rows)
    X, Y = np.meshgrid(xs, ys)
    zmin = float(np.nanmin(z))
    zf = np.where(np.isfinite(z), z, zmin)
    stride = max(1, int(math.ceil(max(n_rows, n_cols) / 90)))
    ls = LightSource(azdeg=315, altdeg=40)
    rgb = ls.shade(zf, cmap=matplotlib.colormaps["YlGn_r"], vert_exag=exaggeration, dx=30.0, dy=30.0, blend_mode="soft")
    fig = Figure(figsize=(5.6, 4.0))
    ax = fig.add_subplot(projection="3d")
    ax.plot_surface(X, Y, zf, rstride=stride, cstride=stride, facecolors=rgb, linewidth=0, antialiased=False, shade=False)
    dx, dy = r - l, t - b
    dz = max(float(np.nanmax(z) - zmin) * exaggeration, 1e-3)
    ax.set_box_aspect((dx, dy, max(dz, 0.02 * max(dx, dy))))
    ax.set_zlim(zmin, zmin + max(float(np.nanmax(z) - zmin), 1e-3))
    x, y = ctx.poly_local.exterior.xy
    zz = bilinear_at(np.where(np.isfinite(ctx.dem), ctx.dem, 0.0).astype("float64"), np.isfinite(ctx.dem), ctx.transform,
                     np.asarray(x) + ctx.cx, np.asarray(y) + ctx.cy)
    ax.plot(x, y, np.where(np.isfinite(zz), zz, zmin) + 0.5, color="#c1121f", lw=2.0, zorder=10)
    ax.view_init(elev=38, azim=-62)
    # Vista cualitativa (AUD-1 M7): sin cifras en los ejes. Las cotas de la ventana de trabajo se calculan aqui y no estan en
    # el manifiesto; las del predio estan en la seccion 3. Solo se rotulan las direcciones.
    ax.set_xlabel("este", fontsize=7, color=MUTED, labelpad=-2)
    ax.set_ylabel("norte", fontsize=7, color=MUTED, labelpad=-2)
    ax.set_zlabel("altura", fontsize=7, color=MUTED, labelpad=-2)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_zticks([])
    fig.text(0.5, 0.965, f"Exageración vertical ×{str(exaggeration).replace('.', ',')}", ha="center", va="top", fontsize=9.5,
             color="white", fontweight="bold", bbox=dict(boxstyle="round,pad=0.3", fc=INK, ec="none"))
    fig.subplots_adjust(left=0.0, right=1.0, top=0.93, bottom=0.0)
    return _png(fig)
