"""PipeWire / WirePlumber integration.

Discovery is done through the PipeWire CLI tools (`pw-dump`, `pw-metadata`,
`pw-link`) rather than linking libpipewire into Python: this keeps the
control plane dependency-free, survives daemon restarts, and works with any
PipeWire >= 0.3.30. All functions degrade gracefully when PipeWire is absent
(returning empty results / raising SystemError only when explicitly asked).

Device model (eqforge.system.model.Device):
    id, name, description, direction (sink/source), media_class,
    profile properties (alsa card name, bluetooth codec...), stable `identity`
    keys used for profile matching.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from eqforge.errors import SystemError
from eqforge.log import get_logger

log = get_logger("system.pipewire")


def pipewire_available() -> bool:
    return shutil.which("pw-dump") is not None


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise SystemError(f"{cmd[0]} failed: {e}",
                          hint="is PipeWire installed and the session running?")
    if r.returncode != 0:
        raise SystemError(f"{cmd[0]} exited {r.returncode}: "
                          f"{r.stderr.strip()[:200]}")
    return r.stdout


@dataclass
class Device:
    id: int
    name: str
    description: str
    direction: str                      # "sink" | "source"
    media_class: str
    node_name: str = ""
    props: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Best-effort human category used by matching + UI."""
        p = {k.lower(): v for k, v in self.props.items()}
        api = str(p.get("api.alsa.path", "")) + str(p.get("api.bluez5.address", ""))
        if "bluez" in json.dumps(p).lower():
            return "bluetooth"
        if "usb" in str(p.get("device.bus", "")).lower():
            return "usb"
        if "hdmi" in self.description.lower() or "hdmi" in self.name.lower():
            return "hdmi"
        if p.get("device.form-factor") in ("headphone", "headset"):
            return "headphones"
        if "speaker" in self.description.lower() or \
           p.get("device.form-factor") == "speakers":
            return "speakers"
        return "other"

    def identity(self) -> dict:
        """Stable-ish keys a matching rule can use."""
        return {
            "name": self.name,
            "node_name": self.node_name,
            "description": self.description,
            "kind": self.kind,
            "alsa_card": self.props.get("api.alsa.card.name", ""),
            "bluez_address": self.props.get("api.bluez5.address", ""),
            "bluez_name": self.props.get("api.bluez5.device", ""),
            "serial": self.props.get("device.serial", ""),
            "vendor": self.props.get("device.vendor.name", ""),
        }

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "description": self.description, "direction": self.direction,
                "media_class": self.media_class, "node_name": self.node_name,
                "kind": self.kind, "identity": self.identity()}


@dataclass
class Stream:
    id: int
    name: str                  # application name
    node_name: str             # e.g. "alsa_output.pci-...-analog-stereo"
    binary: str                # e.g. "firefox"
    media_class: str
    direction: str             # "playback" (app->sink) | "capture"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "node_name": self.node_name,
                "binary": self.binary, "media_class": self.media_class,
                "direction": self.direction}


def list_devices(direction: str | None = None) -> list[Device]:
    """Audio sink/source nodes currently in the graph."""
    if not pipewire_available():
        return []
    dump = json.loads(_run(["pw-dump"]))
    devices: list[Device] = []
    for obj in dump:
        info = obj.get("info", {})
        props = info.get("props", {})
        mc = str(props.get("media.class", ""))
        if mc not in ("Audio/Sink", "Audio/Source", "Audio/Duplex",
                      "Stream/Output/Audio", "Stream/Input/Audio"):
            continue
        if mc.startswith("Stream/"):
            continue  # handled by list_streams
        direction_ = "sink" if "Sink" in mc or mc == "Audio/Duplex" else "source"
        if direction and direction_ != direction:
            continue
        devices.append(Device(
            id=int(obj.get("id", 0)),
            name=str(props.get("node.name", "")),
            description=str(props.get("node.description",
                                      props.get("node.name", ""))),
            direction=direction_,
            media_class=mc,
            node_name=str(props.get("node.name", "")),
            props=props,
        ))
    return devices


def list_streams() -> list[Stream]:
    """Application audio streams currently in the graph."""
    if not pipewire_available():
        return []
    dump = json.loads(_run(["pw-dump"]))
    streams: list[Stream] = []
    for obj in dump:
        info = obj.get("info", {})
        props = info.get("props", {})
        mc = str(props.get("media.class", ""))
        if mc == "Stream/Output/Audio":
            direction = "playback"
        elif mc == "Stream/Input/Audio":
            direction = "capture"
        else:
            continue
        streams.append(Stream(
            id=int(obj.get("id", 0)),
            name=str(props.get("media.name",
                               props.get("node.name", "?"))),
            node_name=str(props.get("node.name", "")),
            binary=str(props.get("application.process.binary", "")),
            media_class=mc,
            direction=direction,
        ))
    return streams


def _dump() -> list[dict]:
    """One `pw-dump` call, parsed."""
    return json.loads(_run(["pw-dump"]))


def _node_names(dump: list[dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Node"):
            continue
        props = obj.get("info", {}).get("props", {}) or {}
        try:
            names[int(obj.get("id", -1))] = str(props.get("node.name", ""))
        except (TypeError, ValueError):
            pass
    return names


def _metadata_stores(dump: list[dict]) -> dict[str, list[dict]]:
    """{store-name: [entries]} from pw-dump Metadata objects.

    WirePlumber keeps user defaults in the 'default' store; plain PipeWire
    setups use 'settings'. Entry values may be JSON objects, plain strings
    or node ids depending on who wrote them.
    """
    stores: dict[str, list[dict]] = {}
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Metadata"):
            continue
        props = obj.get("props") or obj.get("info", {}).get("props") or {}
        name = str(props.get("metadata.name", ""))
        entries = obj.get("metadata") or obj.get("info", {}).get("metadata")
        if name and isinstance(entries, list):
            stores[name] = entries
    return stores


def _sink_name_from_value(value: Any, nodes: dict[int, str],
                          subject: Any = None) -> str | None:
    """Normalize a default.audio.sink metadata value to a node name."""
    if isinstance(value, dict):                    # WirePlumber JSON object
        name = value.get("name")
        return str(name) if name else None
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{"):                      # JSON encoded as string
            try:
                return _sink_name_from_value(json.loads(s), nodes, subject)
            except json.JSONDecodeError:
                m = re.search(r'"name"\s*:\s*"([^"]+)"', s)
                return m.group(1) if m else None
        s = s.strip('"')
        if not s:
            return None
        if s.isdigit():
            return nodes.get(int(s))
        return s
    if isinstance(value, int) and value > 0:       # bare node id
        return nodes.get(value)
    if isinstance(subject, int) and subject > 0:   # id carried in subject
        return nodes.get(subject)
    return None


# pw-metadata text line: update: id:0 key:'K' value:'V' type:'T'
_MD_LINE = re.compile(
    r"key:'(?P<key>[^']*)'\s+value:'(?P<value>.*?)'"
    r"(?:\s+type:'(?P<type>[^']*)')?\s*$")


def _default_sink_from_metadata_cli(nodes: dict[int, str]) -> str | None:
    """Fallback parser for `pw-metadata -n <store> 0` text output."""
    if not shutil.which("pw-metadata"):
        return None
    for store in ("default", "settings"):
        try:
            out = _run(["pw-metadata", "-n", store, "0"])
        except SystemError:
            continue
        for line in out.splitlines():
            m = _MD_LINE.search(line.strip())
            if not m or m.group("key") != "default.audio.sink":
                continue
            raw = m.group("value")
            try:                                   # JSON values survive as-is
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            name = _sink_name_from_value(parsed, nodes)
            if name:
                return name
    return None


def default_sink() -> str | None:
    """Current default audio sink *node name*.

    Reads the PipeWire metadata stores: WirePlumber writes the default as a
    JSON object ({"name": ...}) into the 'default' store; plain PipeWire
    setups keep a string in 'settings'. Structured pw-dump first, pw-metadata
    text output as fallback. Returns None when no default is set.
    """
    if not pipewire_available():
        return None
    try:
        dump = _dump()
    except SystemError:
        return None
    nodes = _node_names(dump)
    stores = _metadata_stores(dump)
    for store in ("default", "settings"):
        for entry in stores.get(store, []):
            if entry.get("key") != "default.audio.sink":
                continue
            name = _sink_name_from_value(entry.get("value"), nodes,
                                         entry.get("subject"))
            if name:
                return name
    try:
        return _default_sink_from_metadata_cli(nodes)
    except SystemError:
        return None


def set_default_sink(node_name: str) -> None:
    if not shutil.which("pw-metadata"):
        raise SystemError("pw-metadata not found")
    # WirePlumber watches the 'default' store (JSON value); plain PipeWire
    # consumers read the 'settings' store (plain string). Write both.
    try:
        _run(["pw-metadata", "-n", "default", "0", "default.audio.sink",
              json.dumps({"name": node_name}), "Spa:String:JSON"])
    except SystemError:
        pass
    _run(["pw-metadata", "-n", "settings", "0", "default.audio.sink",
          node_name])
    log.info("default sink -> %s", node_name)


def graph_snapshot() -> dict[str, Any]:
    """Cheap hashable snapshot used for hotplug detection."""
    return {
        "devices": [d.to_dict() for d in list_devices()],
        "streams": [s.to_dict() for s in list_streams()],
        "default_sink": default_sink(),
    }


def link_info() -> dict:
    """Which ports are connected to what (pw-link -i -o)."""
    if not shutil.which("pw-link"):
        return {}
    out = _run(["pw-link", "-i", "-o"])
    graph: dict[str, list[str]] = {}
    current = None
    for line in out.splitlines():
        if not line.startswith(" ") and line.strip().endswith(":"):
            current = line.strip()[:-1]
            graph[current] = []
        elif current and line.strip():
            graph[current].append(line.strip())
    return graph


_PORT_POS = ("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR", "AUX")


def _ports_of(node_id: int, direction: str | None) -> list[tuple[str, str]]:
    """[(port.name, port.alias-or-empty)] of a node, in channel order."""
    dump = _dump()
    found: list[tuple[int, str, str]] = []
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Port"):
            continue
        info = obj.get("info", {})
        props = info.get("props", {}) or {}
        if str(props.get("node.id", "")) != str(node_id):
            continue
        if direction and info.get("direction") != direction:
            continue
        name = str(props.get("port.name", ""))
        if not name:
            continue
        ch = props.get("audio.channel")
        if isinstance(ch, str) and ch in _PORT_POS:
            key = _PORT_POS.index(ch)
        else:
            try:
                key = int(ch)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                suffix = name.rsplit("_", 1)[-1]
                key = _PORT_POS.index(suffix) if suffix in _PORT_POS else 99
        found.append((key, name, str(props.get("port.alias", ""))))
    found.sort(key=lambda t: t[0])
    return [(n, a) for _, n, a in found]


def node_ports(node_id: int, direction: str | None = None) -> list[str]:
    """Port names of a node, in channel order (FL, FR, ...).

    direction: "input" | "output" | None (both). Names are bare port names
    (e.g. "playback_FL"); prefix with the node name for pw-link.
    """
    return [n for n, _ in _ports_of(node_id, direction)]


def node_port_targets(node_id: int, direction: str | None = None,
                      node_name: str = "") -> list[str]:
    """Fully qualified port names usable with jack_connect()/pw-link.

    pipewire-jack exposes non-JACK ports under their `port.alias`
    (e.g. "Built-in Audio:playback_FL"), *not* "node.name:port.name" —
    jack_connect() with the node-name form fails with EINVAL. Prefer the
    alias when present, fall back to node.name:port.name.
    """
    targets = []
    for name, alias in _ports_of(node_id, direction):
        targets.append(alias if alias else
                       (f"{node_name}:{name}" if node_name else name))
    return targets


def links() -> list[tuple[str, str]]:
    """All graph links as ("out_node:out_port", "in_node:in_port")."""
    dump = _dump()
    nodes = _node_names(dump)
    ports: dict[int, str] = {}
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Port"):
            continue
        props = obj.get("info", {}).get("props", {}) or {}
        try:
            pid = int(obj.get("id", -1))
            nid = int(props.get("node.id", -1))
        except (TypeError, ValueError):
            continue
        ports[pid] = f"{nodes.get(nid, nid)}:{props.get('port.name', '?')}"
    out: list[tuple[str, str]] = []
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Link"):
            continue
        props = obj.get("info", {}).get("props", {}) or {}
        try:
            op = ports.get(int(props["link.output.port"]))
            ip = ports.get(int(props["link.input.port"]))
        except (KeyError, TypeError, ValueError):
            continue
        if op and ip:
            out.append((op, ip))
    return out
