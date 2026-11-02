"""RPC client for the eqforge daemon (used by the CLI).

`call()` connects to the daemon socket; `call_autostart()` transparently
spawns a detached daemon when none is running (like `systemctl --user`
tools or `gpg-agent` do), so CLI commands "just work".
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from eqforge import paths
from eqforge.errors import DaemonError


def daemon_running(timeout: float = 0.5) -> bool:
    sock = paths.daemon_socket()
    if not sock.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
            s.sendall(b'{"method":"ping"}\n')
            data = s.recv(4096)
            return b'"ok": true' in data or b'"ok":true' in data
    except OSError:
        return False


def spawn_daemon(extra_args: list[str] | None = None) -> None:
    """Start a detached daemon process.

    `extra_args` are appended to `daemon run`, so `eqforge daemon start
    --auto-wire` actually reaches the process it spawns instead of being
    accepted by the parser and dropped.
    """
    paths.ensure_dirs()
    logf = open(paths.log_dir() / "daemon.out", "ab")
    cmd = [sys.executable, "-m", "eqforge", "daemon", "run", "--foreground",
           *(extra_args or [])]
    env = dict(os.environ)
    env.setdefault("EQFORGE_DAEMONIZED", "1")
    subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=env, close_fds=True)


def call(method: str, params: dict | None = None, timeout: float = 10.0,
         autostart: bool = True) -> dict:
    """One RPC call. Returns the `result` dict or raises DaemonError."""
    sock = paths.daemon_socket()
    if not sock.exists():
        if not autostart:
            raise DaemonError("daemon not running",
                              hint="start it with `eqforge daemon start`")
        spawn_daemon()
        deadline = time.time() + 6.0
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)

    last_err: Exception | None = None
    for attempt in range(20):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(sock))
                s.sendall((json.dumps({"method": method,
                                       "params": params or {}}) + "\n").encode())
                f = s.makefile("rb")
                line = f.readline()
                if not line:
                    raise DaemonError("daemon closed connection")
                resp = json.loads(line.decode())
            if resp.get("ok"):
                return resp.get("result", {})
            raise DaemonError(resp.get("error", "unknown RPC error"),
                              hint=resp.get("hint"))
        except (ConnectionRefusedError, FileNotFoundError,
                socket.timeout, OSError) as e:
            last_err = e
            time.sleep(0.25)
    raise DaemonError(f"cannot reach daemon ({last_err})",
                      hint="try `eqforge daemon restart` or check "
                           "~/.local/state/eqforge/logs/")


def pid_if_running(timeout: float = 0.5) -> int | None:
    """Return the daemon PID if a live daemon answers on the socket."""
    sock = paths.daemon_socket()
    if not sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
            s.sendall(b'{"method":"ping"}\n')
            data = s.recv(4096)
        resp = json.loads(data.decode())
        if resp.get("ok"):
            pid = (resp.get("result") or {}).get("pid")
            return int(pid) if pid else None
        return None
    except (OSError, ValueError):
        return None


def stop_daemon(timeout: float = 8.0) -> bool:
    """Stop the daemon through its own RPC, falling back to a signal.

    The previous implementation shelled out to `fuser`, which is not installed
    on a stock Debian/Ubuntu system, so `eqforge daemon stop` silently did
    nothing. The shutdown RPC is authoritative; the signal fallback only runs
    when the RPC cannot be delivered but a PID is known from the ping reply.
    """
    import signal
    import time as _time

    pid = pid_if_running(timeout=1.0)
    if pid is None:
        # No live daemon. Clean up leftovers so the next start is not blocked.
        for p in (paths.daemon_socket(), paths.gui_port_file()):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return False

    try:
        call("shutdown", autostart=False, timeout=2.0)
    except DaemonError:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def _alive(p: int) -> bool:
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if not _alive(pid):
            for p in (paths.daemon_socket(), paths.gui_port_file()):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            return True
        _time.sleep(0.05)

    # Last resort: it accepted the RPC but did not exit.
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _time.sleep(0.2)
    return not _alive(pid)


def http_base() -> str | None:
    p = paths.gui_port_file()
    if p.exists():
        try:
            port = int(p.read_text().strip())
            return f"http://127.0.0.1:{port}"
        except ValueError:
            pass
    return None
