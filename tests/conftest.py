"""Shared fixtures. Every test runs in an isolated XDG environment."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Isolate all XDG dirs per test so nothing touches the real home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield tmp_path


@pytest.fixture
def sine_1k():
    from eqforge.audio import synth
    return synth.sine(1000.0, 1.0, 48000, amplitude=0.5)


@pytest.fixture
def pink():
    from eqforge.audio import synth
    return synth.pink_noise(1.0, 48000, amplitude=0.4)


@pytest.fixture
def wav_file(tmp_path, sine_1k):
    import soundfile as sf
    p = tmp_path / "sine1k.wav"
    sf.write(str(p), sine_1k.T, 48000, subtype="PCM_24")
    return p


def write_profile(tmp_path: Path, pid: str, **kw) -> Path:
    """Helper: write a minimal valid profile file, return its path."""
    import json
    d = {
        "format": "eqforge.profile",
        "format_version": 2,
        "id": pid,
        "name": kw.pop("name", pid),
        "preamp": {"mode": "auto"},
        "eq": {"filters": kw.pop("filters", [])},
    }
    if "limiter" not in kw:
        d["limiter"] = {"enabled": True, "ceiling_db": -1.0}
    d.update(kw)
    if d.get("limiter") is None:
        d.pop("limiter")
    p = tmp_path / f"{pid}.json"
    p.write_text(json.dumps(d, indent=2))
    return p
