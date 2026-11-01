"""Headroom computation for auto-preamp.

The worst case for an EQ chain is all filters peaking coherently at once;
their dB gains add. Auto-preamp pulls the input down by the total positive
gain so the EQ itself can never push past unity at any frequency.
"""
from __future__ import annotations


def worst_case_boost_db(filters: list[dict]) -> float:
    """Sum of positive gains of enabled filters (dB)."""
    total = 0.0
    for f in filters:
        if not f.get("enabled", True):
            continue
        g = float(f.get("gain_db", 0.0) or 0.0)
        if g > 0:
            total += g
    return total


def headroom_db(filters: list[dict], margin_db: float = 0.0) -> float:
    """Auto-preamp value: -sum(positive gains) - margin (<= 0)."""
    boost = worst_case_boost_db(filters)
    return -max(0.0, boost + margin_db)


def effective_gain_at_dc_and_bass(filters: list[dict]) -> dict:
    """Quick summary used by the advisor for clipping-risk messages."""
    return {
        "worst_case_boost_db": worst_case_boost_db(filters),
        "n_boosts": sum(1 for f in filters
                        if f.get("enabled", True) and
                        float(f.get("gain_db", 0) or 0) > 0),
        "n_cuts": sum(1 for f in filters
                      if f.get("enabled", True) and
                      float(f.get("gain_db", 0) or 0) < 0),
    }
