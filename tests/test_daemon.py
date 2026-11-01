"""Daemon RPC tests (in-process, no sockets) + client edge cases."""
from __future__ import annotations

import json

import pytest

from eqforge.daemon.server import Daemon, EventBus
from eqforge.daemon.state import EngineState
from eqforge.errors import EQForgeError


@pytest.fixture
def daemon(isolated_home):
    d = Daemon.__new__(Daemon)
    d.state = EngineState()
    d.bus = EventBus()
    d.stop_event = __import__("threading").Event()
    import time as _t
    d.started = _t.time()
    d.state.ensure_active_config()
    return d


def test_ping(daemon):
    r = daemon.rpc("ping")
    assert r["ok"] and "version" in r


def test_status_shape(daemon):
    st = daemon.rpc("status")
    assert st["active_profile"] == "flat"
    assert st["slot_a"] == "flat"
    assert not st["bypass"]


def test_set_profile_and_ab(daemon):
    daemon.rpc("set_profile", {"profile": "gaming"})
    st = daemon.rpc("status")
    assert st["active_profile"] == "gaming"

    daemon.rpc("set_profile", {"profile": "voice", "slot": "B"})
    st = daemon.rpc("status")
    assert st["slot_b"] == "voice" and st["active_slot"] == "A"

    daemon.rpc("ab", {"slot": "B"})
    assert daemon.rpc("status")["active_profile"] == "voice"

    daemon.rpc("ab", {})   # toggle back
    assert daemon.rpc("status")["active_profile"] == "gaming"


def test_set_profile_rejects_unknown(daemon):
    with pytest.raises(EQForgeError):
        daemon.rpc("set_profile", {"profile": "nope"})


def test_bypass_toggle(daemon):
    st = daemon.rpc("bypass", {})
    assert st["bypass"] is True
    st = daemon.rpc("bypass", {"value": False})
    assert st["bypass"] is False


def test_active_dsp_file_written(daemon, isolated_home):
    from eqforge import paths
    daemon.rpc("set_profile", {"profile": "bass-boost"})
    p = paths.active_dsp_file()
    assert p.exists()
    cfg = json.loads(p.read_text())
    assert cfg["preamp_db"] < 0        # auto preamp for boosts
    assert len(cfg["eq"]["filters"]) == 3


def test_save_and_delete_profile(daemon):
    prof = {"format": "eqforge.profile", "format_version": 2,
            "id": "rpc-test", "name": "RPC", "preamp": {"mode": "auto"},
            "eq": {"filters": [{"type": "peak", "freq": 500, "gain_db": 2,
                                "q": 1, "enabled": True}]},
            "limiter": {"enabled": True, "ceiling_db": -1}}
    daemon.rpc("save_profile", {"profile": prof})
    ids = [p["id"] for p in daemon.rpc("list_profiles")["profiles"]]
    assert "rpc-test" in ids
    daemon.rpc("set_profile", {"profile": "rpc-test"})
    daemon.rpc("delete_profile", {"profile": "rpc-test"})
    ids = [p["id"] for p in daemon.rpc("list_profiles")["profiles"]]
    assert "rpc-test" not in ids


def test_resolve_profile_rpc(daemon):
    r = daemon.rpc("resolve_profile", {"profile": "headphone-crossfeed"})
    assert r["dsp"]["crossfeed"]["enabled"] is True
    assert len(r["dsp"]["eq"]["filters"]) == 3  # inherited from base


def test_analyze_file_rpc(daemon, wav_file):
    r = daemon.rpc("analyze_file", {"path": str(wav_file)})
    rep = r["report"]
    assert rep["sample_rate"] == 48000
    assert rep["integrated_lufs"] < 0
    assert r["advice"]["findings"]


def test_analyze_file_missing(daemon):
    with pytest.raises(EQForgeError):
        daemon.rpc("analyze_file", {"path": "/nonexistent.wav"})


def test_unknown_method(daemon):
    with pytest.raises(EQForgeError):
        daemon.rpc("frobnicate")


def test_auto_switch_flag(daemon):
    r = daemon.rpc("auto_switch", {"value": True})
    assert r["auto_switch"] is True
    r = daemon.rpc("auto_switch", {})   # toggle
    assert r["auto_switch"] is False
