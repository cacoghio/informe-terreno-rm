"""Regenera data/sites.json (one-off, reproducible).

Desde 2026-10-30 el archivo se genera SOLO con datos publicos (DPA 2023, GLO-30, huellas de OpenTopography) con
``tools/elegir_pilotos_rm.py``; este script ya no lee ninguna capa propia o de terceros. Lo conserva por compatibilidad.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.elegir_pilotos_rm import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
