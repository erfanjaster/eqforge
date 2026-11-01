"""Curve rendering tests (SVG + ASCII)."""
from __future__ import annotations

import numpy as np

from eqforge.render.curve import render_ascii, render_svg, response_db

FILTERS = [
    {"type": "peak", "freq": 1000, "gain_db": 6, "q": 1.0, "enabled": True},
    {"type": "lowshelf", "freq": 80, "gain_db": 3, "q": 0.71, "enabled": True,
     "use_q": False},
    {"type": "highpass", "freq": 30, "q": 0.707, "enabled": True},
]


def test_response_sane():
    r = response_db(FILTERS)
    assert r.shape == (512,)
    # +6 dB peak at 1 kHz dominates
    idx1k = int(np.argmin(np.abs(np.logspace(np.log10(20), np.log10(20000),
                                             512) - 1000)))
    assert 4.5 < r[idx1k] < 8.0


def test_svg_output():
    svg = render_svg(FILTERS, preamp_db=-9.0, title="Test Curve")
    assert svg.startswith("<svg")
    assert "Test Curve" in svg
    assert "polyline" in svg
    assert svg.rstrip().endswith("</svg>")


def test_svg_escapes_title():
    svg = render_svg([], title="<bad & \"title\">")
    assert "&lt;bad" in svg


def test_ascii_output():
    txt = render_ascii(FILTERS, width=60, height=13)
    lines = txt.splitlines()
    assert len(lines) == 15  # rows + axis + freq labels
    assert "*" in txt
    assert "100" in txt  # freq axis present


def test_ascii_matches_sign_of_boost():
    up = render_ascii([{"type": "peak", "freq": 1000, "gain_db": 10,
                        "q": 1, "enabled": True}])
    down = render_ascii([{"type": "peak", "freq": 1000, "gain_db": -10,
                          "q": 1, "enabled": True}])
    def rows_above_center(t):
        lines = [l for l in t.splitlines() if "|" in l]
        mid = len(lines) // 2
        above = sum(l.split("|")[1].count("*") for l in lines[:mid])
        below = sum(l.split("|")[1].count("*") for l in lines[mid + 1:])
        return above, below
    a_up = rows_above_center(up)
    a_dn = rows_above_center(down)
    assert a_up[0] > a_up[1]
    assert a_dn[1] > a_dn[0]
