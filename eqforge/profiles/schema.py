"""Profile schema validation.

Hand-rolled validator (no external schema dependency): produces a list of
Issues with severity `error` (reject) or `warning` (accept, inform). Strict
on things that affect audio safety, permissive on unknown keys so newer
profiles remain loadable by older versions.
"""
from __future__ import annotations

from dataclasses import dataclass

from eqforge.profiles.model import FILTER_TYPES

RANGES = {
    "freq": (1.0, 1_000_000.0),
    "gain_db": (-48.0, 48.0),
    "q": (0.0001, 200.0),
    "slope": (0.05, 1.0),
}


@dataclass
class Issue:
    severity: str          # "error" | "warning"
    path: str              # JSON-pointer-ish location
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path}: {self.message}"


def _num(d: dict, key, default=None):
    v = d.get(key, default)
    return v


def validate_profile(d: dict) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(d, dict):
        return [Issue("error", "/", "profile must be a JSON object")]

    if d.get("format") not in (None, "eqforge.profile"):
        issues.append(Issue("error", "/format",
                            f"unknown format {d.get('format')!r}"))
    fv = d.get("format_version", 1)
    if not isinstance(fv, int) or fv < 1 or fv > 16:
        issues.append(Issue("error", "/format_version",
                            f"unsupported format_version {fv!r}"))

    if not d.get("id") or not isinstance(d.get("id"), str):
        issues.append(Issue("error", "/id", "missing profile id"))
    elif not _valid_id(d["id"]):
        issues.append(Issue("error", "/id",
                            "id must match [a-z0-9._-]+ (dot-separated)"))

    ext = d.get("extends")
    if ext is not None:
        if not isinstance(ext, list) or not all(isinstance(e, str) for e in ext):
            issues.append(Issue("error", "/extends", "must be a list of ids"))
        elif len(ext) > 8:
            issues.append(Issue("error", "/extends",
                                "inheritance chain too deep (max 8)"))

    pre = d.get("preamp")
    if pre is not None:
        if not isinstance(pre, dict):
            issues.append(Issue("error", "/preamp", "must be an object"))
        else:
            if pre.get("mode", "auto") not in ("auto", "manual"):
                issues.append(Issue("error", "/preamp/mode",
                                    "must be 'auto' or 'manual'"))
            g = pre.get("gain_db")
            if g is not None and not (-60.0 <= float(g) <= 60.0):
                issues.append(Issue("error", "/preamp/gain_db",
                                    f"out of range (-60..60): {g}"))

    eq = d.get("eq")
    if eq is not None:
        if not isinstance(eq, dict):
            issues.append(Issue("error", "/eq", "must be an object"))
        else:
            if eq.get("filters_op", "replace") not in ("replace", "append"):
                issues.append(Issue("error", "/eq/filters_op",
                                    "must be 'replace' or 'append'"))
            filters = eq.get("filters", [])
            if not isinstance(filters, list):
                issues.append(Issue("error", "/eq/filters", "must be a list"))
            elif len(filters) > 64:
                issues.append(Issue("error", "/eq/filters",
                                    "more than 64 filters (native max)"))
            else:
                for i, f in enumerate(filters):
                    issues.extend(_validate_filter(f, f"/eq/filters/{i}"))

    lim = d.get("limiter")
    if lim is not None and isinstance(lim, dict):
        c = lim.get("ceiling_db", -1.0)
        if not (-60.0 <= float(c) <= 0.0):
            issues.append(Issue("error", "/limiter/ceiling_db",
                                f"must be within -60..0 dB: {c}"))
        for k, lo, hi in (("attack_ms", 0.1, 500.0),
                          ("release_ms", 1.0, 10000.0),
                          ("lookahead_ms", 0.0, 50.0),
                          ("knee_db", 0.0, 24.0)):
            v = lim.get(k)
            if v is not None and not (lo <= float(v) <= hi):
                issues.append(Issue("error", f"/limiter/{k}",
                                    f"out of range {lo}..{hi}: {v}"))

    comp = d.get("compressor")
    if comp is not None and isinstance(comp, dict):
        r = comp.get("ratio", 3.0)
        if not (1.0 <= float(r) <= 100.0):
            issues.append(Issue("error", "/compressor/ratio",
                                f"must be 1..100: {r}"))
        t = comp.get("threshold_db", -18.0)
        if not (-100.0 <= float(t) <= 0.0):
            issues.append(Issue("error", "/compressor/threshold_db",
                                f"out of range: {t}"))

    xf = d.get("crossfeed")
    if xf is not None and isinstance(xf, dict):
        lv = xf.get("level_db", -6.0)
        if not (-30.0 <= float(lv) <= 0.0):
            issues.append(Issue("error", "/crossfeed/level_db",
                                f"out of range -30..0: {lv}"))
        fc = xf.get("fc_hz", 700.0)
        if not (50.0 <= float(fc) <= 4000.0):
            issues.append(Issue("error", "/crossfeed/fc_hz",
                                f"out of range 50..4000: {fc}"))

    cv = d.get("convolver")
    if cv is not None and isinstance(cv, dict):
        if cv.get("enabled") and not (cv.get("ir") or cv.get("ir_file")):
            issues.append(Issue("error", "/convolver",
                                "enabled but neither 'ir' nor 'ir_file' given"))
        g = cv.get("gain_db", 0.0)
        if not (-48.0 <= float(g) <= 48.0):
            issues.append(Issue("error", "/convolver/gain_db",
                                f"out of range: {g}"))

    loud = d.get("loudness")
    if loud is not None and isinstance(loud, dict):
        t = loud.get("target_lufs")
        if t is not None and not (-70.0 <= float(t) <= 0.0):
            issues.append(Issue("error", "/loudness/target_lufs",
                                f"out of range: {t}"))

    match = d.get("matching")
    if match is not None:
        issues.extend(_validate_matching(match))

    return issues


def _valid_id(pid: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", pid))


def _validate_filter(f: dict, path: str) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(f, dict):
        return [Issue("error", path, "filter must be an object")]
    t = f.get("type", "peak")
    if t not in FILTER_TYPES:
        issues.append(Issue("error", f"{path}/type",
                            f"unknown filter type {t!r} "
                            f"(allowed: {', '.join(FILTER_TYPES)})"))
    for key, (lo, hi) in RANGES.items():
        if key in f:
            try:
                v = float(f[key])
            except (TypeError, ValueError):
                issues.append(Issue("error", f"{path}/{key}",
                                    f"not a number: {f[key]!r}"))
                continue
            if not (lo <= v <= hi):
                sev = "error" if key in ("freq",) else "warning"
                issues.append(Issue(sev, f"{path}/{key}",
                                    f"{v} outside {lo}..{hi}"))
    ch = f.get("channel", "all")
    if ch != "all" and not (isinstance(ch, int) and 0 <= ch < 16):
        issues.append(Issue("error", f"{path}/channel",
                            f"must be 'all' or channel index 0..15: {ch!r}"))
    if f.get("gain_db") is not None and t in ("lowpass", "highpass", "notch",
                                              "bandpass", "allpass"):
        issues.append(Issue("warning", f"{path}/gain_db",
                            f"gain_db is ignored for {t} filters"))
    if abs(float(f.get("gain_db", 0) or 0)) > 24:
        issues.append(Issue("warning", f"{path}/gain_db",
                            "extreme boost (>24 dB) risks distortion"))
    return issues


def _validate_matching(m: dict) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(m, dict):
        return [Issue("error", "/matching", "must be an object")]
    for section in ("devices", "apps"):
        rules = m.get(section)
        if rules is None:
            continue
        if not isinstance(rules, list):
            issues.append(Issue("error", f"/matching/{section}",
                                "must be a list of rules"))
            continue
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                issues.append(Issue("error", f"/matching/{section}/{i}",
                                    "rule must be an object"))
                continue
            if "profile" not in rule or not isinstance(rule["profile"], str):
                issues.append(Issue("error", f"/matching/{section}/{i}/profile",
                                    "rule must select a profile id"))
            match = rule.get("match")
            if not isinstance(match, dict) or not match:
                issues.append(Issue("error", f"/matching/{section}/{i}/match",
                                    "rule needs a non-empty match object"))
    return issues


def validate_or_raise(d: dict) -> list[Issue]:
    """Validate; raise ProfileValidationError on errors. Returns warnings."""
    from eqforge.errors import ProfileValidationError
    issues = validate_profile(d)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        msg = "; ".join(str(e) for e in errors[:5])
        raise ProfileValidationError(f"profile invalid: {msg}")
    return [i for i in issues if i.severity == "warning"]
