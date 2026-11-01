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

import textwrap
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


def write_system_setup(paths_mod, exec_path: Path | None = None) -> list[Path]:
    """Write conf + unit into XDG dirs; returns written files."""
    written = []
    pw_dir = Path(paths_mod.home()) / ".config/pipewire/pipewire.conf.d"
    pw_dir.mkdir(parents=True, exist_ok=True)
    conf = pw_dir / "99-eqforge.conf"
    conf.write_text(render_pipewire_conf(
        str(paths_mod.active_dsp_file())))
    written.append(conf)

    unit_dir = Path(paths_mod.home()) / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "eqforge-rt.service"
    exec_path = exec_path or Path("/usr/libexec/eqforge/eqforge-rt")
    unit.write_text(render_systemd_unit(
        str(exec_path), str(paths_mod.rt_socket()),
        str(paths_mod.active_dsp_file())))
    written.append(unit)
    return written
