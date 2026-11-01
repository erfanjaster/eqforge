"""ITU-R BS.1770-4 loudness measurement + true peak (ITU-R BS.1770 Annex 2).

Implements:
  - K-weighting (head-model shelf + RLB high-pass), generalized for any rate
  - momentary (400 ms), short-term (3 s) loudness
  - integrated (gated) loudness: absolute gate -70 LUFS + relative gate -10 LU
  - loudness range (LRA): EBU Tech 3342 (10%/95% of gated short-term dist)
  - true peak via 4x oversampled polyphase interpolation

Validated in tests/test_analysis.py against analytically known signals and
cross-checked against the native core meter.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from eqforge.dsp.coeffs import design_biquad  # noqa: F401 (parity reference)

ABS_GATE_LUFS = -70.0
REL_GATE_LU = -10.0


def k_weighting_sos(sample_rate: float) -> np.ndarray:
    """K-weighting filter as cascaded second-order sections (2 stages)."""
    # Stage 1: head-model high shelf (+4 dB HF)
    G, Q, fc = 3.999843853973347, 0.7071752369554196, 1681.974450955533
    K = np.tan(np.pi * fc / sample_rate)
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.4996865530411197
    a0 = 1.0 + K / Q + K * K
    s1 = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0,
          (Vh - Vb * K / Q + K * K) / a0, 1.0, 2.0 * (K * K - 1.0) / a0,
          (1.0 - K / Q + K * K) / a0]
    # Stage 2: revised low-frequency B-curve (RLB) high-pass
    Q, fc = 0.5003270373238773, 38.13547087602444
    K = np.tan(np.pi * fc / sample_rate)
    a0 = 1.0 + K / Q + K * K
    s2 = [1.0 / a0, -2.0 / a0, 1.0 / a0, 1.0, 2.0 * (K * K - 1.0) / a0,
          (1.0 - K / Q + K * K) / a0]
    return np.array([s1, s2], dtype=np.float64)


def channel_weights(channels: int) -> np.ndarray:
    """BS.1770 channel weights: L/R = 1.0, surrounds = 1.41 (LFE excluded
    upstream by convention; for stereo this is [1, 1])."""
    w = np.ones(channels)
    if channels > 2:
        w[2:] = 1.41
    return w


def k_weight(data: np.ndarray, sample_rate: int) -> np.ndarray:
    sos = k_weighting_sos(sample_rate)
    return signal.sosfilt(sos, data, axis=-1)


def _block_loudness(data: np.ndarray, sample_rate: int, block_ms: float,
                    step_ms: float) -> tuple[np.ndarray, int]:
    """Gated-loudness building blocks: loudness per block (LUFS), step in
    samples. Blocks shorter than the window at the tail are dropped."""
    kw = k_weight(data, sample_rate)
    w = channel_weights(kw.shape[0])
    block = max(1, int(round(block_ms * sample_rate / 1000.0)))
    step = max(1, int(round(step_ms * sample_rate / 1000.0)))
    n = kw.shape[1]
    if n < block:
        return np.array([]), step
    # mean square per block via cumulative sums
    sq = (kw ** 2) * w[:, None]
    cs = np.concatenate([np.zeros((sq.shape[0], 1)), np.cumsum(sq, axis=1)],
                        axis=1)
    starts = np.arange(0, n - block + 1, step)
    sums = cs[:, starts + block] - cs[:, starts]
    # BS.1770: SUM channel powers (weights already applied), MEAN over time
    mean_sq = sums.sum(axis=0) / block
    with np.errstate(divide="ignore"):
        loud = np.where(mean_sq > 1e-12,
                        -0.691 + 10.0 * np.log10(np.maximum(mean_sq, 1e-12)),
                        -np.inf)
    return loud, step


def momentary_loudness(data: np.ndarray, sample_rate: int) -> float:
    """Last-400ms momentary loudness of the whole buffer (offline use)."""
    loud, _ = _block_loudness(data, sample_rate, 400.0, 100.0)
    return float(loud[-1]) if len(loud) else -70.0


def short_term_loudness(data: np.ndarray, sample_rate: int) -> float:
    loud, _ = _block_loudness(data, sample_rate, 3000.0, 500.0)
    return float(loud[-1]) if len(loud) else -70.0


def integrated_loudness(data: np.ndarray, sample_rate: int) -> float:
    """Gated integrated loudness (LUFS) per BS.1770-4."""
    loud, _ = _block_loudness(data, sample_rate, 400.0, 100.0)
    if len(loud) == 0:
        return -70.0
    above = loud[np.isfinite(loud) & (loud > ABS_GATE_LUFS)]
    if len(above) == 0:
        return -70.0
    # relative gate on the mean of absolutely-gated blocks
    mean_power = np.mean(10.0 ** ((above + 0.691) / 10.0))
    rel_gate = -0.691 + 10.0 * np.log10(mean_power) + REL_GATE_LU
    gated = above[above > rel_gate]
    if len(gated) == 0:
        return -70.0
    mean_power = np.mean(10.0 ** ((gated + 0.691) / 10.0))
    return float(-0.691 + 10.0 * np.log10(mean_power))


def loudness_range(data: np.ndarray, sample_rate: int) -> float:
    """LRA in LU per EBU Tech 3342 (short-term blocks, gated, 10th-95th pct)."""
    loud, _ = _block_loudness(data, sample_rate, 3000.0, 1000.0)
    loud = loud[np.isfinite(loud)]
    if len(loud) < 3:
        return 0.0
    above = loud[loud > ABS_GATE_LUFS]
    if len(above) < 3:
        return 0.0
    mean_power = np.mean(10.0 ** ((above + 0.691) / 10.0))
    rel_gate = -0.691 + 10.0 * np.log10(mean_power) - 20.0  # LRA gate: -20 LU
    gated = above[above > rel_gate]
    if len(gated) < 3:
        return 0.0
    lo, hi = np.percentile(gated, [10.0, 95.0])
    return float(hi - lo)


# ---------------- true peak ----------------

_TP_TAPS = 32


def _polyphase_bank() -> np.ndarray:
    """[4 phases, 8 taps] windowed-sinc interpolation filter (matches C)."""
    n = np.arange(_TP_TAPS)
    x = n - (_TP_TAPS - 1) / 2.0
    fc = 0.125
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(np.abs(x) < 1e-9, 2 * fc,
                     np.sin(2 * np.pi * fc * x) / (np.pi * np.where(x == 0, 1, x)))
    w = 0.5 - 0.5 * np.cos(2 * np.pi * n / (_TP_TAPS - 1))
    h = 4.0 * s * w
    return np.stack([h[4 * np.arange(8) + p] for p in range(4)])


def true_peak(data: np.ndarray, sample_rate: int | None = None) -> float:
    """Max true peak (linear) across channels via 4x interpolation."""
    del sample_rate  # interpolation factor is rate-independent here
    x = np.atleast_2d(np.asarray(data, dtype=np.float64))
    phases = _polyphase_bank()
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    for c in range(x.shape[0]):
        hist = np.concatenate([np.zeros(7), x[c]])  # hist[i] aligned
        # sliding dot products for the 3 interpolated phases
        for p in (1, 2, 3):
            acc = np.zeros(x.shape[1])
            for k in range(8):
                # hist index for x[n-k] at position n: (7 + n - k)
                acc += phases[p, k] * hist[7 - k: 7 - k + x.shape[1]]
            peak = max(peak, float(np.max(np.abs(acc))))
    return peak


def true_peak_db(data: np.ndarray, sample_rate: int | None = None) -> float:
    tp = true_peak(data, sample_rate)
    return 20.0 * np.log10(tp) if tp > 1e-12 else -np.inf


def peak_db(data: np.ndarray) -> float:
    p = float(np.max(np.abs(data))) if data.size else 0.0
    return 20.0 * np.log10(p) if p > 1e-12 else -np.inf


def rms_db(data: np.ndarray) -> float:
    r = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
    return 20.0 * np.log10(r) if r > 1e-12 else -np.inf


def crest_factor_db(data: np.ndarray) -> float:
    """Peak-to-RMS in dB - a useful 'how dynamic is this' measure."""
    p = peak_db(data)
    r = rms_db(data)
    if not np.isfinite(p) or not np.isfinite(r):
        return 0.0
    return p - r
