"""Audio I/O tests."""
from __future__ import annotations

import numpy as np
import pytest

from eqforge.audio.io import (AudioData, formats_available, probe, read,
                              write)
from eqforge.audio import synth
from eqforge.errors import AudioIOError


def test_wav_roundtrip_24bit(tmp_path):
    x = synth.sine(440, 0.5, 48000, amplitude=0.5)
    p = tmp_path / "t.wav"
    write(AudioData(x, 48000), p)
    back = read(p)
    assert back.sample_rate == 48000
    assert back.channels == 2
    assert back.subtype == "PCM_24"
    np.testing.assert_allclose(back.data, x, atol=2 ** -22)


def test_wav_roundtrip_16bit(tmp_path):
    x = synth.pink_noise(0.5, 44100, amplitude=0.4)
    p = tmp_path / "t16.wav"
    write(AudioData(x, 44100), p, bit_depth=16)
    back = read(p)
    assert back.subtype == "PCM_16"
    # 16-bit quantization error bounded by ~1 LSB (+dither)
    np.testing.assert_allclose(back.data, x, atol=3 * 2 ** -15)


def test_flac_roundtrip_lossless(tmp_path):
    x = synth.sine(997, 0.4, 48000, amplitude=0.6)
    p = tmp_path / "t.flac"
    write(AudioData(x, 48000), p, bit_depth=24)
    back = read(p)
    np.testing.assert_allclose(back.data, x, atol=2 ** -22)


def test_mono_handling(tmp_path):
    x = synth.sine(440, 0.3, 22050, amplitude=0.5, channels=1)
    p = tmp_path / "m.wav"
    write(AudioData(x, 22050), p)
    back = read(p)
    assert back.channels == 1
    assert back.data.shape == x.shape


def test_multichannel(tmp_path):
    x = np.random.default_rng(3).uniform(-0.3, 0.3, (6, 48000))
    p = tmp_path / "mc.wav"
    write(AudioData(x, 48000), p)
    back = read(p)
    assert back.channels == 6
    np.testing.assert_allclose(back.data, x, atol=2 ** -22)


def test_missing_file_raises():
    with pytest.raises(AudioIOError):
        read("/nonexistent/audio.wav")


def test_probe(tmp_path):
    x = synth.sine(440, 1.0, 48000)
    p = tmp_path / "probe.wav"
    write(AudioData(x, 48000), p)
    info = probe(p)
    assert info["sample_rate"] == 48000
    assert info["channels"] == 2
    assert info["duration"] == pytest.approx(1.0, abs=0.02)


def test_formats_available_shape():
    fa = formats_available()
    assert "wav" in fa["decode"] and "flac" in fa["decode"]
    assert fa["soundfile"] is True


@pytest.mark.skipif(formats_available()["ffmpeg"] is None,
                    reason="ffmpeg not installed")
def test_mp3_roundtrip_if_ffmpeg(tmp_path):
    x = synth.sine(440, 2.0, 48000, amplitude=0.5)
    p = tmp_path / "t.mp3"
    write(AudioData(x, 48000), p)
    assert p.exists()
    back = read(p)
    # lossy: just check structure and rough energy
    assert back.channels == 2
    assert back.duration == pytest.approx(2.0, abs=0.2)
    assert 0.1 < float(np.sqrt(np.mean(back.data ** 2))) < 0.6
