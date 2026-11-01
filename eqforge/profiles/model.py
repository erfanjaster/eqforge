"""Profile data model.

A profile is a JSON document describing a complete processing chain plus
matching rules and metadata. Format is versioned (`format_version`) and
validated (eqforge.profiles.schema); older versions are migrated
(eqforge.profiles.versioning).

See docs/profile-format.md for the authoritative specification.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from eqforge.version import PROFILE_FORMAT_VERSION

FORMAT_ID = "eqforge.profile"

FILTER_TYPES = ("peak", "lowshelf", "highshelf", "lowpass", "highpass",
                "bandpass", "notch", "allpass", "lowshelf12", "highshelf12")


@dataclass
class FilterSpec:
    type: str = "peak"
    freq: float = 1000.0
    gain_db: float = 0.0
    q: float = 1.0
    enabled: bool = True
    channel: Any = "all"          # "all" | int
    use_q: bool | None = None     # None -> default per type (shelves: slope)
    slope: float = 0.71           # shelf slope when use_q is False
    name: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": self.type, "freq": self.freq,
                             "q": self.q, "enabled": self.enabled}
        if self.type in ("lowpass", "highpass", "notch", "bandpass",
                         "allpass"):
            pass  # gain irrelevant
        else:
            d["gain_db"] = self.gain_db
        if self.channel != "all":
            d["channel"] = self.channel
        if self.use_q is not None:
            d["use_q"] = self.use_q
            if not self.use_q:
                d["slope"] = self.slope
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FilterSpec":
        return cls(
            type=d.get("type", "peak"),
            freq=float(d.get("freq", 1000.0)),
            gain_db=float(d.get("gain_db", 0.0)),
            q=float(d.get("q", 1.0)),
            enabled=bool(d.get("enabled", True)),
            channel=d.get("channel", "all"),
            use_q=d.get("use_q"),
            slope=float(d.get("slope", 0.71)),
            name=d.get("name"),
        )


@dataclass
class Profile:
    id: str
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    builtin: bool = False
    created: float = 0.0
    modified: float = 0.0
    author: str = ""

    preamp_mode: str = "auto"     # "auto" | "manual"
    preamp_db: float = 0.0        # used when manual

    filters: list[FilterSpec] = field(default_factory=list)
    filters_op: str = "replace"   # inheritance: replace | append

    crossfeed: dict = field(default_factory=dict)
    convolver: dict = field(default_factory=dict)
    compressor: dict = field(default_factory=dict)
    limiter: dict = field(default_factory=lambda: {
        "enabled": True, "ceiling_db": -1.0, "attack_ms": 5.0,
        "release_ms": 60.0, "lookahead_ms": 1.5, "knee_db": 3.0})
    loudness: dict = field(default_factory=dict)   # {"target_lufs": -16}
    matching: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)      # forward-compat payload

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        now = time.time()
        d: dict[str, Any] = {
            "format": FORMAT_ID,
            "format_version": PROFILE_FORMAT_VERSION,
            "id": self.id,
            "name": self.name or self.id,
            "modified": self.modified or now,
        }
        if self.description:
            d["description"] = self.description
        if self.tags:
            d["tags"] = self.tags
        if self.extends:
            d["extends"] = self.extends
        if self.builtin:
            d["builtin"] = True
        if self.created:
            d["created"] = self.created
        if self.author:
            d["author"] = self.author
        d["preamp"] = {"mode": self.preamp_mode}
        if self.preamp_mode == "manual":
            d["preamp"]["gain_db"] = self.preamp_db
        d["eq"] = {"filters": [f.to_dict() for f in self.filters]}
        if self.filters_op != "replace":
            d["eq"]["filters_op"] = self.filters_op
        if self.crossfeed:
            d["crossfeed"] = self.crossfeed
        if self.convolver:
            d["convolver"] = self.convolver
        if self.compressor:
            d["compressor"] = self.compressor
        if self.limiter:
            d["limiter"] = self.limiter
        if self.loudness:
            d["loudness"] = self.loudness
        if self.matching:
            d["matching"] = self.matching
        if self.analysis:
            d["analysis"] = self.analysis
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {"format", "format_version", "id", "name", "description",
                 "tags", "extends", "builtin", "created", "modified",
                 "author", "preamp", "eq", "crossfeed", "convolver",
                 "compressor", "limiter", "loudness", "matching",
                 "analysis"}
        pre = d.get("preamp") or {}
        eq = d.get("eq") or {}
        return cls(
            id=str(d.get("id", "unnamed")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            tags=list(d.get("tags") or []),
            extends=[str(e) for e in (d.get("extends") or [])],
            builtin=bool(d.get("builtin", False)),
            created=float(d.get("created", 0) or 0),
            modified=float(d.get("modified", 0) or 0),
            author=str(d.get("author", "")),
            preamp_mode=str(pre.get("mode", "auto")),
            preamp_db=float(pre.get("gain_db", 0.0)),
            filters=[FilterSpec.from_dict(f) for f in (eq.get("filters") or [])],
            filters_op=str(eq.get("filters_op", "replace")),
            crossfeed=dict(d.get("crossfeed") or {}),
            convolver=dict(d.get("convolver") or {}),
            compressor=dict(d.get("compressor") or {}),
            limiter=dict(d.get("limiter") or {}),
            loudness=dict(d.get("loudness") or {}),
            matching=dict(d.get("matching") or {}),
            analysis=dict(d.get("analysis") or {}),
            extra={k: v for k, v in d.items() if k not in known},
        )
