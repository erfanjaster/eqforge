"""Analysis layer tests: BS.1770 loudness anchors, true peak, spectrum."""
from __future__ import annotations

import numpy as np
import pytest

from eqforge.audio import synth
from eqforge.analysis import loudness as L
from eqforge.analysis import spectrum as S
from eqforge.analysis.report import analyze


def test_integrated_loudness_1k_anchor():
    """ITU anchor: 1 kHz sine, -20 dBFS peak, mono => -23.0 LUFS."""
    x = synth.sine(1000.0, 3.0, 48000, amplitude=0.1, channels=1)
    lufs = L.integrated_loudness(x, 48000)
    assert lufs == pytest.approx(-23.0, abs=0.15)


def test_stereo_dual_mono_is_3db_louder():
    mono = synth.sine(1000.0, 3.0, 48000, amplitude=0.1, channels=1)
    stereo = synth.sine(1000.0, 3.0, 48000, amplitude=0.1, channels=2)
    diff = L.integrated_loudness(stereo, 48000) - \
        L.integrated_loudness(mono, 48000)
    assert diff == pytest.approx(3.01, abs=0.05)


def test_amplitude_scaling_20log():
    x1 = synth.sine(1000.0, 3.0, 48000, amplitude=0.5, channels=1)
    x2 = x1 * 0.5
    d = L.integrated_loudness(x1, 48000) - L.integrated_loudness(x2, 48000)
    assert d == pytest.approx(6.02, abs=0.05)


def test_silence_is_minus_inf():
    x = np.zeros((2, 48000))
    assert L.integrated_loudness(x, 48000) <= -69.0


def test_loudness_range_dynamic_vs_static():
    # static tone -> LRA ~ 0
    static = synth.sine(440.0, 6.0, 48000, amplitude=0.3)
    assert L.loudness_range(static, 48000) < 1.0
    # quiet/loud alternating -> LRA large
    quiet = synth.sine(440.0, 4.0, 48000, amplitude=0.05)
    loud = synth.sine(440.0, 4.0, 48000, amplitude=0.5)
    mixed = np.concatenate([quiet, loud, quiet, loud], axis=1)
    assert L.loudness_range(mixed, 48000) > 8.0


def test_true_peak_slow_sine_accuracy():
    x = synth.sine(100.0, 1.0, 48000, amplitude=0.8, channels=1, phase=0.7)
    assert L.true_peak(x, 48000) == pytest.approx(0.8, abs=0.02)


def test_true_peak_detects_inter_sample():
    """A square-ish waveform at fs/4 with unlucky phase has sample peaks
    below 1.0 but true peaks at ~1.0."""
    sr = 48000
    n = sr // 2
    t = np.arange(n) / sr
    # 11.9 kHz tone: sample peaks can undershoot the analog envelope
    x = (np.sin(2 * np.pi * 11900.0 * t + 0.3))[None, :]
    sample_peak = float(np.max(np.abs(x)))
    tp = L.true_peak(x, sr)
    assert tp >= sample_peak - 1e-9
    assert tp == pytest.approx(1.0, abs=0.03)


def test_true_peak_never_below_sample_peak():
    rng = np.random.default_rng(1)
    x = rng.standard_normal((2, 96000)) * 0.3
    assert L.true_peak(x, 48000) >= float(np.max(np.abs(x))) - 1e-6


def test_clipping_detection():
    x = synth.sine(1000.0, 0.5, 48000, amplitude=2.0)
    x = np.clip(x, -1.0, 1.0)
    rep = analyze(x, 48000, with_spectrum=False)
    assert rep.clipped_samples > 0
    assert rep.clipping_events >= 1


def test_dc_offset_and_silence_ratio():
    x = synth.sine(1000.0, 1.0, 48000, amplitude=0.3)
    x[0] += 0.05  # DC on channel 0
    silence = np.zeros((2, 48000))
    both = np.concatenate([silence, x, silence], axis=1)
    rep = analyze(both, 48000, with_spectrum=False)
    assert abs(rep.dc_offset[0]) > 0.005
    assert rep.silent_ratio > 0.5


def test_band_energies_sum():
    pink = synth.pink_noise(2.0, 48000)
    bands = S.band_energies_db(pink, 48000)
    total = 10 * np.log10(sum(10 ** (v / 10) for v in bands.values()
                              if np.isfinite(v)))
    assert total == pytest.approx(0.0, abs=1.0)  # energies are relative


def test_spectrum_shapes():
    pink = synth.pink_noise(1.0, 48000)
    f, psd = S.welch_spectrum(pink, 48000)
    assert len(f) == len(psd)
    # pink noise tilts down ~3 dB/octave
    lo = psd[(f > 100) & (f < 200)].mean()
    hi = psd[(f > 3200) & (f < 6400)].mean()
    assert lo - hi == pytest.approx(15.0, abs=3.0)  # 5 octaves * 3 dB


def test_resonance_detection():
    """A narrow tone buried in pink noise must be flagged as a resonance."""
    pink = synth.pink_noise(4.0, 48000, amplitude=0.15)
    tone = synth.sine(3000.0, 4.0, 48000, amplitude=0.25)
    x = pink + tone
    rep = analyze(x, 48000)
    from eqforge.smart.advisor import find_resonances
    res = find_resonances(rep)
    assert any(abs(r["freq"] - 3000) / 3000 < 0.05 for r in res), res


def test_report_serializable():
    import json
    x = synth.pink_noise(0.5, 48000)
    rep = analyze(x, 48000)
    json.dumps(rep.to_dict())  # must not raise
