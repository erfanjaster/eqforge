"""CLI commands: runtime/system (devices, streams, status, daemon, gui, system, doctor)."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from eqforge import paths
from eqforge.cli import output as out
from eqforge.errors import EQForgeError, SystemError


# ----------------------------------------------------------------- devices --

def cmd_devices(args) -> int:
    from eqforge.system import pipewire as pw
    if not pw.pipewire_available():
        out.echo(out.yellow("  PipeWire tools not found (pw-dump). "
                            "Install pipewire to see devices."))
        return 1
    try:
        devices = pw.list_devices()
        default = pw.default_sink()
    except EQForgeError as e:
        out.echo(out.yellow(f"  PipeWire present but unreachable: {e.message}"))
        return 1
    if args.json:
        out.echo_json({"devices": [d.to_dict() for d in devices],
                       "default_sink": default})
        return 0
    rows = []
    for d in devices:
        mark = out.green("●") if d.node_name == default else " "
        rows.append([mark, str(d.id), d.description[:40], d.direction, d.kind])
    out.echo(out.table(["", "id", "description", "dir", "kind"], rows,
                       max_widths=[2, 6, 42, 7, 11]))
    if default:
        out.echo(out.dim(f"\n  default sink: {default}"))
    return 0


def cmd_streams(args) -> int:
    from eqforge.system import pipewire as pw
    if not pw.pipewire_available():
        out.echo(out.yellow("  PipeWire tools not found."))
        return 1
    try:
        streams = pw.list_streams()
    except EQForgeError as e:
        out.echo(out.yellow(f"  PipeWire present but unreachable: {e.message}"))
        return 1
    if args.json:
        out.echo_json({"streams": [s.to_dict() for s in streams]})
        return 0
    rows = [[str(s.id), s.name[:34], s.binary[:20], s.direction]
            for s in streams]
    out.echo(out.table(["id", "application", "binary", "dir"], rows,
                       max_widths=[6, 36, 22, 9]))
    return 0


# ------------------------------------------------------------------ status --

def cmd_status(args) -> int:
    from eqforge.daemon import client
    from eqforge.native.loader import info as native_info
    running = client.daemon_running()
    st = client.call("status", autostart=False) if running else None

    nat = native_info()
    rows = [
        ("version", _version()),
        ("engine", (f"native {nat.get('version')} ({nat.get('path')})"
                    if nat["available"] else "numpy fallback (libeqforge not loaded)")),
        ("daemon", out.green("running") if running else out.dim("not running")),
    ]
    if st:
        rt = st.get("rt", {})
        rows += [
            ("active profile", out.cyan(st["active_profile"])),
            ("slot A / B", f"{st['slot_a']} / {st['slot_b']} "
                           f"(active: {st['active_slot']})"),
            ("bypass", out.yellow("ON") if st["bypass"] else "off"),
            ("rt host", out.green("running") if rt.get("running")
             else out.dim("not running")),
        ]
        if rt.get("running"):
            rows += [
                ("rt latency", f"{rt.get('latency_frames', 0)} frames "
                               f"@ {rt.get('sample_rate', '?')} Hz"),
                ("momentary", f"{rt.get('momentary_lufs', -70):.1f} LUFS"),
            ]
        # How the graph is followed. This is worth a line of its own: it is the
        # difference between "the first ~50 ms of playback is unprocessed" and
        # "the first second is".
        watch = st.get("watch") or {}
        if watch.get("running"):
            rows.append(("graph events",
                         out.green(f"events via {Path(watch['command'][0]).name}"
                                   if watch.get("command") else "events")
                         + out.dim(f" ({watch.get('events', 0)} events, "
                                   f"{watch.get('restarts', 0)} restarts)")))
        else:
            rows.append(("graph events",
                         out.dim("polling only")
                         + (out.dim(f" ({watch['error']})")
                            if watch.get("error") else "")))
    from eqforge.system import pipewire as pw
    rows.append(("pipewire", out.green("available") if pw.pipewire_available()
                 else out.dim("not found")))
    if pw.pipewire_available():
        rows.append(("default sink", pw.default_sink() or "?"))
        try:
            from eqforge.system import always_on as ao
            ao_state = ao.status()
        except EQForgeError:
            ao_state = None
        if ao_state and ao_state["installed"]:
            if ao_state["ok"]:
                word = out.green(f"active → {ao_state['node']}")
            elif not ao_state["node_present"]:
                word = out.yellow(f"{ao_state['node']} not in graph")
            else:
                word = out.yellow("installed, target missing")
            rows.append(("always-on", word))
    out.echo(out.kv_table(rows, indent="\n  "))
    out.echo()
    return 0


def _version() -> str:
    from eqforge.version import __version__
    return __version__


# ------------------------------------------------------- runtime controls --

def cmd_set_profile(args) -> int:
    from eqforge.daemon import client
    r = client.call("set_profile", {"profile": args.profile,
                                    "slot": args.slot})
    out.echo(f"  {out.green('✓')} active profile → "
             f"{out.cyan(r['active_profile'])} (slot {r['active_slot']})")
    return 0


def cmd_bypass(args) -> int:
    from eqforge.daemon import client
    value = {"on": True, "off": False}.get(args.state)
    params = {} if value is None else {"value": value}
    r = client.call("bypass", params)
    state = out.yellow("ON") if r["bypass"] else "off"
    out.echo(f"  bypass: {state}")
    return 0


def cmd_ab(args) -> int:
    from eqforge.daemon import client
    r = client.call("ab", {"slot": args.slot})
    out.echo(f"  {out.green('✓')} slot {r['active_slot']} → "
             f"{out.cyan(r['active_profile'])}  "
             f"(A={r['slot_a']}, B={r['slot_b']})")
    return 0


# ------------------------------------------------------------------ daemon --

def _daemon_flags(args) -> list[str]:
    """Flags that must reach the *spawned* daemon, not just this process.

    `eqforge daemon start --auto-wire` used to be indistinguishable from
    `eqforge daemon start`: the parser accepted the flag and the spawn
    command line never carried it.
    """
    flags: list[str] = []
    auto = getattr(args, "auto_wire", None)
    if auto is True:
        flags.append("--auto-wire")
    elif auto is False:
        flags.append("--no-auto-wire")
    if getattr(args, "manage_rt", False):
        flags.append("--manage-rt")
    channels = getattr(args, "channels", None)
    if channels:
        flags += ["--channels", str(channels)]
    return flags


def cmd_daemon(args) -> int:
    from eqforge.daemon import client
    action = args.action
    if action == "run":
        from eqforge.daemon.server import Daemon
        d = Daemon(auto_wire=getattr(args, "auto_wire", None),
                   manage_rt=getattr(args, "manage_rt", False),
                   rt_channels=getattr(args, "channels", None) or 2)
        if not args.no_http:
            d.serve_http(port=args.port or 0)
            base = client.http_base()
            if base:
                out.echo(f"  GUI available at {out.cyan(base)}")
        try:
            d.run_forever(http=False)
        except KeyboardInterrupt:
            pass
        finally:
            d.stop()
        return 0
    if action == "start":
        if client.daemon_running():
            out.echo("  daemon already running")
            out.echo(out.dim("    restart it to change --auto-wire/--manage-rt:"
                             " eqforge daemon restart"))
            return 0
        flags = _daemon_flags(args)
        client.spawn_daemon(flags)
        deadline = time.time() + 6
        while time.time() < deadline:
            if client.daemon_running():
                out.echo(f"  {out.green('✓')} daemon started")
                base = client.http_base()
                if base:
                    out.echo(out.dim(f"    GUI: {base}"))
                if flags:
                    out.echo(out.dim(f"    mode: {' '.join(flags)}"))
                return 0
            time.sleep(0.2)
        raise EQForgeError("daemon did not come up in time",
                           hint=f"check {paths.log_dir() / 'daemon.out'}")
    if action == "stop":
        if client.stop_daemon():
            out.echo(f"  {out.green('✓')} daemon stopped")
        else:
            out.echo("  daemon not running")
        return 0
    if action == "restart":
        client.stop_daemon()
        time.sleep(0.4)
        clone = copy.copy(args)          # keep the flags, change the action
        clone.action = "start"
        return cmd_daemon(clone)
    if action == "status":
        return cmd_status(args)
    raise EQForgeError(f"unknown daemon action {action!r}")


# --------------------------------------------------------------------- gui --

def cmd_gui(args) -> int:
    from eqforge.daemon import client
    if not client.daemon_running():
        client.spawn_daemon()
        deadline = time.time() + 6
        while time.time() < deadline and not client.daemon_running():
            time.sleep(0.2)
    base = client.http_base()
    if not base:
        raise EQForgeError("daemon running but GUI port unknown",
                           hint="restart the daemon: eqforge daemon restart")
    if args.port:
        base = f"http://127.0.0.1:{args.port}"
    out.echo(f"  EQForge GUI: {out.cyan(base)}")
    if not args.no_browser:
        for opener in ("xdg-open", "gio", "sensible-browser"):
            if shutil.which(opener):
                try:
                    subprocess.Popen([opener, base] if opener != "gio"
                                     else ["gio", "open", base],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    break
                except OSError:
                    continue
    if args.wait:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


# ------------------------------------------------------------------ system --

def cmd_system(args) -> int:
    from eqforge.daemon import client
    action = args.action

    if action == "setup":
        from eqforge.system.module_config import write_system_setup
        # An unusable --rt-binary must not end up baked into the user's
        # systemd unit as `ExecStart=.`; validate it, but allow setup to run
        # before the host is built (then the packaged path is written).
        if args.rt_binary:
            exec_path = _resolve_rt_binary(args.rt_binary)
        else:
            exec_path = _rt_binary() or Path(_RT_SYSTEM_PATHS[0])
        results: list = []
        write_system_setup(paths, exec_path=exec_path, results=results)
        # Report what actually happened to each file. Saying "wrote" for a file
        # that was left alone (or silently replaced) is exactly the kind of
        # fake success a user cannot act on.
        out.echo(f"  {out.green('✓')} integration files:")
        for path, status, backup in results:
            if status == "created":
                out.echo(f"    {out.green('created')}  {path}")
            elif status == "same":
                out.echo(f"    {out.dim('unchanged')} {path}")
            else:
                out.echo(f"    {out.yellow('updated')}  {path}")
                if backup is not None:
                    out.echo(f"      previous version saved as {backup}")
        if any(st == "updated" for _, st, _ in results):
            out.echo(out.dim(
                "  review the updates before restarting: those files are yours "
                "to edit"))
        out.echo(out.dim("""
  next steps:
    1. install the native filter (make -C pipewire install) or the rt host
       (make -C rthost install)
    2. systemctl --user daemon-reload
    3. eqforge system start          # rt-host based processing
       # or: systemctl --user restart pipewire   # SPA-module based
    4. eqforge set-profile <id>
"""))
        return 0

    if action == "start":
        rt = _resolve_rt_binary(getattr(args, "rt_binary", None))
        sock = paths.rt_socket()
        if sock.exists():
            r = client.call("rt_command", {"cmd": {"cmd": "status"}},
                            autostart=False) \
                if client.daemon_running() else None
            if (r or {}).get("rt", {}).get("ok"):
                out.echo(f"  rt host already running ({out.cyan(str(rt))})")
                return 0
            sock.unlink(missing_ok=True)   # stale socket from a dead host
        if not client.daemon_running():
            client.spawn_daemon()
            time.sleep(0.5)
        client.call("status")  # ensures active-dsp.json exists
        # Launching (path resolution, the pw-jack wrapper, fd handling, the
        # wait for the control socket and the failure diagnosis) lives in
        # eqforge.system.rthost so the daemon's --manage-rt supervision and
        # this command cannot drift apart.
        launched = _rthost.start(rt_binary=rt, channels=args.channels or 2)
        out.echo(f"  {out.green('✓')} eqforge-rt running (pid {launched.pid})")
        note = f"    host: {launched.rt}"
        if launched.wrapper:
            note += f"  (via {Path(launched.wrapper[0]).name})"
        out.echo(out.dim(note))
        if args.manage:
            _wire_rt(client)
        return 0

    if action == "stop":
        r = client.call("rt_command", {"cmd": {"cmd": "quit"}},
                        autostart=False) \
            if client.daemon_running() else None
        sock = paths.rt_socket()
        if sock.exists():
            sock.unlink(missing_ok=True)
        out.echo(f"  {out.green('✓')} rt host stopped")
        return 0

    if action == "status":
        if not client.daemon_running():
            out.echo("  daemon not running")
            return 1
        r = client.call("rt_command", {"cmd": {"cmd": "status"}})
        rt = r.get("rt") or {}
        if rt.get("ok"):
            out.echo(out.kv_table([
                ("rt host", out.green("running")),
                ("client", rt.get("client", "?")),
                ("sample rate", f"{rt.get('sample_rate')} Hz"),
                ("buffer", f"{rt.get('buffer_size')} frames"),
                ("latency", f"{rt.get('latency_frames')} frames "
                            f"({1000 * rt.get('latency_frames', 0) / max(rt.get('sample_rate', 1), 1):.2f} ms)"),
                ("bypass", "ON" if rt.get("bypass") else "off"),
                ("momentary", f"{rt.get('momentary_lufs', -70):.1f} LUFS"),
            ]))
        else:
            out.echo("  rt host not running")
        return 0

    if action == "wire":
        _wire_rt(client)
        return 0

    if action == "autostart":
        return cmd_autostart(args)

    if action == "always-on":
        return cmd_always_on(args)

    raise EQForgeError(f"unknown system action {action!r}")


# rt-host location/launch machinery lives in eqforge.system.rthost so the
# daemon can use exactly the same code as the CLI. Aliased here for the
# command implementations (and their tests) below.
from eqforge.system import rthost as _rthost          # noqa: E402

_RT_SYSTEM_PATHS = _rthost.SYSTEM_PATHS
_is_executable_file = _rthost.is_executable_file
_in_tree_rt = _rthost.in_tree_rt
_rt_candidates = _rthost.candidates
_rt_binary = _rthost.find_binary
_resolve_rt_binary = _rthost.resolve_binary
_log_tail = _rthost.log_tail
_exec_hint = _rthost.exec_hint
_most_useful = _rthost.most_useful
_rt_start_failure = _rthost.start_failure


def _wire_rt(client) -> None:
    """Policy-wire the rt host into the graph.

    The algorithm lives in `eqforge.system.wiring` so the daemon's automatic
    re-wiring and this manual command can never drift apart.
    """
    from eqforge.system import wiring

    def rt_command(cmd: dict) -> dict | None:
        r = client.call("rt_command", {"cmd": cmd}, autostart=False)
        return (r or {}).get("rt")

    report = wiring.wire_graph(rt_command, echo=out.echo)

    for err in report.errors:
        out.echo(out.yellow(f"  ! {err}"))
    if report.sink is None:
        if report.errors and not report.notes:
            raise SystemError(report.errors[0])
        return                              # wiring already said why
    if report.rt_node is None:
        raise EQForgeError("rt host not running",
                           hint="eqforge system start   "
                                "(or systemctl --user start eqforge-rt)")
    out.echo(f"  rt outputs \u2192 {report.sink} "
             f"({report.outputs_ok} connections)")
    out.echo(f"  streams \u2192 rt inputs "
             f"({report.streams_moved} new, {report.already_wired} already wired)")
    if not report.changed and report.skipped:
        out.echo(out.dim(f"  {report.skipped} stream(s) left on their current "
                         "target on purpose"))
    for note in report.notes:
        out.echo(out.dim(f"  note: {note}"))
    if report.errors and not report.changed:
        raise SystemError(report.errors[0],
                          hint="pw-link could not complete the rewiring")


# --------------------------------------------------------------- autostart --

def _systemctl(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """Run `systemctl --user <args>`; returns (rc, combined output).

    rc 127 means systemctl is not installed at all, which is reported
    differently from a unit that exists but failed to start.
    """
    exe = shutil.which("systemctl")
    if not exe:
        return 127, "systemctl not found"
    try:
        r = subprocess.run([exe, "--user", *args], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"systemctl {' '.join(args)} timed out"
    except OSError as e:
        return 1, f"systemctl failed: {e.strerror or e}"
    return r.returncode, (r.stdout + r.stderr).strip()


# `systemctl is-enabled`/`is-active` answer with one of these words on
# stdout. Anything else means systemd could not answer at all (no user bus
# in an ssh session, for instance) and the reply is a multi-line error that
# must never be squeezed into a status table.
_ENABLED_WORDS = frozenset({
    "enabled", "enabled-runtime", "disabled", "static", "masked", "alias",
    "generated", "indirect", "transient", "bad", "not-found",
})
_ACTIVE_WORDS = frozenset({
    "active", "inactive", "failed", "activating", "deactivating",
    "reloading", "maintenance", "not-found",
})


def _unit_word(action: str, unit: str, known: frozenset,
               fallback: str = "unreachable") -> str:
    """One short word describing a unit, for tables that cannot hold prose."""
    rc, text = _systemctl([action, unit])
    if rc == 127:
        return "n/a"                     # no systemd at all
    first = (text or "").split("\n")[0].strip()
    return first if first in known else fallback


def _autostart_units() -> dict:
    from eqforge.system import module_config as mc
    return mc.autostart_unit_paths(paths.home())


def _daemon_argv(auto_wire: bool) -> list[str]:
    """The daemon command line for the unit - this interpreter, this package."""
    argv = [sys.executable, "-m", "eqforge", "daemon", "run", "--foreground"]
    if auto_wire:
        argv.append("--auto-wire")
    return argv


def _set_auto_wire_config(value: bool) -> None:
    """Persist auto-wire so a hand-started daemon matches the systemd one."""
    from eqforge.profiles.store import ProfileStore
    store = ProfileStore()
    cfg = store.config()
    cfg["auto_wire"] = value
    store.save_config(cfg)


def cmd_always_on(args) -> int:
    """`eqforge system always-on on|off|status`.

    Routing policy, not routing repair: this installs a WirePlumber rule that
    points *new* playback streams at EQForge, so they are processed from their
    first sample instead of being rewritten into the processor a moment later
    (which is audible as "the first second has no EQ").
    """
    from eqforge.system import always_on as ao

    mode = str(getattr(args, "autostart_action", None) or "status").lower()
    node = getattr(args, "node", None)
    as_json = bool(getattr(args, "json", False))

    if mode == "on":
        try:
            report = ao.enable(node, restart=bool(getattr(args, "now", False)),
                               force=bool(getattr(args, "force", False)))
        except EQForgeError as e:
            if as_json:
                out.echo_json({"ok": False, "error": e.message,
                               "hint": e.hint or ""})
            else:
                out.echo(out.yellow(f"  ! {e.message}"))
                if e.hint:
                    out.echo(out.dim(f"    {e.hint}"))
            return 1
        if as_json:
            out.echo_json(dict(report, ok=True))
            return 0
        word = {"created": out.green("created"),
                "same": out.dim("unchanged"),
                "updated": out.yellow("updated")}.get(report["status"],
                                                      report["status"])
        out.echo(f"  {out.green('✓')} always-on routing: streams → "
                 f"{out.cyan(report['node'])}")
        out.echo(f"    {word}  {report['file']}")
        if report.get("backup"):
            out.echo(out.dim(f"    previous version saved as {report['backup']}"))
        if report["restarted"]:
            out.echo(f"    {out.green('✓')} wireplumber restarted")
        else:
            out.echo(out.dim("    apply it with: systemctl --user restart "
                             "wireplumber"))
        out.echo(out.dim("""
  What this changes: new application streams are created on EQForge's inputs,
  so the very first sample is processed instead of the first ~50 ms being
  rewired by the daemon (or up to 2 s with polling). The daemon still watches
  the graph and will re-route anything the policy does not cover.
"""))
        return 0

    if mode == "off":
        res = ao.disable()
        if as_json:
            out.echo_json(res)
            return 0
        if res["removed"]:
            out.echo(f"  {out.green('✓')} always-on rule removed "
                     f"({res['file']})")
            out.echo(out.dim("    apply it with: systemctl --user restart "
                             "wireplumber"))
            out.echo(out.dim("    (the daemon keeps re-routing streams, just "
                             "not before their first sample)"))
        else:
            out.echo(out.dim("  always-on rule was not installed"))
        return 0

    st = ao.status()
    if as_json:
        out.echo_json(st)
        return 0 if st["ok"] else 1
    ver = st["wireplumber"]
    rows = [
        ("rule file", out.green(str(st["file"])) if st["installed"]
         else out.dim("not installed")),
        ("target node", out.cyan(st["node"] or "—")),
        ("node in graph", out.green("yes") if st["node_present"]
         else out.dim("no")),
        ("wireplumber", ".".join(map(str, ver)) if ver else "unknown"),
        ("node.rules", out.green("supported") if st["supported"]
         else out.yellow("unsupported by this WirePlumber")),
    ]
    out.echo(out.kv_table(rows, indent="\n  "))
    out.echo()
    if st["installed"] and not st["node_present"]:
        out.echo(out.yellow(
            "  ! the rule targets a node that is not in the graph right now: "
            "start processing\n    (`eqforge system start`) or re-run "
            "`eqforge system always-on on`"))
    return 0 if st["ok"] else 1


def cmd_autostart(args) -> int:
    """Install or remove the systemd user units that start EQForge at login.

    Two units, because systemd supervises processes better than a Python
    thread can: it restarts the rt host on crash, logs each one separately
    under `journalctl --user -u <unit>`, and applies RT scheduling. The
    daemon then only does what it is good at - profiles, the GUI and
    keeping the graph wired.
    """
    import os

    from eqforge.system import jack, module_config as mc, rthost
    mode = str(getattr(args, "autostart_action", None) or "status").lower()
    units = _autostart_units()

    if mode == "status":
        from eqforge.profiles.store import ProfileStore
        rows = []
        for key in ("rt", "daemon"):
            unit = units[key]
            if unit.exists():
                # one short word each: a table column cannot hold the
                # multi-line error systemd prints when the bus is gone
                enabled = _unit_word("is-enabled", unit.name, _ENABLED_WORDS)
                active = _unit_word("is-active", unit.name, _ACTIVE_WORDS)
                rows.append([unit.name,
                             out.green("installed"),
                             enabled,
                             active])
            else:
                rows.append([unit.name, out.dim("absent"), "-", "-"])
        out.echo(out.table(["unit", "file", "enabled", "active"], rows,
                           max_widths=[26, 12, 14, 12]))
        auto_wire = bool(ProfileStore().config().get("auto_wire"))
        out.echo(f"\n  auto-wire: "
                 f"{out.green('on') if auto_wire else out.dim('off')}"
                 f"  {out.dim(f'({paths.config_file()})')}")
        return 0

    if mode == "on":
        rt = _resolve_rt_binary(getattr(args, "rt_binary", None))
        auto = getattr(args, "auto_wire", None)
        auto_wire = True if auto is None else bool(auto)
        channels = int(getattr(args, "channels", None) or 2)
        info = jack.inspect()
        wrapper = jack.boot_wrapper(info)
        rt_argv = rthost.launch_argv(rt, paths.rt_socket(),
                                     paths.active_dsp_file(), channels,
                                     wrapper=wrapper)
        # Directory that *contains* the eqforge package, so `-m eqforge`
        # resolves even for a plain checkout that was never pip-installed.
        pkg_parent = str(Path(__file__).resolve().parents[2])
        venv_bin = str(Path(sys.executable).resolve().parent)
        existing = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                    if p]
        # venv first so the daemon resolves the same interpreter/tools the
        # user gets interactively; de-duplicated, order preserved.
        path_env = os.pathsep.join(dict.fromkeys([venv_bin, *existing]))

        units["rt"].parent.mkdir(parents=True, exist_ok=True)
        results = []
        for key, content in (
                ("rt", mc.render_rt_unit(rt_argv)),
                ("daemon", mc.render_daemon_unit(_daemon_argv(auto_wire),
                                                 pkg_parent, path_env))):
            status, backup = mc.write_managed(units[key], content)
            results.append((units[key], status, backup))

        _set_auto_wire_config(auto_wire)

        out.echo(f"  {out.green('✓')} autostart enabled")
        for path, status, backup in results:
            if status == "created":
                out.echo(f"    {out.green('created')}  {path}")
            elif status == "same":
                out.echo(f"    {out.dim('unchanged')} {path}")
            else:
                out.echo(f"    {out.yellow('updated')}  {path}")
                if backup is not None:
                    out.echo(f"      previous version saved as {backup}")
        out.echo(out.dim(f"    rt host: {' '.join(rt_argv)}"))
        if wrapper:
            out.echo(out.dim(
                f"    ({Path(wrapper[0]).name} makes the host load PipeWire's "
                "libjack instead of jack2's)"))
            if info.jackd_running:
                out.echo(out.yellow(
                    "    ! a standalone jackd is running right now, but this "
                    "unit targets PipeWire at login"))
        else:
            out.echo(out.dim(
                "    (no pipewire-jack installed: the host will use the system "
                "libjack, so a jackd has to be running)"))
        out.echo(out.dim(f"    auto-wire: {'on' if auto_wire else 'off'} "
                         f"(persisted in {paths.config_file()})"))

        rc, text = _systemctl(["daemon-reload"])
        if rc != 0:
            out.echo(out.yellow(f"  ! systemctl daemon-reload: {text or rc}"))
        rc, text = _systemctl(["enable", "--now",
                               units["rt"].name, units["daemon"].name])
        if rc == 0:
            out.echo(f"  {out.green('✓')} units enabled and started")
            return 0
        if rc == 127:
            out.echo(out.yellow("  ! systemctl not found - the unit files are "
                                "written but not enabled"))
        else:
            out.echo(out.yellow(f"  ! could not enable the units: "
                                f"{text or rc}"))
        out.echo(out.dim("""
  run these yourself once systemd is reachable:
    systemctl --user daemon-reload
    systemctl --user enable --now eqforge-rt.service eqforge-daemon.service
    journalctl --user -u eqforge-rt -f
"""))
        return 1

    if mode == "off":
        rc, text = _systemctl(["disable", "--now",
                               units["rt"].name, units["daemon"].name])
        if rc not in (0, 127):
            out.echo(out.yellow(f"  ! disable: {text or rc}"))
        removed = []
        for key in ("rt", "daemon"):
            p = units[key]
            if p.exists():
                try:
                    p.unlink()
                    removed.append(p)
                except OSError as e:
                    out.echo(out.yellow(f"  ! could not remove {p}: "
                                        f"{e.strerror or e}"))
        _set_auto_wire_config(False)
        out.echo(f"  {out.green('✓')} autostart disabled")
        for p in removed:
            out.echo(f"    removed {p}")
        out.echo(out.dim("  a daemon or rt host you started by hand is still "
                         "running: eqforge system stop / eqforge daemon stop"))
        return 0

    raise EQForgeError(f"unknown autostart action {mode!r}",
                       hint="on | off | status")



# ------------------------------------------------------------------ doctor --

def cmd_doctor(args) -> int:
    """Comprehensive environment + pipeline self-test."""
    from eqforge.audio.io import formats_available
    from eqforge.daemon import client
    from eqforge.native.loader import info as native_info, load
    from eqforge.system import pipewire as pw

    results: list[tuple[str, str, str]] = []

    def check(name: str, fn, fix: str = ""):
        try:
            ok, detail = fn()
            results.append((name, "PASS" if ok else "WARN", detail +
                            (f"  ({fix})" if not ok and fix else "")))
        except Exception as e:  # noqa: BLE001
            results.append((name, "FAIL", f"{e}" + (f"  ({fix})" if fix else "")))

    nat = native_info()
    if not nat["available"] and args.build_core:
        try:
            from eqforge.native.loader import build_native
            build_native()
            load(rebuild=False)
            nat = native_info()
        except Exception as e:  # noqa: BLE001
            results.append(("native core", "FAIL", f"build failed: {e}"))
    check("native core",
          lambda: (nat["available"],
                   nat.get("path", "") if nat["available"]
                   else "numpy fallback active"),
          fix="make -C core  or  eqforge doctor --build-core")
    check("python deps",
          lambda: (_deps_ok(), _deps_detail()),
          fix="pip install -r requirements.txt")
    fa = formats_available()
    check("audio formats",
          lambda: (True, f"decode: {','.join(fa['decode'][:6])}…"),
          )
    check("ffmpeg (mp3 etc.)",
          lambda: (bool(fa["ffmpeg"]), fa["ffmpeg"] or "not installed"),
          fix="apt install ffmpeg")
    check("pipewire tools",
          lambda: (pw.pipewire_available(),
                   "pw-dump found" if pw.pipewire_available() else "not found"),
          fix="apt install pipewire pipewire-audio")
    check("default sink",
          lambda: (bool(pw.default_sink()) or not pw.pipewire_available(),
                   pw.default_sink() or "-"),
          )
    check("daemon",
          lambda: (client.daemon_running(),
                   "running" if client.daemon_running() else "not running"),
          fix="eqforge daemon start")
    check("rt host binary", _rt_binary_check,
          fix="make -C rthost && make -C rthost install")
    check("jack transport", _jack_check,
          fix=_jack_fix_hint())
    check("rt host",
          lambda: (_rt_ok(client), _rt_ok_detail(client)),
          fix="eqforge system start")

    # end-to-end pipeline self-test on synthetic audio
    check("dsp pipeline e2e", _e2e_check)

    ok = all(r[1] != "FAIL" for r in results)
    for name, status, detail in results:
        color = {"PASS": out.green, "WARN": out.yellow, "FAIL": out.red}[status]
        out.echo(f"  {color(status.ljust(4))}  {name.ljust(22)} {out.dim(detail)}")
    out.echo()
    if ok:
        warns = sum(1 for r in results if r[1] == "WARN")
        out.echo(out.green("  EQForge is healthy ✓") +
                 (out.dim(f"  ({warns} warning"
                          f"{'s' if warns != 1 else ''} above)")
                  if warns else ""))
    else:
        out.echo(out.red("  some checks failed - see hints above"))
    return 0 if ok else 1


def _deps_ok() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        return True
    except ImportError:
        return False


def _deps_detail() -> str:
    import importlib.metadata as md
    try:
        return f"numpy {md.version('numpy')}, scipy {md.version('scipy')}"
    except md.PackageNotFoundError:
        return "numpy/scipy present (unversioned install)"


def _rt_binary_check() -> tuple[bool, str]:
    """Does a usable eqforge-rt exist at all? (precondition for `system start`)"""
    rt = _rt_binary()
    if rt is None:
        return False, "eqforge-rt not found - `system start` cannot work"
    return True, str(rt)


def _jack_fix_hint() -> str:
    from eqforge.system import jack
    return jack.PIPEWIRE_JACK_SHORT


def _jack_check() -> tuple[bool, str]:
    """Is a JACK-compatible server reachable, with only one JACK stack installed?

    The rt host is a JACK client, and the jack2-vs-pipewire-jack conflict is
    the most common reason it dies at startup reporting
    "cannot open JACK client (17)". Nothing else in `doctor` looked at that
    layer, so the failure surfaced only as a log file to go and read.
    """
    from eqforge.system import jack
    info = jack.inspect()
    return info.usable and not info.conflict, info.detail


def _rt_ok(client) -> bool:
    if not client.daemon_running():
        return False
    r = client.call("rt_command", {"cmd": {"cmd": "status"}})
    return bool((r.get("rt") or {}).get("ok"))


def _rt_ok_detail(client) -> str:
    return "running" if _rt_ok(client) else "not running (optional)"


def _e2e_check() -> tuple[bool, str]:
    import numpy as np
    from eqforge.audio import synth
    from eqforge.dsp.engine import render
    cfg = {"preamp_db": -6.0,
           "eq": {"filters": [{"type": "peak", "freq": 1000.0,
                               "gain_db": 6.0, "q": 1.0, "enabled": True}]},
           "limiter": {"enabled": True, "ceiling_db": -1.0,
                       "attack_ms": 5, "release_ms": 50, "lookahead_ms": 1.5}}
    x = synth.sine(1000.0, 0.5, 48000, amplitude=0.5)
    r = render(x, 48000, cfg)
    before = float(np.max(np.abs(x)))
    after = float(np.max(np.abs(r.data)))
    gain_db = 20 * np.log10(after / before)
    ok = 2.5 < gain_db < 3.5  # +6 EQ -6 preamp ≈ 0… but limiter ceiling:
    # 0.5*10^0 = 0.5 -> below ceiling, so expect ~0 dB net (+6-6)
    ok = abs(gain_db) < 0.6
    return ok, f"render gain {gain_db:+.2f} dB via {r.backend} backend"


# ----------------------------------------------------------------- version --

def cmd_version(args) -> int:
    from eqforge.native.loader import info as native_info
    nat = native_info()
    out.echo(f"  eqforge {_version()}")
    if nat["available"]:
        out.echo(f"  native core {nat['version']} (ABI {nat['abi']}) at "
                 f"{nat['path']}")
    else:
        out.echo(out.yellow("  native core not loaded - numpy fallback"))
    return 0
