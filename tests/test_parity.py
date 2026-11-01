"""Parity tests: numpy reference vs native C core.

The native core is the product; the numpy layer is the reference/spec. They
must agree on filter design, magnitude response, K-weighting and end-to-end
rendering within float tolerance.
"""
from __future__ import annotations

import ctypes
import json

import numpy as np
import pytest

from eqforge.dsp.coeffs import design_biquad, magnitude_response
from eqforge.native import capi
from eqforge.native.loader import NativeChain, load

needs_native = pytest.mark.skipif(load() is None,
                                  reason="libeqforge not available")

TYPES = ["peak", "lowshelf", "highshelf", "lowpass", "highpass",
         "bandpass", "notch", "allpass"]


@needs_native
@pytest.mark.parametrize("ftype", TYPES)
def test_biquad_design_parity(ftype):
    lib = load()
    sr = 48000.0
    freq, gain, q = 997.0, 5.5, 1.3
    b, a = design_biquad(ftype, freq, gain, q, sr)

    coeffs = capi.BiquadCoeffs()
    use_q = ftype not in ("lowshelf", "highshelf")
    rc = lib.eqf_biquad_design(ctypes.byref(coeffs),
                               capi.FILTER_TYPES[ftype], freq, gain, q,
                               use_q, sr)
    assert rc == 0
    assert coeffs.b0 == pytest.approx(b[0], abs=2e-6)
    assert coeffs.b1 == pytest.approx(b[1], abs=2e-6)
    assert coeffs.b2 == pytest.approx(b[2], abs=2e-6)
    assert coeffs.a1 == pytest.approx(a[1], abs=2e-6)
    assert coeffs.a2 == pytest.approx(a[2], abs=2e-6)


@needs_native
def test_magnitude_response_parity():
    lib = load()
    from eqforge.native.loader import biquad_magnitude_response
    freqs = np.logspace(np.log10(20), np.log10(20000), 97)
    b, a = design_biquad("peak", 1000, 6.0, 1.0, 48000.0)
    ref = magnitude_response(b, a, freqs, 48000.0)
    nat = biquad_magnitude_response("peak", 1000, 6.0, 1.0, 48000.0, freqs)
    np.testing.assert_allclose(nat, ref, rtol=1e-5, atol=1e-7)


@needs_native
def test_k_weighting_parity():
    """Native K-weight design must match the Python BS.1770 implementation."""
    lib = load()
    from eqforge.analysis.loudness import k_weighting_sos
    out = (ctypes.c_float * 5 * 2)()
    rc = lib.eqf_meter_k_weight_design(ctypes.c_float(48000.0), out)
    assert rc == 0
    sos = k_weighting_sos(48000.0)
    for stage in range(2):
        b = sos[stage][:3]
        a = np.array([1.0, sos[stage][4], sos[stage][5]])
        for i, v in enumerate(list(b) + list(a[1:])):
            assert out[stage][i] == pytest.approx(v, abs=2e-5)


CFG = {
    "preamp_db": -6.0,
    "eq": {"filters": [
        {"type": "peak", "freq": 100.0, "gain_db": 5.0, "q": 1.0},
        {"type": "peak", "freq": 1000.0, "gain_db": -4.0, "q": 1.4},
        {"type": "highshelf", "freq": 8000.0, "gain_db": 3.0, "q": 0.7},
    ]},
    "limiter": {"enabled": False},
}


@pytest.mark.parametrize("backend", ["native", "numpy"])
def test_render_backend_frequency_response(backend, monkeypatch):
    """Both backends must realize the same transfer function."""
    from eqforge.native import loader
    if backend == "numpy":
        monkeypatch.setattr(loader, "_LIB", None)
        monkeypatch.setattr(loader, "_TRIED", True)
    elif loader.load() is None:
        pytest.skip("libeqforge not available")
    from eqforge.audio import synth
    from eqforge.dsp.engine import render

    sr = 48000
    freqs = [50, 100, 300, 1000, 3000, 10000]
    gains = []
    for f in freqs:
        x = synth.sine(float(f), 1.0, sr, amplitude=0.2, channels=1)
        r = render(x, sr, CFG)
        assert r.backend == backend
        tail = r.data[:, sr // 2:]
        amp = float(np.max(np.abs(tail)))
        gains.append(20 * np.log10(amp / 0.2))

    # expected from the reference design
    from eqforge.dsp.coeffs import filter_bank_response
    expected = []
    for f in freqs:
        m = filter_bank_response(CFG["eq"]["filters"], np.array([f]), sr)[0]
        expected.append(20 * np.log10(m) - 6.0)  # + preamp

    for f, g, e in zip(freqs, gains, expected):
        assert g == pytest.approx(e, abs=0.3), f"{f} Hz: {g:.2f} vs {e:.2f}"


@needs_native
def test_native_matches_numpy_backend():
    """Cross-backend agreement on the same rendered material."""
    from eqforge.audio import synth
    from eqforge.dsp.engine import render
    x = synth.pink_noise(2.0, 48000, amplitude=0.3)
    rn = render(x, 48000, CFG)
    assert rn.backend == "native"

    # force fallback by hiding the lib temporarily
    from eqforge.native import loader
    saved = loader._LIB
    loader._LIB = None
    loader._TRIED = True
    try:
        rf = render(x, 48000, CFG)
    finally:
        loader._LIB = saved
        loader._TRIED = True
    assert rf.backend == "numpy"

    a, b = rn.data, rf.data
    n = min(a.shape[1], b.shape[1])
    corr = np.corrcoef(a[:, 2000:n].ravel(), b[:, 2000:n].ravel())[0, 1]
    assert corr > 0.999
    rms_diff = float(np.sqrt(np.mean((a[:, 2000:n] - b[:, 2000:n]) ** 2)))
    assert rms_diff < 0.005


@needs_native
def test_native_rejects_bad_config():
    from eqforge.errors import NativeError
    with NativeChain(2, 48000) as ch:
        with pytest.raises(NativeError):
            ch.configure_json("{not json")
        with pytest.raises(NativeError):
            ch.configure_json(json.dumps(
                {"eq": {"filters": [{"type": "peak", "freq": 1000,
                                     "gain_db": 500}]}}))
        with pytest.raises(NativeError):
            ch.configure_json(json.dumps({"limiter": {"enabled": True,
                                                      "ceiling_db": 6}}))
        # still usable afterwards
        ch.configure_json(json.dumps(CFG))
        data = np.zeros((2, 1024), dtype=np.float32)
        ch.process(data)


@needs_native
def test_latency_compensation_alignment():
    """Rendered output must stay time-aligned with input (limiter latency
    compensated by the engine)."""
    from eqforge.audio import synth
    from eqforge.dsp.engine import render
    cfg = json.loads(json.dumps(CFG))
    cfg["limiter"] = {"enabled": True, "ceiling_db": -1.0, "attack_ms": 5,
                      "release_ms": 50, "lookahead_ms": 2.0}
    x = synth.impulse(1, 4800, at=2400)
    r = render(x, 48000, cfg)
    peak_pos = int(np.argmax(np.abs(r.data[0])))
    assert abs(peak_pos - 2400) <= 2, f"impulse moved to {peak_pos}"
