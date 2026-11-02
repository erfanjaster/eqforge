"""Daemon server: Unix-socket JSON-RPC + localhost HTTP (GUI, REST, SSE).

Single process, three threads:
  - RPC loop on $XDG_RUNTIME_DIR/eqforge/daemon.sock (line-delimited JSON)
  - HTTP server on 127.0.0.1 (GUI assets, /api/* REST, /api/events SSE)
  - device watcher: polls the PipeWire graph (~2 s) and applies matching
    rules when auto-switch is enabled; emits SSE events on changes. With
    `auto_wire` it also re-links any stream that stopped passing through
    the rt host, and with `manage_rt` it keeps that host alive.

Why the watcher owns re-wiring: PipeWire destroys a stream node when a
player stops and creates a fresh one when it starts, which WirePlumber
links straight to the default sink. The rt-host deployment therefore loses
each stream the moment it restarts, and audio quietly stops being processed
until someone runs `eqforge system wire` again.

The HTTP server is stdlib-only (no framework) to keep the runtime footprint
small and the dependency surface zero.
"""
from __future__ import annotations

import json
import os
import selectors
import socket
import socketserver
import threading
import time
import traceback
import urllib.parse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eqforge import paths
from eqforge.daemon.state import EngineState
from eqforge.errors import EQForgeError
from eqforge.log import get_logger
from eqforge.system import pipewire as pw
from eqforge.system.matching import collect_rules, select_profile

log = get_logger("daemon.server")

RPC_VERSION = 1


class EventBus:
    """Tiny pub-sub for SSE clients."""

    def __init__(self):
        self._subs: list[list[dict]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> list[dict]:
        q: list[dict] = []
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: list[dict]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: str, data: dict) -> None:
        msg = {"event": event, "data": data, "t": time.time()}
        with self._lock:
            for q in self._subs:
                if len(q) < 512:
                    q.append(msg)


class Daemon:
    # Class-level defaults, so a Daemon built without __init__ (the existing
    # test fixture uses Daemon.__new__ and assigns only what it needs) still
    # has a coherent configuration instead of raising AttributeError.
    auto_wire = False
    manage_rt = False
    rt_channels = 2
    tick = 2.0
    rt_retry = 15.0
    _rt_proc = None
    _rt_last_try = 0.0
    _rt_stop_requested = False
    _wire_sig: str | None = None
    _wire_ok = False

    def __init__(self, auto_wire: bool | None = None, manage_rt: bool = False,
                 rt_channels: int = 2, tick: float = 2.0,
                 rt_retry: float = 15.0):
        self.state = EngineState()
        self.bus = EventBus()
        self.stop_event = threading.Event()
        self.started = time.time()
        self.tick = float(tick)
        self.rt_retry = float(rt_retry)
        self.manage_rt = bool(manage_rt)
        self.rt_channels = int(rt_channels or 2)
        self._rt_proc = None
        self._rt_last_try = 0.0
        self._rt_stop_requested = False
        self._wire_sig: str | None = None
        self._wire_ok = False
        # `auto_wire=None` means "whatever config.json says", so a daemon
        # started by hand behaves like the one systemd starts. It is off
        # unless asked for: the daemon is spawned implicitly by ordinary
        # commands (`eqforge status`), and silently rewiring the user's audio
        # graph from there would be a nasty surprise.
        if auto_wire is None:
            try:
                auto_wire = bool(self.state.store.config().get("auto_wire"))
            except Exception:                       # noqa: BLE001
                auto_wire = False
        self.auto_wire = bool(auto_wire)
        self.state.ensure_active_config()
        self.state.sync_rt()          # best effort; watcher retries
        if self.auto_wire or self.manage_rt:
            log.info("daemon: auto_wire=%s manage_rt=%s",
                     self.auto_wire, self.manage_rt)

    # ---------------- RPC dispatch ----------------
    @staticmethod
    def _require_str(params: dict, key: str, what: str = "") -> str:
        """Fetch a required string parameter or raise a clean RPC error.

        Without this, `params["profile"]` on a malformed request raised
        KeyError, which the HTTP layer turned into a 500 "internal: 'profile'"
        - an unhelpful crash that also told a caller nothing about what to fix.
        """
        if key not in params:
            raise EQForgeError(f"missing required parameter {key!r}"
                               + (f" ({what})" if what else ""))
        value = params[key]
        if not isinstance(value, str) or not value.strip():
            raise EQForgeError(f"parameter {key!r} must be a non-empty string")
        return value

    def rpc(self, method: str, params: dict | None = None) -> dict:
        if params is not None and not isinstance(params, dict):
            raise EQForgeError("params must be a JSON object")
        params = params or {}
        st = self.state
        store = st.store

        if method == "ping":
            return {"ok": True, "version": _version(), "rpc": RPC_VERSION,
                    "pid": os.getpid()}

        if method == "shutdown":
            # Graceful stop. The reply is written by the connection handler
            # before the event loop observes stop_event, so clients get a reply.
            self.stop()
            return {"ok": True, "stopping": True}

        if method == "status":
            s = st.status()
            # Daemon-level supervision flags belong to the daemon, not to
            # EngineState, so they are added here.
            s["auto_wire"] = self.auto_wire
            s["manage_rt"] = self.manage_rt
            return s

        if method == "set_profile":
            slot = params.get("slot")
            if slot is not None and not isinstance(slot, str):
                raise EQForgeError("parameter 'slot' must be a string")
            return st.set_profile(self._require_str(params, "profile"),
                                  slot)

        if method == "bypass":
            if "value" in params:
                return st.set_bypass(bool(params["value"]))
            return st.toggle_bypass()

        if method == "ab":
            return st.ab_switch(params.get("slot"))

        if method == "list_profiles":
            return {"profiles": store.list_ids()}

        if method == "resolve_profile":
            prof_id = self._require_str(params, "profile")
            resolved = store.resolve_any(prof_id)
            from eqforge.profiles.resolve import to_dsp_config
            return {"resolved": resolved,
                    "dsp": to_dsp_config(resolved,
                                         st._base_dir(prof_id))}

        if method == "save_profile":
            from eqforge.profiles.model import Profile
            if "profile" not in params:
                raise EQForgeError("missing required parameter 'profile'")
            raw = params["profile"]
            if not isinstance(raw, dict):
                raise EQForgeError("parameter 'profile' must be an object")
            if not isinstance(raw.get("id"), str) or not raw["id"].strip():
                raise EQForgeError("profile.id must be a non-empty string")
            if not isinstance(raw.get("name"), str) or not raw["name"].strip():
                raise EQForgeError("profile.name must be a non-empty string")
            prof = Profile.from_dict(raw)
            path = store.save(prof, raw=raw)
            self.bus.publish("profiles_changed", {})
            result: dict = {"path": str(path)}
            # Surface non-fatal validation findings: a profile that saves but
            # plays nothing (bands under the wrong key) must not look like a
            # clean success.
            warnings = [str(w) for w in getattr(store, "last_warnings", [])]
            if warnings:
                result["warnings"] = warnings
            return result

        if method == "delete_profile":
            store.delete(self._require_str(params, "profile"))
            self.bus.publish("profiles_changed", {})
            return {"ok": True}

        if method == "devices":
            return {"devices": [d.to_dict() for d in pw.list_devices()],
                    "default_sink": pw.default_sink(),
                    "available": pw.pipewire_available()}

        if method == "streams":
            return {"streams": [s.to_dict() for s in pw.list_streams()]}

        if method == "auto_switch":
            st.auto_switch = bool(params.get("value", not st.auto_switch))
            st._persist()
            return {"auto_switch": st.auto_switch}

        if method == "apply_rules_now":
            return self._apply_rules(reason="manual")

        if method == "analyze_file":
            return self._analyze_file(params)

        if method == "rt_command":
            cmd = params.get("cmd") or {}
            r = st._tell_rt(cmd)
            if isinstance(cmd, dict) and cmd.get("cmd") == "quit":
                # `eqforge system stop` relays its quit through here. A
                # supervisor that resurrects a host the user just asked to
                # stop is fighting them, so remember the intent - it is
                # dropped again as soon as a host is running.
                self._rt_stop_requested = True
                self._rt_proc = None
            return {"rt": r}

        if method == "wire":
            # Manual equivalent of `eqforge system wire`, for the GUI and
            # for scripts that talk to the daemon instead of the CLI.
            from eqforge.system import wiring
            report = wiring.wire_graph(st._tell_rt)
            self._wire_sig = None      # next tick re-checks from scratch
            return {"sink": report.sink, "rt_node": report.rt_node,
                    "outputs": report.outputs_ok,
                    "outputs_new": report.outputs_new,
                    "streams_moved": report.streams_moved,
                    "already_wired": report.already_wired,
                    "skipped": report.skipped,
                    "errors": report.errors, "notes": report.notes,
                    "changed": report.changed}

        if method == "set_auto_wire":
            if "value" not in params:
                raise EQForgeError("missing required parameter 'value'")
            value = bool(params["value"])
            self.auto_wire = value
            self._wire_sig = None
            cfg = st.store.config()
            cfg["auto_wire"] = value
            st.store.save_config(cfg)      # survives a daemon restart
            log.info("auto-wire %s", "enabled" if value else "disabled")
            self.bus.publish("auto_wire", {"enabled": value})
            return {"auto_wire": value}

        raise EQForgeError(f"unknown RPC method {method!r}")

    # ---------------- file analysis (GUI) ----------------
    def _analyze_file(self, params: dict) -> dict:
        from eqforge.analysis.report import analyze
        from eqforge.audio.io import read
        p = str(params.get("path", ""))
        if not p:
            raise EQForgeError("missing path")
        audio = read(p)
        report = analyze(audio.data, audio.sample_rate)
        resolved = None
        pid = params.get("profile") or self.state.active_profile
        try:
            resolved = self.state.store.resolve_any(pid)
        except EQForgeError:
            pid = None
        from eqforge.smart.advisor import advise
        advice = advise(report, resolved, audio.sample_rate)
        return {"path": p, "report": report.to_dict(),
                "advice": advice.to_dict(), "profile": pid}

    # ---------------- device watching ----------------
    def _apply_rules(self, reason: str) -> dict:
        st = self.state
        try:
            devices = pw.list_devices(direction="sink")
            default = pw.default_sink()
        except EQForgeError:
            return {"switched": False, "reason": "pipewire unavailable"}
        target = None
        for d in devices:
            if default and d.node_name == default:
                target = d
                break
        target = target or (devices[0] if devices else None)
        if target is None:
            return {"switched": False, "reason": "no sink"}
        ident = target.identity()
        if st.last_device and st.last_device.get("name") == ident.get("name") \
                and reason != "manual":
            return {"switched": False, "reason": "unchanged"}
        st.last_device = ident
        rules = collect_rules(st.store, st.store.config())
        rule = select_profile(ident, rules)
        if rule and st.auto_switch:
            try:
                st.set_profile(rule.profile)
                log.info("auto-switched to %s for device %s",
                         rule.profile, ident.get("description"))
                self.bus.publish("profile_changed",
                                 {"profile": rule.profile,
                                  "device": ident.get("name"),
                                  "rule": rule.source})
                from eqforge.system.notify import notify
                notify("EQForge", f"Switched to “{rule.profile}” for "
                       f"{ident.get('description', ident.get('name'))}")
                return {"switched": True, "profile": rule.profile}
            except EQForgeError as e:
                log.error("auto-switch failed: %s", e)
                return {"switched": False, "error": str(e)}
        self.bus.publish("device_changed",
                         {"device": ident.get("name"),
                          "matched": rule.profile if rule else None})
        return {"switched": False, "matched": rule.profile if rule else None}

    def watcher_loop(self) -> None:
        last = None
        while not self.stop_event.is_set():
            try:
                if self.manage_rt:
                    self._ensure_rt_host()
                # the rt host only reconfigures on command: if it started
                # before the daemon published active-dsp.json it is still
                # unconfigured, so push the current config once.
                if self.state.rt_needs_sync():
                    if self.state.sync_rt():
                        log.info("rt host synced with %s",
                                 paths.active_dsp_file())
                        self.bus.publish("rt_synced", {})
                # One pw-dump per tick, shared by device matching and the
                # wiring check: each query used to shell out separately, so
                # a tick cost three dumps before auto-wire added more.
                view = pw.snapshot() if pw.pipewire_available() else None
                if view is not None:
                    devices = view.devices()
                    sink = view.default_sink()
                    key = json.dumps([d.name for d in devices] + [sink or ""],
                                     sort_keys=True)
                    if key != last:
                        last = key
                        self.bus.publish("graph_changed",
                                         {"devices": len(devices),
                                          "default_sink": sink})
                        self._apply_rules(reason="graph")
                    if self.auto_wire:
                        self._auto_wire(view)
            except Exception:  # noqa: BLE001 - watcher must never die
                log.debug("watcher error:\n%s", traceback.format_exc())
            self.stop_event.wait(self.tick)

    # ---------------- rt host supervision (optional) ----------------
    def _ensure_rt_host(self) -> None:
        """Keep the rt host alive when this daemon was told to manage it.

        systemd does this better when the units are installed (restart
        policy, journald logs, RT scheduling), so `autostart on` leaves
        management to systemd and this stays off. It exists for the
        by-hand case: `eqforge daemon run --manage-rt`.

        "Alive" means *crash*-free, not "always on": a quit relayed through
        `rt_command` (`eqforge system stop`) is deliberate, so it is not
        undone. Supervision resumes the moment a host is running again.
        """
        from eqforge.system import rthost
        if self._rt_proc is not None and self._rt_proc.poll() is None:
            return                       # still ours, still alive
        if rthost.is_running():
            self._rt_proc = None         # started elsewhere; leave it alone
            self._rt_stop_requested = False   # ...and it is back: supervise it
            return
        if self._rt_stop_requested:
            return                       # stopped on purpose, not crashed
        now = time.time()
        if now - self._rt_last_try < self.rt_retry:
            return                       # back off instead of spinning
        self._rt_last_try = now
        try:
            launched = rthost.start(channels=self.rt_channels, wait=3.0)
        except EQForgeError as e:
            log.warning("cannot start rt host: %s%s", e.message,
                        f" ({e.hint})" if e.hint else "")
            self.bus.publish("rt_error",
                             {"error": e.message, "hint": e.hint or ""})
            return
        self._rt_proc = launched.proc
        self._rt_stop_requested = False  # we are supervising it again
        self._wire_sig = None            # a new host has no links at all
        log.info("started rt host pid %d (%s)", launched.pid, launched.rt)
        self.bus.publish("rt_started", {"pid": launched.pid,
                                        "wrapper": launched.wrapper})

    # ---------------- automatic re-wiring ----------------
    def _auto_wire(self, view) -> None:
        """Re-link streams that stopped passing through the rt host.

        Cheap in the common case: the graph signature is compared first, so
        a tick where nothing changed costs no `pw-link` calls at all.
        """
        from eqforge.system import wiring
        sig = wiring.stream_signature(view)
        if sig is None:
            return                       # PipeWire went away
        if sig == self._wire_sig and self._wire_ok:
            return                       # unchanged since a successful pass
        report = wiring.wire_graph(self.state._tell_rt, graph=view)
        self._wire_ok = report.rt_node is not None and not report.errors
        # Only trust the signature after a pass that could actually do its
        # work; otherwise (host down, sink missing) retry on the next tick.
        self._wire_sig = sig if self._wire_ok else None
        if report.errors:
            log.warning("auto-wire: %s", "; ".join(report.errors[:3]))
        if report.changed:
            log.info("auto-wire: %s", report.summary())
            self.bus.publish("graph_wired",
                             {"sink": report.sink,
                              "streams_moved": report.streams_moved,
                              "already_wired": report.already_wired,
                              "outputs": report.outputs_ok})

    # ---------------- servers ----------------
    def _socket_alive(self, sock_path: Path) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(str(sock_path))
                s.sendall(b'{"method":"ping"}\n')
                s.recv(4096)
            return True
        except OSError:
            return False

    def serve_unix(self) -> None:
        paths.ensure_dirs()
        sock_path = paths.daemon_socket()
        if sock_path.exists():
            if self._socket_alive(sock_path):
                raise RuntimeError("daemon already running")
            sock_path.unlink(missing_ok=True)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(16)
        srv.settimeout(0.5)
        log.info("RPC listening on %s", sock_path)

        sel = selectors.DefaultSelector()
        sel.register(srv, selectors.EVENT_READ)
        while not self.stop_event.is_set():
            for key, _ in sel.select(0.5):
                if key.fileobj is srv:
                    conn, _ = srv.accept()
                    threading.Thread(target=self._handle_conn, args=(conn,),
                                     daemon=True).start()
        sel.close()
        srv.close()
        sock_path.unlink(missing_ok=True)

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            f = conn.makefile("rwb")
            for line in f:
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode())
                    result = self.rpc(req.get("method", ""),
                                      req.get("params") or {})
                    resp = {"ok": True, "result": result,
                            "id": req.get("id")}
                except EQForgeError as e:
                    resp = {"ok": False, "error": str(e.message),
                            "hint": e.hint, "code": e.exit_code}
                except Exception as e:  # noqa: BLE001
                    log.error("rpc crash: %s\n%s", e, traceback.format_exc())
                    resp = {"ok": False, "error": f"internal: {e}"}
                f.write((json.dumps(resp) + "\n").encode())
                f.flush()
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            conn.close()

    def serve_http(self, host: str = "127.0.0.1", port: int = 0) -> int:
        httpd = ThreadingHTTPServer((host, port), HTTPHandler)
        httpd.eqf_daemon = self   # handler picks this up via its `server` arg
        actual = httpd.server_address[1]
        paths.ensure_dirs()
        paths.gui_port_file().write_text(str(actual))
        t = threading.Thread(target=httpd.serve_forever, daemon=True,
                             kwargs={"poll_interval": 0.3})
        t.start()
        self._httpd = httpd
        log.info("HTTP GUI on http://%s:%d", host, actual)
        return actual

    def stop(self) -> None:
        self.stop_event.set()
        # Only tear down an rt host this daemon launched; one owned by
        # systemd (or started by hand) must survive the daemon.
        if self._rt_proc is not None:
            from eqforge.system import rthost
            rthost.stop(self._rt_proc)
            self._rt_proc = None
        httpd = getattr(self, "_httpd", None)
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            httpd.server_close()
            self._httpd = None
        # Remove runtime artefacts immediately so the next start sees a clean
        # slate even if this process is slow to unwind (stale-socket recovery).
        for p in (paths.daemon_socket(), paths.gui_port_file()):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def run_forever(self, http: bool = True, port: int = 0) -> None:
        # fail fast if another daemon owns the socket, before opening HTTP
        paths.ensure_dirs()
        sock_path = paths.daemon_socket()
        if sock_path.exists() and self._socket_alive(sock_path):
            raise RuntimeError("daemon already running")
        threading.Thread(target=self.watcher_loop, daemon=True).start()
        if http:
            self.serve_http(port=port)
        try:
            self.serve_unix()  # blocks until stop_event
        except BaseException:
            self.stop()  # make sure the HTTP server never outlives a failure
            raise


def _version() -> str:
    from eqforge.version import __version__
    return __version__


# ---------------- HTTP handler ----------------

GUI_DIR = Path(__file__).parent.parent / "gui" / "static"


class HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, request, client_address, server, eqf_daemon=None):
        self.daemon = eqf_daemon if eqf_daemon is not None \
            else getattr(server, "eqf_daemon", None)
        super().__init__(request, client_address, server)

    def log_message(self, fmt, *args):  # quiet; we log ourselves
        log.debug("http: " + fmt, *args)

    # ---- origin policy ----
    # The HTTP face of the daemon exposes mutating RPCs (profile edits, bypass,
    # device changes) and has no authentication of its own. Sending
    # `Access-Control-Allow-Origin: *` let ANY web page in the user's browser
    # read responses from, and drive, a daemon on 127.0.0.1 - a cross-origin
    # page could silently reconfigure audio or enumerate the user's profiles.
    # Requests are now only branded as readable by same-origin pages (the
    # bundled GUI) and by the loopback dev-server origins used when the GUI is
    # served from a separate port.
    _ALLOWED_ORIGIN_SUFFIXES = (".localhost", ".test")

    def _origin_allowed(self) -> str | None:
        """Return the Origin value to echo back, or None to send no CORS grant."""
        origin = self.headers.get("Origin")
        if not origin:
            # Same-origin fetches and non-browser clients send no Origin; no
            # CORS header is needed for them.
            return None
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return origin
        if any(host.endswith(sfx) for sfx in self._ALLOWED_ORIGIN_SUFFIXES):
            return origin
        return None

    def _origin_rejected(self) -> bool:
        """True when the request carries a cross-origin Origin we do not serve.

        For a mutating request this is a hard rejection: without it a hostile
        page can still fire-and-forget (a POST whose response it cannot read),
        which is enough to change the user's audio configuration.
        """
        origin = self.headers.get("Origin")
        return bool(origin) and self._origin_allowed() is None

    # ---- helpers ----
    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        allow = self._origin_allowed()
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Vary", "Origin")
        else:
            # Explicitly deny for a browser that asked cross-origin.
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_OPTIONS(self):  # noqa: N802
        """Preflight: only grant CORS to origins we would actually serve."""
        allow = self._origin_allowed()
        extra = {"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                 "Access-Control-Allow-Headers": "Content-Type"}
        if allow is None:
            return self._send(403, b"", "text/plain")
        self._send(204, b"", "text/plain", extra)

    # ---- routes ----
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            return self._static("index.html", "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._static(path.lstrip("/"), None)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        if self._origin_rejected():
            return self._json({"ok": False,
                               "error": "cross-origin request refused"}, 403)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "bad json"}, 400)
        method = path[len("/api/"):]
        try:
            result = self.daemon.rpc(method, body)
            self._json({"ok": True, "result": result})
        except EQForgeError as e:
            self._json({"ok": False, "error": str(e.message),
                        "hint": e.hint}, 400)
        except Exception as e:  # noqa: BLE001
            log.error("api crash: %s\n%s", e, traceback.format_exc())
            self._json({"ok": False, "error": f"internal: {e}"}, 500)

    def _api_get(self, path: str) -> None:
        method = path[len("/api/"):]
        if method == "events":
            return self._sse()
        try:
            result = self.daemon.rpc(method, {})
            self._json({"ok": True, "result": result})
        except EQForgeError as e:
            self._json({"ok": False, "error": str(e.message),
                        "hint": e.hint}, 400)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        allow = self._origin_allowed()
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Vary", "Origin")
        self.end_headers()
        q = self.daemon.bus.subscribe()
        try:
            # initial snapshot
            self.wfile.write(b"event: hello\n"
                             b"data: {\"ok\":true}\n\n")
            last_meters = 0.0
            while not self.daemon.stop_event.is_set():
                while q:
                    msg = q.pop(0)
                    payload = json.dumps(msg)
                    self.wfile.write(
                        f"event: {msg['event']}\ndata: {payload}\n\n".encode())
                now = time.time()
                if now - last_meters > 0.1:
                    last_meters = now
                    meters = {"rt": self.daemon.state.rt_status()}
                    self.wfile.write(
                        ("event: meters\ndata: " +
                         json.dumps(meters) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.daemon.bus.unsubscribe(q)

    def _static(self, rel: str, ctype: str | None) -> None:
        p = (GUI_DIR / rel).resolve()
        if not str(p).startswith(str(GUI_DIR.resolve())) or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        types = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".json": "application/json"}
        ctype = ctype or types.get(p.suffix, "application/octet-stream")
        self._send(200, p.read_bytes(), ctype)
