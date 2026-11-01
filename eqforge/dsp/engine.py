"""Offline processing engine.

Renders audio through a resolved dsp-config using libeqforge when available
(block-processing through NativeChain), otherwise the numpy fallback. Also
implements two-pass loudness normalization and optional TPDF dither on
bit-depth reduction — the pieces the `apply`/`batch` CLI commands compose.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from eqforge.log import get_logger

log = get_logger("dsp.engine")

BLOCK = 4096


@dataclass
class RenderResult:
    data: np.ndarray               # float64 [channels, frames], may exceed 1.0
    sample_rate: int
    backend: str                   # "native" | "numpy"
    latency_frames: int = 0
    trimmed: int = 0               # frames removed from the tail (latency comp)
    snapshot: dict = field(default_factory=dict)


def _get_native(channels: int, sample_rate: int):
    from eqforge.native.loader import load, NativeChain
    if load() is None:
        return None
    try:
        return NativeChain(channels, sample_rate, max_block=16384)
    except Exception as e:  # noqa: BLE001
        log.warning("native chain unavailable (%s); using numpy fallback", e)
        return None


def render(data: np.ndarray, sample_rate: int, cfg: dict,
           block: int = BLOCK, latency_compensate: bool = True) -> RenderResult:
    """Process `data` ([channels, frames], any float dtype) with dsp config.

    The native path introduces look-ahead latency; with latency_compensate
    the engine trims `latency` frames from the output head so the render is
    time-aligned with the input (standard for offline processing).
    """
    x = np.asarray(data, dtype=np.float64)
    if x.ndim == 1:
        x = x[None, :]
    channels, frames = x.shape
    if channels > 16:
        raise ValueError(f"too many channels: {channels}")

    chain = _get_native(channels, sample_rate)
    if chain is not None:
        chain.set_fade(False)   # offline renders must be exact from sample 0
        chain.configure_json(json.dumps(cfg))
        latency = chain.latency_frames()
        # Latency compensation: the chain delays the signal by `latency`
        # frames. Feed the input followed by `latency` zero frames, then drop
        # the first `latency` output frames: the result is time-aligned and
        # full length.
        pad = latency if latency_compensate else 0
        buf = np.zeros((channels, frames + pad), dtype=np.float32)
        buf[:, :frames] = x
        out = np.zeros_like(buf)
        total = frames + pad
        pos = 0
        while pos < total:
            n = min(block, total - pos)
            blk = np.ascontiguousarray(buf[:, pos:pos + n])
            chain.process(blk)
            out[:, pos:pos + n] = blk
            pos += n
        snap = chain.snapshot()
        chain.close()
        if pad:
            out = out[:, latency:latency + frames]
        else:
            out = out[:, :frames]
        return RenderResult(np.asarray(out, dtype=np.float64), sample_rate,
                            "native", latency, latency, snap)

    from eqforge.dsp.fallback import FallbackChain
    fb = FallbackChain(channels, sample_rate)
    fb.configure_json(cfg)
    chunks = []
    pos = 0
    while pos < frames:
        n = min(block, frames - pos)
        chunks.append(fb.process(x[:, pos:pos + n]))
        pos += n
    out = np.concatenate(chunks, axis=1) if chunks else x
    latency = fb.latency_frames
    trimmed = 0
    if latency_compensate and latency > 0 and out.shape[1] > latency:
        out = out[:, latency:]
        out = np.pad(out, ((0, 0), (0, latency)), mode="constant")
        trimmed = latency
    return RenderResult(out, sample_rate, "numpy", latency, trimmed,
                        fb.snapshot())


def measure_lufs(data: np.ndarray, sample_rate: int) -> float:
    """Integrated loudness (gated, BS.1770-4) via the analysis layer."""
    from eqforge.analysis.loudness import integrated_loudness
    return integrated_loudness(data, sample_rate)


def normalize_to_lufs(data: np.ndarray, sample_rate: int, target_lufs: float,
                      true_peak_ceiling_db: float = -1.0) -> tuple[np.ndarray, float]:
    """Two-pass loudness normalization.

    Returns (adjusted_data, applied_gain_db). Gain is limited so that the
    scaled true peak stays at or below the ceiling (analysis-layer true peak).
    """
    from eqforge.analysis.loudness import integrated_loudness, true_peak_db
    cur = integrated_loudness(data, sample_rate)
    if not np.isfinite(cur) or cur < -69.0:
        return data, 0.0
    gain_db = target_lufs - cur
    tp = true_peak_db(data, sample_rate)
    max_gain = true_peak_ceiling_db - tp
    if gain_db > max_gain:
        log.info("normalization gain limited by true-peak ceiling "
                 "(%.1f dB -> %.1f dB)", gain_db, max_gain)
        gain_db = max_gain
    g = 10.0 ** (gain_db / 20.0)
    return data * g, gain_db


def dither_tpdf(data: np.ndarray, bits: int) -> np.ndarray:
    """Triangular-PDF dither at 1 LSB of the target bit depth."""
    if bits >= 32:
        return data
    lsb = 2.0 ** (1 - bits)
    rng = np.random.default_rng(0xE9F0CE11)
    noise = (rng.random(data.shape) + rng.random(data.shape) - 1.0) * lsb
    return data + noise


def quantize(data: np.ndarray, bits: int, dither: bool = True) -> np.ndarray:
    """Quantize float audio [-1, 1] to an integer bit depth (as float)."""
    if bits >= 32:
        return data
    if dither:
        data = dither_tpdf(data, bits)
    levels = 2 ** (bits - 1)
    q = np.round(data * levels) / levels
    return np.clip(q, -1.0, 1.0 - 2.0 ** (1 - bits))
