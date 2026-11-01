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


def _render_backend(frames, process, channels: int, sample_rate: int,
                    cfg: dict, latency: int, block: int,
                    latency_compensate: bool) -> tuple[np.ndarray, int]:
    """Shared block loop + latency compensation for both backends.

    Both backends delay the signal by `latency` frames (look-ahead). To return a
    render that is time-aligned *and* full length, append `latency` silent
    frames to the input, process everything, then drop the first `latency`
    output frames. Doing this identically for both backends means they stay
    comparable sample-by-sample - the earlier fallback path simply sliced the
    head off, which shifted its output `latency` frames earlier than native.
    """
    total_in = frames.shape[1]
    pad = latency if latency_compensate else 0
    padded = np.zeros((channels, total_in + pad), dtype=float)
    padded[:, :total_in] = frames
    out = np.zeros_like(padded)
    pos = 0
    while pos < padded.shape[1]:
        n = min(block, padded.shape[1] - pos)
        out[:, pos:pos + n] = process(padded[:, pos:pos + n])
        pos += n
    if pad:
        out = out[:, latency:latency + total_in]
    else:
        out = out[:, :total_in]
    return out, latency if latency_compensate else 0


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
    channels, frames_n = x.shape
    if channels > 16:
        raise ValueError(f"too many channels: {channels}")
    if frames_n == 0:
        return RenderResult(x.copy(), sample_rate, "native" if _native_ok()
                            else "numpy", 0, 0, {})

    chain = _get_native(channels, sample_rate)
    if chain is not None:
        chain.set_fade(False)   # offline renders must be exact from sample 0
        chain.configure_json(json.dumps(cfg))
        latency = chain.latency_frames()

        def _native_process(blk):
            # the C ABI takes float32 planar buffers and mutates in place
            f32 = np.ascontiguousarray(blk, dtype=np.float32)
            chain.process(f32)
            return f32.astype(np.float64)

        out, trimmed = _render_backend(
            x, _native_process, channels, sample_rate, cfg, latency, block,
            latency_compensate)
        snap = chain.snapshot()
        chain.close()
        return RenderResult(np.asarray(out, dtype=np.float64), sample_rate,
                            "native", latency, trimmed, snap)

    from eqforge.dsp.fallback import FallbackChain
    fb = FallbackChain(channels, sample_rate)
    fb.configure_json(cfg)
    latency = fb.latency_frames
    out, trimmed = _render_backend(
        x, fb.process, channels, sample_rate, cfg, latency, block,
        latency_compensate)
    return RenderResult(np.asarray(out, dtype=np.float64), sample_rate,
                        "numpy", latency, trimmed, fb.snapshot())


def _native_ok() -> bool:
    from eqforge.native.loader import load
    return load() is not None


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
