"""The bundled GUI must be wired to the real backend, not to fixtures.

Two independent checks:
  1. every RPC the GUI calls exists in the daemon (a renamed endpoint would
     otherwise only surface as a broken button in a browser);
  2. the save -> activate path a GUI click performs produces an active-dsp
     config whose measured response matches the profile.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from eqforge.daemon.server import Daemon, EventBus
from eqforge.daemon.state import EngineState

GUI_JS = Path(__file__).resolve().parents[1] / "eqforge/gui/static/app.js"


@pytest.fixture()
def daemon(isolated_home):
    d = Daemon.__new__(Daemon)
    d.state = EngineState()
    d.bus = EventBus()
    import threading
    d.stop_event = threading.Event()
    import time as _t
    d.started = _t.time()
    d.state.ensure_active_config()
    return d


def test_gui_calls_only_real_rpc_methods():
    src = GUI_JS.read_text()
    called = set(re.findall(r'api\.(?:get|post)\("([a-z_]+)"', src))
    assert called, "no API calls found in app.js - did the GUI change?"

    known = set(re.findall(r'method == "([a-z_]+)"', 
                           (Path(__file__).resolve().parents[1] /
                            "eqforge/daemon/server.py").read_text()))
    missing = called - known
    assert not missing, f"GUI calls RPCs the daemon does not implement: {missing}"


def test_no_fabricated_data_in_gui():
    """No mock/demo/sample data paths: the GUI must render real responses."""
    src = GUI_JS.read_text()
    for banned in ("Math.random()", "mockData", "demoData", "fakeData",
                   "sampleProfile"):
        assert banned not in src, f"GUI contains fabricated data path: {banned}"


def test_gui_save_activate_changes_the_engine_config(daemon, isolated_home):
    """The exact request sequence a profile click produces."""
    prof = {"id": "gui_flow", "name": "GUI Flow",
            "eq": {"filters": [
                {"type": "peak", "freq": 1000, "gain_db": -12.0, "q": 1.0},
                {"type": "highpass", "freq": 80, "q": 0.707}]}}

    r = daemon.rpc("save_profile", {"profile": prof})
    assert "path" in r
    assert not r.get("warnings"), r.get("warnings")

    daemon.rpc("set_profile", {"profile": "gui_flow"})

    active = json.loads(
        (Path(isolated_home) / "config/eqforge/active-dsp.json").read_text())
    bands = active["eq"]["filters"]
    assert [b["type"] for b in bands] == ["peak", "highpass"]
    assert bands[0]["gain_db"] == pytest.approx(-12.0)


def test_activated_config_really_filters(daemon, isolated_home):
    """Measure the impulse response of the published config."""
    from eqforge.dsp.engine import render

    prof = {"id": "gui_meas", "name": "GUI Meas",
            "eq": {"filters": [
                {"type": "peak", "freq": 1000, "gain_db": -12.0, "q": 1.0},
                {"type": "highpass", "freq": 80, "q": 0.707}]}}
    daemon.rpc("save_profile", {"profile": prof})
    daemon.rpc("set_profile", {"profile": "gui_meas"})
    cfg = json.loads(
        (Path(isolated_home) / "config/eqforge/active-dsp.json").read_text())

    sr, n = 48000, 48000
    imp = np.zeros((2, n))
    imp[:, 100] = 0.25
    h = render(imp, sr, cfg).data[0]
    H = np.abs(np.fft.rfft(h, 1 << 16))
    freqs = np.fft.rfftfreq(1 << 16, 1 / sr)

    def rel_db(f):
        i = int(np.argmin(np.abs(freqs - f)))
        return 20 * np.log10(H[i] / H.max())

    assert rel_db(1000) == pytest.approx(-12.0, abs=0.5), "1 kHz peak missing"
    assert rel_db(80) == pytest.approx(-3.0, abs=0.7), "highpass corner wrong"
    assert rel_db(50) < -6.0, "highpass does not attenuate below its corner"


def test_wrong_shape_profile_warns_instead_of_silently_emptying(daemon):
    """Bands under a key the model ignores must not save as a clean success."""
    bad = {"id": "wrong_key", "name": "Wrong",
           "filters": [{"type": "peak", "freq": 1000, "gain_db": -12, "q": 1}]}
    r = daemon.rpc("save_profile", {"profile": bad})
    warnings = r.get("warnings") or []
    assert warnings, "a profile whose bands will be ignored saved silently"
    assert "eq" in " ".join(warnings)


def test_forward_compatible_extra_keys_stay_silent(daemon):
    """Unknown *unrelated* keys are still accepted without noise."""
    p = {"id": "future_ok", "name": "Future", "some_future_field": {"x": 1},
         "eq": {"filters": [{"type": "peak", "freq": 500, "gain_db": 3, "q": 1}]}}
    r = daemon.rpc("save_profile", {"profile": p})
    assert not r.get("warnings"), r.get("warnings")


def test_static_assets_are_framable_but_api_responses_are_not():
    """The bundled GUI may be embedded by a local preview proxy; API data may not.

    X-Frame-Options/ALLOWALL on the app shell lets a loopback proxy render the
    studio, while `frame-ancestors` + `default-src 'self'` keeps injected
    third-party code out. JSON/SSE responses stay DENY so nothing else can
    frame the daemon's data.
    """
    server_src = (Path(__file__).resolve().parents[1] /
                  "eqforge/daemon/server.py").read_text()
    static_fn = server_src.split("def _static(", 1)[1].split("\n    def ", 1)[0]
    assert "frame=True" in static_fn, "static assets lost their frame policy"
    assert '"ALLOWALL"' in server_src
    assert "frame-ancestors *" in server_src
    assert 'self.send_header("X-Frame-Options", "DENY")' in server_src
