"""Keep EQForge in the audio path, automatically.

The rt-host deployment inserts `eqforge-rt` between applications and the
default sink by rewiring the PipeWire graph. That wiring is *per stream*,
and PipeWire throws streams away: pause a player and its stream node is
destroyed, press play and a brand-new node appears which WirePlumber links
straight to the sink - bypassing EQForge. The user-visible symptom is audio
that silently stops being processed until `eqforge system wire` is run again.

This module holds the wiring algorithm once, so the CLI (`eqforge system
wire`) and the daemon's watcher loop (every couple of seconds) do exactly
the same thing:

    wire_graph(rt_command)          # idempotent; returns a WireReport
    stream_signature(graph)         # cheap key that changes when wiring is
                                    # stale, so the watcher can skip no-ops

Everything is done from a single `pw-dump` snapshot (see
`pipewire.GraphView`), so a pass costs one subprocess plus the `pw-link`
calls that actually change something.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from eqforge.errors import SystemError
from eqforge.log import get_logger
from eqforge.system import pipewire as pw

log = get_logger("system.wiring")

#: how many channels to wire when the rt host does not say
DEFAULT_CHANNELS = 2

RtCommand = Callable[[dict], dict | None]


@dataclass
class WireReport:
    """What one wiring pass found and did."""

    sink: str | None = None
    rt_node: str | None = None
    channels: int = 0
    outputs_ok: int = 0        # rt -> sink connections present afterwards
    outputs_new: int = 0       # ... of which this pass created
    streams_moved: int = 0     # app streams re-pointed at the rt host
    already_wired: int = 0     # app streams already going through it
    skipped: int = 0           # streams deliberately left alone
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when this pass actually altered the graph."""
        return bool(self.streams_moved or self.outputs_new)

    @property
    def ok(self) -> bool:
        return self.rt_node is not None and not self.errors

    def summary(self) -> str:
        parts = [f"outputs {self.outputs_ok}"]
        if self.outputs_new:
            parts.append(f"{self.outputs_new} new")
        parts.append(f"streams moved {self.streams_moved}")
        parts.append(f"already {self.already_wired}")
        return ", ".join(parts)


def pw_link(*args: str, required: bool = False
            ) -> subprocess.CompletedProcess | None:
    """Run `pw-link`; None when the tool is unavailable.

    `required=True` turns "not installed" into an actionable SystemError
    instead of a FileNotFoundError traceback mid-rewire.
    """
    if not shutil.which("pw-link"):
        if required:
            raise SystemError(
                "pw-link not found: cannot rewire the PipeWire graph",
                hint="install the PipeWire tools (Debian/Ubuntu: "
                     "`apt install pipewire pipewire-audio`, "
                     "Arch: `pacman -S pipewire`, Fedora: `dnf install pipewire`)")
        return None
    try:
        return subprocess.run(["pw-link", *args], capture_output=True)
    except OSError as e:
        raise SystemError(f"pw-link failed: {e.strerror or e}",
                          hint="is PipeWire running in this session?") from e


def _sink_ports(graph: pw.GraphView, sink_name: str,
                channels: int) -> tuple[list[str], list[str]]:
    """(bare playback port names, fully qualified targets) of the sink."""
    sink_id = graph.node_id(sink_name)
    names: list[str] = []
    targets: list[str] = []
    if sink_id is not None:
        bare = graph.node_ports(sink_id, "input")
        full = graph.node_port_targets(sink_id, "input", sink_name)
        pairs = [(n, f) for n, f in zip(bare, full)
                 if not n.startswith("monitor")]
        names = [n for n, _ in pairs]
        targets = [f for _, f in pairs]
    if not names:
        # Port introspection unavailable (or a device with no ports yet):
        # fall back to the conventional stereo/quad layout.
        names = [f"playback_{s}" for s in ("FL", "FR", "FC", "LFE")]
        targets = [f"{sink_name}:{n}" for n in names]
    return names[:channels], targets[:channels]


def wire_graph(rt_command: RtCommand, *, graph: pw.GraphView | None = None,
               channels: int | None = None,
               echo: Callable[[str], None] | None = None) -> WireReport:
    """Route app -> eqforge-rt -> default sink. Idempotent.

    `rt_command(cmd)` talks to the rt host control socket and returns its
    reply dict, or None when the host is unreachable. The CLI passes an
    RPC-wrapped version, the daemon passes `EngineState._tell_rt` - same
    algorithm, no duplicated policy.

    Never raises: a missing pw-link, a dead rt host or a failed connection
    all land in `report.errors` / `report.notes`, because the daemon calls
    this from a timer loop that must not die. `rt_command` itself may raise
    (the CLI's RPC wrapper does when the daemon is down); callers on a timer
    should pass one that cannot.
    """
    report = WireReport()
    say = echo or (lambda msg: None)

    if not pw.pipewire_available():
        report.errors.append("PipeWire tools not found")
        return report

    graph = graph if graph is not None else pw.snapshot()
    sink_name = graph.default_sink()
    if not sink_name:
        report.notes.append("no default sink to wire to")
        say("  no default sink to wire to")
        return report
    if graph.node_id(sink_name) is None:
        report.notes.append(f"default sink {sink_name!r} not in graph")
        say(f"  default sink {sink_name!r} not in graph")
        return report
    report.sink = sink_name

    status = rt_command({"cmd": "status"}) or {}
    rt = status.get("rt") or status          # daemon returns the reply itself
    if not rt.get("ok"):
        report.notes.append("rt host not running")
        return report
    rt_node = str(rt.get("client", "eqforge"))
    report.rt_node = rt_node
    report.channels = int(channels or rt.get("channels")
                          or DEFAULT_CHANNELS)
    chans = report.channels

    playback, targets = _sink_ports(graph, sink_name, chans)
    existing = set(graph.links())

    # 1) rt outputs -> sink playback ports. Ask the host first (it can use
    #    jack_connect), then verify through the graph and fill gaps with
    #    pw-link: the JACK and PipeWire port namespaces do not always agree.
    reply = rt_command({"cmd": "connect", "outputs": targets}) or {}
    report.outputs_ok = int((reply.get("rt") or reply).get("connected", 0) or 0)
    for c, port in enumerate(playback):
        link = (f"{rt_node}:out_{c + 1}", f"{sink_name}:{port}")
        if link in existing:
            report.outputs_ok = max(report.outputs_ok, c + 1)
            continue
        try:
            rc = pw_link(link[0], link[1])
        except SystemError as e:
            report.errors.append(str(e.message))
            say(f"  ! {e.message}")
            break
        if rc is not None and rc.returncode == 0:
            report.outputs_ok = max(report.outputs_ok, c + 1)
            report.outputs_new += 1
        elif rc is not None:
            err = rc.stderr.decode(errors="replace").strip()[:120]
            report.errors.append(f"{link[0]} -> {link[1]}: {err or 'failed'}")

    # 2) app streams feeding the sink -> rt inputs
    rt_inputs = [f"{rt_node}:in_{c + 1}" for c in range(chans)]
    sink_inputs = {f"{sink_name}:{p}" for p in playback}
    for st in graph.streams():
        if st.direction != "playback":
            continue
        if st.node_name in (rt_node, sink_name) or not st.node_name:
            continue
        linked = [(o, i) for o, i in existing
                  if o.startswith(f"{st.node_name}:")]
        if not linked:
            report.skipped += 1
            continue                      # dormant stream, leave it alone
        if all(i in rt_inputs for _, i in linked):
            report.already_wired += 1
            continue                      # already through the rt host
        if not any(i in sink_inputs for _, i in linked):
            report.skipped += 1
            continue                      # lives on another node on purpose
        moved_this = False
        for c, port in enumerate(
                graph.node_ports(st.id, "output")[:chans]):
            src = f"{st.node_name}:{port}"
            dst = rt_inputs[c]
            if (src, dst) in existing:
                continue
            try:
                for o, i in linked:       # unhook from the sink first
                    if o == src:
                        pw_link("-d", src, i, required=True)
                rc = pw_link(src, dst, required=True)
            except SystemError as e:
                # Never raise out of a wiring pass: the daemon calls this on
                # a timer and a missing pw-link must not kill the watcher.
                report.errors.append(str(e.message))
                say(f"  ! {e.message}")
                break
            if rc is not None and rc.returncode == 0:
                report.streams_moved += 1
                moved_this = True
            elif rc is not None:
                err = rc.stderr.decode(errors="replace").strip()[:120]
                report.errors.append(f"{src} -> {dst}: {err or 'failed'}")
                say(f"  ! {src} -> {dst}: {err or 'failed'}")
        if moved_this:
            log.info("rewired %s (%s) through %s", st.node_name,
                     st.binary or "?", rt_node)

    if report.streams_moved == 0 and report.already_wired == 0:
        report.notes.append(
            "no app streams were playing into the default sink right now")
    return report


def stream_signature(graph: pw.GraphView | None = None) -> str | None:
    """A key that changes exactly when the wiring may have gone stale.

    Covers the cases that break processing in practice: a stream appearing
    or disappearing (player stopped/started), WirePlumber re-pointing a
    stream at the sink, the default sink changing, and the rt host's own
    links vanishing after a restart. Comparing this is much cheaper than
    re-running `wire_graph`, which is what makes 2 s polling affordable.
    """
    if not pw.pipewire_available():
        return None
    try:
        graph = graph if graph is not None else pw.snapshot()
        sink = graph.default_sink() or ""
        playback = sorted((s.id, s.node_name)
                          for s in graph.streams() if s.direction == "playback")
        names = {n for _, n in playback}
        relevant = sorted(
            (o, i) for o, i in graph.links()
            if (sink and (f"{sink}:" in i or f"{sink}:" in o))
            or any(o.startswith(f"{n}:") for n in names))
        return json.dumps({"sink": sink, "streams": playback,
                           "links": relevant}, sort_keys=True)
    except SystemError:
        return None
