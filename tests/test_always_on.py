"""`eqforge system always-on`: routing policy, not routing repair.

The rule this installs tells WirePlumber to create application streams on
EQForge's inputs, which is the only way to remove the unprocessed onset
completely (a daemon can only react *after* the stream exists). The tests pin
the safety properties that make it reasonable to write a policy file on a
user's machine at all:

  * nothing is written when there is no node to target;
  * the file is idempotent and never silently overwrites an edited version;
  * `off` really removes it;
  * a WirePlumber that would ignore the file is refused, not obeyed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eqforge.errors import SystemError
from eqforge.system import always_on as ao


@pytest.fixture
def fake_graph(monkeypatch):
    """Pretend the rt host's JACK client is in the graph."""
    monkeypatch.setattr(ao, "detect_node", lambda: "eqforge")
    monkeypatch.setattr(ao, "_supports_node_rules", lambda: True)
    monkeypatch.setattr(ao, "wireplumber_version", lambda: (0, 5))
    return "eqforge"


def _file() -> Path:
    return ao.rule_file()


def test_enable_writes_a_node_rule(fake_graph):
    report = ao.enable()
    assert report["status"] == "created"
    assert report["node"] == "eqforge"
    text = _file().read_text()
    assert 'target.object = "eqforge"' in text
    assert 'media.class = "Stream/Output/Audio"' in text
    # It must not touch microphones or anything else that is not app playback.
    assert "Stream/Input/Audio" not in text


def test_enable_is_idempotent(fake_graph):
    ao.enable()
    before = _file().read_text()
    report = ao.enable()
    assert report["status"] == "same"
    assert _file().read_text() == before


def test_enable_preserves_a_hand_edited_file(fake_graph):
    ao.enable()
    _file().write_text("# my edits\n" + _file().read_text())
    report = ao.enable()
    assert report["status"] == "updated"
    assert report["backup"] is not None
    assert Path(report["backup"]).exists()
    assert "my edits" in Path(report["backup"]).read_text()


def test_enable_without_a_node_is_refused(monkeypatch):
    monkeypatch.setattr(ao, "detect_node", lambda: None)
    monkeypatch.setattr(ao, "_supports_node_rules", lambda: True)
    with pytest.raises(SystemError) as err:
        ao.enable()
    assert "no EQForge node" in err.value.message
    assert err.value.hint
    assert not _file().exists()


def test_enable_refuses_an_unknown_node(monkeypatch):
    class View:
        def node_id(self, name):
            return None

    monkeypatch.setattr(ao, "_supports_node_rules", lambda: True)
    monkeypatch.setattr(ao.pw, "pipewire_available", lambda: True)
    monkeypatch.setattr(ao.pw, "snapshot", lambda: View())
    with pytest.raises(SystemError) as err:
        ao.enable("nope")
    assert "not in the audio graph" in err.value.message
    assert not _file().exists()


def test_old_wireplumber_is_refused_unless_forced(monkeypatch):
    monkeypatch.setattr(ao, "detect_node", lambda: "eqforge")
    monkeypatch.setattr(ao, "_supports_node_rules", lambda: False)
    monkeypatch.setattr(ao, "wireplumber_version", lambda: (0, 4))
    with pytest.raises(SystemError) as err:
        ao.enable()
    assert "0.4" in err.value.message
    assert not _file().exists()

    report = ao.enable(force=True)
    assert report["status"] == "created"
    assert _file().exists()


def test_disable_removes_the_rule(fake_graph):
    ao.enable()
    res = ao.disable()
    assert res["removed"] is True
    assert not _file().exists()
    # disabling twice is not an error
    assert ao.disable()["removed"] is False


def test_status_reports_the_installed_target(fake_graph):
    ao.enable()
    st = ao.status()
    assert st["installed"] is True
    assert st["node"] == "eqforge"
    assert st["node_present"] is True
    assert st["ok"] is True


def test_status_flags_a_target_that_disappeared(monkeypatch):
    monkeypatch.setattr(ao, "_supports_node_rules", lambda: True)
    monkeypatch.setattr(ao, "detect_node", lambda: "eqforge_filter")
    ao.enable()
    monkeypatch.setattr(ao, "detect_node", lambda: None)   # host stopped
    st = ao.status()
    assert st["installed"] is True
    assert st["node_present"] is False
    assert st["ok"] is False


def test_render_rule_only_targets_playback_streams():
    text = ao.render_rule("eqforge_filter")
    assert text.count("matches") == 1
    assert 'target.object = "eqforge_filter"' in text
    assert "Stream/Output/Audio" in text


def test_cli_status_and_off(fake_graph, capsys):
    from eqforge.cli import commands_system as cs
    import types

    args = types.SimpleNamespace(action="always-on", autostart_action="on",
                                 node=None, now=False, force=False, json=False,
                                 verbose=False, quiet=False, rt_binary=None,
                                 channels=None, manage=False, auto_wire=None)
    assert cs.cmd_system(args) == 0
    assert "always-on routing" in capsys.readouterr().out

    args.autostart_action = "status"
    assert cs.cmd_system(args) == 0
    assert "eqforge" in capsys.readouterr().out

    args.autostart_action = "off"
    assert cs.cmd_system(args) == 0
    assert "removed" in capsys.readouterr().out
