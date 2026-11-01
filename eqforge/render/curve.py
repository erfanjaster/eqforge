"""EQ magnitude-response curve rendering.

Pure-stdlib SVG (log-frequency 20 Hz..20 kHz, dB gain axis) and an ASCII
renderer for terminal output. Both consume the same frequency response
computed from the profile's filters via eqforge.dsp.coeffs (or the native
designer when available).
"""
from __future__ import annotations

import math

import numpy as np

from eqforge.dsp.coeffs import filter_bank_response

FREQS = np.logspace(math.log10(20), math.log10(20000), 512)


def response_db(filters: list[dict], sample_rate: int = 48000) -> np.ndarray:
    lin = filter_bank_response(filters, FREQS, sample_rate)
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(np.maximum(lin, 1e-9))


def render_svg(filters: list[dict], preamp_db: float = 0.0,
               width: int = 900, height: int = 380,
               gain_range_db: float = 18.0,
               spectrum: tuple[np.ndarray, np.ndarray] | None = None,
               title: str = "") -> str:
    """Return an SVG document of the EQ curve (dark theme, grid, labels)."""
    resp = response_db(filters)
    total = resp + preamp_db

    pad_l, pad_r, pad_t, pad_b = 56, 16, 34, 34
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def X(f: float) -> float:
        return pad_l + w * (math.log10(max(f, 20)) - math.log10(20)) / \
            (math.log10(20000) - math.log10(20))

    ymin, ymax = -gain_range_db, gain_range_db
    if spectrum:
        pass
    peak = float(np.max(np.abs(total))) if len(total) else 0
    ymax = max(gain_range_db, min(30.0, math.ceil(peak / 6) * 6 + 3))
    ymin = -ymax

    def Y(db: float) -> float:
        return pad_t + h * (ymax - db) / (ymax - ymin)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="Inter, Cantarell, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#14161c"/>',
    ]
    if title:
        parts.append(f'<text x="{pad_l}" y="20" fill="#e8eaf2" font-size="14" '
                     f'font-weight="600">{_esc(title)}</text>')
    # grid: decades + 1/3 decades
    for dec in range(1, 5):
        for mult in (1, 2, 5):
            f = mult * 10 ** dec
            if f > 20000 or f < 20:
                continue
            x = X(f)
            parts.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" '
                         f'y2="{pad_t + h}" stroke="#262a35" stroke-width="1"/>')
            label = f"{f//1000}k" if f >= 1000 else str(f)
            parts.append(f'<text x="{x:.1f}" y="{height - 12}" fill="#8a90a2" '
                         f'font-size="10" text-anchor="middle">{label}</text>')
    step = 6 if (ymax - ymin) <= 36 else 12
    g = int(math.ceil(ymin / step) * step)
    while g <= ymax:
        y = Y(g)
        color = "#3a3f4d" if g == 0 else "#22252f"
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + w}" '
                     f'y2="{y:.1f}" stroke="{color}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" fill="#8a90a2" '
                     f'font-size="10" text-anchor="end">{g:+d}</text>')
        g += step

    # optional measured spectrum backdrop
    if spectrum:
        sf, sdb = spectrum
        pts = []
        ref = float(np.median(sdb[(sf >= 250) & (sf <= 8000)])) if len(sf) else 0
        for f, v in zip(sf, sdb):
            if f < 20 or f > 20000:
                continue
            v_rel = v - ref
            if v_rel > ymax or v_rel < ymin:
                continue
            pts.append(f"{X(f):.1f},{Y(v_rel):.1f}")
        if pts:
            parts.append('<polyline points="' + " ".join(pts) +
                         '" fill="none" stroke="#4a5165" stroke-width="1.5" '
                         'opacity="0.8"/>')

    # per-filter ghosts
    for fdef in filters:
        if not fdef.get("enabled", True):
            continue
        r = 20 * np.log10(np.maximum(filter_bank_response(
            [fdef], FREQS, 48000), 1e-9))
        pts = " ".join(f"{X(f):.1f},{Y(min(max(v, ymin), ymax)):.1f}"
                       for f, v in zip(FREQS, r))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#4d6ea8" '
                     'stroke-width="1" opacity="0.45"/>')
        fx = X(min(max(float(fdef.get("freq", 1000)), 20), 20000))
        parts.append(f'<circle cx="{fx:.1f}" cy="{Y(0):.1f}" r="3" '
                     'fill="#7aa2f7"/>')

    # total curve
    pts = " ".join(f"{X(f):.1f},{Y(min(max(v, ymin), ymax)):.1f}"
                   for f, v in zip(FREQS, total))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#9ece6a" '
                 'stroke-width="2.5"/>')
    parts.append(f'<text x="{width - pad_r}" y="20" fill="#8a90a2" '
                 f'font-size="11" text-anchor="end">preamp {preamp_db:+.1f} dB'
                 f' · {len(filters)} filters</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_ascii(filters: list[dict], preamp_db: float = 0.0,
                 width: int = 76, height: int = 17,
                 gain_range_db: float = 15.0) -> str:
    """Terminal rendition of the curve - works over SSH, in pipes, anywhere."""
    resp = response_db(filters) + preamp_db
    idx = np.linspace(0, len(FREQS) - 1, width).astype(int)
    vals = resp[idx]
    peak = float(np.max(np.abs(vals))) if len(vals) else 0
    ymax = max(gain_range_db, peak + 1)

    rows = [[" "] * width for _ in range(height)]
    zero_row = (height - 1) // 2
    for c in range(height):
        pass
    for x, v in enumerate(vals):
        v = max(min(v, ymax), -ymax)
        y = int(round(zero_row - v / ymax * (height // 2)))
        rows[y][x] = "*"
    for x in range(width):
        if rows[zero_row][x] == " ":
            rows[zero_row][x] = "-"

    out = []
    for r, row in enumerate(rows):
        db = ymax - r * (2 * ymax) / (height - 1)
        label = f"{db:+5.1f}" if abs(db) < 100 else "     "
        out.append(f"{label} |{''.join(row)}")
    axis = "       +" + "-" * width
    freqs = "        20   50   100   200   500   1k    2k    5k    10k   20k"
    out.append(axis)
    out.append(freqs)
    return "\n".join(out)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
