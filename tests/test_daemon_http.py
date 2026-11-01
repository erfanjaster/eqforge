"""HTTP surface of the daemon: origin policy and request validation.

The daemon's HTTP face has no authentication and exposes mutating RPCs, so its
CORS policy is the security boundary that stops an arbitrary web page from
reconfiguring the user's audio. The audit found it served
`Access-Control-Allow-Origin: *` to every caller, which meant a hostile page
could read responses from and drive the daemon on 127.0.0.1.

These tests run a real HTTP server on a loopback port and speak to it with
urllib, so they exercise the header logic and the 403 path rather than the RPC
methods in isolation.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from eqforge.daemon.server import Daemon, EventBus
from eqforge.daemon.state import EngineState
from eqforge.errors import EQForgeError


@pytest.fixture()
def http_daemon(isolated_home):
    d = Daemon.__new__(Daemon)
    d.state = EngineState()
    d.bus = EventBus()
    d.stop_event = threading.Event()
    import time as _t
    d.started = _t.time()
    d.state.ensure_active_config()
    port = d.serve_http(port=0)
    yield d, port
    d._httpd.shutdown()
    d._httpd.server_close()


def _req(port, method, path, body=None, origin=None, http="POST"):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}",
                                data=data, method=http, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


EVIL = "https://evil.example"


def test_cross_origin_mutation_is_refused(http_daemon):
    _, port = http_daemon
    code, body, hdrs = _req(port, "POST", "save_profile",
                            {"profile": {"id": "pwned", "name": "pwned"}},
                            origin=EVIL)
    assert code == 403
    assert "Access-Control-Allow-Origin" not in hdrs
    assert json.loads(body)["ok"] is False


def test_cross_origin_get_does_not_grant_cors(http_daemon):
    """A readable cross-origin GET is enough to enumerate a user's setup."""
    _, port = http_daemon
    code, _, hdrs = _req(port, "GET", "status", origin=EVIL, http="GET")
    assert "Access-Control-Allow-Origin" not in hdrs


def test_cross_origin_preflight_is_refused(http_daemon):
    _, port = http_daemon
    code, _, hdrs = _req(port, "OPTIONS", "status", origin=EVIL,
                         http="OPTIONS")
    assert code == 403
    assert "Access-Control-Allow-Origin" not in hdrs


def test_loopback_origin_is_allowed_and_echoed(http_daemon):
    _, port = http_daemon
    origin = f"http://127.0.0.1:{port}"
    code, _, hdrs = _req(port, "POST", "ping", {}, origin=origin)
    assert code == 200
    assert hdrs.get("Access-Control-Allow-Origin") == origin
    assert "Origin" in hdrs.get("Vary", "")


def test_localhost_dev_origin_is_allowed(http_daemon):
    """The GUI is sometimes served from a separate dev port."""
    _, port = http_daemon
    for origin in ("http://localhost:5173", "http://app.localhost:8080"):
        code, _, hdrs = _req(port, "POST", "ping", {}, origin=origin)
        assert code == 200, origin
        assert hdrs.get("Access-Control-Allow-Origin") == origin


def test_no_origin_header_still_works(http_daemon):
    """Same-origin fetches and CLI/curl clients send no Origin."""
    _, port = http_daemon
    code, body, hdrs = _req(port, "POST", "ping", {})
    assert code == 200
    assert json.loads(body)["ok"] is True
    assert "Access-Control-Allow-Origin" not in hdrs


@pytest.mark.parametrize("method,body,expect", [
    ("save_profile", {}, "missing required parameter 'profile'"),
    ("save_profile", {"profile": "nope"},
     "parameter 'profile' must be an object"),
    ("save_profile", {"profile": {"name": "x"}},
     "profile.id must be a non-empty string"),
    ("save_profile", {"profile": {"id": "x"}},
     "profile.name must be a non-empty string"),
    ("set_profile", {}, "missing required parameter 'profile'"),
    ("set_profile", {"profile": 42},
     "parameter 'profile' must be a non-empty string"),
    ("delete_profile", {}, "missing required parameter 'profile'"),
])
def test_malformed_requests_are_clean_400s(http_daemon, method, body, expect):
    """A bad request must not surface as a 500 with a KeyError string."""
    _, port = http_daemon
    origin = f"http://127.0.0.1:{port}"
    code, raw, _ = _req(port, "POST", method, body, origin=origin)
    assert code == 400, raw
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert expect in payload["error"]


def test_set_profile_slot_must_be_a_string(http_daemon):
    _, port = http_daemon
    origin = f"http://127.0.0.1:{port}"
    code, raw, _ = _req(port, "POST", "set_profile",
                        {"profile": "flat", "slot": 3}, origin=origin)
    assert code == 400
    assert "slot" in json.loads(raw)["error"]


def test_non_object_params_are_rejected(isolated_home):
    """The dispatcher itself validates the params container."""
    d = Daemon.__new__(Daemon)
    d.state = EngineState()
    d.bus = EventBus()
    d.stop_event = threading.Event()
    d.state.ensure_active_config()
    with pytest.raises(EQForgeError):
        d.rpc("ping", ["not", "a", "dict"])
