"""Smart advisor tests."""
from __future__ import annotations

import numpy as np
import pytest

from eqforge.analysis.report import analyze
from eqforge.audio import synth
from eqforge.smart import targets as T
from eqforge.smart.advisor import (advise, advise_content, advise_profile,
                                   find_resonances)
from eqforge.smart.headroom import headroom_db, worst_case_boost_db


# ------------------------------------------------------------ headroom --

def test_headroom_math():
    f = [{"gain_db": 6, "enabled": True}, {"gain_db": -3, "enabled": True},
         {"gain_db": 2, "enabled": True}, {"gain_db": 12, "enabled": False}]
    assert worst_case_boost_db(f) == 8.0
    assert headroom_db(f) == -8.0
    assert headroom_db(f, margin_db=1.5) == -9.5


# --------------------------------------------------------- profile rules --

def test_profile_extreme_boost_warning():
    resolved = {"eq": {"filters": [
        {"type": "peak", "freq": 100, "gain_db": 20, "q": 1, "enabled": True}]},
        "limiter": {"enabled": True}}
    findings = advise_profile(resolved)
    assert any(f.id == "profile.extreme_boost" for f in findings)


def test_profile_no_limiter_action():
    resolved = {"eq": {"filters": [
        {"type": "peak", "freq": 100, "gain_db": 9, "q": 1, "enabled": True}]},
        "limiter": {"enabled": False}}
    findings = advise_profile(resolved)
    assert any(f.id == "profile.no_limiter" and f.severity == "action"
               for f in findings)


def test_profile_narrow_boost():
    resolved = {"eq": {"filters": [
        {"type": "peak", "freq": 2000, "gain_db": 9, "q": 20,
         "enabled": True}]}}
    assert any(f.id == "profile.narrow_boost"
               for f in advise_profile(resolved))


# --------------------------------------------------------- content rules --

def test_clipped_content_flagged():
    x = np.clip(synth.sine(1000, 0.5, 48000, amplitude=2.0), -1, 1)
    rep = analyze(x, 48000, with_spectrum=False)
    findings = advise_content(rep)
    assert any(f.id == "audio.clipped" for f in findings)


def test_quiet_content_suggests_normalize():
    x = synth.sine(1000, 2.0, 48000, amplitude=0.01)
    rep = analyze(x, 48000, with_spectrum=False)
    findings = advise_content(rep)
    norm = [f for f in findings if f.id == "audio.very_quiet"]
    assert norm and norm[0].action["action"] == "normalize"


def test_resonance_suggests_notch():
    pink = synth.pink_noise(4.0, 48000, amplitude=0.12)
    tone = synth.sine(2500.0, 4.0, 48000, amplitude=0.3)
    rep = analyze(pink + tone, 48000)
    findings = advise_content(rep)
    res = [f for f in findings if f.id == "audio.resonance"]
    assert res
    assert res[0].action["filter"]["gain_db"] < 0  # a cut, not a boost


# -------------------------------------------------------- combined rules --

def test_combined_clipping_risk():
    x = synth.sine(100, 2.0, 48000, amplitude=0.9)
    rep = analyze(x, 48000, with_spectrum=False)
    resolved = {"eq": {"filters": [
        {"type": "lowshelf", "freq": 100, "gain_db": 10, "q": 0.7,
         "enabled": True}]},
        "limiter": {"enabled": False},
        "preamp": {"mode": "manual", "gain_db": 0}}
    advice = advise(rep, resolved, 48000)
    assert any(f.id == "combined.clipping_risk" for f in advice.findings)
    assert advice.suggested_preamp_db is not None
    assert advice.suggested_preamp_db < 0


def test_combined_clean_case():
    x = synth.pink_noise(2.0, 48000, amplitude=0.2)
    rep = analyze(x, 48000, with_spectrum=False)
    resolved = {"eq": {"filters": []}, "limiter": {"enabled": True}}
    advice = advise(rep, resolved, 48000)
    assert any(f.id == "all.good" for f in advice.findings)


def test_advice_serializable():
    import json
    x = synth.pink_noise(1.0, 48000)
    rep = analyze(x, 48000, with_spectrum=False)
    json.dumps(advise(rep, {"eq": {"filters": []}}, 48000).to_dict())


# -------------------------------------------------------------- targets --

def test_target_curves():
    f = np.array([20.0, 1000.0, 10000.0])
    flat = T.target_db("flat", f)
    np.testing.assert_allclose(flat, 0.0, atol=1e-9)
    harman = T.target_db("harman-overear", f)
    assert harman[0] > 4.0          # bass lift
    assert abs(harman[1]) < 1.0     # ~0 at 1k
    assert harman[2] < 0            # treble dip region


def test_match_eq_recovers_tilt():
    """Content with a known bass-heavy tilt: matching should suggest cuts
    in the bass (or equivalent) and reduce deviation."""
    sr = 48000
    # synthesize "measured" spectrum: harman target + 6 dB bass shelf
    centers = T.one_third_octave_bands()
    base = T.target_db("harman-overear", centers)
    tilted = base + 6.0 / (1.0 + (centers / 150.0) ** 2)

    # match_eq expects a dense spectrum; interpolate to pseudo-spectrum
    dense_f = np.logspace(np.log10(20), np.log10(20000), 2048)
    dense_db = np.interp(np.log(dense_f), np.log(centers), tilted)
    filters = T.match_eq(dense_db, dense_f, "harman-overear",
                         max_gain_db=6.0, max_filters=8)
    assert filters
    low = [f for f in filters if f["freq"] < 300]
    assert low and all(f["gain_db"] < 0 for f in low), low


def test_deviation_summary():
    centers = T.one_third_octave_bands()
    base = T.target_db("harman-overear", centers)
    dense_f = np.logspace(np.log10(20), np.log10(20000), 1024)
    dense_db = np.interp(np.log(dense_f), np.log(centers), base)
    s = T.deviation_summary(dense_db, dense_f, "harman-overear")
    assert s["rms_deviation_db"] < 3.0
