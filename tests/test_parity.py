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


# ---------------------------------------------------------------------------
# Per-stage cross-backend parity
#
# The single end-to-end check above is not enough to localise a regression: the
# audit found the two backends drifting by -27 dB on a full chain while rms_diff
# stayed under 0.005, because the stages partially cancelled. These tests pin
# each stage *individually* with a decibel threshold, so a regression names the
# component that broke instead of just "the render differs".
# ---------------------------------------------------------------------------

BASE = {"preamp_db": 0.0, "eq": {"filters": []},
        "limiter": {"enabled": False}}


def _cfg(**over):
    """Build a config on top of BASE with a dotted-path override."""
    import copy
    c = copy.deepcopy(BASE)
    for key, value in over.items():
        if key in ("preamp_db",):
            c[key] = value
        else:
            c.setdefault(key, {}).update(value)
    return c


def _render_both(cfg, seconds=1.0, sr=48000):
    """Render one input through both backends; return (native, numpy) floats."""
    from eqforge.audio import synth
    from eqforge.dsp.engine import render
    from eqforge.native import loader

    x = synth.pink_noise(seconds, sr, amplitude=0.25)
    rn = render(x, sr, cfg)
    assert rn.backend == "native", "native backend expected"

    saved = loader._LIB
    loader._LIB = None
    loader._TRIED = True
    try:
        rf = render(x, sr, cfg)
    finally:
        loader._LIB = saved
        loader._TRIED = True
    assert rf.backend == "numpy", "fallback backend expected"
    return rn.data, rf.data


def _db(ratio):
    return 20.0 * np.log10(max(ratio, 1e-30))


EQ_4BAND = [
    {"type": "highpass", "freq": 60, "q": 0.707},
    {"type": "peak", "freq": 240, "gain_db": -3.5, "q": 1.1},
    {"type": "peak", "freq": 1800, "gain_db": 4.0, "q": 0.9},
    {"type": "highshelf", "freq": 8000, "gain_db": -2.0, "q": 0.707},
]

# Thresholds are pinned ~15 dB above the measured floor rather than being round
# numbers, so that a regression has to move the error by only a few dB to be
# caught. The last column of each comment is the value measured on
# 2026-11-01; re-measure and update both numbers together if the arithmetic
# legitimately changes.
STAGES = [
    # (label, config, max relative error in dB)
    ("noop", _cfg(), -135.0),                     # measured -150.5
    ("preamp", _cfg(preamp_db=-6.0), -125.0),     # measured -140.2
    ("eq single peak", _cfg(eq={"filters": [
        {"type": "peak", "freq": 1000, "gain_db": 6.0, "q": 1.0}]}), -90.0),
                                                  # measured -105.8
    ("eq 4 band", _cfg(eq={"filters": EQ_4BAND}), -70.0),   # measured -82.5
    ("crossfeed", _cfg(crossfeed={"enabled": True, "level_db": -6.0},
                       eq={"filters": []}), -120.0),        # measured -134.9
    ("compressor", _cfg(compressor={"enabled": True, "threshold_db": -18.0,
                                    "ratio": 4.0, "attack_ms": 10.0,
                                    "release_ms": 100.0}), -100.0),
                                                  # measured -114.8
    ("limiter", _cfg(limiter={"enabled": True, "ceiling_db": -1.0,
                              "attack_ms": 1.0, "release_ms": 40.0,
                              "lookahead_ms": 2.0}), -135.0),  # measured -150.5
]


@needs_native
@pytest.mark.parametrize("label,cfg,limit_db", STAGES,
                         ids=[s[0] for s in STAGES])
def test_stage_parity_within_limit(label, cfg, limit_db):
    """Each processing stage must agree between backends to `limit_db`."""
    a, b = _render_both(cfg)
    n = min(a.shape[1], b.shape[1])
    # compare the steady state: the first 2000 frames carry start-up transient
    a = a[:, 2000:n]
    b = b[:, 2000:n]
    peak = max(float(np.abs(a).max()), 1e-12)
    max_abs = float(np.abs(a - b).max())
    rel_db = _db(max_abs / peak)
    assert rel_db < limit_db, (
        f"{label}: backends differ by {rel_db:.1f} dB "
        f"(limit {limit_db:.0f} dB, max abs {max_abs:.3e})")


@needs_native
def test_full_chain_parity():
    """Stress: every stage at once, which is what the engine actually runs."""
    cfg = _cfg(
        preamp_db=-3.0,
        eq={"filters": EQ_4BAND},
        crossfeed={"enabled": True, "level_db": -7.0},
        compressor={"enabled": True, "threshold_db": -16.0, "ratio": 3.0,
                    "attack_ms": 8.0, "release_ms": 120.0},
        limiter={"enabled": True, "ceiling_db": -1.0, "attack_ms": 1.0,
                 "release_ms": 40.0, "lookahead_ms": 2.0},
    )
    a, b = _render_both(cfg)
    n = min(a.shape[1], b.shape[1])
    a, b = a[:, 2000:n], b[:, 2000:n]
    peak = float(np.abs(a).max())
    rel_db = _db(float(np.abs(a - b).max()) / peak)
    # measured -81.1 dB; the old -55 dB bound would have accepted a 26 dB
    # regression over the whole chain without a murmur.
    assert rel_db < -70.0, f"full chain differs by {rel_db:.1f} dB"


@needs_native
@pytest.mark.parametrize("level", [0.0005, 0.25, 1.0])
def test_limiter_parity_at_multiple_levels(level):
    """Limiter parity across quiet, moderate and over-driven input.

    The audit's *cause* here was a history slice with an extra offset, which
    only misbehaved once the limiter actually engaged - i.e. at high level.
    A single mid-level test would not have found it.
    """
    import copy
    from eqforge.audio import synth
    from eqforge.dsp.engine import render
    from eqforge.native import loader

    cfg = _cfg(limiter={"enabled": True, "ceiling_db": -1.0,
                        "attack_ms": 1.5, "release_ms": 40.0,
                        "lookahead_ms": 2.0})
    sr = 48000
    x = synth.pink_noise(1.0, sr, amplitude=1.0)
    x = np.clip(x * (level / max(float(np.abs(x).max()), 1e-12)), -4.0, 4.0)

    rn = render(x, sr, cfg)
    saved = loader._LIB
    loader._LIB = None
    loader._TRIED = True
    try:
        rf = render(x, sr, cfg)
    finally:
        loader._LIB = saved
        loader._TRIED = True

    a, b = rn.data[:, 2000:], rf.data[:, 2000:]
    max_abs = float(np.abs(a - b).max())
    ceiling = 10.0 ** (-1.0 / 20.0)
    assert max_abs < ceiling * 1e-3, (
        f"level {level}: limiter backends differ by {max_abs:.3e}")


@needs_native
def test_parity_detects_a_broken_backend(monkeypatch):
    """Guard against the test itself going stale.

    If the comparison were insensitive (wrong slice, zero-length input, silent
    output) the parity tests above would pass no matter what the fallback did.
    This deliberately corrupts the fallback and asserts the comparison notices.
    """
    from eqforge.dsp import fallback, engine

    real = fallback.FallbackChain.process

    def corrupt(self, block):
        out = real(self, block)
        return out * 1.5          # +3.5 dB: unmissable but not absurd

    a, b = _render_both(_cfg(preamp_db=-3.0))
    n = min(a.shape[1], b.shape[1])
    baseline_peak = float(np.abs(a[:, 2000:n]).max())

    monkeypatch.setattr(fallback.FallbackChain, "process", corrupt)
    _, broken = _render_both(_cfg(preamp_db=-3.0))
    n2 = min(a.shape[1], broken.shape[1])
    diff_db = _db(float(np.abs(a[:, 2000:n2] - broken[:, 2000:n2]).max())
                  / baseline_peak)
    assert diff_db > -20.0, (
        "parity comparison failed to notice a +3.5 dB backend error "
        f"(saw {diff_db:.1f} dB) - the test is not sensitive")


# ---------------------------------------------------------------------------
# Shelf shape-parameter semantics
#
# The audit found the native core and the fallback reference disagreeing about
# what a shelf band's "q" field means: the native parser defaulted use_q to
# true (Q semantics) while the coefficient designer defaulted shelves to a
# shelf *slope*. A -2 dB high shelf was up to 0.15 dB different in the
# transition band, and a "slope"-only band was 3.7 dB out because the native
# side ignored the field entirely. Both sides now follow one documented rule:
#   1. explicit "use_q" wins
#   2. otherwise an explicit "slope" means slope semantics
#   3. otherwise shelves default to slope, everything else to Q
# ---------------------------------------------------------------------------

# The threshold is per case because a low-frequency shelf has a pole near the
# unit circle (r = 0.986 at 120 Hz), so its float32 state carries real
# numerical noise. Measured float32-vs-float64 floors with identical
# coefficients (i.e. no semantic disagreement at all):
#     highshelf 8 kHz     -> -140 dB
#     lowshelf 120 Hz     ->  -96 dB (impulse) / -87 dB (pink noise, 0.25 amp)
# The pink-noise figure is the one that matters here because it is the signal
# this test uses: more energy excites the near-unit-circle pole harder.
# A genuine semantic mismatch shows up at -40 to -75 dB, so a limit a few dB
# under the measured floor still catches it by a wide margin.
SHELF_CASES = [
    ("highshelf q only",     {"type": "highshelf", "freq": 8000,
                              "gain_db": -2.0, "q": 0.707}, -120.0),
    ("highshelf slope only", {"type": "highshelf", "freq": 8000,
                              "gain_db": -2.0, "slope": 0.71}, -120.0),
    ("highshelf both",       {"type": "highshelf", "freq": 8000,
                              "gain_db": -2.0, "q": 0.707, "slope": 0.9},
                                                                 -120.0),
    ("highshelf use_q",      {"type": "highshelf", "freq": 8000,
                              "gain_db": -2.0, "q": 0.707, "use_q": True},
                                                                 -120.0),
    ("lowshelf q only",      {"type": "lowshelf", "freq": 120,
                              "gain_db": -3.0, "q": 0.707}, -82.0),
    ("lowshelf slope only",  {"type": "lowshelf", "freq": 120,
                              "gain_db": -3.0, "slope": 0.8}, -82.0),
]


@needs_native
@pytest.mark.parametrize("label,band,limit_db", SHELF_CASES,
                         ids=[c[0] for c in SHELF_CASES])
def test_shelf_shape_semantics_parity(label, band, limit_db):
    """A shelf band must mean the same thing in both backends."""
    a, b = _render_both(_cfg(eq={"filters": [band]}))
    n = min(a.shape[1], b.shape[1])
    a, b = a[:, 2000:n], b[:, 2000:n]
    rel_db = _db(float(np.abs(a - b).max()) / float(np.abs(a).max()))
    assert rel_db < limit_db, (
        f"{label}: shelf shape interpreted differently "
        f"(backends differ by {rel_db:.1f} dB, limit {limit_db:.0f})")


@needs_native
def test_shelf_default_shape_is_slope():
    """The default for a shelf band must be slope, in both backends.

    Guards the specific regression: a shelf with only "q" must behave like
    slope=q, not like Q semantics.
    """
    from eqforge.dsp.coeffs import band_shape

    value, use_q = band_shape({"type": "highshelf", "q": 0.707})
    assert use_q is False, "shelf with only q must use slope semantics"
    assert value == pytest.approx(0.707)

    # and that decision must agree with the native core for the same band
    a, b = _render_both(_cfg(eq={"filters": [
        {"type": "highshelf", "freq": 6000, "gain_db": -4.0, "q": 0.6}]}))
    n = min(a.shape[1], b.shape[1])
    rel_db = _db(float(np.abs(a[:, 2000:n] - b[:, 2000:n]).max())
                 / float(np.abs(a[:, 2000:n]).max()))
    assert rel_db < -120.0, (
        f"native disagrees with band_shape(): {rel_db:.1f} dB")
