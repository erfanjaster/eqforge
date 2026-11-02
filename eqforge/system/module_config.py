"""Generate native integration configs for system-wide processing.

Two deployment styles are supported (both documented in
docs/pipewire-integration.md):

A. SPA filter module (native node, recommended when libeqforge-filter.so is
   installed): a PipeWire config fragment hosts the node via
   libpipewire-module-adapter; WirePlumber links default output through it.

B. RT host (JACK client via pipewire-jack): `eqforge-rt` runs as a user
   service; the daemon performs the graph rewiring. No PipeWire config
   needed beyond session permissions.

`eqforge system setup` writes the files and prints next steps; this module
produces their content so it is reviewable before touching the system.
"""
from __future__ import annotations

import shlex
import shutil
import textwrap
import time
from pathlib import Path

PIPEWIRE_CONF = """\
# EQForge system-wide filter (PipeWire config fragment)
# Install to: ~/.config/pipewire/pipewire.conf.d/99-eqforge.conf
# Then restart: systemctl --user restart pipewire pipewire-pulse wireplumber
#
# NOTE: PipeWire >= 0.3.5x resolves SPA factories through the context.spa-libs
# table (regex -> library relative to the spa-0.2 plugin dir), and
# libpipewire-module-adapter only registers a factory when loaded via
# context.modules - persistent nodes must be created through context.objects.
# The plugin library must be installed first, e.g.:
#   Debian/Ubuntu: /usr/lib/x86_64-linux-gnu/spa-0.2/eqforge/libeqforge-filter.so
#   Arch/Fedora:   /usr/lib/spa-0.2/eqforge/libeqforge-filter.so

context.spa-libs = {
    eqforge.* = eqforge/libeqforge-filter
}

context.modules = [
    { name = libpipewire-module-adapter }
]

context.objects = [
    { factory = adapter
        args = {
            factory.name     = eqforge.filter
            node.name        = "eqforge_filter"
            node.description = "EQForge Audio Processor"
            media.class      = "Audio/Sink"
            audio.channels   = 2
            audio.position   = [ FL FR ]
            config           = "%(config)s"
            # explicit override if the plugin lives elsewhere (absolute path):
            # library.name = "%(libdir)s/libeqforge-filter.so"
        }
    }
]
"""

WIREDUMP_LINK_HINT = """\
# To route everything through the filter, set it as the default sink:
#   wpctl status                    # find eqforge_filter id
#   wpctl set-default <id>
# or let the eqforge daemon manage routing:
#   eqforge system start --manage
"""

SYSTEMD_USER_UNIT = """\
[Unit]
Description=EQForge real-time audio host
After=pipewire.service
Wants=pipewire.service

[Service]
Type=simple
ExecStart=%(exec)s --name eqforge --socket %(socket)s --config %(config)s
Restart=on-failure
RestartSec=2
# RT priorities via rtkit (PipeWire sessions usually already provide these)
Nice=-11

[Install]
WantedBy=default.target
"""


def render_pipewire_conf(config_path: str, libdir: str = "") -> str:
    return PIPEWIRE_CONF % {"config": config_path, "libdir": libdir}


def render_systemd_unit(exec_path: str, socket_path: str,
                        config_path: str) -> str:
    return SYSTEMD_USER_UNIT % {"exec": exec_path, "socket": socket_path,
                                "config": config_path}


def write_managed(path: Path, content: str) -> tuple[str, Path | None]:
    """Write `content` to a path eqforge owns on behalf of the user.

    Returns (status, backup_path) where status is one of:
      "created"   - the file did not exist
      "same"      - it already had exactly this content (nothing written)
      "updated"   - it existed with different content; the old file is kept
                    as "<name>.bak.<timestamp>" before being replaced

    The previous behaviour overwrote an edited file with no recovery path,
    which silently destroyed user changes to the PipeWire drop-in - a real
    hazard because that file is documented as user-editable.
    """
    if path.exists():
        existing = path.read_text()
        if existing == content:
            return "same", None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        # never clobber an existing backup from the same second
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak.{stamp}-{n}")
            n += 1
        shutil.copy2(path, backup)
        path.write_text(content)
        return "updated", backup
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return "created", None


def write_system_setup(paths_mod, exec_path: Path | None = None,
                       results: list | None = None) -> list[Path]:
    """Write conf + unit into XDG dirs; returns written files.

    Existing files are backed up before being replaced (see write_managed).
    Pass `results` to receive (path, status, backup) tuples.
    """
    written = []
    pw_dir = Path(paths_mod.home()) / ".config/pipewire/pipewire.conf.d"
    pw_dir.mkdir(parents=True, exist_ok=True)
    conf = pw_dir / "99-eqforge.conf"
    status, backup = write_managed(
        conf, render_pipewire_conf(str(paths_mod.active_dsp_file())))
    written.append(conf)
    if results is not None:
        results.append((conf, status, backup))

    unit_dir = Path(paths_mod.home()) / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "eqforge-rt.service"
    exec_path = exec_path or Path("/usr/libexec/eqforge/eqforge-rt")
    status, backup = write_managed(
        unit, render_systemd_unit(str(exec_path), str(paths_mod.rt_socket()),
                                  str(paths_mod.active_dsp_file())))
    written.append(unit)
    if results is not None:
        results.append((unit, status, backup))
    return written


# --------------------------------------------------------------------------
# systemd user units for `eqforge system autostart on`
#
# Generated per install, not shipped as static files: the rt host may live in
# a checkout (rthost/build/eqforge-rt) or in /usr/libexec, the daemon may run
# from a venv or from site-packages, and under pipewire-jack the host needs a
# `pw-jack` prefix. A static template cannot know any of that, which is why
# packaging/systemd/*.service only fits a packaged install.
# --------------------------------------------------------------------------

RT_UNIT = """\
[Unit]
Description=EQForge real-time audio host (JACK/PipeWire client)
Documentation=https://github.com/erfanjaster/eqforge
# The host is a JACK client: without the sound server up, jack_client_open()
# fails and systemd would just restart it in a loop.
After=pipewire.service pipewire.socket
Wants=pipewire.service

[Service]
Type=simple
ExecStart=%(exec)s
Restart=on-failure
RestartSec=2
# RT scheduling headroom; PipeWire sessions with rtkit grant this anyway.
Nice=-11
# %%t is systemd's per-user runtime directory (== $XDG_RUNTIME_DIR in a
# session). Pinning it keeps the control socket where the daemon and the CLI
# look for it even when the unit is started outside a normal session.
Environment=XDG_RUNTIME_DIR=%%t

[Install]
WantedBy=default.target
"""

DAEMON_UNIT = """\
[Unit]
Description=EQForge control daemon (profiles, GUI, automatic rewiring)
Documentation=https://github.com/erfanjaster/eqforge
# Starts after the rt host so the first wiring pass has something to wire to.
After=pipewire.service wireplumber.service eqforge-rt.service
Wants=pipewire.service eqforge-rt.service

[Service]
Type=simple
ExecStart=%(exec)s
Environment=PYTHONPATH=%(pythonpath)s
Environment=PATH=%(path)s
Environment=XDG_RUNTIME_DIR=%%t
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def _argv_line(argv: list[str]) -> str:
    """An ExecStart= value: absolute, quoted, no shell involved."""
    return " ".join(shlex.quote(str(a)) for a in argv)


def render_rt_unit(exec_argv: list[str]) -> str:
    """Unit for the native rt host.

    `exec_argv` must come from `rthost.launch_argv()` so that the pw-jack
    wrapper, socket path and config path are the same ones the CLI uses.
    """
    return RT_UNIT % {"exec": _argv_line(exec_argv)}


def render_daemon_unit(exec_argv: list[str], pythonpath: str = "",
                       path: str = "") -> str:
    """Unit for the Python daemon (GUI, profiles, auto-wire)."""
    return DAEMON_UNIT % {"exec": _argv_line(exec_argv),
                          "pythonpath": pythonpath or "",
                          "path": path or "/usr/local/bin:/usr/bin:/bin"}


def autostart_unit_paths(home: Path | str) -> dict[str, Path]:
    """Where the generated user units live."""
    d = Path(home) / ".config/systemd/user"
    return {"rt": d / "eqforge-rt.service",
            "daemon": d / "eqforge-daemon.service"}
