"""Spectral analysis: Welch PSD, band energies, balance descriptors."""
from __future__ import annotations

import numpy as np
from scipy import signal

# ISO 1/3-octave-ish grouping used for the "balance" view and advisor
BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 250),
    ("low-mid", 250, 500),
    ("mid", 500, 2000),
    ("upper-mid", 2000, 4000),
    ("presence", 4000, 6000),
    ("brilliance", 6000, 20000),
]


def welch_spectrum(data: np.ndarray, sample_rate: int,
                   nperseg: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """Mean-across-channels Welch PSD. Returns (freqs, psd_dB)."""
    x = np.atleast_2d(np.asarray(data, dtype=np.float64))
    nperseg = min(nperseg, x.shape[1])
    if nperseg < 64:
        return np.array([]), np.array([])
    f, pxx = signal.welch(x, fs=sample_rate, nperseg=nperseg,
                          noverlap=nperseg // 2, axis=-1,
                          window="hann", scaling="density")
    mean_psd = pxx.mean(axis=0)
    with np.errstate(divide="ignore"):
        psd_db = 10.0 * np.log10(np.maximum(mean_psd, 1e-20))
    return f, psd_db


def band_energies_db(data: np.ndarray, sample_rate: int) -> dict[str, float]:
    """Relative energy per named band (dB, referenced to total)."""
    f, psd_db = welch_spectrum(data, sample_rate)
    if len(f) == 0:
        return {}
    psd = 10.0 ** (psd_db / 10.0)
    total = np.trapezoid(psd, f) if hasattr(np, "trapezoid") else np.trapz(psd, f)
    out = {}
    for name, lo, hi in BANDS:
        m = (f >= lo) & (f < hi)
        if not np.any(m):
            out[name] = -np.inf
            continue
        e = np.trapezoid(psd[m], f[m]) if hasattr(np, "trapezoid") else np.trapz(psd[m], f[m])
        out[name] = float(10.0 * np.log10(e / total)) if e > 0 and total > 0 else -np.inf
    return out


def spectral_flatness(data: np.ndarray, sample_rate: int) -> float:
    """Wiener entropy: geometric_mean/arithmetic_mean of PSD (0..1)."""
    f, psd_db = welch_spectrum(data, sample_rate)
    if len(f) == 0:
        return 0.0
    psd = 10.0 ** (psd_db / 10.0)
    m = f > 20
    psd = psd[m]
    if len(psd) == 0:
        return 0.0
    gmean = np.exp(np.mean(np.log(np.maximum(psd, 1e-20))))
    amean = np.mean(psd)
    return float(gmean / amean) if amean > 0 else 0.0


def spectral_centroid(data: np.ndarray, sample_rate: int) -> float:
    f, psd_db = welch_spectrum(data, sample_rate)
    if len(f) == 0:
        return 0.0
    psd = 10.0 ** (psd_db / 10.0)
    s = psd.sum()
    return float((f * psd).sum() / s) if s > 0 else 0.0


def smoothed_spectrum(f: np.ndarray, psd_db: np.ndarray,
                      octave_fraction: float = 12.0) -> tuple[np.ndarray, np.ndarray]:
    """1/N-octave smoothing for pleasant visualizations and comparisons."""
    if len(f) == 0:
        return f, psd_db
    keep = f > 0
    f, psd_db = f[keep], psd_db[keep]
    psd = 10.0 ** (psd_db / 10.0)
    factor = 2.0 ** (1.0 / (2 * octave_fraction))
    edges_lo = f / factor
    edges_hi = f * factor
    # cumulative-sum based moving integration in log-frequency
    c = np.concatenate([[0.0], np.cumsum(psd)])
    hi_idx = np.searchsorted(f, edges_hi)
    lo_idx = np.searchsorted(f, edges_lo)
    spans = np.maximum(hi_idx - lo_idx, 1)
    sums = c[hi_idx] - c[lo_idx]
    avg = sums / spans
    with np.errstate(divide="ignore"):
        return f, 10.0 * np.log10(np.maximum(avg, 1e-20))
