"""Test-signal synthesis for diagnostics and verification.

`eqforge synth` exposes these; they are also used by the test-suite and by
`eqforge doctor` to validate the whole pipeline end-to-end.
"""
from __future__ import annotations

import numpy as np


def sine(freq: float, duration: float, sample_rate: int, amplitude: float = 1.0,
         channels: int = 2, phase: float = 0.0) -> np.ndarray:
    t = np.arange(int(duration * sample_rate)) / sample_rate
    x = amplitude * np.sin(2 * np.pi * freq * t + phase)
    return np.tile(x, (channels, 1))


def sweep(f0: float, f1: float, duration: float, sample_rate: int,
          amplitude: float = 1.0, channels: int = 2,
          log: bool = True) -> np.ndarray:
    """Exponential (log=True) or linear sine sweep."""
    n = int(duration * sample_rate)
    t = np.arange(n) / sample_rate
    if log:
        k = (f1 / f0) ** (1.0 / duration)
        phase = 2 * np.pi * f0 * (k ** t - 1) / np.log(k)
    else:
        phase = 2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * duration))
    x = amplitude * np.sin(phase)
    return np.tile(x, (channels, 1))


def pink_noise(duration: float, sample_rate: int, amplitude: float = 0.5,
               channels: int = 2, seed: int = 42) -> np.ndarray:
    """Pink noise via FFT spectral shaping (-3 dB/octave)."""
    rng = np.random.default_rng(seed)
    n = int(duration * sample_rate)
    out = np.empty((channels, n))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    shape = np.where(freqs > 0, 1.0 / np.sqrt(np.maximum(freqs, 1.0)), 0.0)
    shape[0] = 0.0
    for c in range(channels):
        white = rng.standard_normal(n)
        spec = np.fft.rfft(white) * shape
        x = np.fft.irfft(spec, n)
        x /= np.max(np.abs(x)) + 1e-12
        out[c] = x * amplitude
    return out


def white_noise(duration: float, sample_rate: int, amplitude: float = 0.5,
                channels: int = 2, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((channels, int(duration * sample_rate))) * amplitude


def impulse(channels: int = 2, taps: int = 1024, at: int = 0) -> np.ndarray:
    x = np.zeros((channels, taps))
    x[:, min(at, taps - 1)] = 1.0
    return x


def multitone(freqs: list[float], duration: float, sample_rate: int,
              amplitude_each: float = 0.1, channels: int = 2) -> np.ndarray:
    """Sum of sines - handy for quick frequency-response spot checks."""
    t = np.arange(int(duration * sample_rate)) / sample_rate
    x = np.zeros_like(t)
    for f in freqs:
        x += amplitude_each * np.sin(2 * np.pi * f * t)
    return np.tile(x, (channels, 1))


def full_scale_square(duration: float, sample_rate: int, freq: float = 1000.0,
                      channels: int = 2) -> np.ndarray:
    """A pathological signal for clipping/true-peak tests."""
    t = np.arange(int(duration * sample_rate)) / sample_rate
    x = np.sign(np.sin(2 * np.pi * freq * t))
    return np.tile(x, (channels, 1))
