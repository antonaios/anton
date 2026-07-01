"""Materiality assessment — decides whether an announcement warrants an alert.

Defaults (per the #44 brief / plan §10 operator decision #3, pending #46c
precision authoring):

  * revenue variance vs consensus  ≥ ±5%
  * EBITDA variance vs consensus    ≥ ±10%
  * any guidance change (raised / lowered) is material
  * divisional underperformance     ≥ -15% YoY on any division

These are the bridge-phase defaults. If the operator authors
``_claude/earnings-materiality.md`` (#46c) with a ``materiality`` YAML block, we
read the overrides; absent that file we run on the defaults and never block on
operator content.

The assessment mutates the :class:`Comparison` in place — setting ``material``
and appending human-readable ``material_reasons`` — and also returns the bool so
callers can branch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from routines.earnings.report import Comparison, ExtractedEarnings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterialityThresholds:
    """Variance bands above which a result is "material". Fractions, not %."""

    revenue_vs_consensus: float = 0.05      # ±5%
    ebitda_vs_consensus: float = 0.10       # ±10%
    guidance_change_material: bool = True   # any raised/lowered is material
    divisional_underperformance: float = 0.15   # -15% YoY on a division

    @classmethod
    def defaults(cls) -> "MaterialityThresholds":
        return cls()


# Vault-relative location of the optional operator-authored override (#46c).
MATERIALITY_CONFIG_REL = Path("_claude") / "earnings-materiality.md"


def load_thresholds(vault_root: Optional[Path]) -> MaterialityThresholds:
    """Load thresholds from ``_claude/earnings-materiality.md`` (#46c) if present,
    else the defaults. Never raises — a missing/garbled config falls back to
    defaults so the routine is never blocked on operator content."""
    base = MaterialityThresholds.defaults()
    if vault_root is None:
        return base
    path = vault_root / MATERIALITY_CONFIG_REL
    if not path.is_file():
        return base
    try:
        from routines.shared.md_config import extract_section

        text = path.read_text(encoding="utf-8")
        rows = extract_section(text, "materiality")
    except Exception as e:  # noqa: BLE001 — config read must never crash the run
        log.warning("earnings materiality: failed to read %s (%s) — using defaults", path, e)
        return base

    # extract_section returns a list; accept a single-mapping list [{...}].
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return base
    cfg = rows[0]

    def _f(key: str, default: float) -> float:
        v = cfg.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _b(key: str, default: bool) -> bool:
        v = cfg.get(key)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        # A YAML-as-string value ("false"/"no"/"0") must NOT be truthy
        # (bool("false") is True) — parse it explicitly (#44 Codex SEV-3).
        s = str(v).strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
        return default

    return MaterialityThresholds(
        revenue_vs_consensus=_f("revenue_vs_consensus", base.revenue_vs_consensus),
        ebitda_vs_consensus=_f("ebitda_vs_consensus", base.ebitda_vs_consensus),
        guidance_change_material=_b("guidance_change_material", base.guidance_change_material),
        divisional_underperformance=_f("divisional_underperformance", base.divisional_underperformance),
    )


def _fmt_pct(frac: float) -> str:
    return f"{frac * 100:+.1f}%"


def assess(
    comparison: Comparison,
    extracted: ExtractedEarnings,
    thresholds: Optional[MaterialityThresholds] = None,
) -> bool:
    """Set ``comparison.material`` + ``material_reasons`` and return the bool.

    Material if ANY of: revenue beats/misses consensus beyond the revenue band;
    EBITDA beyond its band; guidance raised/lowered; a division YoY below the
    underperformance floor."""
    thresholds = thresholds or MaterialityThresholds.defaults()
    reasons: list[str] = []

    band = {
        "revenue": thresholds.revenue_vs_consensus,
        "ebitda": thresholds.ebitda_vs_consensus,
    }
    for line in comparison.vs_consensus:
        threshold = band.get(line.metric)
        if threshold is None or line.delta_pct is None:
            continue
        if abs(line.delta_pct) >= threshold:
            reasons.append(
                f"{line.metric.upper()} {line.verdict} consensus by "
                f"{_fmt_pct(line.delta_pct)} (threshold ±{threshold * 100:.0f}%)"
            )

    if thresholds.guidance_change_material and extracted.guidance_change in ("raised", "lowered"):
        reasons.append(f"guidance {extracted.guidance_change}")

    for div in extracted.divisions:
        if div.revenue_yoy is not None and div.revenue_yoy <= -thresholds.divisional_underperformance:
            reasons.append(
                f"{div.name} revenue {_fmt_pct(div.revenue_yoy)} YoY "
                f"(threshold {-thresholds.divisional_underperformance * 100:.0f}%)"
            )

    comparison.material = bool(reasons)
    comparison.material_reasons = reasons
    return comparison.material


__all__ = ["MaterialityThresholds", "MATERIALITY_CONFIG_REL", "load_thresholds", "assess"]
