"""Corre los lotes de data/sites_validacion.json en vivo (AW3D30 + ICESat-2/GEDI), una vez cada uno, sin video.

    python run_validacion.py                 # los que falten
    python run_validacion.py --sites v01_llano_pudahuel,v02_llano_lampa
    python run_validacion.py --max-nuevos 8  # tope de sitios por corrida (AW3D30: 50 llamadas/dia, 2 por sitio)

Retoma sola: un sitio con out_validacion/<sitio>/run_metrics.json en estado ok no se repite. Cada sitio corre en un
subproceso propio (como run_pilots) porque la lectura de GEDI llega a ~8 GB de memoria.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from terreno.geometry import load_polygon  # noqa: E402

SITES_PATH = ROOT / "data" / "sites_validacion.json"
OUT = ROOT / "out_validacion"


def _worker(key: str, out_dir: str) -> int:
    import run_pilots as rp
    sites = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    m = rp.run_site(key, load_polygon(sites[key]["polygon"]), Path(out_dir), live=True)
    print(json.dumps({"site": key, "status": m.get("status"), "level": m.get("level"), "seals": m.get("seals"),
                      "aw3d30": (m.get("aw3d30") or {}).get("status"), "truth": (m.get("truth") or {}).get("status"),
                      "n_truth": (m.get("truth") or {}).get("n")}, ensure_ascii=False))
    return 0


def _hecho(d: Path) -> bool:
    f = d / "run_metrics.json"
    if not f.exists():
        return False
    m = json.loads(f.read_text(encoding="utf-8"))
    return m.get("status") == "ok" and (m.get("truth") or {}).get("status") == "ok" and \
        (m.get("aw3d30") or {}).get("status") == "ejecutado"


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", default="")
    ap.add_argument("--max-nuevos", type=int, default=0)
    ap.add_argument("--worker", nargs=2, metavar=("SITE", "OUT_DIR"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.worker:
        return _worker(*a.worker)
    sites = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    keys = [k for k in (a.sites.split(",") if a.sites else list(sites)) if k]
    hechos = 0
    for k in keys:
        d = OUT / k
        if _hecho(d):
            print(f"[{k}] ya hecho, se salta", flush=True)
            continue
        if a.max_nuevos and hechos >= a.max_nuevos:
            print(f"[{k}] pendiente (tope --max-nuevos {a.max_nuevos})", flush=True)
            continue
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", k, str(d)],
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        hechos += 1
        tail = (proc.stdout.strip().splitlines() or [""])[-1] if proc.returncode == 0 else (proc.stderr or proc.stdout)[-400:]
        print(f"[{k}] {time.perf_counter() - t0:.0f}s exit={proc.returncode} {tail}", flush=True)
    print("Listo.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
