"""RBJ biquad design in numpy — the reference implementation.

Mirrors core/src/biquad.c exactly (Audio EQ Cookbook formulas). Used for:
  - curve rendering when the native lib is unavailable,
  - the pure-numpy fallback processing chain,
  - cross-checking native coefficient math in tests.
"""
from __future__ import annotations

import numpy as np

FILTER_TYPES = (
    "peak", "lowshelf", "highshelf", "lowpass", "highpass",
    "bandpass", "notch", "allpass", "lowshelf12", "highshelf12",
)


def design_biquad(filter_type: str, freq: float, gain_db: float, q: float,
                  sample_rate: float, use_q: bool | None = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Return (b, a) with a[0] == 1, matching eqf_biquad_design."""
    if filter_type not in FILTER_TYPES:
        raise ValueError(f"unknown filter type {filter_type!r}")
    sr = float(sample_rate)
    f = min(max(float(freq), 1.0), sr / 2 - 1.0)
    A = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * f / sr
    cw0, sw0 = np.cos(w0), np.sin(w0)

    if use_q is None:
        use_q = filter_type not in ("lowshelf", "highshelf")

    # NOTE: this is the authoritative shape rule and the native parser in
    # core/src/chain.c mirrors it exactly. Callers that have a band dict should
    # go through band_shape() below rather than guessing.
    if not use_q and 0.0 < q <= 1.0:
        alpha = sw0 / 2.0 * np.sqrt((A + 1.0 / A) * (1.0 / q - 1.0) + 2.0)
    else:
        qq = max(float(q), 1e-4)
        alpha = sw0 / (2.0 * qq)

    t = filter_type
    if t == "peak":
        b = np.array([1 + alpha * A, -2 * cw0, 1 - alpha * A])
        a = np.array([1 + alpha / A, -2 * cw0, 1 - alpha / A])
    elif t in ("lowpass",):
        b = np.array([(1 - cw0) / 2, 1 - cw0, (1 - cw0) / 2])
        a = np.array([1 + alpha, -2 * cw0, 1 - alpha])
    elif t in ("highpass",):
        b = np.array([(1 + cw0) / 2, -(1 + cw0), (1 + cw0) / 2])
        a = np.array([1 + alpha, -2 * cw0, 1 - alpha])
    elif t == "bandpass":
        b = np.array([alpha, 0.0, -alpha])
        a = np.array([1 + alpha, -2 * cw0, 1 - alpha])
    elif t == "notch":
        b = np.array([1.0, -2 * cw0, 1.0])
        a = np.array([1 + alpha, -2 * cw0, 1 - alpha])
    elif t == "allpass":
        b = np.array([1 - alpha, -2 * cw0, 1 + alpha])
        a = np.array([1 + alpha, -2 * cw0, 1 - alpha])
    elif t in ("lowshelf", "lowshelf12"):
        sqA = np.sqrt(A)
        t2 = 2 * sqA * alpha
        b = np.array([A * ((A + 1) - (A - 1) * cw0 + t2),
                      2 * A * ((A - 1) - (A + 1) * cw0),
                      A * ((A + 1) - (A - 1) * cw0 - t2)])
        a = np.array([(A + 1) + (A - 1) * cw0 + t2,
                      -2 * ((A - 1) + (A + 1) * cw0),
                      (A + 1) + (A - 1) * cw0 - t2])
    elif t in ("highshelf", "highshelf12"):
        sqA = np.sqrt(A)
        t2 = 2 * sqA * alpha
        b = np.array([A * ((A + 1) + (A - 1) * cw0 + t2),
                      -2 * A * ((A - 1) + (A + 1) * cw0),
                      A * ((A + 1) + (A - 1) * cw0 - t2)])
        a = np.array([(A + 1) - (A - 1) * cw0 + t2,
                      2 * ((A - 1) - (A + 1) * cw0),
                      (A + 1) - (A - 1) * cw0 - t2])
    else:
        raise ValueError(t)

    if abs(a[0]) < 1e-12:
        raise ValueError("degenerate filter design")
    return b / a[0], a / a[0]


def magnitude_response(b: np.ndarray, a: np.ndarray, freqs: np.ndarray,
                       sample_rate: float) -> np.ndarray:
    """Linear magnitude response at the given frequencies."""
    w = 2.0 * np.pi * np.asarray(freqs, dtype=np.float64) / sample_rate
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    num = b[0] + b[1] * z1 + b[2] * z2
    den = a[0] + a[1] * z1 + a[2] * z2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(np.where(np.abs(den) < 1e-30, 0.0, num / den))


def filter_bank_response(filters: list[dict], freqs: np.ndarray,
                         sample_rate: float) -> np.ndarray:
    """Combined response of a list of filter dicts (profile `filters` items)."""
    resp = np.ones_like(np.asarray(freqs, dtype=np.float64))
    for f in filters:
        if not f.get("enabled", True):
            continue
        shape, use_q = band_shape(f)
        b, a = design_biquad(
            f.get("type", "peak"), f.get("freq", 1000.0),
            f.get("gain_db", 0.0), shape, sample_rate,
            use_q=use_q)
        resp *= magnitude_response(b, a, freqs, sample_rate)
    return resp


def band_shape(band: dict) -> tuple[float, bool]:
    """Return (shape_value, use_q) for a filter band, matching the native core.

    Rule (identical to core/src/chain.c):
      1. an explicit ``use_q`` wins;
      2. otherwise an explicit ``slope`` means shelf-slope semantics;
      3. otherwise shelves default to slope, everything else to Q.
    The shape value comes from whichever field was supplied, so a stray ``q``
    on a shelf is never silently reinterpreted as a slope or vice versa.
    """
    ftype = band.get("type", "peak")
    is_shelf = ftype in ("lowshelf", "highshelf")
    has_slope = "slope" in band
    explicit = band.get("use_q")
    if explicit is None:
        use_q = False if has_slope else not is_shelf
    else:
        use_q = bool(explicit)
    if use_q:
        return float(band.get("q", 1.0)), True
    if has_slope:
        return float(band["slope"]), False
    return float(band.get("q", 0.71)), False
