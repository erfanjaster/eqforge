"""DSP engine behavior tests (backend-agnostic where possible)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from eqforge.audio import synth
from eqforge.dsp.engine import (dither_tpdf, normalize_to_lufs, quantize,
                                render)


def measure_gain_db(x, y, sr, skip=0.25):
    """Steady-state gain between two steady signals."""
    n0 = int(sr * skip)
    a = float(np.max(np.abs(x[:, n0:])))
    b = float(np.max(np.abs(y[:, n0:])))
    return 20 * np.log10(b / max(a, 1e-12))


def test_gain_stage():
    x = synth.sine(1000, 1.0, 48000, amplitude=0.3)
    r = render(x, 48000, {"preamp_db": -6.0, "eq": {"filters": []},
                          "limiter": {"enabled": False}})
    assert measure_gain_db(x, r.data, 48000) == pytest.approx(-6.0, abs=0.1)


def test_limiter_ceiling():
    x = synth.sine(1000, 2.0, 48000, amplitude=0.95)
    cfg = {"preamp_db": 6.0, "eq": {"filters": []},
           "limiter": {"enabled": True, "ceiling_db": -3.0, "attack_ms": 1,
                       "release_ms": 40, "lookahead_ms": 2}}
    r = render(x, 48000, cfg)
    tail = r.data[:, 48000:]
    peak = float(np.max(np.abs(tail)))
    ceiling = 10 ** (-3 / 20)
    assert peak <= ceiling + 1e-4
    assert peak > ceiling * 0.6  # not squashed to silence


def test_highpass_removes_dc_and_sub():
    sr = 48000
    sub = synth.sine(30, 2.0, sr, amplitude=0.8)
    tone = synth.sine(1000, 2.0, sr, amplitude=0.3)
    x = sub + tone
    cfg = {"preamp_db": 0, "eq": {"filters": [
        {"type": "highpass", "freq": 120, "q": 0.707, "enabled": True}]},
        "limiter": {"enabled": False}}
    r = render(x, sr, cfg)

    def level(sig, freq):
        sig = sig[sr:]
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        f = np.fft.rfftfreq(len(sig), 1 / sr)
        return 20 * np.log10(spec[np.argmin(np.abs(f - freq))] + 1e-12)

    # 2nd-order HPF @120 Hz: 30 Hz is 2 octaves below => ~24 dB down
    att = level(x[0], 30) - level(r.data[0], 30)
    assert 20 < att < 28, att
    # 1 kHz passes untouched
    assert abs(level(x[0], 1000) - level(r.data[0], 1000)) < 0.3


def test_crossfeed_moves_energy():
    sr = 48000
    l = synth.sine(200, 2.0, sr, amplitude=0.5, channels=1)
    x = np.concatenate([l, np.zeros_like(l)], axis=0)
    cfg_off = {"eq": {"filters": []}, "limiter": {"enabled": False}}
    cfg_on = json.loads(json.dumps(cfg_off))
    cfg_on["crossfeed"] = {"enabled": True, "level_db": -6, "fc_hz": 700}
    r_off = render(x, sr, cfg_off)
    r_on = render(x, sr, cfg_on)
    r_energy_off = float(np.sqrt(np.mean(r_off.data[1][sr:] ** 2)))
    r_energy_on = float(np.sqrt(np.mean(r_on.data[1][sr:] ** 2)))
    assert r_energy_off < 1e-6
    assert r_energy_on > 0.1  # ~-6 dB of 0.5 through the LP network


def test_convolver_delta_ir_is_transparent():
    sr = 48000
    x = synth.pink_noise(1.0, sr, amplitude=0.4)
    cfg = {"eq": {"filters": []}, "limiter": {"enabled": False},
           "convolver": {"enabled": True, "gain_db": 0,
                         "ir": [[1.0] + [0.0] * 7, [1.0] + [0.0] * 7]}}
    r = render(x, sr, cfg)
    np.testing.assert_allclose(r.data, x, atol=1e-5)


def test_compressor_reduces_loud_peaks():
    sr = 48000
    x = synth.sine(1000, 3.0, sr, amplitude=0.9)
    cfg = {"eq": {"filters": []}, "limiter": {"enabled": False},
           "compressor": {"enabled": True, "threshold_db": -20, "ratio": 8,
                          "knee_db": 0, "attack_ms": 2, "release_ms": 100}}
    r = render(x, sr, cfg)
    tail_in = float(np.max(np.abs(x[:, 2 * sr:])))
    tail_out = float(np.max(np.abs(r.data[:, 2 * sr:])))
    assert tail_out < tail_in * 0.6


def test_normalize_to_lufs():
    sr = 48000
    x = synth.pink_noise(4.0, sr, amplitude=0.1)
    y, gain = normalize_to_lufs(x, sr, -16.0)
    from eqforge.analysis.loudness import integrated_loudness
    got = integrated_loudness(y, sr)
    assert got == pytest.approx(-16.0, abs=0.5)
    assert gain != 0.0


def test_normalize_respects_true_peak_ceiling():
    sr = 48000
    x = synth.full_scale_square(1.0, sr, 200.0) * 0.5
    y, gain = normalize_to_lufs(x, sr, 0.0, true_peak_ceiling_db=-1.0)
    from eqforge.analysis.loudness import true_peak_db
    assert true_peak_db(y, sr) <= -0.99


def test_quantize_16bit_levels():
    x = np.linspace(-1, 1, 10000)[None, :]
    q = quantize(x, 16, dither=False)
    step = 2.0 ** -15
    residual = (q[0] * 2 ** 15) - np.round(q[0] * 2 ** 15)
    assert np.allclose(residual, 0, atol=1e-6)


def test_dither_is_lsb_scale():
    x = np.zeros((2, 100000))
    d = dither_tpdf(x, 16)
    lsb = 2.0 ** -15
    assert float(np.std(d)) < lsb * 0.9
    assert float(np.max(np.abs(d))) <= lsb + 1e-12


def test_render_mono_and_multichannel():
    for ch in (1, 2, 6):
        x = np.random.default_rng(ch).standard_normal((ch, 48000)) * 0.2
        r = render(x, 48000, {"eq": {"filters": [
            {"type": "peak", "freq": 1000, "gain_db": 3, "q": 1}]},
            "limiter": {"enabled": False}})
        assert r.data.shape == x.shape


def test_render_short_blocks():
    """Tiny inputs (< one internal block) must still render."""
    x = synth.sine(1000, 0.01, 48000, amplitude=0.5)  # 480 frames
    r = render(x, 48000, {"eq": {"filters": [
        {"type": "peak", "freq": 1000, "gain_db": 6, "q": 1}]},
        "limiter": {"enabled": False}})
    assert r.data.shape[1] == x.shape[1]


def test_per_channel_filters():
    """A filter on channel 1 only must leave channel 0 untouched."""
    sr = 48000
    x = synth.sine(1000, 1.5, sr, amplitude=0.4)
    cfg = {"eq": {"filters": [
        {"type": "peak", "freq": 1000, "gain_db": 6, "q": 1,
         "channel": "1", "enabled": True}]},
        "limiter": {"enabled": False}}
    r = render(x, sr, cfg)
    g0 = measure_gain_db(x[0:1], r.data[0:1], sr)
    g1 = measure_gain_db(x[1:2], r.data[1:2], sr)
    assert abs(g0) < 0.2
    assert g1 == pytest.approx(6.0, abs=0.3)
