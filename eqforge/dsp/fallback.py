"""Pure-numpy fallback implementation of the EQForge processing chain.

Used when libeqforge is unavailable (no compiler, exotic platform). Same
resolved-dsp-config JSON semantics; equivalent transfer functions with
block-based state carryover. Not sample-exact against the native core
(different filter recursion form), but validated to agree closely in the
frequency domain and on offline renders.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from eqforge.dsp.coeffs import design_biquad


class FallbackChain:
    """Chain: preamp -> EQ -> crossfeed -> convolver -> compressor -> limiter."""

    def __init__(self, channels: int, sample_rate: int):
        self.channels = int(channels)
        self.sample_rate = int(sample_rate)
        self._configure(None)

    # ------------------------------------------------------------------
    def _configure(self, cfg: dict | None) -> None:
        cfg = cfg or {}
        self.preamp = 10.0 ** (cfg.get("preamp_db", 0.0) / 20.0)
        self.postgain = 10.0 ** (cfg.get("post_gain_db", 0.0) / 20.0)

        # EQ: per-channel cascaded biquads with lfilter state
        eq = cfg.get("eq", {})
        filters = eq.get("filters", [])
        self._sos_per_ch: list[np.ndarray] = []
        for c in range(self.channels):
            sos_list = []
            for f in filters:
                if not f.get("enabled", True):
                    continue
                ch = f.get("channel", "all")
                if ch != "all" and int(ch) != c:
                    continue
                b, a = design_biquad(f.get("type", "peak"), f.get("freq", 1000),
                                     f.get("gain_db", 0), f.get("q", 1.0),
                                     self.sample_rate, use_q=f.get("use_q"))
                # sos row: [b0 b1 b2 1 a1 a2]
                sos_list.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            self._sos_per_ch.append(np.array(sos_list, dtype=np.float64)
                                    if sos_list else np.zeros((0, 6)))
        self._zi = [np.zeros((len(s), 2)) for s in self._sos_per_ch]

        # crossfeed
        xf = cfg.get("crossfeed", {}) or {}
        self.xf_on = bool(xf.get("enabled")) and self.channels == 2
        self.xf_feed = 10.0 ** (xf.get("level_db", -6.0) / 20.0)
        fc = xf.get("fc_hz", 700.0)
        b, a = design_biquad("lowpass", fc, 0.0, 0.7071, self.sample_rate)
        self._xf_sos = np.array([[b[0], b[1], b[2], 1.0, a[1], a[2]]] * 2)
        self._xf_zi = [np.zeros((2, 2)), np.zeros((2, 2))]

        # convolver
        cv = cfg.get("convolver", {}) or {}
        self.conv_on = bool(cv.get("enabled")) and "ir" in cv
        self.conv_gain = 10.0 ** (cv.get("gain_db", 0.0) / 20.0)
        if self.conv_on:
            ir = np.asarray(cv["ir"], dtype=np.float64)
            if ir.ndim == 1:
                ir = np.tile(ir, (self.channels, 1))
            self._irs = ir[: self.channels]
            self._conv_hist = [np.zeros(len(self._irs[c]))
                               for c in range(len(self._irs))]
        else:
            self._irs = None

        # compressor (simple feed-forward; dB-domain smoothing)
        cp = cfg.get("compressor", {}) or {}
        self.comp_on = bool(cp.get("enabled"))
        self.comp_threshold = cp.get("threshold_db", -18.0)
        self.comp_ratio = max(cp.get("ratio", 3.0), 1.0)
        self.comp_knee = cp.get("knee_db", 6.0)
        self.comp_atk = np.exp(-1.0 / max(cp.get("attack_ms", 10.0) * 1e-3 *
                                          self.sample_rate, 1.0))
        self.comp_rel = np.exp(-1.0 / max(cp.get("release_ms", 120.0) * 1e-3 *
                                          self.sample_rate, 1.0))
        self.comp_makeup = 10.0 ** (cp.get("makeup_db", 0.0) / 20.0)
        self._comp_gain_db = 0.0

        # limiter (look-ahead, like the native one but block-oriented)
        lm = cfg.get("limiter", {}) or {}
        self.lim_on = bool(lm.get("enabled"))
        self.lim_ceiling = 10.0 ** (lm.get("ceiling_db", -1.0) / 20.0)
        self.lim_knee = 10.0 ** ((lm.get("ceiling_db", -1.0) -
                                  lm.get("knee_db", 3.0)) / 20.0)
        self.lim_atk = np.exp(-1.0 / max(lm.get("attack_ms", 5.0) * 1e-3 *
                                         self.sample_rate, 1.0))
        self.lim_rel = np.exp(-1.0 / max(lm.get("release_ms", 60.0) * 1e-3 *
                                         self.sample_rate, 1.0))
        self.lim_lookahead = int(lm.get("lookahead_ms", 1.5) * 1e-3 *
                                 self.sample_rate)
        self._lim_delay = np.zeros((self.channels, max(self.lim_lookahead + 1, 1)))
        self._lim_gain = 1.0
        self.latency_frames = self.lim_lookahead if self.lim_on else 0

        # meters
        self.momentary_lufs = -70.0
        self.out_peak = 0.0
        self.out_true_peak = 0.0
        self.in_clipped = False
        self.out_clipped = False
        self._kfilter = self._design_k_weight()

    def _design_k_weight(self):
        sos1 = []
        G, Q, fc = 3.999843853973347, 0.7071752369554196, 1681.974450955533
        K = np.tan(np.pi * fc / self.sample_rate)
        Vh = 10 ** (G / 20.0)
        Vb = Vh ** 0.4996865530411197
        a0 = 1 + K / Q + K * K
        sos1.append([(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0,
                     (Vh - Vb * K / Q + K * K) / a0, 1.0,
                     2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
        Q, fc = 0.5003270373238773, 38.13547087602444
        K = np.tan(np.pi * fc / self.sample_rate)
        a0 = 1 + K / Q + K * K
        sos1.append([1 / a0, -2 / a0, 1 / a0, 1.0,
                     2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
        return np.array(sos1)

    def configure_json(self, cfg: dict) -> None:
        self._configure(cfg)

    # ------------------------------------------------------------------
    def process(self, data: np.ndarray) -> np.ndarray:
        """data: float32/64 [channels, frames] -> processed copy."""
        x = np.array(data, dtype=np.float64, copy=True)
        n_ch, frames = x.shape
        self.in_clipped |= bool(np.max(np.abs(x)) >= 1.0)
        x *= self.preamp

        # EQ
        for c in range(min(n_ch, self.channels)):
            sos = self._sos_per_ch[c]
            if sos.shape[0] > 0:
                x[c], self._zi[c] = signal.sosfilt(sos, x[c], zi=self._zi[c])

        # crossfeed (stereo)
        if self.xf_on and n_ch >= 2:
            l_snap, r_snap = x[0].copy(), x[1].copy()
            for i, src in enumerate((l_snap, r_snap)):
                dst = 1 - i
                y, self._xf_zi[dst] = signal.sosfilt(
                    self._xf_sos, src, zi=self._xf_zi[dst])
                x[dst] += self.xf_feed * y

        # convolver
        if self.conv_on and self._irs is not None:
            for c in range(min(n_ch, len(self._irs))):
                ir = self._irs[c]
                hist = self._conv_hist[c]
                ext = np.concatenate([hist, x[c]])
                y = np.convolve(ext, ir)[: len(ext)]
                x[c] = y[len(hist):] * self.conv_gain
                self._conv_hist[c] = ext[-len(hist):]

        # compressor (vectorized envelope approximation per block)
        if self.comp_on:
            det = np.max(np.abs(x), axis=0)
            det_db = 20.0 * np.log10(np.maximum(det, 1e-9))
            T, R, W = self.comp_threshold, self.comp_ratio, self.comp_knee
            over = det_db - T
            gr = np.where(
                over >= W / 2, (1 - 1 / R) * over,
                np.where(over > -W / 2,
                         (1 - 1 / R) * (over + W / 2) ** 2 / (2 * W), 0.0))
            target = -gr
            g = self._comp_gain_db
            out = np.empty_like(target)
            coeff = np.where(target < g, self.comp_atk, self.comp_rel)
            for i in range(len(target)):
                g = coeff[i] * g + (1 - coeff[i]) * target[i]
                out[i] = g
            self._comp_gain_db = g
            x *= (10.0 ** (out / 20.0) * self.comp_makeup)

        # limiter with look-ahead
        if self.lim_on:
            la = self.lim_lookahead
            delayed = np.empty_like(x)
            for c in range(n_ch):
                delayed[c] = np.concatenate([self._lim_delay[c], x[c]])
            peak_env = np.max(np.abs(x), axis=0)  # detector on look-ahead side
            gains = np.empty(frames)
            g = self._lim_gain
            for i in range(frames):
                det = max(peak_env[i], 1e-9)
                if det > self.lim_ceiling:
                    want = self.lim_ceiling / det
                elif det > self.lim_knee:
                    t = (det - self.lim_knee) / (self.lim_ceiling -
                                                 self.lim_knee + 1e-9)
                    want = 1 - t * t * (1 - self.lim_knee /
                                        (self.lim_ceiling + 1e-9)) * 0.5
                else:
                    want = 1.0
                coeff = self.lim_atk if want < g else self.lim_rel
                g = coeff * g + (1 - coeff) * want
                gains[i] = g
            self._lim_gain = g
            if la > 0:
                x = delayed[:, :frames] * gains
                self._lim_delay = delayed[:, frames:frames + la].copy() \
                    if delayed.shape[1] >= frames + la else \
                    np.zeros((n_ch, la))
            else:
                x = x * gains
            np.clip(x, -self.lim_ceiling, self.lim_ceiling, out=x)

        x *= self.postgain

        # meters
        self.out_peak = float(np.max(np.abs(x))) if x.size else 0.0
        self.out_clipped |= self.out_peak >= 1.0
        kx = signal.sosfilt(self._kfilter, x)
        weights = np.ones(n_ch)
        if n_ch > 2:
            weights[2:] = 1.41
        ms = float(np.mean((kx ** 2).T @ weights))
        if ms > 1e-12:
            self.momentary_lufs = -0.691 + 10.0 * np.log10(ms)
        return x

    def snapshot(self) -> dict:
        return {
            "momentary_lufs": self.momentary_lufs,
            "out_peak": self.out_peak,
            "latency_frames": self.latency_frames,
            "in_clipped": self.in_clipped,
            "out_clipped": self.out_clipped,
        }
