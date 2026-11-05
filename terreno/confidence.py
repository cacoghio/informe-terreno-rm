"""score_confidence: regla de confianza A/B/C (spec sec. 4). TODO es hipotesis a calibrar.

Los umbrales viven en ``data/confidence_rules.json`` (tabla literal de la spec); aqui no hay ningun numero de corte.
Manda el peor factor; un factor sin dato cuenta como B como maximo (nunca mejora el nivel ni lo esconde si ya hay un C).
"""
from __future__ import annotations

import json
import math
import operator
from dataclasses import dataclass, field
from pathlib import Path

from terreno.errors import RejectRequest

RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "confidence_rules.json"
SIN_DATO = "sin_dato"  # no se mide por diseno del POC (p. ej. dosel) o no se pidio (p. ej. AW3D30): tope B
NO_DISPONIBLE = "no_disponible"  # SI se mide, pero la lectura fallo (red, capa ausente): tope B Y revision humana (AUD-1 I7)
_MISSING = (SIN_DATO, NO_DISPONIBLE)
_OPS = {">=": operator.ge, ">": operator.gt, "<": operator.lt, "<=": operator.le}
_ORDER = {"A": 0, "B": 1, "C": 2}


def load_rules(path: Path | None = None) -> dict:
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


@dataclass
class Confidence:
    overall: str  # "A" | "B" | "C"
    by_factor: dict[str, str]  # "A" | "B" | "C" | "sin_dato"
    by_section: dict[str, str]
    human_review_required: bool  # True si C
    omit_point_slope: bool  # True si C: el informe omite cifras de pendiente puntuales
    limiting_factors: list[str] = field(default_factory=list)  # factores que fijan el nivel (o sin_dato si limitan en B)
    values: dict = field(default_factory=dict)
    rules_version: str = ""
    hypothesis: bool = True
    unavailable_factors: list[str] = field(default_factory=list)  # factores medibles cuya lectura fallo
    review_reason: str = ""
    by_seal: dict[str, str] = field(default_factory=dict)  # "medibilidad_lote" / "confianza_dato" (v0.3.0): la causa del nivel


def _level(spec: dict, value: float) -> str:
    if _OPS[spec["A"]["op"]](value, spec["A"]["value"]):
        return "A"
    if _OPS[spec["B"]["op"]](value, spec["B"]["value"]):
        return "B"
    return spec.get("worst_level", "C")  # worst_level="B": el factor baja el nivel a B pero no lo veta con C (sin evidencia aun)


def _combine(levels: list[str], cap_b: str) -> str:
    """Peor nivel; si algun factor no tiene dato, tope B (a menos que ya haya un C)."""
    known = [l for l in levels if l not in _MISSING]
    worst = max(known, key=_ORDER.__getitem__) if known else "A"
    if any(l in _MISSING for l in levels) or not known:
        worst = max(worst, cap_b, key=_ORDER.__getitem__)
    return worst


def score_confidence(factors: dict[str, float | None], *, rules: dict | None = None,
                     unavailable: list[str] | tuple[str, ...] = ()) -> Confidence:
    """unavailable: factores que SI se pueden medir pero cuya lectura fallo (si traen valor, se ignora).

    Un factor asi queda 'no_disponible' (distinto de 'sin_dato', que es lo que el POC no mide por diseno): el nivel no
    pasa de B, el factor se nombra como limitante y la revision humana es obligatoria, para que un B no parezca completo.
    """
    rules = rules or load_rules()
    spec = rules["factors"]
    unknown = (set(factors) | set(unavailable)) - set(spec)
    if unknown:
        raise RejectRequest(f"factor de confianza desconocido: {sorted(unknown)}")
    by_factor: dict[str, str] = {}
    values: dict = {}
    lost: list[str] = []
    for name, fspec in spec.items():
        v = factors.get(name)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            by_factor[name] = NO_DISPONIBLE if name in unavailable else SIN_DATO
            if name in unavailable:
                lost.append(name)
            values[name] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise RejectRequest(f"valor invalido para el factor {name}: {v!r}")
        if v < 0:
            raise RejectRequest(f"valor negativo para el factor {name}: {v!r}")
        by_factor[name] = _level(fspec, float(v))
        values[name] = float(v)
    cap = rules.get("missing_factor_cap", "B")
    overall = _combine(list(by_factor.values()), cap)
    by_section = {sec: _combine([by_factor[f] for f in fs], cap) for sec, fs in rules["sections"].items()}
    by_seal = {seal: _combine([by_factor[f] for f in sspec["factors"]], cap) for seal, sspec in rules.get("seals", {}).items()}
    limiting = [n for n, l in by_factor.items() if l == overall] if overall in ("B", "C") else []
    if overall == "B" and not limiting:
        limiting = [n for n in lost] + [n for n, l in by_factor.items() if l == SIN_DATO]
    elif lost:
        limiting = limiting + [n for n in lost if n not in limiting]
    reason = ""
    if lost:
        reason = ("no se pudo leer un factor medible (" + ", ".join(lost) + "): el nivel no puede darse por completo")
    return Confidence(
        overall=overall, by_factor=by_factor, by_section=by_section, by_seal=by_seal,
        human_review_required=overall == "C" or bool(lost), omit_point_slope=overall == "C",
        unavailable_factors=lost, review_reason=reason,
        limiting_factors=limiting, values=values, rules_version=str(rules.get("version", "")),
        hypothesis=bool(rules.get("hypothesis", True)),
    )


def factors_from_results(terrain_stats: dict, comparison=None, aux=None, *, canopy_pct: float | None = None,
                         canopy=None) -> dict:
    """Arma el diccionario de factores con lo que realmente se midio; lo que falta queda en None (sin dato).

    * ``terrain_stats`` puede no traer las claves de pendiente (predio no concluyente): factor sin dato.
    * Un desacuerdo medido sin datums alineados NO se usa (el RMS incluiria el sesgo de datum): sin dato.
    * El desacuerdo es el RMS centrado (sin la diferencia media): en los 8 pilotos RM esa media es el sesgo propio de
      AW3D30 frente a ICESat-2/GEDI, no incertidumbre de GLO-30 (calibracion 2026-10-31).
    * ``canopy_pct`` explicito manda; si no, se usa ``canopy`` (CanopyStats de ESA WorldCover). Este modulo nunca lo inventa.
    """
    if canopy_pct is None and canopy is not None:
        canopy_pct = getattr(canopy, "canopy_pct", None)
    disagreement = None
    if comparison is not None and getattr(comparison, "datum_aligned", False):
        rms, mean = comparison.rms_diff_m, comparison.mean_diff_m
        disagreement = math.sqrt(max(rms * rms - mean * mean, 0.0)) if mean is not None else rms
    return {
        "pixels": terrain_stats.get("n_pixels"),
        "elev_range_m": terrain_stats.get("elev_range"),
        "slope_mean_pct": terrain_stats.get("slope_mean_pct"),
        "disagreement_rms_m": disagreement,
        "canopy_pct": canopy_pct,
        "void_water_pct": getattr(aux, "void_water_fill_pct", None) if aux is not None else None,
        "hem_p90_m": getattr(aux, "hem_p90_m", None) if aux is not None else None,
    }
