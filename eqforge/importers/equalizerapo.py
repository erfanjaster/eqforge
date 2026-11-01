"""EqualizerAPO config.txt importer (partial, the common subset).

Understood directives:
    Preamp: -6.2 dB
    Filter: ON PK Fc 105 Hz Gain 5.1 dB Q 1.1
    Filter 1: ON LSC 30 Hz Gain 3 dB
    GraphicEQ: 25 -1.5; 40 0.0; 63 2.1; ...
    Device: ...   (captured into matching hints, not imported as filters)

Lines starting with '#' or '//' are comments. Unknown directives are
collected in `unknown` so callers can warn the user.
"""
from __future__ import annotations

import re
from pathlib import Path

from eqforge.errors import ImportError_
from eqforge.importers.autoeq import TYPE_MAP

_APO_FILTER_RE = re.compile(
    r"Filter(?:\s*\d+)?\s*:\s*(?P<on>ON|OFF)\s+(?P<type>[A-Za-z]+)\s+"
    r"(?:Fc\s+)?(?P<fc>[\d.]+)\s*Hz?\s*(?:Gain\s+(?P<gain>[-\d.]+)\s*dB)?"
    r"\s*(?:Q\s+(?P<q>[\d.]+))?", re.IGNORECASE)
_GRAPHIC_RE = re.compile(r"GraphicEQ\s*:\s*(?P<rest>.+)", re.IGNORECASE)
_PREAMP_RE = re.compile(r"^\s*Preamp\s*:\s*([-\d.]+)\s*dB", re.IGNORECASE)
_DEVICE_RE = re.compile(r"^\s*Device\s*:\s*(.+)$", re.IGNORECASE)


def looks_like_apo(text: str) -> bool:
    head = text[:3000].lower()
    return "graphiceq" in head or "equalizer apo" in head or \
        bool(re.search(r"^\s*filter\s*:\s*on", head, re.MULTILINE))


def graphic_eq_to_filters(rest: str) -> list[dict]:
    """GraphicEQ: 25 -1.5; 40 0.0; ... -> peak filters (Q from spacing)."""
    pts = []
    for part in rest.replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        mm = re.match(r"([\d.]+)\s+([-\d.]+)", part)
        if mm:
            pts.append((float(mm.group(1)), float(mm.group(2))))
    if not pts:
        return []
    filters = []
    for i, (f, g) in enumerate(pts):
        if i + 1 < len(pts):
            q = max(f / max(pts[i + 1][0] - f, 1.0), 0.5)
        else:
            q = filters[-1]["q"] if filters else 1.41
        filters.append({"type": "peak", "freq": f, "gain_db": g,
                        "q": round(min(q, 10.0), 3), "enabled": True})
    return filters


def parse_apo(text: str) -> dict:
    filters: list[dict] = []
    preamp = None
    devices: list[str] = []
    unknown: list[str] = []

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = _PREAMP_RE.match(s)
        if m:
            preamp = float(m.group(1))
            continue
        m = _DEVICE_RE.match(s)
        if m:
            devices.append(m.group(1).strip().rstrip(';').strip())
            continue
        m = _GRAPHIC_RE.search(s)
        if m:
            filters.extend(graphic_eq_to_filters(m.group("rest")))
            continue
        m = _APO_FILTER_RE.search(s)
        if m:
            t = TYPE_MAP.get(m.group("type").upper())
            if t is None:
                unknown.append(s)
                continue
            filters.append({
                "type": t,
                "freq": float(m.group("fc")),
                "gain_db": float(m.group("gain")) if m.group("gain") else 0.0,
                "q": float(m.group("q")) if m.group("q") else (
                    0.71 if t in ("lowshelf", "highshelf") else 1.0),
                "enabled": m.group("on").upper() == "ON",
            })
            continue
        if re.match(r"^(Copy|Include|Channel|Stage|Delay|Eval|Locale)\b", s):
            unknown.append(s)

    if not filters and preamp is None:
        raise ImportError_("no EqualizerAPO directives recognized")
    return {"preamp_db": preamp, "filters": filters,
            "devices": devices, "unknown": unknown}


def import_file(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ImportError_(f"cannot read {path}: {e}")
    return parse_apo(text)
