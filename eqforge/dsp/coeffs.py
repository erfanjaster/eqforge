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
        b, a = design_biquad(
            f.get("type", "peak"), f.get("freq", 1000.0),
            f.get("gain_db", 0.0), f.get("q", 1.0), sample_rate,
            use_q=f.get("use_q"))
        resp *= magnitude_response(b, a, freqs, sample_rate)
    return resp
