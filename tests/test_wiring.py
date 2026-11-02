"""Automatic re-wiring: the algorithm that keeps EQForge in the audio path.

The user-visible bug this covers: pause a player and PipeWire destroys its
stream node; press play and a new node appears which WirePlumber links
straight to the default sink, so audio silently stops being processed until
`eqforge system wire` is run again. These tests drive `wire_graph` and
`stream_signature` against a fake graph so the policy is pinned without
needing a live PipeWire session.
"""
from __future__ import annotations

import subprocess

import pytest

from eqforge.errors import SystemError
from eqforge.system import wiring
from eqforge.system.pipewire import Stream

SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"
RT = "eqforge"


def _stream(node_id: int, node_name: str, direction: str = "playback",
            binary: str = "player") -> Stream:
    return Stream(id=node_id, name=node_name, node_name=node_name,
                  binary=binary, media_class="Stream/Output/Audio",
                  direction=direction)


class FakeGraph:
    """Stands in for pipewire.GraphView (one snapshot, many queries)."""

    def __init__(self, sink=SINK, streams=(), links=(), ports=None,
                 sink_ports=("playback_FL", "playback_FR")):
        self.sink = sink
        self._streams = list(streams)
        self._links = list(links)
        self._ports = ports or {}
        self._sink_ports = list(sink_ports)
        self._ids = {sink: 10}
        for s in self._streams:
            self._ids[s.node_name] = s.id
        self._ids[RT] = 99

    def default_sink(self):
        return self.sink

    def node_id(self, name):
        return self._ids.get(name)

    def streams(self):
        return list(self._streams)

    def links(self):
        return list(self._links)

    def node_ports(self, node_id, direction=None):
        if node_id == 10:
            return ["monitor_0"] + list(self._sink_ports)
        return list(self._ports.get(node_id, []))

    def node_port_targets(self, node_id, direction=None, node_name=""):
        if node_id == 10:
            return ([f"{self.sink}:monitor_0"]
                    + [f"{self.sink}:{p}" for p in self._sink_ports])
        return [f"{node_name}:{p}" for p in self._ports.get(node_id, [])]


class FakePwLink:
    """Records pw-link invocations; returns success unless told otherwise."""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.calls: list[tuple] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, *args, required=False):
        self.calls.append(tuple(args))
        return subprocess.CompletedProcess(("pw-link", *args),
                                           self.returncode, b"", self.stderr)

    @property
    def created(self):
        return [c for c in self.calls if c[0] != "-d"]

    @property
    def deleted(self):
        return [c for c in self.calls if c[0] == "-d"]

    @property
    def stream_calls(self):
        """Calls that touch an app stream, i.e. not the rt host's own outputs."""
        return [c for c in self.calls if not c[0].startswith(f"{RT}:out_")]


def _rt_command(channels: int = 2, ok: bool = True, connected: int = 0):
    seen = []

    def cmd(c):
        seen.append(c)
        if c.get("cmd") == "status":
            return {"ok": ok, "client": RT, "channels": channels}
        return {"connected": connected}

    cmd.seen = seen
    return cmd


@pytest.fixture(autouse=True)
def pipewire_present(monkeypatch):
    monkeypatch.setattr(wiring.pw, "pipewire_available", lambda: True)


# --------------------------------------------------------------------------
# moving a stream onto the rt host
# --------------------------------------------------------------------------

def test_stream_on_the_sink_is_moved_onto_the_rt_host(monkeypatch):
    """The core behaviour: app -> sink becomes app -> eqforge -> sink."""
    graph = FakeGraph(
        streams=[_stream(7, "music-player")],
        links=[("music-player:out_FL", f"{SINK}:playback_FL"),
               ("music-player:out_FR", f"{SINK}:playback_FR"),
               (f"{RT}:out_1", f"{SINK}:playback_FL")],
        ports={7: ["out_FL", "out_FR"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.rt_node == RT and report.sink == SINK
    assert report.streams_moved == 2
    assert report.changed is True
    assert ("music-player:out_FL", f"{RT}:in_1") in link.created
    assert ("music-player:out_FR", f"{RT}:in_2") in link.created
    # and it was unhooked from the sink first, otherwise both links exist
    assert ("-d", "music-player:out_FL", f"{SINK}:playback_FL") in link.deleted


def test_already_wired_stream_is_left_alone(monkeypatch):
    """Idempotence: a second pass must not unlink and relink everything."""
    graph = FakeGraph(
        streams=[_stream(7, "music-player")],
        links=[("music-player:out_FL", f"{RT}:in_1"),
               ("music-player:out_FR", f"{RT}:in_2"),
               (f"{RT}:out_1", f"{SINK}:playback_FL"),
               (f"{RT}:out_2", f"{SINK}:playback_FR")],
        ports={7: ["out_FL", "out_FR"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.already_wired == 1
    assert report.streams_moved == 0
    assert report.changed is False, "a fully wired graph was modified"
    assert link.deleted == [], "a correctly wired stream was unhooked"
    assert link.stream_calls == []


def test_stream_on_another_device_is_not_hijacked(monkeypatch):
    """A stream deliberately routed elsewhere must stay there."""
    other = "alsa_output.usb-headphones"
    graph = FakeGraph(
        streams=[_stream(7, "music-player")],
        links=[("music-player:out_FL", f"{other}:playback_FL")],
        ports={7: ["out_FL", "out_FR"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.streams_moved == 0
    assert report.skipped == 1
    assert link.stream_calls == [], "a stream on another device was touched"


def test_dormant_stream_with_no_links_is_skipped(monkeypatch):
    graph = FakeGraph(streams=[_stream(7, "idle-player")], links=[],
                      ports={7: ["out_FL"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.skipped == 1 and report.streams_moved == 0
    assert link.stream_calls == []


def test_capture_streams_are_ignored(monkeypatch):
    graph = FakeGraph(
        streams=[_stream(7, "mic-app", direction="capture")],
        links=[("mic-app:out_1", f"{SINK}:playback_FL")],
        ports={7: ["out_1"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.streams_moved == 0


def test_the_rt_host_itself_is_never_treated_as_an_app(monkeypatch):
    graph = FakeGraph(
        streams=[_stream(99, RT)],
        links=[(f"{RT}:out_1", f"{SINK}:playback_FL")],
        ports={99: ["out_1", "out_2"]})
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.streams_moved == 0
    assert not any("eqforge:out_1" == c[0] and c[1].startswith("eqforge:in")
                   for c in link.created), "the host was wired into itself"


def test_monitor_ports_are_excluded_from_the_sink_targets(monkeypatch):
    graph = FakeGraph(streams=[], links=[(f"{RT}:out_1", f"{SINK}:playback_FL")])
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)
    rt = _rt_command()

    wiring.wire_graph(rt, graph=graph)

    connect = [c for c in rt.seen if c.get("cmd") == "connect"][0]
    assert f"{SINK}:monitor_0" not in connect["outputs"]
    assert connect["outputs"] == [f"{SINK}:playback_FL",
                                  f"{SINK}:playback_FR"]


def test_channels_are_honoured(monkeypatch):
    graph = FakeGraph(
        streams=[_stream(7, "app")],
        links=[(f"app:out_{c}", f"{SINK}:playback_{p}")
               for c, p in (("1", "FL"), ("2", "FR"), ("3", "FC"), ("4", "LFE"))],
        ports={7: ["out_1", "out_2", "out_3", "out_4"]},
        sink_ports=("playback_FL", "playback_FR", "playback_FC", "playback_LFE"))
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)

    report = wiring.wire_graph(_rt_command(channels=4), graph=graph)

    assert report.channels == 4
    assert ("app:out_3", f"{RT}:in_3") in link.created


def test_explicit_channels_override_the_host(monkeypatch):
    graph = FakeGraph(streams=[], links=[])
    monkeypatch.setattr(wiring, "pw_link", FakePwLink())
    report = wiring.wire_graph(_rt_command(channels=2), graph=graph,
                               channels=6)
    assert report.channels == 6


# --------------------------------------------------------------------------
# failure modes must be reported, never raised (the daemon polls this)
# --------------------------------------------------------------------------

def test_rt_host_down_is_reported_not_raised(monkeypatch):
    graph = FakeGraph(streams=[_stream(7, "app")], links=[])
    monkeypatch.setattr(wiring, "pw_link", FakePwLink())

    report = wiring.wire_graph(lambda cmd: None, graph=graph)

    assert report.rt_node is None
    assert report.ok is False
    assert "rt host not running" in report.notes


def test_pw_link_failure_is_recorded(monkeypatch):
    graph = FakeGraph(
        streams=[_stream(7, "app")],
        links=[("app:out_FL", f"{SINK}:playback_FL")],
        ports={7: ["out_FL"]})
    monkeypatch.setattr(wiring, "pw_link",
                        FakePwLink(returncode=1, stderr=b"port not found"))

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.errors and "port not found" in report.errors[0]
    assert report.streams_moved == 0


def test_missing_pw_link_does_not_kill_the_pass(monkeypatch):
    graph = FakeGraph(
        streams=[_stream(7, "app")],
        links=[("app:out_FL", f"{SINK}:playback_FL")],
        ports={7: ["out_FL"]})

    def gone(*a, required=False):
        raise SystemError("pw-link not found: cannot rewire the PipeWire graph")

    monkeypatch.setattr(wiring, "pw_link", gone)

    report = wiring.wire_graph(_rt_command(), graph=graph)   # must not raise
    assert report.errors and "pw-link not found" in report.errors[0]


def test_no_default_sink_is_a_note_not_a_crash(monkeypatch):
    graph = FakeGraph(sink=None, streams=[], links=[])
    monkeypatch.setattr(wiring, "pw_link", FakePwLink())

    report = wiring.wire_graph(_rt_command(), graph=graph)

    assert report.sink is None
    assert any("no default sink" in n for n in report.notes)


def test_no_pipewire_at_all(monkeypatch):
    monkeypatch.setattr(wiring.pw, "pipewire_available", lambda: False)
    report = wiring.wire_graph(_rt_command(), graph=FakeGraph())
    assert report.errors and not report.ok


def test_fallback_port_names_when_introspection_is_empty(monkeypatch):
    """A sink whose ports are not (yet) introspectable still gets wired."""
    graph = FakeGraph(streams=[], links=[], sink_ports=())
    link = FakePwLink()
    monkeypatch.setattr(wiring, "pw_link", link)
    rt = _rt_command()

    wiring.wire_graph(rt, graph=graph)

    connect = [c for c in rt.seen if c.get("cmd") == "connect"][0]
    assert connect["outputs"][0] == f"{SINK}:playback_FL"


# --------------------------------------------------------------------------
# the change detector that makes 2 s polling affordable
# --------------------------------------------------------------------------

def _sig(graph):
    return wiring.stream_signature(graph)


def test_signature_is_stable_when_nothing_changed():
    g1 = FakeGraph(streams=[_stream(7, "app")],
                   links=[("app:out_FL", f"{RT}:in_1")])
    g2 = FakeGraph(streams=[_stream(7, "app")],
                   links=[("app:out_FL", f"{RT}:in_1")])
    assert _sig(g1) == _sig(g2)


def test_signature_changes_when_a_new_stream_appears():
    """Exactly the pause/play case: a brand-new node feeding the sink."""
    before = FakeGraph(streams=[], links=[])
    after = FakeGraph(streams=[_stream(42, "app")],
                      links=[("app:out_FL", f"{SINK}:playback_FL")])
    assert _sig(before) != _sig(after)


def test_signature_changes_when_a_stream_is_relinked_to_the_sink():
    """Same node, but WirePlumber moved it off the rt host."""
    wired = FakeGraph(streams=[_stream(7, "app")],
                      links=[("app:out_FL", f"{RT}:in_1")])
    bypassed = FakeGraph(streams=[_stream(7, "app")],
                         links=[("app:out_FL", f"{SINK}:playback_FL")])
    assert _sig(wired) != _sig(bypassed)


def test_signature_changes_when_the_rt_host_loses_its_output_link():
    wired = FakeGraph(streams=[], links=[(f"{RT}:out_1", f"{SINK}:playback_FL")])
    broken = FakeGraph(streams=[], links=[])
    assert _sig(wired) != _sig(broken)


def test_signature_changes_with_the_default_sink():
    a = FakeGraph(sink="sink-a", streams=[], links=[])
    b = FakeGraph(sink="sink-b", streams=[], links=[])
    assert _sig(a) != _sig(b)


def test_signature_ignores_unrelated_links():
    """A capture stream elsewhere must not force a wiring pass every tick."""
    base = FakeGraph(streams=[_stream(7, "app")],
                     links=[("app:out_FL", f"{RT}:in_1")])
    noisy = FakeGraph(streams=[_stream(7, "app"), _stream(8, "mic",
                                                         direction="capture")],
                      links=[("app:out_FL", f"{RT}:in_1"),
                             ("mic:out_1", "recorder:in_1")])
    assert _sig(base) == _sig(noisy)


def test_signature_is_none_without_pipewire(monkeypatch):
    monkeypatch.setattr(wiring.pw, "pipewire_available", lambda: False)
    assert wiring.stream_signature(FakeGraph()) is None
