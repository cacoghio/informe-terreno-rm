"""Ganchos listos para ``run_pipeline(..., hooks=default_hooks())`` (Tasks 9-10). Los de visor y video los aporta quien los implemente."""
from __future__ import annotations

from typing import Callable

from terreno.glb import build_glb_hook
from terreno.report import render_pdf_hook


def default_hooks() -> dict[str, Callable]:
    """build_glb -> terreno.glb, predio.geojson, layers/*.png; render_pdf -> report.pdf. Cada uno recibe (manifest, out_dir)."""
    return {"build_glb": build_glb_hook, "render_pdf": render_pdf_hook}
