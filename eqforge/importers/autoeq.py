"""AutoEQ / jaakkopasanen parametric & graphic EQ .txt importer.

Handles the widely used AutoEQ text format:

    Preamp: -6.2 dB
    Filter 1: ON PK       Fc 105.0 Hz  Gain  5.10 dB  Q 1.100
    Filter 2: ON LSC      Fc  30.0 Hz  Gain  3.00 dB
    Filter 3: ON HSC      Fc 9000 Hz   Gain -2.50 dB
    Filter: ON PK Fc 100 Hz Gain 1.5 dB Q 2.00
    Filter 4: OFF ...

Also accepts the CSV variant:

    Filter,Frequency,Quality,Gain
    1 ON PK,105,1.1,5.1

Type tokens seen in the wild: PK/PeakingEQ, LSC/LowShelf, HSC/HighShelf,
LPF/LowPass, HPF/HighPass, NOTCH/BandPass...
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from eqforge.errors import ImportError_

TYPE_MAP = {
    "PK": "peak", "PEAKINGEQ": "peak", "PEAK": "peak",
    "LSC": "lowshelf", "LOWSHELF": "lowshelf", "LS": "lowshelf",
    "HSC": "highshelf", "HIGHSHELF": "highshelf", "HS": "highshelf",
    "LPF": "lowpass", "LOWPASS": "lowpass", "LP": "lowpass",
    "HPF": "highpass", "HIGHPASS": "highpass", "HP": "highpass",
    "NOTCH": "notch", "BP": "bandpass", "BANDPASS": "bandpass",
}

_FILTER_RE = re.compile(
    r"Filter\s*\d*\s*:\s*(?P<on>ON|OFF)\s+(?P<type>[A-Za-z]+)\s+"
    r"Fc\s+(?P<fc>[\d.]+)\s*Hz\s*(?:Gain\s+(?P<gain>[-\d.]+)\s*dB)?"
    r"\s*(?:Q\s+(?P<q>[\d.]+))?",
    re.IGNORECASE)

_PREAMP_RE = re.compile(r"Preamp\s*:\s*([-\d.]+)\s*dB", re.IGNORECASE)

_CSV_RE = re.compile(
    r"^\s*\d+\s+(?P<on>ON|OFF)\s+(?P<type>[A-Za-z]+)\s*,\s*"
    r"(?P<fc>[\d.]+)\s*,\s*(?P<q>[\d.]+)?\s*,\s*(?P<gain>[-\d.]+)",
    re.IGNORECASE)


def looks_like_autoeq(text: str) -> bool:
    head = text[:4000].lower()
    return ("filter" in head and "fc" in head) or \
           bool(_CSV_RE.search(text[:4000])) or "preamp" in head


def parse_autoeq(text: str) -> dict:
    """Parse AutoEQ-style text. Returns {'preamp_db': float|None, 'filters': [...]}."""
    filters: list[dict] = []
    preamp = None
    m = _PREAMP_RE.search(text)
    if m:
        preamp = float(m.group(1))

    for mm in _FILTER_RE.finditer(text):
        t = TYPE_MAP.get(mm.group("type").upper())
        if t is None:
            continue
        f: dict = {
            "type": t,
            "freq": float(mm.group("fc")),
            "gain_db": float(mm.group("gain")) if mm.group("gain") else 0.0,
            "q": float(mm.group("q")) if mm.group("q") else (
                0.71 if t in ("lowshelf", "highshelf") else 1.0),
            "enabled": mm.group("on").upper() == "ON",
        }
        filters.append(f)

    if not filters:
        for line in text.splitlines():
            mm = _CSV_RE.match(line)
            if not mm:
                continue
            t = TYPE_MAP.get(mm.group("type").upper())
            if t is None:
                continue
            filters.append({
                "type": t,
                "freq": float(mm.group("fc")),
                "gain_db": float(mm.group("gain")),
                "q": float(mm.group("q")) if mm.group("q") else 1.0,
                "enabled": mm.group("on").upper() == "ON",
            })

    if not filters and preamp is None:
        raise ImportError_("no AutoEQ filters found in input",
                           hint="expected lines like 'Filter 1: ON PK Fc 105 Hz Gain 5.1 dB Q 1.1'")
    return {"preamp_db": preamp, "filters": filters}


def import_file(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ImportError_(f"cannot read {path}: {e}")
    return parse_autoeq(text)
