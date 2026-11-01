"""The EQForge advisor: analysis-driven findings and automatic suggestions.

Every finding is:
  - explainable (points at the measurement that triggered it),
  - actionable (carries a machine-readable `action` where one exists),
  - severity-graded: "ok" | "info" | "warn" | "action".

Rules cover three levels:
  1. profile-only checks (static sanity of an EQ design),
  2. content-only checks (what the audio itself needs),
  3. combined checks (will THIS profile clip/distort THAT content?).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from eqforge.analysis.report import AnalysisReport
from eqforge.smart.headroom import worst_case_boost_db


@dataclass
class Finding:
    id: str
    severity: str                    # ok | info | warn | action
    message: str
    hint: str = ""
    action: dict | None = None       # machine-readable suggestion

    def to_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity,
                "message": self.message, "hint": self.hint,
                "action": self.action}


@dataclass
class Advice:
    findings: list[Finding] = field(default_factory=list)
    suggested_preamp_db: float | None = None
    suggested_filters: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "suggested_preamp_db": self.suggested_preamp_db,
            "suggested_filters": self.suggested_filters,
        }

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("warn", "action")]


# ---------------------------------------------------------------- profile --

def advise_profile(resolved: dict, sample_rate: int | None = None) -> list[Finding]:
    out: list[Finding] = []
    eq = resolved.get("eq") or {}
    filters = [f for f in (eq.get("filters") or []) if f.get("enabled", True)]
    lim = resolved.get("limiter") or {}
    pre = resolved.get("preamp") or {}
    nyq = sample_rate / 2.0 if sample_rate else None

    boost = worst_case_boost_db(filters)

    for i, f in enumerate(filters):
        g = abs(float(f.get("gain_db", 0) or 0))
        q = float(f.get("q", 1.0) or 1.0)
        fc = float(f.get("freq", 0) or 0)
        if g > 15:
            out.append(Finding(
                "profile.extreme_boost", "warn",
                f"filter #{i+1} ({f.get('type')} @ {fc:g} Hz) boosts {g:g} dB",
                "boosts above ~15 dB often distort; prefer cutting the "
                "surrounding bands instead",
                {"action": "reduce_gain", "index": i,
                 "max_db": 12.0}))
        if q > 8 and g > 6:
            out.append(Finding(
                "profile.narrow_boost", "warn",
                f"filter #{i+1} is very narrow (Q={q:g}) with {g:g} dB boost",
                "narrow high-Q boosts can ring/howl; widen Q or lower gain",
                {"action": "widen_q", "index": i, "q": max(1.0, q / 3)}))
        if f.get("type") == "highpass" and fc < 25:
            out.append(Finding(
                "profile.useless_hpf", "info",
                f"highpass at {fc:g} Hz does almost nothing audible",
                "most content has negligible energy below 25 Hz"))
        if nyq and fc > nyq * 0.98:
            out.append(Finding(
                "profile.freq_above_nyquist", "warn",
                f"filter #{i+1} at {fc:g} Hz is at/above Nyquist for "
                f"{sample_rate} Hz audio",
                "the native core clamps it, but it will have no effect"))

    if boost > 6 and not lim.get("enabled", False):
        out.append(Finding(
            "profile.no_limiter", "action",
            f"EQ can add up to +{boost:.1f} dB but the limiter is off",
            "enable the limiter (or rely on auto-preamp) to avoid clipping",
            {"action": "enable_limiter"}))

    if pre.get("mode") == "manual":
        g = float(pre.get("gain_db", 0))
        if g > 0 and boost > 0:
            out.append(Finding(
                "profile.preamp_conflict", "warn",
                f"manual preamp +{g:g} dB on top of +{boost:.1f} dB EQ boost",
                "this will very likely clip; use preamp mode 'auto'"))

    if len(filters) > 32:
        out.append(Finding(
            "profile.filter_count", "info",
            f"{len(filters)} active filters",
            "CPU cost grows linearly; fine on desktop, watch it on weak hardware"))
    return out


# ----------------------------------------------------------------- content --

def advise_content(report: AnalysisReport) -> list[Finding]:
    out: list[Finding] = []

    if report.clipped_samples:
        out.append(Finding(
            "audio.clipped", "warn",
            f"{report.clipped_samples} clipped samples "
            f"({report.clipping_events} events) already in the source",
            "the file was recorded/rendered too hot; processing can't undo "
            "this distortion"))
    if report.inter_sample_peak_over_0dbfs:
        out.append(Finding(
            "audio.true_peak_over", "warn",
            f"true peaks reach {report.true_peak_db:+.1f} dBTP",
            "lossy encoding (MP3/AAC) will likely add distortion; keep a "
            "limiter ceiling at -1 dBTP"))
    if report.integrated_lufs is not None and report.integrated_lufs > -70:
        if report.integrated_lufs > -8:
            out.append(Finding(
                "audio.very_loud", "info",
                f"integrated loudness {report.integrated_lufs:.1f} LUFS - very dense master",
                "expect little dynamic headroom; gentle EQ cuts work better than boosts"))
        elif report.integrated_lufs < -31:
            out.append(Finding(
                "audio.very_quiet", "info",
                f"integrated loudness {report.integrated_lufs:.1f} LUFS - very quiet",
                "consider normalizing (eqforge apply --normalize -16)",
                {"action": "normalize", "target_lufs": -16.0}))
    if any(abs(d) > 0.002 for d in report.dc_offset):
        out.append(Finding(
            "audio.dc_offset", "info",
            "DC offset detected",
            "a highpass at 20-30 Hz removes it; some gear dislikes DC"))
    bands = report.band_energy_db or {}
    sub = bands.get("sub")
    if sub is not None and np.isfinite(sub) and sub > -8:
        out.append(Finding(
            "audio.low_end_buildup", "info",
            f"sub-bass (20-60 Hz) carries {sub:.1f} dB of total energy - unusually heavy",
            "a highpass around 30 Hz or a sub tilt-down usually tightens this",
            {"action": "add_filter",
             "filter": {"type": "highpass", "freq": 30, "q": 0.707}}))
    if report.crest_factor_db < 5 and report.duration_s > 5:
        out.append(Finding(
            "audio.low_crest", "info",
            f"crest factor only {report.crest_factor_db:.1f} dB",
            "heavily limited material - additional loudness processing "
            "will mostly distort"))
    if report.silent_ratio > 0.6 and report.duration_s > 2:
        out.append(Finding(
            "audio.mostly_silent", "info",
            f"{report.silent_ratio*100:.0f}% of the file is near-silent",
            "loudness numbers may be misleading"))

    res = find_resonances(report)
    for r in res[:3]:
        out.append(Finding(
            "audio.resonance", "info",
            f"narrow spectral peak at ~{r['freq']:.0f} Hz "
            f"(+{r['prominence']:.1f} dB over local average)",
            "could be a room/headphone resonance; a gentle notch can help",
            {"action": "add_filter",
             "filter": {"type": "peak", "freq": round(r["freq"], 1),
                        "gain_db": round(-min(r["prominence"] / 2, 6.0), 1),
                        "q": round(r["q"], 2), "name": "resonance guard"}}))
    return out


def find_resonances(report: AnalysisReport, min_prominence_db: float = 11.0
                    ) -> list[dict]:
    """Narrowband peaks: fine-grained spectrum minus coarse local average."""
    spec = report.spectrum or {}
    f = np.asarray(spec.get("freqs", []), dtype=np.float64)
    db = np.asarray(spec.get("db", []), dtype=np.float64)
    if len(f) < 32:
        return []
    # local baseline: running median over +/- 1/2 octave in log space
    logf = np.log2(np.maximum(f, 1e-6))
    baseline = np.empty_like(db)
    for i in range(len(db)):
        m = (logf > logf[i] - 0.75) & (logf < logf[i] + 0.75)
        baseline[i] = np.median(db[m]) if np.any(m) else db[i]
    prom = db - baseline
    peaks = []
    for i in range(2, len(db) - 2):
        if prom[i] >= min_prominence_db and prom[i] == np.max(prom[i-2:i+3]) \
                and 40 < f[i] < 16000:
            # estimate width -> Q
            lo = i
            while lo > 0 and prom[lo] > prom[i] / 2:
                lo -= 1
            hi = i
            while hi < len(prom) - 1 and prom[hi] > prom[i] / 2:
                hi += 1
            f_lo, f_hi = f[lo], f[hi]
            bw = max(f_hi - f_lo, 1.0)
            q = float(np.clip(f[i] / bw, 1.0, 30.0))
            peaks.append({"freq": float(f[i]),
                          "prominence": float(prom[i]), "q": q})
    peaks.sort(key=lambda p: -p["prominence"])
    # de-duplicate within 1/6 octave
    kept: list[dict] = []
    for p in peaks:
        if all(abs(np.log2(p["freq"] / k["freq"])) > 1 / 6 for k in kept):
            kept.append(p)
    return kept


# ---------------------------------------------------------------- combined --

def advise_combined(report: AnalysisReport | None, resolved: dict,
                    sample_rate: int | None = None) -> list[Finding]:
    """Will this profile damage this content?"""
    out: list[Finding] = []
    eq = resolved.get("eq") or {}
    filters = [f for f in (eq.get("filters") or []) if f.get("enabled", True)]
    pre = resolved.get("preamp") or {}
    lim = resolved.get("limiter") or {}
    ceiling = float(lim.get("ceiling_db", -1.0)) if lim.get("enabled", True) else 0.0

    if report is None or not np.isfinite(report.peak_db) or report.peak_db < -69:
        return out

    if pre.get("mode", "auto") == "auto":
        preamp = -worst_case_boost_db(filters)
    else:
        preamp = float(pre.get("gain_db", 0))

    # worst-case output peak (coherent EQ sum, no limiting)
    est_peak = report.peak_db + preamp + worst_case_boost_db(filters)
    if est_peak > ceiling + 0.2:
        out.append(Finding(
            "combined.clipping_risk", "action" if not lim.get("enabled", True)
            else "info",
            f"worst-case output peak ~{est_peak:+.1f} dBFS vs ceiling "
            f"{ceiling:+.1f} dB",
            ("the limiter will catch this (expect gain pumping on hot peaks)"
             if lim.get("enabled", True) else
             "enable the limiter or lower the preamp"),
            {"action": "set_preamp_db",
             "value": round(preamp - (est_peak - ceiling), 2)}))

    loud = resolved.get("loudness") or {}
    target = loud.get("target_lufs")
    if target is not None and np.isfinite(report.integrated_lufs) \
            and report.integrated_lufs > -69:
        delta = float(target) - report.integrated_lufs
        if abs(delta) > 1.0:
            out.append(Finding(
                "combined.normalize", "info",
                f"content is {report.integrated_lufs:.1f} LUFS; profile "
                f"targets {target:.1f} LUFS ({delta:+.1f} dB)",
                "apply will normalize automatically during file processing",
                {"action": "normalize", "target_lufs": float(target),
                 "gain_db": round(delta, 2)}))
    return out


def advise(report: AnalysisReport | None = None,
           resolved_profile: dict | None = None,
           sample_rate: int | None = None) -> Advice:
    advice = Advice()
    resolved_profile = resolved_profile or {}
    advice.findings.extend(advise_profile(resolved_profile, sample_rate))
    if report is not None:
        advice.findings.extend(advise_content(report))
        advice.findings.extend(
            advise_combined(report, resolved_profile, sample_rate))

    actions = [f.action for f in advice.findings if f.action]
    for a in actions:
        if a.get("action") == "set_preamp_db" and \
                advice.suggested_preamp_db is None:
            advice.suggested_preamp_db = float(a["value"])
        if a.get("action") == "add_filter" and \
                len(advice.suggested_filters) < 3:
            advice.suggested_filters.append(a["filter"])
    if not advice.findings:
        advice.findings.append(Finding(
            "all.good", "ok", "no issues found - this content and profile "
            "look healthy together"))
    return advice
