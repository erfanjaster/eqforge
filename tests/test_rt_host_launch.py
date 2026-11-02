"""`eqforge system start` must never mistake a directory for the rt host.

Regression cover for a real failure: with `EQFORGE_RT` unset, `_rt_binary()`
built `Path("")` -> `PosixPath('.')`, which is truthy, exists, and passes
`os.access(p, os.X_OK)` because a traversable directory has its x bit set.
`subprocess.Popen(['.'])` then died with

    PermissionError: [Errno 13] Permission denied: '.'

as an unhandled traceback. These tests pin the guard (regular file + exec
bit), the actionable errors that replaced the traceback, and the JACK
transport detection that explains the *next* failure a user hits once the
path resolves (jack2 installed instead of pipewire-jack -> status 17).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from eqforge.cli import commands_system as cs
from eqforge.errors import SystemError
from eqforge.system import jack, rthost, wiring

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# helpers / fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def reap():
    """Kill any detached fake host a test started, so nothing is left behind."""
    procs: list = []
    yield procs
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            pass


def _socket_creating_host(tmp_path: Path) -> Path:
    """A stand-in eqforge-rt that opens its control socket, then idles."""
    return _write_host(tmp_path, """\
sock=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--socket" ]; then sock="$a"; fi
  prev="$a"
done
[ -n "$sock" ] && : > "$sock"
exec sleep 20
""")


def _fake_daemon(monkeypatch):
    """Stub the daemon client so `system start` needs no real daemon."""
    from eqforge.daemon import client
    monkeypatch.setattr(client, "daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(client, "spawn_daemon", lambda *a, **k: None)
    monkeypatch.setattr(client, "call", lambda *a, **k: {})
    return client


def _start_args(**kw):
    base = dict(action="start", rt_binary=None, channels=None, manage=False,
                json=False, verbose=False, quiet=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _stream(node_id: int, node_name: str):
    from eqforge.system.pipewire import Stream
    return Stream(id=node_id, name=node_name, node_name=node_name,
                  binary=node_name, media_class="Stream/Output/Audio",
                  direction="playback")


def _write_host(tmp_path: Path, body: str, name: str = "eqforge-rt",
                mode: int = 0o755) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(mode)
    return p


@pytest.fixture
def no_rt_installed(monkeypatch, tmp_path):
    """A machine with no eqforge-rt anywhere: no env, no build, no PATH.

    Patched on `rthost`, which owns the resolution logic; the CLI module
    only holds aliases to it, so patching the aliases would hide nothing.
    """
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    monkeypatch.setattr(rthost, "SYSTEM_PATHS", ())
    monkeypatch.setattr(rthost, "in_tree_rt",
                        lambda: tmp_path / "absent" / "eqforge-rt")
    monkeypatch.setattr(rthost.shutil, "which", lambda name: None)
    return tmp_path


@pytest.fixture
def fake_daemon(monkeypatch):
    return _fake_daemon(monkeypatch)


# --------------------------------------------------------------------------
# 1. the path-resolution guard
# --------------------------------------------------------------------------

def test_empty_env_var_does_not_resolve_to_the_cwd(no_rt_installed,
                                                   monkeypatch):
    """The exact reported bug: EQFORGE_RT unset/empty used to yield '.'."""
    monkeypatch.setenv("EQFORGE_RT", "")
    assert cs._rt_binary() is None
    assert Path(".") not in cs._rt_candidates()
    assert cs._rt_binary() != Path(".")


def test_a_directory_candidate_is_never_returned(no_rt_installed, tmp_path,
                                                 monkeypatch):
    """The user's path is considered - and then rejected, not executed."""
    monkeypatch.setenv("EQFORGE_RT", str(tmp_path))          # a directory
    assert tmp_path in cs._rt_candidates()
    assert cs._rt_binary() is None
    assert cs._rt_binary() != tmp_path


def test_directory_is_not_executable_even_though_x_bit_is_set(tmp_path):
    """Why `os.access(dir, X_OK)` alone is not a check: it returns True."""
    assert os.access(tmp_path, os.X_OK) is True      # the trap
    assert cs._is_executable_file(tmp_path) is False  # the fix


def test_non_executable_file_is_rejected(no_rt_installed, tmp_path,
                                         monkeypatch):
    host = _write_host(tmp_path, "exit 0", mode=0o644)
    monkeypatch.setenv("EQFORGE_RT", str(host))
    assert cs._rt_binary() is None
    assert cs._is_executable_file(host) is False


def test_missing_file_is_rejected(no_rt_installed, tmp_path, monkeypatch):
    monkeypatch.setenv("EQFORGE_RT", str(tmp_path / "nope"))
    assert cs._rt_binary() is None


def test_broken_symlink_is_rejected(no_rt_installed, tmp_path, monkeypatch):
    link = tmp_path / "eqforge-rt"
    link.symlink_to(tmp_path / "does-not-exist")
    monkeypatch.setenv("EQFORGE_RT", str(link))
    assert cs._rt_binary() is None


def test_real_executable_is_found(no_rt_installed, tmp_path, monkeypatch):
    host = _write_host(tmp_path, "exit 0")
    monkeypatch.setenv("EQFORGE_RT", str(host))
    assert cs._rt_binary() == host


def test_env_var_beats_in_tree_build(no_rt_installed, tmp_path, monkeypatch):
    env_host = _write_host(tmp_path, "exit 0", name="env-rt")
    tree_host = _write_host(tmp_path, "exit 0", name="tree-rt")
    monkeypatch.setattr(rthost, "in_tree_rt", lambda: tree_host)
    monkeypatch.setenv("EQFORGE_RT", str(env_host))
    assert cs._rt_binary() == env_host
    assert cs._rt_binary() != tree_host


def test_in_tree_build_is_used_when_no_env_var(no_rt_installed, tmp_path,
                                               monkeypatch):
    tree_host = _write_host(tmp_path, "exit 0", name="tree-rt")
    monkeypatch.setattr(rthost, "in_tree_rt", lambda: tree_host)
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    assert cs._rt_binary() == tree_host


def test_cli_flag_beats_env_var(no_rt_installed, tmp_path, monkeypatch):
    flag_host = _write_host(tmp_path, "exit 0", name="flag-rt")
    env_host = _write_host(tmp_path, "exit 0", name="env-rt")
    monkeypatch.setenv("EQFORGE_RT", str(env_host))
    assert cs._rt_binary(str(flag_host)) == flag_host


def test_path_lookup_is_the_last_resort(no_rt_installed, tmp_path,
                                        monkeypatch):
    on_path = _write_host(tmp_path, "exit 0")
    monkeypatch.setattr(cs.shutil, "which",
                        lambda name: str(on_path) if name == "eqforge-rt"
                        else None)
    assert cs._rt_binary() == on_path


def test_explicit_bad_override_raises_instead_of_falling_back(
        no_rt_installed, tmp_path, monkeypatch):
    """A user who names a path is told it is wrong, not silently overruled."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    packaged = _write_host(pkg_dir, "exit 0")
    monkeypatch.setattr(rthost, "SYSTEM_PATHS", (str(packaged),))

    with pytest.raises(SystemError) as e:
        cs._resolve_rt_binary(str(tmp_path))                 # directory
    assert "directory" in e.value.message
    assert e.value.exit_code == 6

    with pytest.raises(SystemError) as e:
        cs._resolve_rt_binary(str(tmp_path / "gone"))        # missing
    assert "missing file" in e.value.message

    not_exec = _write_host(tmp_path, "exit 0", name="noexec", mode=0o644)
    with pytest.raises(SystemError) as e:
        cs._resolve_rt_binary(str(not_exec))
    assert "chmod +x" in (e.value.hint or "")

    # ...and it never quietly used the packaged binary instead
    assert cs._rt_binary() == packaged


def test_env_override_that_is_bad_raises_too(no_rt_installed, tmp_path,
                                             monkeypatch):
    monkeypatch.setenv("EQFORGE_RT", str(tmp_path))
    with pytest.raises(SystemError) as e:
        cs._resolve_rt_binary()
    assert "EQFORGE_RT" in e.value.message


def test_resolve_reports_not_found_actionably(no_rt_installed):
    with pytest.raises(SystemError) as e:
        cs._resolve_rt_binary()
    assert "eqforge-rt binary not found" in e.value.message
    assert "make -C rthost" in (e.value.hint or "")


# --------------------------------------------------------------------------
# 2. launching the host
# --------------------------------------------------------------------------

def _spy(procs):
    """Wrap Popen so the test can reap whatever got detached."""
    real = subprocess.Popen

    def popen(cmd, **kw):
        p = real(cmd, **kw)
        procs.append(p)
        return p

    return popen


def test_start_runs_the_resolved_host_and_reports_the_pid(
        isolated_home, fake_daemon, tmp_path, monkeypatch, capsys, reap):
    """End to end: a host that opens its socket is reported as running."""
    from eqforge import paths
    host = _socket_creating_host(tmp_path)
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    monkeypatch.setattr(cs.subprocess, "Popen", _spy(reap))

    rc = cs.cmd_system(_start_args(rt_binary=str(host)))
    out_text = capsys.readouterr().out

    assert rc == 0
    assert "eqforge-rt running (pid" in out_text
    assert str(host) in out_text
    assert paths.rt_socket().exists(), "the host never opened its socket"


def test_start_honours_the_rt_binary_flag(isolated_home, fake_daemon,
                                          tmp_path, monkeypatch, reap):
    """`--rt-binary` used to be accepted by the parser and then ignored."""
    recorded = {}
    host = _socket_creating_host(tmp_path)
    real = subprocess.Popen

    def spy(cmd, **kw):
        recorded["cmd"] = list(cmd)
        p = real(cmd, **kw)
        reap.append(p)
        return p

    monkeypatch.setattr(cs.subprocess, "Popen", spy)
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    cs.cmd_system(_start_args(rt_binary=str(host), channels=4))

    assert recorded["cmd"][0] == str(host)
    assert "--channels" in recorded["cmd"]
    assert recorded["cmd"][recorded["cmd"].index("--channels") + 1] == "4"


def test_jack_failure_is_explained_not_buried(isolated_home, fake_daemon,
                                              tmp_path, monkeypatch):
    """The real-world failure: no JACK server -> status 17 -> actionable hint."""
    host = _write_host(tmp_path, """\
echo "Cannot connect to server request channel" >&2
echo "JackShmReadWritePtr::~JackShmReadWritePtr - Init not done for -1" >&2
echo "eqforge-rt: cannot open JACK client (17). Is PipeWire/JACK running?" >&2
exit 2
""")
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    with pytest.raises(SystemError) as e:
        cs.cmd_system(_start_args(rt_binary=str(host)))

    err = e.value
    assert "exit 2" in err.message
    # the host's own line is shown, libjack noise is not what leads
    assert "cannot open JACK client (17)" in err.message
    assert "JackShmReadWritePtr" not in err.message
    # and the status bitmask is decoded with the packaging fix named
    assert "JackFailure|JackServerFailed" in (err.hint or "")
    assert "pipewire-jack" in (err.hint or "")
    assert err.exit_code == 6


def test_popen_oserror_becomes_a_clean_error(isolated_home, fake_daemon,
                                             tmp_path, monkeypatch):
    """Popen failing must not escape as a raw traceback."""
    host = _write_host(tmp_path, "exec sleep 5")
    monkeypatch.delenv("EQFORGE_RT", raising=False)

    def boom(cmd, **kw):
        raise PermissionError(13, "Permission denied", str(cmd[0]))

    monkeypatch.setattr(cs.subprocess, "Popen", boom)
    with pytest.raises(SystemError) as e:
        cs.cmd_system(_start_args(rt_binary=str(host)))
    assert "could not execute" in e.value.message
    assert "chmod +x" in (e.value.hint or "")


def test_host_that_never_opens_its_socket_is_stopped(
        isolated_home, fake_daemon, tmp_path, monkeypatch, reap):
    """Alive but silent: don't leak the process, don't hang for the timeout."""
    host = _write_host(tmp_path, "exec sleep 30")
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    monkeypatch.setattr(cs.subprocess, "Popen", _spy(reap))

    calls = {"n": 0}

    def fast_time():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 1e9   # expire the wait at once

    monkeypatch.setattr(cs.time, "time", fast_time)

    with pytest.raises(SystemError) as e:
        cs.cmd_system(_start_args(rt_binary=str(host)))

    assert "rt host failed to start" in e.value.message
    assert reap, "nothing was launched"
    assert reap[0].wait(timeout=5) is not None, \
        "the stalled host was left running"


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_start_does_not_leak_the_log_file_descriptor(
        isolated_home, fake_daemon, tmp_path, monkeypatch, reap):
    host = _socket_creating_host(tmp_path)
    monkeypatch.delenv("EQFORGE_RT", raising=False)
    monkeypatch.setattr(cs.subprocess, "Popen", _spy(reap))

    before = len(os.listdir("/proc/self/fd"))
    cs.cmd_system(_start_args(rt_binary=str(host)))
    after = len(os.listdir("/proc/self/fd"))

    assert after <= before, f"leaked {after - before} fd(s)"


def test_log_tail_reads_nothing_when_the_log_is_absent(tmp_path):
    assert cs._log_tail(tmp_path / "absent.log") == []


def test_most_useful_prefers_the_host_line(tmp_path):
    lines = ["Cannot connect to server request channel",
             "jack server is not running or cannot be started",
             "JackShmReadWritePtr::~JackShmReadWritePtr - Init not done",
             "JackShmReadWritePtr::~JackShmReadWritePtr - Init not done",
             "eqforge-rt: cannot open JACK client (17)."]
    picked = cs._most_useful(lines, keep=2)
    assert picked[0].startswith("eqforge-rt:")
    assert len(picked) == 2
    assert len(set(picked)) == 2, "duplicates were not removed"


# --------------------------------------------------------------------------
# 3. `system setup` must not bake a bad ExecStart into the user's unit
# --------------------------------------------------------------------------

def test_setup_rejects_a_directory_rt_binary(isolated_home, tmp_path):
    args = types.SimpleNamespace(action="setup", rt_binary=str(tmp_path),
                                 channels=None, manage=False, json=False,
                                 verbose=False, quiet=False)
    with pytest.raises(SystemError) as e:
        cs.cmd_system(args)
    assert "directory" in e.value.message
    unit = Path(isolated_home) / ".config/systemd/user/eqforge-rt.service"
    assert not unit.exists() or "ExecStart=." not in unit.read_text()


def test_setup_writes_the_resolved_exec_path(isolated_home, tmp_path, capsys):
    host = _write_host(tmp_path, "exec sleep 1")
    args = types.SimpleNamespace(action="setup", rt_binary=str(host),
                                 channels=None, manage=False, json=False,
                                 verbose=False, quiet=False)
    assert cs.cmd_system(args) == 0
    unit = Path(isolated_home) / ".config/systemd/user/eqforge-rt.service"
    assert f"ExecStart={host} " in unit.read_text()


# --------------------------------------------------------------------------
# 4. pw-link hardening in the wiring path
# --------------------------------------------------------------------------

def test_pw_link_missing_is_tolerated_when_optional(monkeypatch):
    monkeypatch.setattr(wiring.shutil, "which", lambda name: None)
    assert wiring.pw_link("a", "b") is None


def test_pw_link_missing_raises_when_required(monkeypatch):
    monkeypatch.setattr(wiring.shutil, "which", lambda name: None)
    with pytest.raises(SystemError) as e:
        wiring.pw_link("a", "b", required=True)
    assert "pw-link not found" in e.value.message
    assert "pipewire" in (e.value.hint or "")


def test_pw_link_oserror_becomes_system_error(monkeypatch):
    monkeypatch.setattr(wiring.shutil, "which", lambda name: "/usr/bin/pw-link")

    def boom(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(wiring.subprocess, "run", boom)
    with pytest.raises(SystemError) as e:
        wiring.pw_link("a", "b", required=True)
    assert "pw-link failed" in e.value.message


def test_wire_graph_records_errors_instead_of_raising(monkeypatch):
    """The daemon calls this on a timer: a broken pw-link must not kill it."""
    monkeypatch.setattr(wiring.pw, "pipewire_available", lambda: True)

    class G:
        def default_sink(self): return "sink"
        def node_id(self, name): return 42
        def node_ports(self, nid, direction=None): return ["monitor_0", "playback_FL"]
        def node_port_targets(self, nid, direction=None, node_name=""):
            return ["sink:monitor_0", "sink:playback_FL"]
        def links(self): return [("app:out_FL", "sink:playback_FL")]
        def streams(self):
            return [_stream(7, "app")]

    def rt_command(cmd):
        if cmd.get("cmd") == "status":
            return {"ok": True, "client": "eqforge", "channels": 2}
        return {"connected": 0}

    def no_pw_link(*a, **kw):
        raise SystemError("pw-link not found: cannot rewire the PipeWire graph")

    monkeypatch.setattr(wiring, "pw_link", no_pw_link)
    report = wiring.wire_graph(rt_command, graph=G())
    assert report.errors, "the failure was swallowed silently"
    assert report.streams_moved == 0


# --------------------------------------------------------------------------
# 5. JACK transport detection (the jack2 vs pipewire-jack conflict)
# --------------------------------------------------------------------------

def test_decode_status_names_the_bits():
    assert jack.decode_status(17) == "JackFailure|JackServerFailed"
    assert jack.decode_status(1) == "JackFailure"
    assert jack.decode_status(0) == "no error"
    assert "0x" in jack.decode_status(1 << 20)          # unknown bits survive


def _which(*names):
    table = {n: f"/usr/bin/{n}" for n in names}
    return lambda name: table.get(name)


def _host(*sockets):
    """Injection kwargs for a machine where exactly these UNIX sockets exist.

    Parent directories are reported as existing (detection probes dirs too),
    but only the listed paths are sockets - which is what server detection
    requires, since stale non-socket artifacts outlive a dead server.
    """
    listed = {str(Path(x)) for x in sockets}
    have = set(listed)
    for x in sockets:
        have.update(str(parent) for parent in Path(x).parents)
    return {
        "exists": lambda p: str(p) in have,
        "is_socket": lambda p: str(p) in listed,
        "glob": lambda pattern: sorted(x for x in listed
                                       if fnmatch.fnmatch(x, pattern)),
    }


def test_inspect_pipewire_jack_is_usable(tmp_path):
    run = str(tmp_path)
    info = jack.inspect(which=_which("pw-jack"),
                        environ={"XDG_RUNTIME_DIR": run},
                        **_host(f"{run}/pipewire-0"))
    assert info.usable and not info.conflict and not info.hazard
    assert info.server_socket == f"{run}/pipewire-0"
    assert "pipewire-jack" in info.detail


def test_inspect_flags_the_jack2_conflict(tmp_path):
    """The reported Arch system: PipeWire running, jack2 owning libjack."""
    run = str(tmp_path)
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": run},
                        **_host(f"{run}/pipewire-0"))
    assert info.conflict and not info.usable
    assert "CONFLICT" in info.detail
    assert "no pipewire-jack" in info.detail
    assert "pipewire-jack" in info.fix
    assert info.pipewire_running and not info.pipewire_jack


def test_inspect_flags_two_stacks_as_a_hazard_not_a_conflict(tmp_path):
    run = str(tmp_path)
    info = jack.inspect(which=_which("jackd", "pw-jack"),
                        environ={"XDG_RUNTIME_DIR": run},
                        **_host(f"{run}/pipewire-0"))
    assert info.hazard and info.usable and not info.conflict
    assert "pw-jack" in info.fix


def test_inspect_real_jackd_server_is_usable(tmp_path):
    run = str(tmp_path)
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": run},
                        **_host(f"{run}/jack-0"))
    assert info.usable and not info.conflict
    assert info.jackd_running
    assert info.server_socket == f"{run}/jack-0"


def test_inspect_with_nothing_installed_never_raises():
    info = jack.inspect(which=lambda n: None, exists=lambda p: False,
                        is_socket=lambda p: False, glob=lambda p: [],
                        environ={"XDG_RUNTIME_DIR": "/nonexistent-xyz"})
    assert not info.usable and not info.conflict and not info.jackd_running
    assert info.server_socket is None
    assert info.fix


def test_inspect_honours_pipewire_remote(tmp_path):
    sock = tmp_path / "custom-pipe"
    info = jack.inspect(which=_which("pw-jack"),
                        environ={"XDG_RUNTIME_DIR": str(tmp_path),
                                 "PIPEWIRE_REMOTE": str(sock)},
                        **_host(str(sock)))
    assert info.usable and info.server_socket == str(sock)


def test_inspect_falls_back_to_the_logind_runtime_dir():
    """No XDG_RUNTIME_DIR (cron/ssh): use /run/user/<uid>, not a hardcoded 1000."""
    run = f"/run/user/{os.getuid()}"
    info = jack.inspect(which=_which("pw-jack"), environ={},
                        **_host(f"{run}/pipewire-0"))
    assert info.pipewire_running and info.usable
    assert info.server_socket == f"{run}/pipewire-0"


def test_inspect_ignores_another_users_runtime_dir():
    """A socket under someone else's /run/user must not be mistaken for ours."""
    other = f"/run/user/{os.getuid() + 12345}"
    info = jack.inspect(which=_which("pw-jack"), environ={},
                        **_host(f"{other}/pipewire-0"))
    assert not info.pipewire_running
    assert info.server_socket is None


def test_inspect_finds_jackd_in_dev_shm(tmp_path):
    """Measured jack2 1.9.21 layout: the request channel lives in /dev/shm.

    With XDG_RUNTIME_DIR set, the runtime dir stays *empty*, so probing only
    $XDG_RUNTIME_DIR/jack-0 misses a perfectly healthy jackd.
    """
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": str(tmp_path)},
                        **_host("/dev/shm/jack_default_0_0"))
    assert info.jackd_running and info.usable
    assert info.server_socket == "/dev/shm/jack_default_0_0"


def test_inspect_globs_dev_shm_for_other_server_names(tmp_path):
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": str(tmp_path)},
                        **_host("/dev/shm/jack_proaudio_1000_0"))
    assert info.jackd_running
    assert info.server_socket == "/dev/shm/jack_proaudio_1000_0"


def test_stale_shm_artifacts_are_not_a_running_server(tmp_path):
    """Measured after jackd exited: the registry file and jack_db-0 directory
    remain, the request socket is gone. Existence alone must not imply
    'running', or doctor would report a server that is not there."""
    stale = ["/dev/shm/jack-shm-registry", "/dev/shm/jack_db-0"]
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": str(tmp_path)},
                        exists=lambda p: str(p) in stale,
                        is_socket=lambda p: False,
                        glob=lambda pattern: list(stale))
    assert not info.jackd_running and not info.usable


def test_a_regular_file_where_a_socket_should_be_is_rejected(tmp_path):
    """Socket-ness, not existence, is the probe."""
    run = str(tmp_path)
    info = jack.inspect(which=_which("pw-jack"),
                        environ={"XDG_RUNTIME_DIR": run},
                        exists=lambda p: True,          # everything 'exists'
                        is_socket=lambda p: False,      # nothing is a socket
                        glob=lambda pattern: [])
    assert not info.pipewire_running and not info.usable


def test_inspect_legacy_socket_path_uses_the_uid_not_the_username(tmp_path):
    """libjack formats the legacy dir as `%s/jack-%d` - a numeric uid."""
    run = str(tmp_path)
    legacy = f"/tmp/jack-{os.getuid()}/jack-0"
    info = jack.inspect(which=_which("jackd"),
                        environ={"XDG_RUNTIME_DIR": run},
                        **_host(legacy))
    assert info.jackd_running
    assert info.server_socket == legacy

    by_name = jack.inspect(which=_which("jackd"),
                           environ={"XDG_RUNTIME_DIR": run, "USER": "alice"},
                           **_host("/tmp/jack-alice/jack-0"))
    assert not by_name.jackd_running, "probed a user name where jackd uses a uid"


# --------------------------------------------------------------------------
# 6. doctor surfaces both new checks
# --------------------------------------------------------------------------

def test_doctor_reports_rt_binary_and_jack_transport(isolated_home, capsys,
                                                     monkeypatch, tmp_path):
    monkeypatch.setattr(rthost, "SYSTEM_PATHS", ())
    monkeypatch.setattr(rthost, "in_tree_rt", lambda: tmp_path / "absent")
    monkeypatch.setattr(rthost.shutil, "which", lambda name: None)
    monkeypatch.delenv("EQFORGE_RT", raising=False)

    args = types.SimpleNamespace(build_core=False, json=False, verbose=False,
                                 quiet=False)
    cs.cmd_doctor(args)
    text = capsys.readouterr().out
    assert "rt host binary" in text
    assert "jack transport" in text


def test_doctor_jack_check_reports_the_conflict(monkeypatch, tmp_path):
    conflict = jack.JackInfo(pipewire_jack=False, jackd=True,
                             pipewire_running=True, jackd_running=False,
                             server_socket=None,
                             conflict=True, hazard=False, usable=False,
                             detail="server: none - CONFLICT: jack2/jackd",
                             fix=jack.PIPEWIRE_JACK_FIX)
    monkeypatch.setattr(jack, "inspect", lambda **kw: conflict)
    ok, detail = cs._jack_check()
    assert ok is False
    assert "CONFLICT" in detail
    # the remediation doctor prints next to a WARN
    assert "pipewire-jack" in cs._jack_fix_hint()


def test_doctor_jack_check_passes_on_a_clean_pipewire_jack_setup(monkeypatch):
    clean = jack.JackInfo(pipewire_jack=True, jackd=False,
                          pipewire_running=True, jackd_running=False,
                          server_socket="/run/user/1000/pipewire-0",
                          conflict=False, hazard=False, usable=True,
                          detail="server: pipewire-jack", fix="")
    monkeypatch.setattr(jack, "inspect", lambda **kw: clean)
    ok, detail = cs._jack_check()
    assert ok is True
    assert "pipewire-jack" in detail


def test_doctor_rt_binary_check_passes_when_a_host_exists(
        no_rt_installed, tmp_path, monkeypatch):
    ok, detail = cs._rt_binary_check()
    assert ok is False and "not found" in detail

    host = _write_host(tmp_path, "exit 0")
    monkeypatch.setenv("EQFORGE_RT", str(host))
    ok, detail = cs._rt_binary_check()
    assert ok is True and detail == str(host)


# --------------------------------------------------------------------------
# 7. CLI level: the user-visible contract
# --------------------------------------------------------------------------

def _run_cli(args, home, extra_env=None):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "NO_COLOR": "1",
        "PYTHONPATH": str(REPO_ROOT),
        "EQFORGE_DAEMONIZED": "1",
    })
    env.pop("EQFORGE_RT", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "eqforge"] + args,
                          capture_output=True, text=True, env=env,
                          cwd=str(home), timeout=120)


def _stop_daemon(home):
    """A test that reaches the launch path spawns a real daemon; reap it."""
    try:
        _run_cli(["daemon", "stop"], home)
    except Exception:                                       # noqa: BLE001
        pass


def test_cli_start_with_a_directory_override_fails_cleanly(isolated_home):
    """No traceback, a real exit code, and a message the user can act on."""
    r = _run_cli(["system", "start", "--rt-binary", str(isolated_home)],
                 isolated_home)
    assert r.returncode == 6, r.stderr
    assert "Traceback" not in r.stderr
    assert "PermissionError" not in r.stderr
    assert "directory" in r.stderr


def test_cli_start_never_resolves_to_the_cwd(isolated_home):
    """EQFORGE_RT empty must not make the CLI try to execute '.'."""
    try:
        r = _run_cli(["system", "start"], isolated_home,
                     extra_env={"EQFORGE_RT": ""})
        assert "Permission denied: '.'" not in r.stderr
        assert "PermissionError" not in r.stderr
        assert "Traceback" not in r.stderr
        assert r.returncode != 0
    finally:
        _stop_daemon(isolated_home)


def test_cli_setup_with_a_directory_override_fails_cleanly(isolated_home):
    r = _run_cli(["system", "setup", "--rt-binary", str(isolated_home)],
                 isolated_home)
    assert r.returncode == 6, r.stderr
    assert "Traceback" not in r.stderr
    unit = isolated_home / ".config/systemd/user/eqforge-rt.service"
    assert not unit.exists(), "a unit file was written for a bogus path"
