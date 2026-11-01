"""Aggregate analysis report: everything `eqforge analyze` computes."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from eqforge.analysis import loudness as L
from eqforge.analysis import spectrum as S


@dataclass
class AnalysisReport:
    sample_rate: int = 0
    channels: int = 0
    duration_s: float = 0.0
    frames: int = 0

    # levels
    peak_db: float = -np.inf
    true_peak_db: float = -np.inf
    rms_db: float = -np.inf
    crest_factor_db: float = 0.0
    dc_offset: list[float] = field(default_factory=list)

    # loudness
    integrated_lufs: float = -70.0
    loudness_range_lu: float = 0.0
    momentary_lufs_max: float = -70.0
    short_term_lufs_max: float = -70.0

    # integrity
    clipped_samples: int = 0
    clipping_events: int = 0
    silent_ratio: float = 0.0
    inter_sample_peak_over_0dbfs: bool = False

    # spectrum
    band_energy_db: dict = field(default_factory=dict)
    spectral_centroid_hz: float = 0.0
    spectral_flatness: float = 0.0
    spectrum: dict = field(default_factory=dict)   # decimated for UI/storage

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = _sanitize(asdict(self))
        for k, v in list(d.items()):
            if isinstance(v, float) and (np.isnan(v) or v in (np.inf, -np.inf)):
                d[k] = None
        d["band_energy_db"] = {
            k: (None if not np.isfinite(v) else round(v, 2))
            for k, v in self.band_energy_db.items()}
        return d

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), **kw)


def _sanitize(obj):
    """Recursively convert numpy scalars to plain Python types."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return obj


def _count_clipping(x: np.ndarray) -> tuple[int, int]:
    """(samples at/over full-scale, distinct events) for float input."""
    hot = np.abs(x) >= 0.99999
    n = int(hot.sum())
    if n == 0:
        return 0, 0
    edges = np.diff(hot.astype(np.int8).ravel())
    events = int((edges == 1).sum()) + int(hot.ravel()[0])
    return n, events


def analyze(data: np.ndarray, sample_rate: int,
            with_spectrum: bool = True) -> AnalysisReport:
    x = np.atleast_2d(np.asarray(data, dtype=np.float64))
    r = AnalysisReport()
    r.sample_rate = int(sample_rate)
    r.channels = int(x.shape[0])
    r.frames = int(x.shape[1])
    r.duration_s = r.frames / float(sample_rate) if sample_rate else 0.0

    r.peak_db = L.peak_db(x)
    r.true_peak_db = L.true_peak_db(x)
    r.rms_db = L.rms_db(x)
    r.crest_factor_db = L.crest_factor_db(x)
    r.dc_offset = [float(np.mean(x[c])) for c in range(r.channels)]

    r.integrated_lufs = L.integrated_loudness(x, sample_rate)
    r.loudness_range_lu = L.loudness_range(x, sample_rate)

    # max momentary / short-term over the timeline
    m, _ = L._block_loudness(x, sample_rate, 400.0, 100.0)
    if len(m):
        r.momentary_lufs_max = float(np.max(m[np.isfinite(m)])) if np.any(np.isfinite(m)) else -70.0
    s, _ = L._block_loudness(x, sample_rate, 3000.0, 500.0)
    if len(s):
        r.short_term_lufs_max = float(np.max(s[np.isfinite(s)])) if np.any(np.isfinite(s)) else -70.0

    r.clipped_samples, r.clipping_events = _count_clipping(x)
    r.inter_sample_peak_over_0dbfs = bool(r.true_peak_db > 0.0)
    rms_env = np.sqrt(np.mean(x ** 2, axis=0))
    r.silent_ratio = float(np.mean(rms_env < 10 ** (-60 / 20)))

    r.band_energy_db = S.band_energies_db(x, sample_rate)
    r.spectral_centroid_hz = S.spectral_centroid(x, sample_rate)
    r.spectral_flatness = S.spectral_flatness(x, sample_rate)

    if with_spectrum:
        f, psd = S.welch_spectrum(x, sample_rate)
        if len(f):
            fs, psd_s = S.smoothed_spectrum(f, psd)
            # decimate to <= 1024 points (log spacing) for storage/UI
            if len(fs) > 1024:
                idx = np.unique(np.logspace(0, np.log10(len(fs) - 1), 1024).astype(int))
                fs, psd_s = fs[idx], psd_s[idx]
            r.spectrum = {
                "freqs": [round(float(v), 2) for v in fs],
                "db": [round(float(v), 3) for v in psd_s],
            }

    if r.clipped_samples:
        r.notes.append(f"{r.clipped_samples} clipped samples "
                       f"({r.clipping_events} events)")
    if r.inter_sample_peak_over_0dbfs:
        r.notes.append("inter-sample (true) peaks exceed 0 dBFS - lossy "
                       "transcodes will likely distort")
    if r.integrated_lufs > -5:
        r.notes.append("extremely loud master (loudness-war territory)")
    if r.loudness_range_lu > 18:
        r.notes.append("very wide dynamic range - consider context-appropriate "
                       "playback gain")
    return r
