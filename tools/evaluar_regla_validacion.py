"""Evalua la regla A/B/C (sello «confianza del dato») con los 8 pilotos + los sitios de validacion.

REGLA PREFIJADA (escrita en tools/elegir_sitios_validacion.py antes de correr los sitios): la regla discrimina si
  1. el RMSE medio contra ICESat-2/GEDI es no decreciente A <= B <= C,
  2. hay al menos 3 sitios en cada nivel, y
  3. la correlacion de rangos (Spearman) entre el nivel (A=0, B=1, C=2) y el RMSE de cada sitio es >= 0,5.
Si algo falla se informa tal cual; no se ajustan cortes sobre estos mismos sitios.

    python tools/evaluar_regla_validacion.py     # escribe AUDITORIA/validacion_regla_sitios.{json,md}
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from terreno.ranking import spearman  # noqa: E402

AUDIT = ROOT.parent / "AUDITORIA"
MIN_POR_NIVEL = 3
RHO_MIN = 0.5
ORD = {"A": 0, "B": 1, "C": 2}


def cargar() -> list[dict]:
    rows = []
    for s in json.loads((ROOT / "out" / "metrics.json").read_text(encoding="utf-8"))["sites"]:
        rows.append(("piloto", s["site"], s["run1"]))
    for f in sorted((ROOT / "out_validacion").glob("*/run_metrics.json")):
        rows.append(("validacion", f.parent.name, json.loads(f.read_text(encoding="utf-8"))))
    out = []
    for grupo, key, m in rows:
        t = m.get("truth") or {}
        if m.get("status") != "ok" or t.get("status") != "ok" or not m.get("seals"):
            continue
        out.append({"grupo": grupo, "site": key, "level": m["level"], "lote": m["seals"]["medibilidad_lote"],
                    "dato": m["seals"]["confianza_dato"], "rmse": float(t["rmse_m"]), "n": t.get("n"),
                    "slope_pct": m.get("slope_mean_pct"), "canopy_pct": (m.get("factor_values") or {}).get("canopy_pct"),
                    "aw3d30": (m.get("aw3d30") or {}).get("status")})
    return out


def evaluar(rows: list[dict], sello: str = "dato") -> dict:
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r[sello], []).append(r["rmse"])
    medias = {k: sum(v) / len(v) for k, v in by.items()}
    medianas = {k: statistics.median(v) for k, v in by.items()}
    niveles = [k for k in "ABC" if k in medias]
    ordenado = len(niveles) == 3 and all(medias[a] <= medias[b] for a, b in zip(niveles, niveles[1:]))
    suficiente = all(len(by.get(k, [])) >= MIN_POR_NIVEL for k in "ABC")
    rho = spearman([ORD[r[sello]] for r in rows], [r["rmse"] for r in rows]) if len(rows) >= 3 else None
    ok = ordenado and suficiente and rho is not None and rho >= RHO_MIN
    return {"sello": sello, "n_sitios": len(rows), "n_por_nivel": {k: len(by.get(k, [])) for k in "ABC"},
            "rmse_medio_m": {k: round(v, 2) for k, v in medias.items()}, "rmse_mediana_m": {k: round(v, 2) for k, v in medianas.items()},
            "ordenado": ordenado, "suficiente": suficiente, "spearman": None if rho is None else round(rho, 2),
            "discrimina": bool(ok)}


def main() -> int:
    rows = cargar()
    res = {"dato": evaluar(rows, "dato"), "lote": evaluar(rows, "lote"), "nivel_final": evaluar(rows, "level"),
           "regla": {"min_por_nivel": MIN_POR_NIVEL, "rho_min": RHO_MIN}, "sitios": rows}
    AUDIT.mkdir(parents=True, exist_ok=True)
    (AUDIT / "validacion_regla_sitios.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    L = [f"Validación de la regla A/B/C con {len(rows)} sitios RM (8 pilotos + sitios de validación).", "",
         f"Regla prefijada: error medio no decreciente A ≤ B ≤ C, al menos {MIN_POR_NIVEL} sitios por nivel y correlación de rangos ≥ {RHO_MIN}.", "",
         "| Sello | Sitios (A / B / C) | Error medio A / B / C (m) | Mediana A / B / C (m) | Correlación | ¿Discrimina? |", "|---|---|---|---|---|---|"]
    for k, nombre in (("dato", "Confianza del dato"), ("lote", "Medibilidad del lote"), ("nivel_final", "Nivel final")):
        e = res[k]
        f = lambda d: " / ".join(str(d.get(x, "–")) for x in "ABC")  # noqa: E731
        L.append(f"| {nombre} | {f(e['n_por_nivel'])} | {f(e['rmse_medio_m'])} | {f(e['rmse_mediana_m'])} | {e['spearman']} | {'sí' if e['discrimina'] else 'no'} |")
    L += ["", "| Sitio | Sello lote | Sello dato | Nivel | Pendiente (%) | Dosel (%) | Error contra huellas (m) | Huellas |", "|---|---|---|---|---|---|---|---|"]
    fm = lambda v: "–" if v is None else f"{v:.1f}"  # noqa: E731
    for r in rows:
        L.append(f"| {r['site']} | {r['lote']} | {r['dato']} | {r['level']} | {fm(r['slope_pct'])} | {fm(r['canopy_pct'])} | {r['rmse']:.2f} | {r['n']} |")
    (AUDIT / "validacion_regla_sitios.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:9]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
