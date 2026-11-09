"""Actions used by the graphical interface.

The GUI is a client of the daemon, but it must not be a second-class client:
operations that previously printed instructions to run a shell command live
here, next to the same system/profile primitives used by the CLI.  Keeping
these functions outside the HTTP dispatcher also makes them straightforward
to test without a browser.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from eqforge import paths
from eqforge.errors import EQForgeError, SystemError


MAX_UPLOAD_BYTES = 128 * 1024 * 1024


def _safe_name(name: str, default: str = "upload") -> str:
    """A browser supplied basename, never a path chosen by the browser."""
    name = Path(str(name or default)).name
    return name[:180] or default


def import_profile(store, filename: str, content: str,
                   profile_id: str | None = None,
                   name: str | None = None) -> dict:
    """Import an EQForge/AutoEQ/REW/APO/CSV document selected in the browser."""
    if not isinstance(content, str) or not content.strip():
        raise EQForgeError("the selected profile file is empty")
    if len(content.encode("utf-8")) > 4 * 1024 * 1024:
        raise EQForgeError("profile imports are limited to 4 MB")
    filename = _safe_name(filename, "profile.txt")
    if not Path(filename).suffix:
        filename += ".txt"

    from eqforge.importers.detect import detect_and_import

    with tempfile.TemporaryDirectory(prefix="eqforge-import-") as tmp:
        src = Path(tmp) / filename
        src.write_text(content, encoding="utf-8")
        prof = detect_and_import(src, store=store, new_id=profile_id or None,
                                 name=name or None)

    # Importing a preset twice must not silently overwrite the user's edited
    # copy. An explicitly entered id is intentional; automatic ids get a
    # readable numeric suffix instead.
    if not profile_id:
        existing = {p["id"] for p in store.list_ids()}
        base = prof.id
        n = 2
        while prof.id in existing:
            prof.id = f"{base}-{n}"
            n += 1
    prof.builtin = False
    path = store.save(prof)
    return {"id": prof.id, "name": prof.name, "path": str(path),
            "filters": len(prof.filters)}


def export_profile(store, profile_id: str) -> dict:
    """Return a self-contained profile for an in-browser download."""
    resolved = store.resolve(profile_id)
    resolved.pop("extends", None)
    resolved["exported_from"] = profile_id
    import time
    resolved["exported_at"] = time.time()
    return {"filename": f"{profile_id}.eqforge.json",
            "content": json.dumps(resolved, indent=2) + "\n"}


def decode_audio_upload(filename: str, encoded: str, directory: str) -> Path:
    """Validate and persist one browser upload for the audio readers."""
    if not isinstance(encoded, str) or not encoded:
        raise EQForgeError("missing audio upload data")
    # FileReader produces a base64 payload. Estimate before decoding so a
    # maliciously large request cannot allocate twice the allowed size.
    if len(encoded) > (MAX_UPLOAD_BYTES * 4 // 3 + 8):
        raise EQForgeError("audio uploads are limited to 128 MB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise EQForgeError("invalid audio upload encoding") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise EQForgeError("audio uploads are limited to 128 MB")
    if not raw:
        raise EQForgeError("the selected audio file is empty")
    filename = _safe_name(filename, "audio.wav")
    if not Path(filename).suffix:
        filename += ".wav"
    path = Path(directory) / filename
    path.write_bytes(raw)
    return path


def suggest_eq(spectrum: dict, target: str = "harman-overear",
               max_gain: float = 6.0, max_filters: int = 8) -> dict:
    """Turn an analysis overlay into editable corrective bands."""
    import numpy as np
    from eqforge.smart import targets as targets_mod

    if not isinstance(spectrum, dict):
        raise EQForgeError("spectrum must be an object")
    try:
        freqs = np.asarray(spectrum.get("freqs", []), dtype=float)
        db = np.asarray(spectrum.get("db", []), dtype=float)
    except (TypeError, ValueError) as exc:
        raise EQForgeError("invalid spectrum data") from exc
    if len(freqs) < 4 or len(freqs) != len(db):
        raise EQForgeError("not enough spectrum data to suggest EQ")
    valid = np.isfinite(freqs) & np.isfinite(db) & (freqs > 0)
    freqs, db = freqs[valid], db[valid]
    if len(freqs) < 4:
        raise EQForgeError("not enough valid spectrum data to suggest EQ")
    target = str(target or "harman")
    try:
        filters = targets_mod.match_eq(
            db, freqs, target, max_gain_db=float(max_gain),
            max_filters=int(max_filters))
        summary = targets_mod.deviation_summary(db, freqs, target)
    except (KeyError, ValueError) as exc:
        raise EQForgeError(f"cannot match target {target!r}: {exc}") from exc
    return {"target": target, "suggested_filters": filters,
            "deviation": summary}


def set_default_device(node_name: str) -> dict:
    from eqforge.system import pipewire as pw

    sinks = {d.node_name: d for d in pw.list_devices(direction="sink")}
    if node_name not in sinks:
        raise EQForgeError(f"unknown output device {node_name!r}")
    pw.set_default_sink(node_name)
    return {"default_sink": node_name,
            "description": sinks[node_name].description}


def start_rt(daemon, channels: int = 2) -> dict:
    from eqforge.system import rthost

    if rthost.is_running():
        return {"running": True, "already_running": True,
                "rt": daemon.state.rt_status()}
    paths.rt_socket().unlink(missing_ok=True)
    daemon.state.ensure_active_config()
    launched = rthost.start(channels=channels)
    daemon._rt_proc = launched.proc
    daemon._rt_stop_requested = False
    daemon._wire_sig = None
    daemon.state.sync_rt()
    daemon.bus.publish("rt_started", {"pid": launched.pid,
                                      "wrapper": launched.wrapper})
    return {"running": True, "already_running": False,
            "pid": launched.pid, "host": str(launched.rt),
            "wrapper": launched.wrapper, "rt": daemon.state.rt_status()}


def stop_rt(daemon) -> dict:
    daemon.state._tell_rt({"cmd": "quit"})
    daemon._rt_stop_requested = True
    proc = getattr(daemon, "_rt_proc", None)
    if proc is not None:
        from eqforge.system import rthost
        rthost.stop(proc)
    daemon._rt_proc = None
    paths.rt_socket().unlink(missing_ok=True)
    return {"running": False}


def system_overview(daemon) -> dict:
    """Everything the System page needs, including setup/startup health."""
    from eqforge.native.loader import info as native_info
    from eqforge.system import always_on, rthost
    from eqforge.cli import commands_system as cs

    units = cs._autostart_units()
    autostart = []
    for key in ("rt", "daemon"):
        unit = units[key]
        installed = unit.exists()
        autostart.append({
            "name": unit.name, "installed": installed,
            "enabled": cs._unit_word("is-enabled", unit.name, cs._ENABLED_WORDS)
            if installed else "not-found",
            "active": cs._unit_word("is-active", unit.name, cs._ACTIVE_WORDS)
            if installed else "not-found",
        })
    try:
        always = always_on.status()
    except (EQForgeError, OSError) as exc:
        always = {"installed": False, "ok": False, "error": str(exc)}
    binary = rthost.find_binary()
    return {
        "native": native_info(),
        "rt_binary": str(binary) if binary else None,
        "autostart": autostart,
        "always_on": always,
        "auto_wire": daemon.auto_wire,
        "manage_rt": daemon.manage_rt,
        "rt": daemon.state.rt_status(),
    }


def system_setup() -> dict:
    from eqforge.cli import commands_system as cs
    from eqforge.system import module_config
    from eqforge.system import rthost

    binary = rthost.find_binary() or Path(rthost.SYSTEM_PATHS[0])
    results = []
    module_config.write_system_setup(paths, exec_path=binary, results=results)
    rc, detail = cs._systemctl(["daemon-reload"])
    return {"files": [{"path": str(path), "status": status,
                       "backup": str(backup) if backup else None}
                      for path, status, backup in results],
            "daemon_reload": rc == 0,
            "detail": detail}


def autostart(value: bool) -> dict:
    """Use the CLI's unit generator without leaking terminal output to users."""
    from eqforge.cli import commands_system as cs

    args = SimpleNamespace(autostart_action="on" if value else "off",
                           rt_binary=None, auto_wire=True, channels=2,
                           json=True)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        rc = cs.cmd_autostart(args)
    text = output.getvalue().strip()
    if rc not in (0, 1):
        raise SystemError("could not change login autostart",
                          hint=text[-1200:] or None)
    return {"enabled": bool(value), "complete": rc == 0,
            "detail": text[-1600:]}


def always_on(value: bool) -> dict:
    from eqforge.system import always_on as ao

    if value:
        report = ao.enable(restart=True)
    else:
        report = ao.disable()
        restarted, detail = ao._restart_wireplumber()
        report.update({"restarted": restarted,
                       "error": None if restarted else detail})
    report["enabled"] = bool(value)
    return report


def doctor() -> dict:
    """JSON counterpart of `eqforge doctor`, with no argparse/printing."""
    from eqforge.audio.io import formats_available
    from eqforge.cli import commands_system as cs
    from eqforge.native.loader import info as native_info
    from eqforge.system import jack, pipewire as pw, rthost

    checks = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "status": "PASS" if ok else "WARN",
                       "detail": str(detail), "hint": hint})

    native = native_info()
    add("Native DSP core", native["available"],
        native.get("path", "NumPy fallback active") if native["available"]
        else "NumPy fallback active",
        "Build the native core for the lowest DSP overhead.")
    try:
        add("Python dependencies", cs._deps_ok(), cs._deps_detail(),
            "Install the project's Python requirements.")
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        add("Python dependencies", False, str(exc))
    formats = formats_available()
    add("Audio formats", True,
        "Decode: " + ", ".join(formats["decode"][:8]))
    add("Compressed audio", bool(formats["ffmpeg"]),
        formats["ffmpeg"] or "ffmpeg not installed",
        "Install ffmpeg to process MP3, M4A, AAC and Opus files.")
    available = pw.pipewire_available()
    add("PipeWire", available, "Available" if available else "Tools not found",
        "Install and start PipeWire for system-wide processing.")
    add("Default output", bool(pw.default_sink()) or not available,
        pw.default_sink() or "No default output")
    binary = rthost.find_binary()
    add("Real-time host", binary is not None,
        str(binary) if binary else "eqforge-rt binary not found",
        "Build or install the real-time host to enable live processing.")
    jack_info = jack.inspect()
    add("JACK transport", jack_info.usable and not jack_info.conflict,
        jack_info.detail, jack_info.fix or "")
    running = rthost.is_running()
    add("Live engine", running, "Running" if running else "Stopped (optional)",
        "Use Start processing on the System tab.")
    try:
        ok, detail = cs._e2e_check()
        add("DSP pipeline", ok, detail)
    except Exception as exc:  # noqa: BLE001
        add("DSP pipeline", False, str(exc))
    return {"checks": checks,
            "healthy": all(c["status"] == "PASS" for c in checks),
            "warnings": sum(c["status"] != "PASS" for c in checks)}


def runtime_log(lines: int = 100) -> dict:
    """Small diagnostics view; avoids asking the user to inspect log files."""
    from eqforge.system.rthost import log_tail

    lines = max(1, min(int(lines or 100), 300))
    return {"rt": log_tail(paths.log_dir() / "rt.out", lines=lines,
                           window=131072),
            "daemon": log_tail(paths.log_dir() / "daemon.out", lines=lines,
                               window=131072)}
