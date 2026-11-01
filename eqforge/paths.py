"""XDG-compliant filesystem locations for EQForge.

Layout:
  ~/.config/eqforge/            profiles/, config.json, active-dsp.json
  ~/.local/share/eqforge/       imports/, cache/
  ~/.local/state/eqforge/       logs/
  $XDG_RUNTIME_DIR/eqforge/     daemon.sock, rt.sock, gui.port
"""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    if value:
        return Path(value)
    return default


def home() -> Path:
    return Path(os.path.expanduser("~"))


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", home() / ".config") / "eqforge"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", home() / ".local" / "share") / "eqforge"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", home() / ".local" / "state") / "eqforge"


def runtime_dir() -> Path:
    return _xdg("XDG_RUNTIME_DIR", Path("/tmp")) / "eqforge"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def imports_dir() -> Path:
    return data_dir() / "imports"


def log_dir() -> Path:
    return state_dir() / "logs"


def config_file() -> Path:
    return config_dir() / "config.json"


def active_dsp_file() -> Path:
    """Resolved dsp-config consumed by the native hosts (SPA module / RT)."""
    return config_dir() / "active-dsp.json"


def daemon_socket() -> Path:
    return runtime_dir() / "daemon.sock"


def rt_socket() -> Path:
    return runtime_dir() / "rt.sock"


def gui_port_file() -> Path:
    return runtime_dir() / "gui.port"


def ensure_dirs() -> None:
    for d in (config_dir(), profiles_dir(), data_dir(), imports_dir(),
              state_dir(), log_dir(), runtime_dir()):
        d.mkdir(parents=True, exist_ok=True)
    try:
        runtime_dir().chmod(0o700)
    except OSError:
        pass
