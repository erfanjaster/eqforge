"""Pure-numpy fallback implementation of the EQForge processing chain.

Used when libeqforge is unavailable (no compiler, exotic platform). It consumes
the *same* resolved dsp-config document and reproduces the *same* algorithms as
``core/src/*.c`` so that the two backends are interchangeable:

  input gain -> EQ -> crossfeed -> convolver -> compressor -> limiter -> out gain

Stage-by-stage correspondence with the native core:

| stage      | native                          | here                              |
|------------|---------------------------------|-----------------------------------|
| biquad     | RBJ design, DF2T, float         | RBJ design (float64), sosfilt     |
| crossfeed  | Bauer law, 1st-order LP, 0.5 sum| same                              |
| convolver  | direct-form FIR                 | direct-form FIR (np.convolve)     |
| compressor | per-sample peak/RMS detector,   | per-sample detector, identical    |
|            | dB-domain one-pole smoothing    | dB-domain one-pole smoothing      |
| limiter    | 4x true-peak detector, sliding  | 4x true-peak detector, sliding    |
|            | window min, instant attack,     | window min, instant attack,       |
|            | release smoothing, look-ahead   | release smoothing, look-ahead     |

Arithmetic is float64 here vs float32 in the core, so the backends agree to
float32 precision rather than bit-exactly; tests/test_parity.py measures the
difference instead of assuming it.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from eqforge.dsp.coeffs import band_shape, design_biquad

# 4x polyphase true-peak interpolation kernel, identical to dynamics.c
_TP_PHASES: np.ndarray | None = None


def _tp_phases() -> np.ndarray:
    global _TP_PHASES
    if _TP_PHASES is None:
        taps = 32
        n = np.arange(taps)
        x = n - (taps - 1) / 2.0
        fc = 0.125
        with np.errstate(divide="ignore", invalid="ignore"):
            s = np.where(np.abs(x) < 1e-9, 2.0 * fc,
                         np.sin(2 * np.pi * fc * x) /
                         (np.pi * np.where(x == 0.0, 1.0, x)))
        w = 0.5 - 0.5 * np.cos(2 * np.pi * n / (taps - 1))
        h = 4.0 * s * w
        phases = np.stack([h[4 * np.arange(8) + p] for p in range(4)])
        _TP_PHASES = np.ascontiguousarray(phases, dtype=np.float64)
    return _TP_PHASES


def true_peak_envelope(x: np.ndarray) -> np.ndarray:
    """Per-sample 4x-interpolated peak estimate of a 1-D signal.

    *x* is the signal; the result at index n is max(|x[n]|, |interpolated
    points between x[n] and x[n-1]|), which is what the native detector uses.
    """
    phases = _tp_phases()
    n = len(x)
    env = np.abs(x)
    if n == 0:
        return env
    # history aligned so that hist[:, n] == x[n-k] for k = 0..7
    hist = np.concatenate([np.zeros(7), x])
    for p in range(1, 4):
        acc = np.zeros(n)
        for k in range(8):
            acc += phases[p, k] * hist[7 - k: 7 - k + n]
        np.maximum(env, np.abs(acc), out=env)
    return env


def _want_gain_db(det_db: np.ndarray, ceiling_db: float,
                  knee_db: float) -> np.ndarray:
    """Limiter target gain in dB; mirrors limiter_want_gain_db() in C."""
    over = det_db - ceiling_db
    if not ceiling_db < 0.0:
        return np.zeros_like(over)
    out = np.zeros_like(over)
    linear = over >= 0.0
    out[linear] = -over[linear]
    if knee_db > 0.0:
        knee = (over > -knee_db) & (over < 0.0)
        if np.any(knee):
            t = (over[knee] + knee_db) / knee_db
            out[knee] = -(over[knee] * t * t)
    return out


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

        # ---- EQ: per-channel cascaded biquads with sos state ----
        filters = (cfg.get("eq") or {}).get("filters") or []
        self._sos_per_ch: list[np.ndarray] = []
        for c in range(self.channels):
            sos_list = []
            for f in filters:
                if not f.get("enabled", True):
                    continue
                ch = f.get("channel", "all")
                if ch != "all" and int(ch) != c:
                    continue
                shape, use_q = band_shape(f)
                b, a = design_biquad(f.get("type", "peak"), f.get("freq", 1000),
                                     f.get("gain_db", 0), shape,
                                     self.sample_rate, use_q=use_q)
                sos_list.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            self._sos_per_ch.append(
                np.array(sos_list, dtype=np.float64)
                if sos_list else np.zeros((0, 6)))
        self._zi = [np.zeros((len(s), 2)) for s in self._sos_per_ch]

        # ---- crossfeed: Bauer's law, one first-order section per direction ----
        #     L' = 0.5*(L + feed*H(R)),  R' = 0.5*(R + feed*H(L))
        # The 0.5 keeps a centred source at a constant level, and H is a single
        # first-order low-pass so the summed response stays monotone between
        # its two asymptotes. This must match eqf_crossfeed_configure(); see
        # the comment there for what the previous two-section, unnormalised
        # version measured.
        xf = cfg.get("crossfeed") or {}
        self.xf_on = bool(xf.get("enabled")) and self.channels == 2
        self.xf_feed = 10.0 ** (xf.get("level_db", -6.0) / 20.0)
        fc = float(xf.get("fc_hz", 700.0))
        k = np.tan(np.pi * fc / self.sample_rate)
        norm = 1.0 / (1.0 + k)
        self._xf_sos = np.array([[k * norm, k * norm, 0.0, 1.0,
                                  (k - 1.0) * norm, 0.0]])
        self._xf_zi = [np.zeros((1, 2)), np.zeros((1, 2))]

        # ---- convolver ----
        cv = cfg.get("convolver") or {}
        self.conv_on = bool(cv.get("enabled")) and "ir" in cv
        self.conv_gain = 10.0 ** (cv.get("gain_db", 0.0) / 20.0)
        if self.conv_on:
            ir = np.asarray(cv["ir"], dtype=np.float64)
            if ir.ndim == 1:
                ir = np.tile(ir, (self.channels, 1))
            self._irs = ir[: self.channels]
            self._conv_hist = [np.zeros(max(len(self._irs[c]) - 1, 0))
                               for c in range(len(self._irs))]
        else:
            self._irs = None
            self._conv_hist = []

        # ---- compressor (per-sample detector, dB-domain smoothing) ----
        cp = cfg.get("compressor") or {}
        self.comp_on = bool(cp.get("enabled"))
        self.comp_threshold = float(cp.get("threshold_db", -18.0))
        self.comp_ratio = max(float(cp.get("ratio", 3.0)), 1.0)
        self.comp_knee = max(float(cp.get("knee_db", 6.0)), 0.0)
        self.comp_atk = self._coeff(cp.get("attack_ms", 10.0))
        self.comp_rel = self._coeff(cp.get("release_ms", 120.0))
        self.comp_rms = bool(cp.get("rms_detect", False))
        self.comp_rms_coeff = self._coeff(cp.get("rms_window_ms", 20.0))
        self.comp_makeup = 10.0 ** (cp.get("makeup_db", 0.0) / 20.0)
        self._comp_gain_db = 0.0
        self._comp_env = np.zeros(self.channels)

        # ---- limiter (identical algorithm to dynamics.c) ----
        lm = cfg.get("limiter") or {}
        self.lim_on = bool(lm.get("enabled"))
        self.lim_ceiling_db = float(lm.get("ceiling_db", -1.0))
        self.lim_knee_db = max(float(lm.get("knee_db", 3.0)), 0.0)
        self.lim_ceiling = 10.0 ** (self.lim_ceiling_db / 20.0)
        self.lim_atk = self._coeff(lm.get("attack_ms", 5.0))
        self.lim_rel = self._coeff(lm.get("release_ms", 60.0))
        la = int(round(float(lm.get("lookahead_ms", 1.5)) * 1e-3 *
                       self.sample_rate))
        self.lim_lookahead = max(la, 0)
        # instant descent whenever look-ahead is in use (see dynamics.c)
        self.lim_eff_atk = 0.0 if self.lim_lookahead > 0 else self.lim_atk
        self._lim_audio = np.zeros((self.channels, self.lim_lookahead))
        self._lim_want = np.zeros(self.lim_lookahead)
        self._lim_gain_db = 0.0
        # True-peak-detector state carried across blocks (8 samples/channel).
        self._lim_hist = np.zeros((self.channels, 7))

        self.latency_frames = self.lim_lookahead if self.lim_on else 0

        # ---- meters ----
        self.momentary_lufs = -70.0
        self.out_peak = 0.0
        self.out_true_peak = 0.0
        self.in_clipped = False
        self.out_clipped = False
        self._kfilter = self._design_k_weight()
        self._kzi = np.zeros((self._kfilter.shape[0], self.channels, 2))

    def _coeff(self, ms) -> float:
        """One-pole coefficient, identical to coeff_for() in dynamics.c."""
        ms = float(ms)
        if ms <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (ms * 1e-3 * self.sample_rate)))

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
    def _limit(self, x: np.ndarray) -> np.ndarray:
        """Look-ahead limiter with a sliding-window minimum (mirrors C)."""
        la = self.lim_lookahead
        frames = x.shape[1]
        # 1. per-sample true-peak envelope (max across channels), carried over
        #    block boundaries through the 7-sample detector history.
        env = np.abs(x).max(axis=0)
        for c in range(self.channels):
            hist = np.concatenate([self._lim_hist[c], x[c]])
            for p in (1, 2, 3):
                acc = np.zeros(frames)
                for k in range(8):
                    acc += _tp_phases()[p, k] * hist[7 - k:7 - k + frames]
                env = np.maximum(env, np.abs(acc))
            # keep the newest 7 input samples for the next block
            self._lim_hist[c] = hist[-7:]
        det_db = 20.0 * np.log10(np.maximum(env, 1e-9))
        want = _want_gain_db(det_db, self.lim_ceiling_db, self.lim_knee_db)

        # 2. sliding-window minimum over [i, i+la]
        w_all = np.concatenate([self._lim_want, want])
        if la > 0 and len(w_all) >= la + 1:
            from numpy.lib.stride_tricks import sliding_window_view
            min_want = sliding_window_view(w_all, la + 1).min(axis=1)
        else:
            min_want = w_all[:frames] if frames <= len(w_all) else want
        min_want = min_want[:frames]

        # 3. gain: instant descent, one-pole release (sequential in dB)
        g = self._lim_gain_db
        atk, rel = self.lim_eff_atk, self.lim_rel
        gains = np.empty(frames)
        for i in range(frames):
            target = float(min_want[i])
            coeff = atk if target < g else rel
            g = coeff * g + (1.0 - coeff) * target
            gains[i] = g
        self._lim_gain_db = g

        # 4. apply to the delayed signal
        a_all = np.concatenate([self._lim_audio, x], axis=1)
        out = a_all[:, :frames] * (10.0 ** (gains / 20.0))
        self._lim_audio = a_all[:, frames:frames + la].copy() if la else \
            np.zeros((self.channels, 0))
        self._lim_want = w_all[frames:frames + la].copy() if la else \
            np.zeros(0)
        np.clip(out, -self.lim_ceiling, self.lim_ceiling, out=out)
        return out

    def process(self, data: np.ndarray) -> np.ndarray:
        """data: float32/64 [channels, frames] -> processed copy."""
        x = np.array(data, dtype=np.float64, copy=True)
        n_ch, frames = x.shape
        if frames == 0:
            return x
        self.in_clipped |= bool(np.max(np.abs(x)) >= 1.0)
        x *= self.preamp

        # EQ
        for c in range(min(n_ch, self.channels)):
            sos = self._sos_per_ch[c]
            if sos.shape[0] > 0:
                x[c], self._zi[c] = signal.sosfilt(sos, x[c], zi=self._zi[c])

        # crossfeed (stereo): snapshot both channels first, no intra-block
        # feedback - same rule as eqf_crossfeed_process().
        if self.xf_on and n_ch >= 2:
            l_snap, r_snap = x[0].copy(), x[1].copy()
            y, self._xf_zi[0] = signal.sosfilt(self._xf_sos, r_snap,
                                               zi=self._xf_zi[0])
            x[0] = 0.5 * (l_snap + self.xf_feed * y)
            y, self._xf_zi[1] = signal.sosfilt(self._xf_sos, l_snap,
                                               zi=self._xf_zi[1])
            x[1] = 0.5 * (r_snap + self.xf_feed * y)

        # convolver (direct form, zero latency)
        if self.conv_on and self._irs is not None:
            for c in range(min(n_ch, len(self._irs))):
                ir = self._irs[c]
                hist = self._conv_hist[c]
                ext = np.concatenate([hist, x[c]])
                full = np.convolve(ext, ir)[: len(ext)]
                x[c] = full[len(hist):] * self.conv_gain
                self._conv_hist[c] = ext[-len(hist):] if len(hist) else \
                    np.zeros(0)

        # compressor: per-sample detector + dB-domain smoothing (mirrors C)
        if self.comp_on:
            atk, rel = self.comp_atk, self.comp_rel
            T, R, W = self.comp_threshold, self.comp_ratio, self.comp_knee
            g = self._comp_gain_db
            gains = np.empty(frames)
            env = self._comp_env
            for i in range(frames):
                if self.comp_rms:
                    env = self.comp_rms_coeff * env + \
                        (1.0 - self.comp_rms_coeff) * (x[:, i] ** 2)
                    det = float(np.sqrt(np.max(env)))
                else:
                    det = float(np.max(np.abs(x[:, i])))
                det = max(det, 1e-9)
                det_db = 20.0 * np.log10(det)
                over = det_db - T
                if W > 0.0 and -W / 2.0 < over < W / 2.0:
                    gr = (1.0 - 1.0 / R) * (over + W / 2.0) ** 2 / (2.0 * W)
                elif over >= W / 2.0:
                    gr = (1.0 - 1.0 / R) * over
                else:
                    gr = 0.0
                target = -gr
                coeff = atk if target < g else rel
                g = coeff * g + (1.0 - coeff) * target
                gains[i] = g
            self._comp_env = env
            self._comp_gain_db = g
            x *= (10.0 ** (gains / 20.0) * self.comp_makeup)[None, :]

        # limiter
        if self.lim_on:
            x = self._limit(x)

        x *= self.postgain

        # meters
        self.out_peak = float(np.max(np.abs(x))) if x.size else 0.0
        self.out_clipped |= self.out_peak >= 1.0
        kx, self._kzi = signal.sosfilt(self._kfilter, x, axis=-1, zi=self._kzi)
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
