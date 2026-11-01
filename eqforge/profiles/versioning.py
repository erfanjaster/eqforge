"""Profile format versioning and migration.

Migrations run as a chain v1 -> v2 -> ... -> current. Each step is a pure
dict->dict transform, so migrating is idempotent and testable.
"""
from __future__ import annotations

from eqforge.errors import ProfileMigrationError
from eqforge.version import PROFILE_FORMAT_VERSION


def _migrate_1_to_2(d: dict) -> dict:
    """v1 (flat EQ-only) -> v2 (chain + matching + preamp modes)."""
    d = dict(d)
    if "preamp_db" in d and "preamp" not in d:
        d["preamp"] = {"mode": "manual", "gain_db": float(d.pop("preamp_db"))}
    if "filters" in d and "eq" not in d:
        d["eq"] = {"filters": d.pop("filters")}
    if "eq" in d and isinstance(d["eq"], list):
        d["eq"] = {"filters": d["eq"]}
    # normalize legacy filter keys
    for f in (d.get("eq", {}) or {}).get("filters", []) or []:
        if isinstance(f, dict):
            if "frequency" in f and "freq" not in f:
                f["freq"] = f.pop("frequency")
            if "gain" in f and "gain_db" not in f:
                f["gain_db"] = f.pop("gain")
            from eqforge.importers.autoeq import TYPE_MAP
            t = f.get("type")
            if isinstance(t, str):
                norm = TYPE_MAP.get(t.upper(),
                                    t.lower().replace("_", ""))
                f["type"] = norm
    if "device_match" in d:
        d.setdefault("matching", {})["devices"] = d.pop("device_match")
    if "app_match" in d:
        d.setdefault("matching", {})["apps"] = d.pop("app_match")
    d["format_version"] = 2
    return d


_MIGRATIONS = {1: _migrate_1_to_2}


def migrate(d: dict) -> dict:
    """Migrate a profile dict to the current format version."""
    d = dict(d)
    v = int(d.get("format_version", 1) or 1)
    if v > PROFILE_FORMAT_VERSION:
        raise ProfileMigrationError(
            f"profile format v{v} is newer than this EQForge "
            f"(v{PROFILE_FORMAT_VERSION})",
            hint="update eqforge to load this profile")
    seen = set()
    while v < PROFILE_FORMAT_VERSION:
        if v in seen:
            raise ProfileMigrationError(f"migration loop at v{v}")
        seen.add(v)
        fn = _MIGRATIONS.get(v)
        if fn is None:
            raise ProfileMigrationError(f"no migration path from v{v}")
        d = fn(d)
        v = int(d.get("format_version", v + 1))
    return d


def needs_migration(d: dict) -> bool:
    return int(d.get("format_version", 1) or 1) < PROFILE_FORMAT_VERSION
