"""The event-driven PipeWire watcher.

The symptom these tests defend against is concrete: audio that starts playing
takes up to two seconds (the daemon's old poll interval) to pass through the
equalizer. `GraphWatcher` turns that into a reaction measured in tens of
milliseconds, and these tests pin the three properties that matter:

  * events are delivered promptly, not on a timer;
  * a burst of events (one stream start = node + ports + links) is coalesced
    into a single callback, not one per event;
  * a monitor that cannot run never wedges the daemon or its shutdown.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from eqforge.system import watch


def _fake_monitor(tmp_path: Path, body: str) -> Path:
    """A stand-in for `pw-dump -m` that prints whatever `body` says."""
    script = tmp_path / "monitor.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


@pytest.fixture
def collected():
    """A callback that records calls and wakes the test."""
    calls: list[str] = []
    woke = threading.Event()

    def on_change(reason: str) -> None:
        calls.append(reason)
        woke.set()

    return calls, woke, on_change


def test_reports_event_promptly(tmp_path, monkeypatch, collected):
    calls, woke, on_change = collected
    mon = _fake_monitor(
        tmp_path,
        "i=0\n"
        "while [ $i -lt 50 ]; do\n"
        "  printf '%s\\n' '{\"id\":7,\"type\":\"PipeWire:Interface:Node\"}'\n"
        "  i=$((i+1))\n"
        "  sleep 0.02\n"
        "done\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change, debounce=0.01)
    assert w.start()
    t0 = time.monotonic()
    try:
        assert woke.wait(2.0), "watcher never delivered an event"
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"reaction took {elapsed:.2f}s"
        assert calls == ["graph"]
        assert w.raw_events >= 1
    finally:
        w.stop()
    assert not w.running


def test_burst_is_coalesced(tmp_path, monkeypatch, collected):
    """One playback start emits many events; the callback runs once."""
    calls, woke, on_change = collected
    mon = _fake_monitor(
        tmp_path,
        "printf '%s\\n' '{\"type\":\"PipeWire:Interface:Node\",\"id\":1}'\n"
        "printf '%s\\n' '{\"type\":\"PipeWire:Interface:Port\",\"id\":2}'\n"
        "printf '%s\\n' '{\"type\":\"PipeWire:Interface:Link\",\"id\":3}'\n"
        "sleep 30\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change, debounce=0.15)
    assert w.start()
    try:
        assert woke.wait(2.0)
        time.sleep(0.4)              # no further burst was produced
        assert len(calls) == 1, calls
    finally:
        w.stop()


def test_irrelevant_events_ignored(tmp_path, monkeypatch, collected):
    calls, woke, on_change = collected
    mon = _fake_monitor(
        tmp_path,
        "printf '%s\\n' '{\"type\":\"PipeWire:Interface:Client\",\"id\":9}'\n"
        "sleep 30\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change, debounce=0.02)
    assert w.start()
    try:
        assert not woke.wait(0.5), "a client object must not trigger rewiring"
        assert calls == []
    finally:
        w.stop()


def test_callback_exception_does_not_kill_the_watcher(tmp_path, monkeypatch):
    """A failing rewiring pass must not stop future passes."""
    seen = []
    second = threading.Event()

    def on_change(reason: str) -> None:
        seen.append(reason)
        if len(seen) == 1:
            raise RuntimeError("boom")
        second.set()

    mon = _fake_monitor(
        tmp_path,
        "i=0\n"
        "while [ $i -lt 100 ]; do\n"
        "  printf '%s\\n' '{\"type\":\"PipeWire:Interface:Node\",\"id\":$i}'\n"
        "  i=$((i+1))\n"
        "  sleep 0.1\n"
        "done\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change, debounce=0.01)
    assert w.start()
    try:
        assert second.wait(3.0)
        assert len(seen) >= 2
    finally:
        w.stop()


def test_missing_monitor_is_reported_not_fatal(monkeypatch, collected):
    _, _, on_change = collected
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", "definitely-not-a-real-tool")
    w = watch.GraphWatcher(on_change)
    assert w.start() is False
    assert w.event_capable is False
    w.stop()                          # must be a no-op, not an exception


def test_dead_monitor_is_detected(tmp_path, monkeypatch, collected):
    """A tool that exists but refuses to monitor must not be retried forever."""
    _, _, on_change = collected
    mon = _fake_monitor(tmp_path, "exit 1\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change, max_restarts=1, restart_backoff=0.01)
    assert w.start()
    deadline = time.monotonic() + 5.0
    while w.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not w.event_capable
    w.stop()


def test_stats_shape(tmp_path, monkeypatch, collected):
    _, _, on_change = collected
    mon = _fake_monitor(tmp_path, "sleep 30\n")
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", str(mon))
    w = watch.GraphWatcher(on_change)
    assert set(w.stats()) == {"running", "event_capable", "events",
                              "raw_events", "restarts", "command", "error"}
    assert w.stats()["command"][0] == str(mon)


def test_monitor_command_default(monkeypatch):
    monkeypatch.delenv("EQFORGE_MONITOR_CMD", raising=False)
    assert watch.monitor_command() == ["pw-dump", "-m"]
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", "pw-mon --foo")
    assert watch.monitor_command() == ["pw-mon", "--foo"]


def test_available_reflects_environment(monkeypatch):
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", "definitely-not-a-real-tool")
    assert watch.available() is False
    monkeypatch.setenv("EQFORGE_MONITOR_CMD", "sh")
    assert watch.available() is True
    assert os.path.basename(watch.monitor_command()[0]) == "sh"
