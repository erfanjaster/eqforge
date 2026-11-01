"""Daemon server: Unix-socket JSON-RPC + localhost HTTP (GUI, REST, SSE).

Single process, three threads:
  - RPC loop on $XDG_RUNTIME_DIR/eqforge/daemon.sock (line-delimited JSON)
  - HTTP server on 127.0.0.1 (GUI assets, /api/* REST, /api/events SSE)
  - device watcher: polls the PipeWire graph (~2 s) and applies matching
    rules when auto-switch is enabled; emits SSE events on changes.

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
    def __init__(self):
        self.state = EngineState()
        self.bus = EventBus()
        self.stop_event = threading.Event()
        self.started = time.time()
        self.state.ensure_active_config()
        self.state.sync_rt()          # best effort; watcher retries

    # ---------------- RPC dispatch ----------------
    def rpc(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        st = self.state
        store = st.store

        if method == "ping":
            return {"ok": True, "version": _version(), "rpc": RPC_VERSION}

        if method == "status":
            return st.status()

        if method == "set_profile":
            return st.set_profile(str(params["profile"]),
                                  params.get("slot"))

        if method == "bypass":
            if "value" in params:
                return st.set_bypass(bool(params["value"]))
            return st.toggle_bypass()

        if method == "ab":
            return st.ab_switch(params.get("slot"))

        if method == "list_profiles":
            return {"profiles": store.list_ids()}

        if method == "resolve_profile":
            resolved = store.resolve_any(str(params["profile"]))
            from eqforge.profiles.resolve import to_dsp_config
            return {"resolved": resolved,
                    "dsp": to_dsp_config(resolved,
                                         st._base_dir(str(params["profile"])))}

        if method == "save_profile":
            from eqforge.profiles.model import Profile
            prof = Profile.from_dict(params["profile"])
            path = store.save(prof)
            self.bus.publish("profiles_changed", {})
            return {"path": str(path)}

        if method == "delete_profile":
            store.delete(str(params["profile"]))
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
            r = st._tell_rt(params.get("cmd") or {})
            return {"rt": r}

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
                # the rt host only reconfigures on command: if it started
                # before the daemon published active-dsp.json it is still
                # unconfigured, so push the current config once.
                if self.state.rt_needs_sync():
                    if self.state.sync_rt():
                        log.info("rt host synced with %s",
                                 paths.active_dsp_file())
                        self.bus.publish("rt_synced", {})
                snap = pw.graph_snapshot() if pw.pipewire_available() else None
                if snap is not None:
                    key = json.dumps(
                        [d["name"] for d in snap["devices"]] +
                        [snap["default_sink"] or ""], sort_keys=True)
                    if key != last:
                        last = key
                        self.bus.publish("graph_changed",
                                         {"devices": len(snap["devices"]),
                                          "default_sink": snap["default_sink"]})
                        self._apply_rules(reason="graph")
            except Exception:  # noqa: BLE001 - watcher must never die
                log.debug("watcher error:\n%s", traceback.format_exc())
            self.stop_event.wait(2.0)

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
        httpd = getattr(self, "_httpd", None)
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            httpd.server_close()
            self._httpd = None

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

    # ---- helpers ----
    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

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
        self.send_header("Access-Control-Allow-Origin", "*")
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
