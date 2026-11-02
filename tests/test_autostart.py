"""Boot/login autostart, and the daemon's new supervisory jobs.

Covers `eqforge system autostart on|off|status`, the systemd user units it
generates (which must match *this* install: venv interpreter, in-tree or
packaged rt host, pw-jack wrapper), and the daemon-side auto-wire /
manage-rt behaviour that stops the user having to re-run `system wire`
every time a player restarts a stream.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import types
from pathlib import Path

import pytest

from eqforge import paths
from eqforge.cli import commands_system as cs
from eqforge.daemon.server import Daemon
from eqforge.system import jack, module_config as mc, rthost, wiring


def _host(tmp_path: Path, name: str = "eqforge-rt") -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexec sleep 5\n")
    p.chmod(0o755)
    return p


def _autostart_args(action="on", **kw):
    base = dict(action="autostart", autostart_action=action, rt_binary=None,
                channels=None, manage=False, auto_wire=None, json=False,
                verbose=False, quiet=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _unit_dir() -> Path:
    return Path(paths.home()) / ".config/systemd/user"


def _read(name: str) -> str:
    return (_unit_dir() / name).read_text()


# --------------------------------------------------------------------------
# unit rendering
# --------------------------------------------------------------------------

def test_rt_unit_carries_the_wrapper_and_absolute_paths():
    argv = ["/usr/bin/pw-jack", "/home/u/eqforge/rthost/build/eqforge-rt",
            "--name", "eqforge", "--socket", "/run/user/1000/eqforge/rt.sock",
            "--config", "/home/u/.config/eqforge/active-dsp.json",
            "--channels", "2"]
    unit = mc.render_rt_unit(argv)
    assert f"ExecStart={' '.join(argv)}" in unit
    assert "Restart=on-failure" in unit
    assert "Nice=-11" in unit
    assert "After=pipewire.service" in unit          # a JACK client needs it
    assert "WantedBy=default.target" in unit
    assert "Environment=XDG_RUNTIME_DIR=%t" in unit  # %% escaped correctly


def test_daemon_unit_uses_this_interpreter_and_enables_auto_wire():
    argv = ["/home/u/eqforge/.venv/bin/python", "-m", "eqforge", "daemon",
            "run", "--foreground", "--auto-wire"]
    unit = mc.render_daemon_unit(argv, "/home/u/eqforge",
                                 "/home/u/eqforge/.venv/bin:/usr/bin:/bin")
    assert f"ExecStart={' '.join(argv)}" in unit
    assert "Environment=PYTHONPATH=/home/u/eqforge" in unit
    assert "Environment=PATH=/home/u/eqforge/.venv/bin:/usr/bin:/bin" in unit
    assert "After=pipewire.service wireplumber.service eqforge-rt.service" \
        in unit
    assert "Restart=on-failure" in unit


def test_unit_paths_with_spaces_are_quoted():
    unit = mc.render_rt_unit(["/usr/bin/pw-jack", "/home/a b/eqforge-rt",
                              "--name", "eqforge"])
    assert "'/home/a b/eqforge-rt'" in unit


def test_unit_paths_are_absolute_for_the_unit_dir():
    p = mc.autostart_unit_paths("/home/u")
    assert p["rt"] == Path("/home/u/.config/systemd/user/eqforge-rt.service")
    assert p["daemon"] == Path(
        "/home/u/.config/systemd/user/eqforge-daemon.service")


# --------------------------------------------------------------------------
# `system autostart on`
# --------------------------------------------------------------------------

def test_autostart_on_writes_both_units(isolated_home, tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    rc = cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    assert rc == 0
    rt_unit = _read("eqforge-rt.service")
    assert str(host) in rt_unit
    daemon_unit = _read("eqforge-daemon.service")
    assert "--auto-wire" in daemon_unit
    assert os.path.realpath(str(Path(daemon_unit.split("ExecStart=")[1]
                                      .split()[0])))


def test_autostart_on_wraps_with_pw_jack_when_pipewire_provides_jack(
        isolated_home, tmp_path, monkeypatch):
    """The wrapper is what made `pw-jack eqforge system start` necessary."""
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "boot_wrapper",
                        lambda *a, **k: ["/usr/bin/pw-jack"])

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    exec_line = [l for l in _read("eqforge-rt.service").splitlines()
                 if l.startswith("ExecStart=")][0]
    assert exec_line.startswith("ExecStart=/usr/bin/pw-jack ")
    assert str(host) in exec_line


def _info(**kw):
    base = dict(pipewire_jack=True, jackd=True, pipewire_running=True,
                jackd_running=False, server_socket=None, conflict=False,
                hazard=True, usable=True, detail="test", fix="")
    base.update(kw)
    return jack.JackInfo(**base)


def test_boot_wrapper_ignores_what_happens_to_be_running_now():
    """A unit is written once - perhaps from an ssh session with no audio, or
    while a hand-started jackd is up - and executed at every login. Baking in
    the answer to "is PipeWire serving JACK *now*" yields a unit that fails
    with status 17 on every boot, so the unit only depends on what is
    installed. The interactive path keeps refusing to wrap over a live jackd.
    """
    now = _info(pipewire_running=False, jackd_running=True)
    pw = lambda n: "/usr/bin/pw-jack"                      # noqa: E731
    assert jack.boot_wrapper(now, which=pw) == ["/usr/bin/pw-jack"]
    assert jack.launch_wrapper(now, which=pw) == []


def test_boot_wrapper_is_empty_without_pipewire_jack():
    assert jack.boot_wrapper(_info(pipewire_jack=False),
                             which=lambda n: "/usr/bin/pw-jack") == []
    assert jack.boot_wrapper(_info(), which=lambda n: None) == []


def test_autostart_on_wraps_even_with_no_audio_session(isolated_home,
                                                       tmp_path, monkeypatch,
                                                       capsys):
    """End to end through the real `boot_wrapper`: PipeWire not running and a
    jackd up *while generating* must still produce a pw-jack unit."""
    host = _host(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "pw-jack").write_text("#!/bin/sh\nexec \"$@\"\n")
    (fake / "pw-jack").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "inspect",
                        lambda **kw: _info(pipewire_running=False,
                                           jackd_running=True))

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    exec_line = [l for l in _read("eqforge-rt.service").splitlines()
                 if l.startswith("ExecStart=")][0]
    assert exec_line.startswith(f"ExecStart={fake / 'pw-jack'} ")
    assert str(host) in exec_line
    # the decision is announced, not left for the user to reverse-engineer
    assert "jackd is running right now" in capsys.readouterr().out


def test_autostart_on_persists_auto_wire_in_config(isolated_home, tmp_path,
                                                   monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    cfg = json.loads(paths.config_file().read_text())
    assert cfg["auto_wire"] is True

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host),
                                     auto_wire=False))
    cfg = json.loads(paths.config_file().read_text())
    assert cfg["auto_wire"] is False
    assert "--auto-wire" not in _read("eqforge-daemon.service")


def test_autostart_on_enables_and_starts_the_units(isolated_home, tmp_path,
                                                   monkeypatch):
    host = _host(tmp_path)
    calls = []
    monkeypatch.setattr(cs, "_systemctl",
                        lambda a, **k: (calls.append(a), (0, ""))[1])
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    assert ["daemon-reload"] in calls
    assert ["enable", "--now", "eqforge-rt.service",
            "eqforge-daemon.service"] in calls


def test_autostart_on_without_systemd_still_writes_the_files(
        isolated_home, tmp_path, monkeypatch, capsys):
    """No systemd (or no user bus) must not lose the generated units."""
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl",
                        lambda a, **k: (1, "Failed to connect to bus"))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    rc = cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    assert rc == 1
    text = capsys.readouterr().out
    assert "systemctl --user enable --now" in text
    assert (_unit_dir() / "eqforge-rt.service").exists()


def test_autostart_on_requires_a_usable_rt_host(isolated_home, tmp_path,
                                                monkeypatch):
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(rthost, "SYSTEM_PATHS", ())
    monkeypatch.setattr(rthost, "in_tree_rt", lambda: tmp_path / "absent")
    monkeypatch.setattr(rthost.shutil, "which", lambda n: None)

    with pytest.raises(cs.SystemError) as e:
        cs.cmd_autostart(_autostart_args("on"))
    assert "not found" in e.value.message
    assert not (_unit_dir() / "eqforge-rt.service").exists()


def test_autostart_on_backs_up_a_unit_it_changes(isolated_home, tmp_path,
                                                 monkeypatch):
    host = _host(tmp_path)
    other = _host(tmp_path, name="other-rt")
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    before = _read("eqforge-rt.service")
    cs.cmd_autostart(_autostart_args("on", rt_binary=str(other)))

    assert _read("eqforge-rt.service") != before
    backups = list(_unit_dir().glob("eqforge-rt.service.bak.*"))
    assert backups, "the previous unit was replaced with no backup"
    assert str(host) in backups[0].read_text()


def test_autostart_on_is_idempotent(isolated_home, tmp_path, monkeypatch,
                                    capsys):
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, ""))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])

    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    capsys.readouterr()
    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    text = capsys.readouterr().out

    assert "unchanged" in text
    assert not list(_unit_dir().glob("*.bak.*"))


def test_autostart_off_removes_units_and_clears_config(isolated_home,
                                                       tmp_path, monkeypatch):
    host = _host(tmp_path)
    calls = []
    monkeypatch.setattr(cs, "_systemctl",
                        lambda a, **k: (calls.append(a), (0, ""))[1])
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])
    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))

    assert cs.cmd_autostart(_autostart_args("off")) == 0

    assert not (_unit_dir() / "eqforge-rt.service").exists()
    assert not (_unit_dir() / "eqforge-daemon.service").exists()
    assert json.loads(paths.config_file().read_text())["auto_wire"] is False
    assert any(a[0] == "disable" for a in calls)


def test_autostart_status_reports_units_and_flag(isolated_home, tmp_path,
                                                 monkeypatch, capsys):
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, "enabled"))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])
    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    capsys.readouterr()

    assert cs.cmd_autostart(_autostart_args("status")) == 0
    text = capsys.readouterr().out
    assert "eqforge-rt.service" in text
    assert "eqforge-daemon.service" in text
    assert "auto-wire" in text


def test_autostart_status_works_with_nothing_installed(isolated_home,
                                                       monkeypatch, capsys):
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (127, "not found"))
    assert cs.cmd_autostart(_autostart_args("status")) == 0
    text = capsys.readouterr().out
    assert "absent" in text


def test_autostart_rejects_an_unknown_action(isolated_home):
    with pytest.raises(cs.EQForgeError):
        cs.cmd_autostart(_autostart_args("sideways"))


def test_systemctl_helper_reports_a_missing_binary(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda n: None)
    rc, text = cs._systemctl(["daemon-reload"])
    assert rc == 127 and "not found" in text


def test_systemctl_helper_survives_a_timeout(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda n: "/usr/bin/systemctl")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(cs.subprocess, "run", slow)
    rc, text = cs._systemctl(["is-active", "x"])
    assert rc == 1 and "timed out" in text


BUS_ERROR = ("Failed to connect to bus: No such file or directory\n"
             "See the log for details.")


def test_unit_word_passes_a_known_state_through(monkeypatch):
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, "enabled"))
    assert cs._unit_word("is-enabled", "u", cs._ENABLED_WORDS) == "enabled"
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (1, "static\n"))
    assert cs._unit_word("is-enabled", "u", cs._ENABLED_WORDS) == "static"
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (3, "inactive"))
    assert cs._unit_word("is-active", "u", cs._ACTIVE_WORDS) == "inactive"


def test_unit_word_collapses_a_bus_error_to_one_word(monkeypatch):
    """No user bus (ssh without a session) must not leak prose into a table."""
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (1, BUS_ERROR))
    assert cs._unit_word("is-enabled", "u",
                         cs._ENABLED_WORDS) == "unreachable"
    assert cs._unit_word("is-active", "u", cs._ACTIVE_WORDS) == "unreachable"


def test_unit_word_reports_n_a_without_systemd(monkeypatch):
    monkeypatch.setattr(cs, "_systemctl",
                        lambda a, **k: (127, "systemctl not found"))
    assert cs._unit_word("is-enabled", "u", cs._ENABLED_WORDS) == "n/a"


def test_autostart_status_table_survives_an_unreachable_bus(isolated_home,
                                                            tmp_path,
                                                            monkeypatch,
                                                            capsys):
    host = _host(tmp_path)
    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (0, "enabled"))
    monkeypatch.setattr(jack, "boot_wrapper", lambda *a, **k: [])
    cs.cmd_autostart(_autostart_args("on", rt_binary=str(host)))
    capsys.readouterr()

    monkeypatch.setattr(cs, "_systemctl", lambda a, **k: (1, BUS_ERROR))
    assert cs.cmd_autostart(_autostart_args("status")) == 0

    text = capsys.readouterr().out
    assert "Failed to connect" not in text
    assert text.count("unreachable") == 4      # both columns, both units
    assert "…" not in text                     # nothing was clipped


# --------------------------------------------------------------------------
# the launcher the units and the CLI share
# --------------------------------------------------------------------------

def test_launch_argv_matches_the_unit_and_the_manual_command(tmp_path):
    host = _host(tmp_path)
    argv = rthost.launch_argv(host, tmp_path / "rt.sock", tmp_path / "cfg.json",
                              2, wrapper=["/usr/bin/pw-jack"])
    assert argv == ["/usr/bin/pw-jack", str(host), "--name", "eqforge",
                    "--socket", str(tmp_path / "rt.sock"),
                    "--config", str(tmp_path / "cfg.json"),
                    "--channels", "2"]


def test_launch_argv_asks_jack_whether_to_wrap(tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(jack, "launch_wrapper",
                        lambda *a, **k: ["/usr/bin/pw-jack"])
    argv = rthost.launch_argv(host, tmp_path / "s", tmp_path / "c", 2)
    assert argv[0] == "/usr/bin/pw-jack"

    monkeypatch.setattr(jack, "launch_wrapper", lambda *a, **k: [])
    argv = rthost.launch_argv(host, tmp_path / "s", tmp_path / "c", 2)
    assert argv[0] == str(host)


def test_is_running_detects_a_real_control_socket(tmp_path):
    sock = tmp_path / "rt.sock"
    assert rthost.is_running(sock) is False

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)
    try:
        assert rthost.is_running(sock) is True
    finally:
        srv.close()


def test_is_running_rejects_a_stale_socket_file(tmp_path):
    """A leftover socket file with nobody listening is not a running host."""
    sock = tmp_path / "rt.sock"
    sock.write_text("")
    assert rthost.is_running(sock) is False


def test_start_publishes_a_config_when_there_is_none(isolated_home, tmp_path,
                                                     monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(rthost, "resolve_binary", lambda o=None: host)
    sock = paths.rt_socket()
    real_popen = subprocess.Popen        # captured before it is patched

    def fake_popen(argv, **kw):
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.write_text("")            # pretend the host opened its socket
        return real_popen(["sleep", "1"],
                          **{k: v for k, v in kw.items()
                             if k in ("stdout", "stderr", "stdin")})

    monkeypatch.setattr(rthost.subprocess, "Popen", fake_popen)
    launched = rthost.start(rt_binary=host, wait=2.0)

    assert launched.pid > 0
    assert paths.active_dsp_file().exists(), "the host would start unconfigured"
    json.loads(paths.active_dsp_file().read_text())   # and it is valid JSON
    rthost.stop(launched.proc)


def test_stop_terminates_a_managed_host_and_clears_the_socket(isolated_home,
                                                             tmp_path):
    proc = subprocess.Popen(["sleep", "30"])
    sock = paths.rt_socket()
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.write_text("")

    assert rthost.stop(proc, sock) is True
    assert proc.wait(timeout=5) is not None
    assert not sock.exists()


# --------------------------------------------------------------------------
# daemon: auto-wire
# --------------------------------------------------------------------------

@pytest.fixture
def daemon(isolated_home):
    d = Daemon(auto_wire=True, tick=0.01, rt_retry=0.0)
    yield d
    d.stop_event.set()


def test_daemon_reads_auto_wire_from_config(isolated_home):
    paths.ensure_dirs()
    paths.config_file().write_text(json.dumps({"auto_wire": True}))
    d = Daemon()
    assert d.auto_wire is True


def test_daemon_flag_overrides_config(isolated_home):
    paths.ensure_dirs()
    paths.config_file().write_text(json.dumps({"auto_wire": True}))
    assert Daemon(auto_wire=False).auto_wire is False


def test_daemon_defaults_to_not_touching_the_graph(isolated_home):
    """It is spawned implicitly by ordinary commands; it must not rewire
    someone's audio graph just because they ran `eqforge status`."""
    assert Daemon().auto_wire is False
    assert Daemon().manage_rt is False


def test_auto_wire_skips_an_unchanged_graph(daemon, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring, "stream_signature", lambda g: "sig-1")
    monkeypatch.setattr(wiring, "wire_graph",
                        lambda rt, graph=None, **k: calls.append(graph) or
                        wiring.WireReport(rt_node="eqforge"))

    daemon._auto_wire(object())
    daemon._auto_wire(object())

    assert len(calls) == 1, "an unchanged graph was rewired again"


def test_auto_wire_reruns_when_the_signature_changes(daemon, monkeypatch):
    calls = []
    sigs = iter(["sig-1", "sig-2", "sig-2"])
    monkeypatch.setattr(wiring, "stream_signature", lambda g: next(sigs))
    monkeypatch.setattr(wiring, "wire_graph",
                        lambda rt, graph=None, **k: calls.append(1) or
                        wiring.WireReport(rt_node="eqforge"))

    for _ in range(3):
        daemon._auto_wire(object())

    assert len(calls) == 2


def test_auto_wire_retries_while_the_host_is_down(daemon, monkeypatch):
    """A failed pass must not be cached as 'up to date'."""
    calls = []
    monkeypatch.setattr(wiring, "stream_signature", lambda g: "sig-1")
    monkeypatch.setattr(wiring, "wire_graph",
                        lambda rt, graph=None, **k: calls.append(1) or
                        wiring.WireReport(rt_node=None,
                                          notes=["rt host not running"]))

    daemon._auto_wire(object())
    daemon._auto_wire(object())

    assert len(calls) == 2


def test_auto_wire_publishes_an_event_when_it_moves_a_stream(daemon,
                                                             monkeypatch):
    monkeypatch.setattr(wiring, "stream_signature", lambda g: "sig-1")
    monkeypatch.setattr(
        wiring, "wire_graph",
        lambda rt, graph=None, **k: wiring.WireReport(
            rt_node="eqforge", sink="sink", streams_moved=2, outputs_ok=2))

    events = daemon.bus.subscribe()
    daemon._auto_wire(object())

    assert any(e["event"] == "graph_wired"
               and e["data"]["streams_moved"] == 2 for e in events)


def test_auto_wire_does_nothing_without_pipewire(daemon, monkeypatch):
    called = []
    monkeypatch.setattr(wiring, "stream_signature", lambda g: None)
    monkeypatch.setattr(wiring, "wire_graph",
                        lambda *a, **k: called.append(1))
    daemon._auto_wire(object())
    assert called == []


# --------------------------------------------------------------------------
# daemon: rt host supervision (--manage-rt)
# --------------------------------------------------------------------------

def test_ensure_rt_host_starts_it_when_absent(daemon, monkeypatch):
    daemon.manage_rt = True
    started = []
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: False)
    monkeypatch.setattr(rthost, "start",
                        lambda **k: started.append(k) or rthost.LaunchResult(
                            pid=4242, rt=Path("/x/eqforge-rt"), argv=[],
                            wrapper=[], socket=Path("/x/rt.sock"),
                            logfile=Path("/x/rt.out"), proc=None))

    daemon._ensure_rt_host()

    assert started and started[0]["channels"] == daemon.rt_channels
    assert daemon._rt_proc is None        # proc=None in the stub
    assert daemon._wire_sig is None       # forces a fresh wiring pass


def test_ensure_rt_host_leaves_an_externally_started_host_alone(daemon,
                                                                monkeypatch):
    daemon.manage_rt = True
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: True)

    def never(**k):
        raise AssertionError("systemd owns this host; the daemon must not")

    monkeypatch.setattr(rthost, "start", never)
    daemon._ensure_rt_host()          # must not raise


def test_ensure_rt_host_backs_off_after_a_failure(daemon, monkeypatch):
    daemon.manage_rt = True
    daemon.rt_retry = 60.0
    attempts = []
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: False)

    def fail(**k):
        attempts.append(1)
        raise cs.SystemError("rt host failed to start (exit 2)")

    monkeypatch.setattr(rthost, "start", fail)
    daemon._ensure_rt_host()
    daemon._ensure_rt_host()

    assert len(attempts) == 1, "a broken host was retried without backoff"


class _FakeProc:
    """Stands in for subprocess.Popen without spawning anything."""

    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 0


def _launch(pid=4242, proc=None):
    return rthost.LaunchResult(pid=pid, rt=Path("/x/eqforge-rt"), argv=[],
                               wrapper=[], socket=Path("/x/rt.sock"),
                               logfile=Path("/x/rt.out"), proc=proc)


def test_a_relayed_quit_is_remembered_as_deliberate(daemon):
    """`eqforge system stop` sends its quit *through* the daemon, so the
    daemon knows the host did not crash and must not undo the user."""
    seen = []
    daemon.state._tell_rt = lambda cmd: seen.append(cmd) or {"ok": True}
    daemon._rt_proc = _FakeProc()

    daemon.rpc("rt_command", {"cmd": {"cmd": "quit"}})

    assert seen == [{"cmd": "quit"}]        # the host really was told
    assert daemon._rt_stop_requested is True
    assert daemon._rt_proc is None          # nothing of ours is running


def test_a_status_relay_does_not_end_supervision(daemon):
    daemon.state._tell_rt = lambda cmd: {"ok": True}
    daemon.rpc("rt_command", {"cmd": {"cmd": "status"}})
    assert daemon._rt_stop_requested is False


def test_daemon_does_not_resurrect_a_deliberately_stopped_host(daemon,
                                                               monkeypatch):
    daemon.manage_rt = True
    daemon._rt_stop_requested = True
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: False)

    def never(**k):
        raise AssertionError("the user stopped it on purpose; leave it down")

    monkeypatch.setattr(rthost, "start", never)
    daemon._ensure_rt_host()
    daemon._ensure_rt_host()                # and still not on the next tick


def test_supervision_resumes_when_a_host_comes_back(daemon, monkeypatch):
    """Started again by hand or by its systemd unit -> adopt, and resume
    supervising crashes from then on."""
    daemon.manage_rt = True
    daemon._rt_stop_requested = True
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(rthost, "start", lambda **k: started.append(k))

    daemon._ensure_rt_host()

    assert started == []                    # someone else owns this host
    assert daemon._rt_stop_requested is False


def test_a_crash_after_a_restart_is_supervised_again(daemon, monkeypatch):
    daemon.manage_rt = True
    daemon._rt_stop_requested = True
    up = [True]
    monkeypatch.setattr(rthost, "is_running", lambda *a, **k: up[0])
    started = []
    monkeypatch.setattr(rthost, "start",
                        lambda **k: started.append(k) or _launch())

    daemon._ensure_rt_host()                # host is back, adopted
    assert started == []
    up[0] = False                           # ...and now it genuinely dies
    daemon._ensure_rt_host()

    assert started, "a real crash was not supervised after a restart"


def test_stop_terminates_only_a_host_the_daemon_launched(daemon):
    proc = subprocess.Popen(["sleep", "30"])
    daemon._rt_proc = proc
    daemon.stop()
    assert proc.wait(timeout=5) is not None


# --------------------------------------------------------------------------
# daemon RPC surface
# --------------------------------------------------------------------------

def test_rpc_status_exposes_the_supervision_flags(daemon):
    s = daemon.rpc("status")
    assert s["auto_wire"] is True
    assert s["manage_rt"] is False


def test_rpc_set_auto_wire_persists(daemon):
    r = daemon.rpc("set_auto_wire", {"value": True})
    assert r["auto_wire"] is True and daemon.auto_wire is True
    assert json.loads(paths.config_file().read_text())["auto_wire"] is True

    daemon.rpc("set_auto_wire", {"value": False})
    assert daemon.auto_wire is False
    assert json.loads(paths.config_file().read_text())["auto_wire"] is False


def test_rpc_set_auto_wire_validates_params(daemon):
    with pytest.raises(cs.EQForgeError):
        daemon.rpc("set_auto_wire", {})


def test_rpc_wire_returns_the_report(daemon, monkeypatch):
    monkeypatch.setattr(wiring, "wire_graph",
                        lambda rt, graph=None, **k: wiring.WireReport(
                            rt_node="eqforge", sink="sink", outputs_ok=2,
                            streams_moved=1, already_wired=3))
    daemon._wire_sig = "stale"

    r = daemon.rpc("wire")

    assert r["streams_moved"] == 1 and r["already_wired"] == 3
    assert r["sink"] == "sink"
    assert daemon._wire_sig is None     # next tick re-checks from scratch


def test_rpc_wire_survives_no_pipewire(daemon, monkeypatch):
    monkeypatch.setattr(wiring.pw, "pipewire_available", lambda: False)
    r = daemon.rpc("wire")
    assert r["errors"] and r["changed"] is False


def test_daemon_flags_reach_the_spawned_process(monkeypatch):
    """`daemon start --auto-wire` must not be silently dropped."""
    from eqforge.daemon import client
    seen = {}
    monkeypatch.setattr(client, "spawn_daemon",
                        lambda extra=None: seen.setdefault("args", extra))
    monkeypatch.setattr(client, "daemon_running", lambda *a, **k: False)
    monkeypatch.setattr(client, "http_base", lambda: None)

    args = types.SimpleNamespace(action="start", auto_wire=True,
                                 manage_rt=True, channels=4, no_http=False,
                                 port=None, json=False)
    with pytest.raises(cs.EQForgeError):        # never comes up in the test
        cs.cmd_daemon(args)

    assert seen["args"] == ["--auto-wire", "--manage-rt", "--channels", "4"]


def test_daemon_flags_helper_is_tri_state():
    assert cs._daemon_flags(types.SimpleNamespace(
        auto_wire=None, manage_rt=False, channels=None)) == []
    assert cs._daemon_flags(types.SimpleNamespace(
        auto_wire=True, manage_rt=False, channels=None)) == ["--auto-wire"]
    assert cs._daemon_flags(types.SimpleNamespace(
        auto_wire=False, manage_rt=False, channels=None)) == ["--no-auto-wire"]
