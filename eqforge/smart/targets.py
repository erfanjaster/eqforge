"""Target curves and spectral-matching EQ generation.

Includes a tabulated Harman over-ear (2018) approximation used for:
  - "how far is this content/headphone from the target" reporting
  - gentle corrective-EQ suggestions (smooth, limited, sanity-checked)

Deliberately conservative: suggestions are smoothed to 1/3-octave resolution,
gain-limited, and capped in filter count. The goal is useful starting points,
not "magic".
"""
from __future__ import annotations

import numpy as np

# Harman over-ear 2018 target (Olive et al.), coarse tabulation in dB re
# 1 kHz, interpolated log-frequency. Values are an approximation suitable for
# suggestions, not for measurement work.
HARMAN_OVEREAR = (
    (10, 6.0), (20, 6.4), (30, 6.6), (50, 6.4), (80, 5.6), (100, 4.8),
    (200, 2.2), (400, 0.4), (700, -0.3), (1000, 0.0), (2000, 1.6),
    (3000, 0.6), (5000, -1.2), (7000, -0.4), (10000, -2.4), (20000, -1.0),
)

TARGETS = {
    "harman-overear": HARMAN_OVEREAR,
    "flat": ((10, 0.0), (20000, 0.0)),
    "broadcast": ((20, -2.0), (100, 0.0), (2000, 1.0), (8000, 0.0),
                  (20000, -2.0)),
}


def target_db(name: str, freqs: np.ndarray) -> np.ndarray:
    """Interpolated target curve in dB at `freqs`."""
    if name not in TARGETS:
        raise ValueError(f"unknown target {name!r} "
                         f"(have: {', '.join(TARGETS)})")
    table = TARGETS[name]
    f = np.array([t[0] for t in table], dtype=np.float64)
    g = np.array([t[1] for t in table], dtype=np.float64)
    return np.interp(np.log(np.maximum(freqs, 1.0)), np.log(f), g)


def one_third_octave_bands(fmin: float = 31.5, fmax: float = 16000.0
                           ) -> np.ndarray:
    n = int(np.ceil(np.log2(fmax / fmin) * 3)) + 1
    return fmin * 2.0 ** (np.arange(n) / 3.0)


def match_eq(spectrum_db: np.ndarray, spectrum_freqs: np.ndarray,
             target_name: str = "harman-overear",
             max_gain_db: float = 6.0, max_filters: int = 8,
             smoothing_oct: float = 1 / 3) -> list[dict]:
    """Generate a conservative corrective filter list.

    Steps:
      1. Smooth the measured spectrum to 1/3-octave resolution.
      2. Compute deviation from the target at ISO band centers.
      3. Keep the largest deviations, quantize to sensible Q, clamp gains.
    """
    from eqforge.analysis.spectrum import smoothed_spectrum
    if len(spectrum_freqs) == 0:
        return []
    frac = 1.0 / smoothing_oct if smoothing_oct > 0 else 3.0
    f_s, db_s = smoothed_spectrum(spectrum_freqs, spectrum_db,
                                  octave_fraction=frac / 2.0)
    centers = one_third_octave_bands()
    measured = np.interp(np.log(centers), np.log(np.maximum(f_s, 1e-6)), db_s)
    tgt = target_db(target_name, centers)

    # normalize both to 0 dB mean over the vocal/music range (250 Hz..8 kHz)
    band = (centers >= 250) & (centers <= 8000)
    offset = float(np.median(measured[band] - tgt[band]))
    deviation = tgt - (measured - offset)

    # pick worst offenders
    order = np.argsort(-np.abs(deviation))
    picks: list[dict] = []
    used: set[int] = set()
    for idx in order:
        if len(picks) >= max_filters:
            break
        dev = float(deviation[idx])
        if abs(dev) < 2.0:
            break
        # avoid double-covering adjacent bands
        if any(abs(int(idx) - u) <= 1 for u in used):
            continue
        used.add(int(idx))
        # correction = target - measured: cut where content is too loud
        gain = float(np.clip(dev, -max_gain_db, max_gain_db))
        f0 = float(centers[idx])
        if f0 < 70:
            picks.append({"type": "lowshelf", "freq": round(f0, 1),
                          "gain_db": round(gain, 2), "q": 0.71,
                          "use_q": False, "enabled": True,
                          "name": f"match {f0:.0f} Hz"})
        elif f0 > 11000:
            picks.append({"type": "highshelf", "freq": round(min(f0, 14000), 1),
                          "gain_db": round(gain, 2), "q": 0.71,
                          "use_q": False, "enabled": True,
                          "name": f"match {f0:.0f} Hz"})
        else:
            picks.append({"type": "peak", "freq": round(f0, 1),
                          "gain_db": round(gain, 2),
                          "q": round(float(np.clip(2.2 ** (1 / 3), 0.8, 2.5)), 2),
                          "enabled": True, "name": f"match {f0:.0f} Hz"})
    picks.sort(key=lambda p: p["freq"])
    return picks


def deviation_summary(spectrum_db: np.ndarray, spectrum_freqs: np.ndarray,
                      target_name: str = "harman-overear") -> dict:
    """How far content is from a target, per band (for reports)."""
    from eqforge.analysis.spectrum import smoothed_spectrum
    f_s, db_s = smoothed_spectrum(spectrum_freqs, spectrum_db, octave_fraction=6)
    centers = one_third_octave_bands()
    measured = np.interp(np.log(centers), np.log(np.maximum(f_s, 1e-6)), db_s)
    tgt = target_db(target_name, centers)
    band = (centers >= 250) & (centers <= 8000)
    offset = float(np.median(measured[band] - tgt[band]))
    dev = (measured - offset) - tgt
    return {
        "target": target_name,
        "rms_deviation_db": float(np.sqrt(np.mean(dev ** 2))),
        "max_deviation_db": float(np.max(np.abs(dev))),
        "per_band": {f"{c:.0f}": round(float(d), 2)
                     for c, d in zip(centers, dev)},
    }
