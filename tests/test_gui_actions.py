"""Browser-complete GUI workflows use real profile/audio/system primitives."""
from __future__ import annotations

import base64
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from eqforge.daemon.server import Daemon, EventBus
from eqforge.daemon.state import EngineState


@pytest.fixture()
def daemon(isolated_home):
    d = Daemon.__new__(Daemon)
    d.state = EngineState()
    d.bus = EventBus()
    d.stop_event = threading.Event()
    d.started = time.time()
    d.state.ensure_active_config()
    return d


def test_import_selected_csv_and_repeat_does_not_overwrite(daemon):
    content = "freq,gain,q\n100,2.5,0.8\n1000,-3,1.2\n"
    first = daemon.rpc("import_profile", {"filename": "My Headphones.csv",
                                          "content": content})
    second = daemon.rpc("import_profile", {"filename": "My Headphones.csv",
                                           "content": content})
    assert first["filters"] == 2
    assert first["id"] == "imported.my-headphones"
    assert second["id"] == first["id"] + "-2"
    resolved = daemon.rpc("resolve_profile", {"profile": first["id"]})["resolved"]
    assert resolved["eq"]["filters"][1]["gain_db"] == -3


def test_export_is_a_self_contained_download(daemon):
    daemon.rpc("save_profile", {"profile": {
        "id": "download-me", "name": "Download me",
        "eq": {"filters": [{"type": "peak", "freq": 500,
                            "gain_db": 2, "q": 1}]}}})
    result = daemon.rpc("export_profile", {"profile": "download-me"})
    document = json.loads(result["content"])
    assert result["filename"] == "download-me.eqforge.json"
    assert document["id"] == "download-me"
    assert document["exported_from"] == "download-me"
    assert document["eq"]["filters"][0]["freq"] == 500


def test_browser_audio_upload_is_analyzed_and_cleaned_up(daemon, wav_file):
    result = daemon.rpc("analyze_upload", {
        "filename": "chosen-in-browser.wav",
        "data": base64.b64encode(wav_file.read_bytes()).decode("ascii")})
    from pathlib import Path
    assert result["report"]["duration_s"] == pytest.approx(1.0)
    assert result["report"]["sample_rate"] == 48000
    assert result["path"].endswith("chosen-in-browser.wav")
    assert not Path(result["path"]).exists(), "temporary upload was not removed"


def test_target_matching_returns_editable_real_filters(daemon):
    freqs = np.geomspace(20, 20000, 256).tolist()
    spectrum = {"freqs": freqs, "db": [0.0] * len(freqs)}
    result = daemon.rpc("suggest_eq", {"spectrum": spectrum,
                                       "target": "harman-overear"})
    assert result["suggested_filters"]
    assert all(20 <= f["freq"] <= 20000 for f in result["suggested_filters"])
    assert all(abs(f["gain_db"]) <= 6 for f in result["suggested_filters"])


def test_deleting_active_custom_profile_falls_back_to_flat(daemon):
    profile = {"id": "temporary", "name": "Temporary", "eq": {"filters": []}}
    daemon.rpc("save_profile", {"profile": profile})
    daemon.rpc("set_profile", {"profile": "temporary"})
    result = daemon.rpc("delete_profile", {"profile": "temporary"})
    assert result["active_profile"] == "flat"
    assert daemon.state.active_profile == "flat"


def test_start_button_uses_the_real_rt_launcher(daemon, monkeypatch):
    from eqforge.system import rthost
    launched = SimpleNamespace(proc=object(), pid=1234, rt="/tmp/eqforge-rt",
                               wrapper=[])
    monkeypatch.setattr(rthost, "is_running", lambda: False)
    monkeypatch.setattr(rthost, "start", lambda channels: launched)
    monkeypatch.setattr(daemon.state, "sync_rt", lambda: True)
    result = daemon.rpc("start_rt", {"channels": 2})
    assert result["running"] is True
    assert result["pid"] == 1234
    assert daemon._rt_proc is launched.proc


def test_doctor_has_a_json_result_for_the_gui(daemon):
    result = daemon.rpc("doctor", {})
    assert len(result["checks"]) >= 8
    assert {c["status"] for c in result["checks"]} <= {"PASS", "WARN"}
    assert isinstance(result["healthy"], bool)
