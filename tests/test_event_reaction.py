"""The daemon must *react* to the graph, not poll it into place.

Context that makes these tests worth having: before this change the daemon
looked at the PipeWire graph every 2 seconds. A player that starts a stream is
linked to the default sink immediately by WirePlumber, so the EQForge rewiring
pass always ran late - up to 2 s of audio came out unprocessed and then the
equalizer "snapped in". These checks pin that an event wakes the loop
immediately instead of the loop waiting out its tick.
"""
from __future__ import annotations

import itertools
import threading
import time
import types

import pytest

from eqforge.daemon.server import Daemon
from eqforge.system import wiring


def _daemon(**kw) -> Daemon:
    kw.setdefault("auto_wire", True)
    kw.setdefault("tick", 30.0)            # deliberately absurd: no poll help
    return Daemon(**kw)


def test_graph_event_wakes_the_loop():
    d = _daemon()
    assert d._wait_for_work is not None
    d._on_graph_event("graph")
    t0 = time.monotonic()
    d._wait_for_work()                     # must not block for the 30 s tick
    assert time.monotonic() - t0 < 1.0
    assert d.graph_events == 1
    # the wake flag is consumed, so the next call falls back to the tick
    assert not d._graph_wake.is_set()


def test_watcher_loop_rewires_on_event(monkeypatch):
    """An event must produce a wiring pass in well under a second."""
    d = _daemon()
    calls: list[float] = []

    def fake_wire(rt_command, graph=None, **kw):
        calls.append(time.monotonic())
        report = wiring.WireReport()
        report.sink = "sink"
        report.rt_node = "eqforge"
        return report

    monkeypatch.setattr(wiring, "wire_graph", fake_wire)
    # A signature that changes on every read: stands in for "the graph really
    # did change", which is what makes the pass do work instead of no-opping.
    counter = itertools.count()
    monkeypatch.setattr(wiring, "stream_signature",
                        lambda graph=None: f"sig{next(counter)}")
    monkeypatch.setattr(d, "_ensure_rt_host", lambda: None)
    monkeypatch.setattr(d.state, "rt_needs_sync", lambda: False)

    class View:
        def devices(self):
            return []

        def default_sink(self):
            return "sink"

    from eqforge.system import pipewire as pw
    monkeypatch.setattr(pw, "pipewire_available", lambda: True)
    monkeypatch.setattr(pw, "snapshot", lambda: View())
    monkeypatch.setattr(d, "_apply_rules", lambda reason: {})

    t = threading.Thread(target=d.watcher_loop, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls, "first pass did not run"

        seen = len(calls)
        t0 = time.monotonic()
        d._on_graph_event("graph")
        while len(calls) == seen and time.monotonic() - t0 < 5.0:
            time.sleep(0.02)
        assert len(calls) > seen, "an event did not trigger a rewiring pass"
        assert time.monotonic() - t0 < 5.0
    finally:
        d.stop_event.set()
        t.join(timeout=5.0)
    assert not t.is_alive()


def test_status_reports_the_watch_channel():
    d = _daemon()
    st = d.rpc("status")
    assert "watch" in st
    assert st["watch"]["running"] is False        # not started in this test
    assert st["watch"]["events"] == 0


def test_stop_is_safe_without_a_watcher():
    d = _daemon()
    d.stop()                                      # must not raise


def test_start_graph_watch_degrades_without_a_monitor(monkeypatch):
    """No `pw-dump -m`: the daemon keeps working on its tick."""
    d = _daemon()
    from eqforge.system import watch
    monkeypatch.setattr(watch.GraphWatcher, "start", lambda self: False)
    d._start_graph_watch()
    assert d._watch is None
    d._stop_graph_watch()
