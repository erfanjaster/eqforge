"""Device/app matching rule tests."""
from __future__ import annotations

import json

import pytest

from eqforge.system.matching import Rule, collect_rules, select_profile
from tests.conftest import write_profile


def test_glob_and_substring_matching():
    r = Rule("hp", {"name": "*WH-1000*"})
    assert r.matches({"name": "alsa_output.usb-Sony_WH-1000XM4.stereo"})
    assert not r.matches({"name": "alsa_output.pci-analog"})

    r2 = Rule("dac", {"name": "dac"})   # substring, case-insensitive
    assert r2.matches({"name": "USB-DAC-101"})


def test_multi_key_and_semantics():
    r = Rule("bt", {"kind": "bluetooth", "name": "*buds*"})
    assert r.matches({"kind": "bluetooth", "name": "Galaxy Buds Pro"})
    assert not r.matches({"kind": "usb", "name": "Galaxy Buds Pro"})


def test_missing_key_never_matches():
    r = Rule("x", {"bluez_address": "AA:BB"})
    assert not r.matches({"name": "something"})


def test_priority_and_specificity():
    generic = Rule("gen", {"kind": "bluetooth"}, priority=0)
    specific = Rule("spec", {"kind": "bluetooth", "name": "*WH-1000*"},
                    priority=0)
    high = Rule("high", {"kind": "bluetooth"}, priority=10)
    ident = {"kind": "bluetooth", "name": "Sony WH-1000XM4"}
    assert select_profile(ident, [generic, specific]).profile == "spec"
    assert select_profile(ident, [generic, specific, high]).profile == "high"
    assert select_profile({"kind": "usb"}, [generic, specific]) is None


def test_collect_rules_from_profiles_and_config(tmp_path):
    from eqforge.profiles.store import ProfileStore
    write_profile(tmp_path, "office-dac",
                  matching={"devices": [
                      {"profile": "office-dac",
                       "match": {"name": "*Audio-GD*"}, "priority": 5}]})
    store = ProfileStore(user_dir=tmp_path)
    cfg = {"rules": [{"profile": "flat",
                      "match": {"kind": "hdmi"}, "priority": 1}]}
    rules = collect_rules(store, cfg)
    profiles = {r.profile for r in rules}
    assert "office-dac" in profiles      # from profile file
    assert "flat" in profiles            # from config
    hit = select_profile({"name": "alsa_output.usb-Audio-GD"}, rules)
    assert hit.profile == "office-dac"


def test_app_stream_matching():
    from eqforge.system.matching import match_stream
    rules = [
        Rule("gaming", {"binary": "steam*", "name": "*game*"}, priority=5),
        Rule("browser", {"binary": "firefox"}, priority=1),
    ]
    assert match_stream("firefox", "Firefox", "x", rules).profile == "browser"
    assert match_stream("steam_app123", "Game Audio", "y",
                        rules).profile == "gaming"
    assert match_stream("mpv", "music", "z", rules) is None
