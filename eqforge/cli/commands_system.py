"""CLI commands: runtime/system (devices, streams, status, daemon, gui, system, doctor)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from eqforge import paths
from eqforge.cli import output as out
from eqforge.errors import EQForgeError


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
    from eqforge.system import pipewire as pw
    rows.append(("pipewire", out.green("available") if pw.pipewire_available()
                 else out.dim("not found")))
    if pw.pipewire_available():
        rows.append(("default sink", pw.default_sink() or "?"))
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

def cmd_daemon(args) -> int:
    from eqforge.daemon import client
    action = args.action
    if action == "run":
        from eqforge.daemon.server import Daemon
        d = Daemon()
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
            return 0
        client.spawn_daemon()
        deadline = time.time() + 6
        while time.time() < deadline:
            if client.daemon_running():
                out.echo(f"  {out.green('✓')} daemon started")
                base = client.http_base()
                if base:
                    out.echo(out.dim(f"    GUI: {base}"))
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
        return cmd_daemon(type("A", (), {"action": "start"})())
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
        written = write_system_setup(paths,
                                     exec_path=Path(args.rt_binary)
                                     if args.rt_binary else None)
        out.echo(f"  {out.green('✓')} wrote integration files:")
        for w in written:
            out.echo(f"    {w}")
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
        rt = _rt_binary()
        if rt is None:
            raise EQForgeError(
                "eqforge-rt binary not found",
                hint="build it: make -C rthost && make -C rthost install, "
                     "or pass --rt-binary PATH")
        sock = paths.rt_socket()
        if sock.exists():
            r = client.call("rt_command", {"cmd": {"cmd": "status"}})
            if r.get("rt", {}).get("ok"):
                out.echo("  rt host already running")
                return 0
            sock.unlink(missing_ok=True)
        paths.ensure_dirs()
        cfg = paths.active_dsp_file()
        if not client.daemon_running():
            client.spawn_daemon()
            time.sleep(0.5)
        client.call("status")  # ensures active-dsp.json exists
        logfile = open(paths.log_dir() / "rt.out", "ab")
        cmd = [str(rt), "--name", "eqforge", "--socket", str(sock),
               "--config", str(cfg), "--channels", str(args.channels or 2)]
        proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile,
                                start_new_session=True,
                                stdin=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)
        if proc.poll() is not None or not sock.exists():
            raise EQForgeError("rt host failed to start",
                               hint=f"see {paths.log_dir() / 'rt.out'}")
        out.echo(f"  {out.green('✓')} eqforge-rt running (pid {proc.pid})")
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

    raise EQForgeError(f"unknown system action {action!r}")


def _rt_binary() -> Path | None:
    for cand in (Path(os.environ.get("EQFORGE_RT", "")),
                 Path(__file__).resolve().parents[2] / "rthost/build/eqforge-rt",
                 Path("/usr/libexec/eqforge/eqforge-rt"),
                 Path("/usr/local/libexec/eqforge/eqforge-rt")):
        if cand and cand.exists() and os.access(cand, os.X_OK):
            return cand
    which = shutil.which("eqforge-rt")
    return Path(which) if which else None


def _wire_rt(client) -> None:
    """Policy-wire the rt host into the graph (full chain, best effort).

    1. connects rt-host outputs -> default sink playback ports, and
    2. moves application playback streams that currently feed the default
       sink onto the rt-host inputs, so audio actually flows
       app -> eqforge -> sink.
    Idempotent: already-correct links are skipped.
    """
    from eqforge.system import pipewire as pw
    sink_name = pw.default_sink()
    if not sink_name:
        out.echo(out.yellow("  no default sink to wire to"))
        return
    sink = next((d for d in pw.list_devices() if d.node_name == sink_name),
                None)
    if sink is None:
        out.echo(out.yellow(f"  default sink {sink_name!r} not in graph"))
        return
    r = client.call("rt_command", {"cmd": {"cmd": "status"}}, autostart=False)
    rt = (r or {}).get("rt") or {}
    if not rt.get("ok"):
        raise EQForgeError("rt host not running",
                           hint="eqforge system start   "
                                "(or systemctl --user start eqforge-rt)")
    rt_node = str(rt.get("client", "eqforge"))
    channels = int(rt.get("channels", 2) or 2)

    names = pw.node_ports(sink.id, "input")
    full = pw.node_port_targets(sink.id, "input", sink_name)
    pairs = [(n, f) for n, f in zip(names, full)
             if not n.startswith("monitor")]
    playback = [n for n, _ in pairs]
    targets = [f for _, f in pairs]
    if not playback:
        playback = [f"playback_{s}" for s in ("FL", "FR", "FC", "LFE")]
        targets = [f"{sink_name}:{p}" for p in playback]
    playback = playback[:channels]
    targets = targets[:channels]

    # 1) rt outputs -> sink playback ports (handled inside the rt host)
    r = client.call("rt_command",
                    {"cmd": {"cmd": "connect", "outputs": targets}},
                    autostart=False)
    connected = int((r or {}).get("rt", {}).get("connected", 0))

    # verify through the graph; fall back to pw-link for anything missing
    # (jack_connect can fail when the JACK namespace disagrees with PW's)
    existing = set(pw.links())
    for c, port in enumerate(playback):
        link = (f"{rt_node}:out_{c + 1}", f"{sink_name}:{port}")
        if link in existing:
            connected = max(connected, c + 1)
            continue
        if shutil.which("pw-link"):
            rc = subprocess.run(["pw-link", link[0], link[1]],
                                capture_output=True)
            if rc.returncode == 0:
                connected += 1
    out.echo(f"  rt outputs → {sink_name} ({connected} connections)")

    # 2) app streams feeding the sink -> rt inputs
    existing = set(pw.links())
    sink_inputs = {f"{sink_name}:{p}" for p in playback}
    rt_inputs = [f"{rt_node}:in_{c + 1}" for c in range(channels)]
    moved = already = 0
    for st in pw.list_streams():
        if st.direction != "playback":
            continue
        if st.node_name in (rt_node, sink_name) or not st.node_name:
            continue
        linked = [(o, i) for o, i in existing
                  if o.startswith(f"{st.node_name}:")]
        if not linked:
            continue                      # dormant stream, leave it
        if all(i in rt_inputs for _, i in linked):
            already += 1
            continue                      # already through the rt host
        if not any(i in sink_inputs for _, i in linked):
            continue                      # lives on another node on purpose
        for c, port in enumerate(pw.node_ports(st.id, "output")[:channels]):
            src = f"{st.node_name}:{port}"
            dst = rt_inputs[c]
            if (src, dst) in existing:
                continue
            for o, i in linked:           # unhook from the sink first
                if o == src:
                    subprocess.run(["pw-link", "-d", src, i],
                                   capture_output=True)
            rc = subprocess.run(["pw-link", src, dst], capture_output=True)
            if rc.returncode == 0:
                moved += 1
            else:
                err = rc.stderr.decode(errors="replace").strip()[:120]
                out.echo(out.yellow(f"  ! {src} → {dst}: {err or 'failed'}"))
    out.echo(f"  streams → rt inputs "
             f"({moved} new, {already} already wired)")
    if moved == 0 and already == 0:
        out.echo(out.dim("  note: no app streams were playing into the "
                         "default sink right now; new streams may need "
                         "`eqforge system wire` again (or use the SPA "
                         "filter path for automatic routing)"))


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
        out.echo(out.green("  EQForge is healthy ✓"))
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
