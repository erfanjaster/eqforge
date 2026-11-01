"""Device / application matching: rules that pick profiles automatically.

Rule shape (from profile `matching` or config `rules`):

    {"profile": "my-headphones",
     "match": {"name": "*WH-1000*", "kind": "bluetooth"},
     "priority": 10}

`match` supports substring globs (*) on any identity key (see
Device.identity()); all provided keys must match (AND). Higher priority wins;
ties break toward the more specific rule (more match keys).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from eqforge.log import get_logger

log = get_logger("system.matching")


@dataclass
class Rule:
    profile: str
    match: dict
    priority: int = 0
    source: str = ""            # where the rule came from (profile id / config)

    def matches(self, identity: dict) -> bool:
        for key, pattern in self.match.items():
            value = identity.get(key)
            if value is None:
                return False
            value = str(value)
            pattern = str(pattern)
            if "*" in pattern or "?" in pattern:
                if not fnmatch.fnmatch(value.lower(), pattern.lower()):
                    return False
            elif pattern.lower() not in value.lower():
                return False
        return True

    @property
    def specificity(self) -> int:
        return len(self.match)


def collect_rules(profile_store, config: dict | None = None) -> list[Rule]:
    """Rules from config (global) + every profile's `matching` section."""
    rules: list[Rule] = []
    for r in (config or {}).get("rules", []):
        try:
            rules.append(Rule(profile=r["profile"], match=r.get("match", {}),
                              priority=int(r.get("priority", 0)),
                              source="config"))
        except (KeyError, TypeError, ValueError):
            log.warning("skipping malformed rule: %r", r)
    for entry in profile_store.list_ids():
        try:
            d = profile_store.load_raw(entry["id"])
        except Exception:  # noqa: BLE001
            continue
        m = d.get("matching") or {}
        for section, idkey in (("devices", "device"), ("apps", "app")):
            for r in m.get(section, []) or []:
                if isinstance(r, dict) and r.get("profile"):
                    rules.append(Rule(profile=r["profile"],
                                      match=r.get("match", {}),
                                      priority=int(r.get("priority", 0)),
                                      source=f"{entry['id']}:{idkey}"))
    return rules


def select_profile(identity: dict, rules: list[Rule]) -> Rule | None:
    """Best matching rule for a device/app identity, or None."""
    candidates = [r for r in rules if r.matches(identity)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r.priority, r.specificity), reverse=True)
    return candidates[0]


def match_stream(binary: str, name: str, node_name: str,
                 rules: list[Rule]) -> Rule | None:
    identity = {"name": name, "binary": binary, "node_name": node_name}
    return select_profile(identity, rules)
