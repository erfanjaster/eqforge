"""Format auto-detection and unified import into an EQForge Profile.

Detects:
  - native eqforge profile JSON
  - AutoEQ .txt / .csv (jaakkopasanen style)
  - REW export .txt
  - EqualizerAPO config.txt
  - plain CSV: freq,gain[,q[,type]] with header or without

`eqforge import-profile <file>` uses this; programmatic callers get a
Profile object ready to save.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from eqforge.errors import ImportError_
from eqforge.importers import autoeq, equalizerapo, rew
from eqforge.log import get_logger
from eqforge.profiles.model import FilterSpec, Profile

log = get_logger("importers.detect")


def detect_format(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ImportError_(f"cannot read {path}: {e}")

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            d = json.loads(text)
            if isinstance(d, dict) and (d.get("format") == "eqforge.profile"
                                        or "eq" in d or "filters" in d):
                return "eqforge"
        except json.JSONDecodeError:
            pass
    if rew.looks_like_rew(text):
        return "rew"
    if equalizerapo.looks_like_apo(text):
        return "equalizerapo"
    if autoeq.looks_like_autoeq(text):
        return "autoeq"
    if _looks_like_csv(text):
        return "csv"
    raise ImportError_(
        f"unrecognized EQ format: {path.name}",
        hint="supported: eqforge JSON, AutoEQ txt/csv, REW txt, "
             "EqualizerAPO config, plain freq/gain CSV")


def _looks_like_csv(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    sample = "\n".join(lines[:20])
    try:
        rows = list(csv.reader(sample.splitlines()))
    except csv.Error:
        return False
    if not rows:
        return False
    numeric_rows = 0
    for row in rows:
        if len(row) >= 2:
            try:
                float(row[0]); float(row[1])
                numeric_rows += 1
            except ValueError:
                continue
    return numeric_rows >= 2


def parse_csv(text: str) -> dict:
    """freq,gain[,q[,type]] rows; optional header line."""
    filters = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 2:
            continue
        try:
            f = float(row[0]); g = float(row[1])
        except ValueError:
            continue  # header or junk
        q = float(row[2]) if len(row) > 2 and row[2].strip() else 1.0
        t = row[3].strip().lower() if len(row) > 3 and row[3].strip() else "peak"
        t = autoeq.TYPE_MAP.get(t.upper(), t)
        filters.append({"type": t, "freq": f, "gain_db": g, "q": q,
                        "enabled": True})
    if not filters:
        raise ImportError_("no numeric rows found in CSV")
    return {"preamp_db": None, "filters": filters}


def detect_and_import(path: Path, store=None, new_id: str | None = None,
                      name: str | None = None) -> Profile:
    """Import any supported file, returning an (unsaved) Profile."""
    path = Path(path)
    fmt = detect_format(path)
    log.info("importing %s as %s", path.name, fmt)

    if fmt == "eqforge":
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("format") == "eqforge.profile":
            from eqforge.profiles.versioning import migrate
            d = migrate(d)
            prof = Profile.from_dict(d)
            if new_id:
                prof.id = new_id
            prof.builtin = False
            return prof
        # bare {filters:...} JSON
        parsed = d
    elif fmt == "autoeq":
        parsed = autoeq.import_file(path)
    elif fmt == "rew":
        parsed = rew.import_file(path)
    elif fmt == "equalizerapo":
        parsed = equalizerapo.import_file(path)
        for u in parsed.get("unknown", [])[:5]:
            log.warning("EqualizerAPO directive not imported: %s", u)
    elif fmt == "csv":
        parsed = parse_csv(path.read_text(encoding="utf-8", errors="replace"))
    else:
        raise ImportError_(f"unsupported format {fmt}")

    filters = [FilterSpec.from_dict(f) for f in parsed.get("filters", [])]
    if len(filters) > 64:
        raise ImportError_(f"{len(filters)} filters exceeds the 64-band limit",
                           hint="disable or merge bands, or split into two profiles")
    pid = new_id or _suggest_id(path)
    prof = Profile(
        id=pid,
        name=name or path.stem.replace("_", " ").replace("-", " ").title(),
        description=f"Imported from {path.name} ({fmt} format)",
        tags=["imported", fmt],
        created=time.time(),
        filters=filters,
    )
    pre = parsed.get("preamp_db")
    if pre is not None:
        prof.preamp_mode = "manual"
        prof.preamp_db = float(pre)
    devices = parsed.get("devices")
    if devices:
        prof.matching = {"devices": [
            {"profile": pid, "match": {"name": d}, "priority": 5}
            for d in devices]}
    return prof


def _suggest_id(path: Path) -> str:
    import re
    stem = re.sub(r"[^a-z0-9._-]+", "-", path.stem.lower()).strip("-")
    return f"imported.{stem[:60]}" or f"imported.{int(time.time())}"
