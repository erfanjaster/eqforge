"""Room EQ Wizard (REW) equaliser .txt importer.

REW exports look like:

    EQ Dump saved by REW
    Filter  1: ON  PK       Fc    105.0 Hz  Gain  +5.1 dB  Q 1.100
    Filter  2: ON  LSC      Fc     30.0 Hz  Gain  +3.0 dB  Q 0.707
    Filter  3: ON  HP       Fc     20.0 Hz  Q 0.707
    Filter  4: ON  Peak     Fc    3000 Hz   Gain  -2.5 dB  BW 1.5 oct

Note REW may express bandwidth in octaves (BW) instead of Q; we convert:
    Q = f0 / (bw_oct * f0) ... classic: Q = 1 / (2*sinh(ln(2)/2*BW))
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from eqforge.errors import ImportError_
from eqforge.importers.autoeq import TYPE_MAP

_REW_RE = re.compile(
    r"Filter\s*\d*\s*:\s*(?P<on>ON|OFF)\s+(?P<type>[A-Za-z]+)\s+"
    r"Fc\s+(?P<fc>[\d.]+)\s*Hz\s*"
    r"(?:Gain\s+(?P<sign>[+-]?)(?P<gain>[\d.]+)\s*dB)?"
    r"\s*(?:(?:Q\s+(?P<q>[\d.]+))|(?:BW\s+(?P<bw>[\d.]+)\s*oct))?",
    re.IGNORECASE)


def bw_oct_to_q(bw: float) -> float:
    """Octave bandwidth -> Q (standard peaking filter relation)."""
    if bw <= 0:
        return 1.0
    return 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * bw))


def looks_like_rew(text: str) -> bool:
    head = text[:2000].lower()
    return "rew" in head or ("filter" in head and "bw" in head)


def parse_rew(text: str) -> dict:
    filters = []
    for mm in _REW_RE.finditer(text):
        t = TYPE_MAP.get(mm.group("type").upper())
        if t is None:
            continue
        q = None
        if mm.group("q"):
            q = float(mm.group("q"))
        elif mm.group("bw"):
            q = round(bw_oct_to_q(float(mm.group("bw"))), 4)
        gain = 0.0
        if mm.group("gain"):
            gain = float(mm.group("gain"))
            if mm.group("sign") == "-":
                gain = -gain
        filters.append({
            "type": t,
            "freq": float(mm.group("fc")),
            "gain_db": gain,
            "q": q if q is not None else (0.71 if t in
                                          ("lowshelf", "highshelf") else 1.0),
            "enabled": mm.group("on").upper() == "ON",
        })
    if not filters:
        raise ImportError_("no REW filters found",
                           hint="export from REW: EQ window -> Export filter settings -> txt")
    return {"preamp_db": None, "filters": filters}


def import_file(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ImportError_(f"cannot read {path}: {e}")
    return parse_rew(text)
