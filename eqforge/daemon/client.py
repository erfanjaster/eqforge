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


def spawn_daemon() -> None:
    """Start a detached daemon process."""
    paths.ensure_dirs()
    logf = open(paths.log_dir() / "daemon.out", "ab")
    cmd = [sys.executable, "-m", "eqforge", "daemon", "run", "--foreground"]
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


def stop_daemon() -> bool:
    """Ask systemd or kill via pidfile-less socket probe (best effort)."""
    try:
        call("ping", autostart=False, timeout=1)
    except DaemonError:
        return False
    # no dedicated stop RPC: signal the process owning the socket
    try:
        import signal
        out = subprocess.run(["fuser", str(paths.daemon_socket())],
                             capture_output=True, text=True, timeout=5)
        pids = out.stdout.split()
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return False


def http_base() -> str | None:
    p = paths.gui_port_file()
    if p.exists():
        try:
            port = int(p.read_text().strip())
            return f"http://127.0.0.1:{port}"
        except ValueError:
            pass
    return None
