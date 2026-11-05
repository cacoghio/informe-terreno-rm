"""Prueba del factor de dosel/construido con datos de huella (no de predio): ¿el error de GLO-30 contra ICESat-2/GEDI sube
donde ESA WorldCover dice arbolado o construido?

Usa lo que ya hay en out/<sitio>/ (footprints.json + raster de contexto del manifiesto) y lee WorldCover una vez por sitio.
No baja granulos nuevos. Cada huella aporta un residuo (GLO-30 - verdad), su clase WorldCover y la pendiente local de GLO-30,
asi que son miles de puntos en vez de 8 (un punto por sitio). Control de pendiente: se estratifica en <15 % y >=15 %.

    python tools/analisis_dosel_vs_error.py            # escribe AUDITORIA/dosel_vs_error_huellas.{json,md}
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from terreno.reproject import Raster  # noqa: E402
from terreno.truth import Footprint, sample_residuals  # noqa: E402
from terreno.worldcover import BASE_URL, CLASS_BUILT, CLASS_TREE, tile_name, tile_url  # noqa: E402

OUT = ROOT / "out"
AUDIT = ROOT.parent / "AUDITORIA"
SLOPE_CUT = 15.0
GROUPS = {"arbolado": (CLASS_TREE,), "construido": (CLASS_BUILT,), "otro": None}


def classes_at(lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Clase WorldCover de cada punto (una ventana por tesela; 0 si queda fuera)."""
    out = np.zeros(lons.size, dtype="uint8")
    tiles = {(math.floor(la / 3) * 3, math.floor(lo / 3) * 3) for la, lo in zip(lats, lons)}
    for la0, lo0 in tiles:
        sel = (np.floor(lats / 3) * 3 == la0) & (np.floor(lons / 3) * 3 == lo0)
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
            with rasterio.open(tile_url(BASE_URL, tile_name(la0, lo0))) as src:
                w = src.window(lons[sel].min() - 1e-3, lats[sel].min() - 1e-3, lons[sel].max() + 1e-3, lats[sel].max() + 1e-3)
                w = w.round_offsets().round_lengths()
                arr = src.read(1, window=w)
                tf = src.window_transform(w)
        cols = np.floor((lons[sel] - tf.c) / tf.a).astype(int)
        rows = np.floor((lats[sel] - tf.f) / tf.e).astype(int)
        ok = (cols >= 0) & (cols < arr.shape[1]) & (rows >= 0) & (rows < arr.shape[0])
        vals = np.zeros(sel.sum(), dtype="uint8")
        vals[ok] = arr[rows[ok], cols[ok]]
        out[sel] = vals
    return out


def stats(d: np.ndarray) -> dict:
    if d.size < 10:
        return {"n": int(d.size), "bias_m": None, "rmse_m": None}
    return {"n": int(d.size), "bias_m": round(float(d.mean()), 2), "rmse_m": round(float(math.sqrt((d ** 2).mean())), 2)}


def site_rows(site_dir: Path) -> dict | None:
    fp_path, man_path = site_dir / "footprints.json", site_dir / "manifest.json"
    if not (fp_path.exists() and man_path.exists()):
        return None
    man = json.loads(man_path.read_text(encoding="utf-8"))
    ctx = man["volatile"]["paths"].get("context_raster_utm")
    if not ctx:
        return None
    fps = [Footprint(**f) for f in json.loads(fp_path.read_text(encoding="utf-8"))]
    ras = Raster(path=Path(ctx), epsg=int(man["grid"]["epsg"]), pixel_m=float(man["grid"]["pixel_m"]), nodata=float("nan"))
    res = sample_residuals(ras, fps)
    if not res.used:
        return None
    lons = np.array([f.lon for f in res.used])
    lats = np.array([f.lat for f in res.used])
    return {"d": res.diffs, "slope": res.slopes, "cls": classes_at(lons, lats)}


def group_mask(cls: np.ndarray, name: str) -> np.ndarray:
    codes = GROUPS[name]
    return ~np.isin(cls, (CLASS_TREE, CLASS_BUILT)) & (cls > 0) if codes is None else np.isin(cls, codes)


def main() -> int:
    sites = {}
    for d in sorted(OUT.glob("rm0*")):
        r = site_rows(d)
        if r is not None:
            sites[d.name] = r
    pooled_d = np.concatenate([s["d"] for s in sites.values()])
    pooled_s = np.concatenate([s["slope"] for s in sites.values()])
    pooled_c = np.concatenate([s["cls"] for s in sites.values()])
    result = {"n_sitios": len(sites), "n_huellas": int(pooled_d.size), "por_sitio": {}, "agrupado": {}}
    for k, s in sites.items():
        result["por_sitio"][k] = {g: stats(s["d"][group_mask(s["cls"], g)]) for g in GROUPS}
        result["por_sitio"][k]["todas"] = stats(s["d"])
    lo = np.isfinite(pooled_s) & (pooled_s < SLOPE_CUT)
    hi = np.isfinite(pooled_s) & (pooled_s >= SLOPE_CUT)
    for g in GROUPS:
        m = group_mask(pooled_c, g)
        result["agrupado"][g] = {"todas": stats(pooled_d[m]), f"pendiente<{SLOPE_CUT:g}": stats(pooled_d[m & lo]),
                                 f"pendiente>={SLOPE_CUT:g}": stats(pooled_d[m & hi])}
    # Efecto dentro de cada sitio (quita las diferencias entre sitios): RMSE(arbolado o construido) - RMSE(otro)
    dif = {}
    for k, s in sites.items():
        a = s["d"][group_mask(s["cls"], "arbolado") | group_mask(s["cls"], "construido")]
        b = s["d"][group_mask(s["cls"], "otro")]
        if a.size >= 10 and b.size >= 10:
            dif[k] = round(float(math.sqrt((a ** 2).mean()) - math.sqrt((b ** 2).mean())), 2)
    result["rmse_dosel_menos_otro_por_sitio"] = dif
    AUDIT.mkdir(parents=True, exist_ok=True)
    (AUDIT / "dosel_vs_error_huellas.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ["Residuo GLO-30 menos verdad (ICESat-2/GEDI), por clase de ESA WorldCover bajo la huella.", "",
             f"{result['n_huellas']} huellas en {result['n_sitios']} sitios RM.", "",
             "| Clase | n | sesgo (m) | RMSE (m) | RMSE pend. <15 % | RMSE pend. >=15 % |", "|---|---|---|---|---|---|"]
    for g, v in result["agrupado"].items():
        t, a, b = v["todas"], v[f"pendiente<{SLOPE_CUT:g}"], v[f"pendiente>={SLOPE_CUT:g}"]
        lines.append(f"| {g} | {t['n']} | {t['bias_m']} | {t['rmse_m']} | {a['rmse_m']} (n={a['n']}) | {b['rmse_m']} (n={b['n']}) |")
    lines += ["", "RMSE de (arbolado o construido) menos RMSE de otro, dentro de cada sitio (m): " + json.dumps(dif, ensure_ascii=False)]
    (AUDIT / "dosel_vs_error_huellas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
