"""CLI end-to-end tests (run the actual `python -m eqforge` entry point)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cli(args: list[str], tmp_path: Path, expect_rc: int = 0,
            input_text: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "NO_COLOR": "1",
        "PYTHONPATH": str(REPO_ROOT),
    })
    r = subprocess.run([sys.executable, "-m", "eqforge"] + args,
                       capture_output=True, text=True, env=env,
                       cwd=str(tmp_path), timeout=120, input=input_text)
    assert r.returncode == expect_rc, \
        f"`eqforge {' '.join(args)}` rc={r.returncode}\n" \
        f"stdout: {r.stdout[-1500:]}\nstderr: {r.stderr[-1500:]}"
    return r


def test_cli_version(isolated_home):
    r = run_cli(["version"], isolated_home)
    assert "eqforge 1.0.0" in r.stdout


def test_cli_list_profiles(isolated_home):
    r = run_cli(["list-profiles"], isolated_home)
    assert "flat" in r.stdout and "bass-boost" in r.stdout
    r = run_cli(["list-profiles", "--json"], isolated_home)
    data = json.loads(r.stdout)
    assert any(p["id"] == "flat" for p in data)


def test_cli_show_ascii_curve(isolated_home):
    r = run_cli(["show", "bass-boost"], isolated_home)
    assert "Bass Boost" in r.stdout
    assert "*" in r.stdout  # ascii curve drawn
    r = run_cli(["show", "bass-boost", "--resolved"], isolated_home)
    assert "preamp_db" in r.stdout


def test_cli_show_svg(isolated_home, tmp_path_factory):
    tmp = isolated_home
    svg = tmp / "curve.svg"
    run_cli(["show", "music", "--svg", str(svg)], tmp)
    content = svg.read_text()
    assert content.startswith("<svg")
    assert "polyline" in content


def test_cli_validate_and_new(isolated_home):
    run_cli(["new", "my.profile", "--from", "flat"], isolated_home)
    run_cli(["validate", "my.profile"], isolated_home)
    r = run_cli(["diff", "flat", "my.profile"], isolated_home)
    assert "DSP-identical" in r.stdout or "identical" in r.stdout.lower()


def test_cli_synth_analyze_apply(isolated_home):
    tmp = isolated_home
    run_cli(["synth", "pink", str(tmp / "p.wav"), "--duration", "2"], tmp)
    r = run_cli(["analyze", str(tmp / "p.wav"), "--json"], tmp)
    rep = json.loads(r.stdout)
    assert rep["sample_rate"] == 48000
    assert rep["integrated_lufs"] < 0
    r = run_cli(["apply", "bass-boost", str(tmp / "p.wav"),
                 str(tmp / "out.wav")], tmp)
    assert "✓" in r.stdout or "out.wav" in r.stdout
    r = run_cli(["analyze", str(tmp / "out.wav"), "--json"], tmp)
    rep2 = json.loads(r.stdout)
    # bass boosted: sub-band energy share went up relative to source
    assert rep2["band_energy_db"]["sub"] > rep["band_energy_db"]["sub"]


def test_cli_apply_normalize(isolated_home):
    tmp = isolated_home
    run_cli(["synth", "sine", str(tmp / "s.wav"), "--duration", "3",
             "--amplitude", "0.05"], tmp)
    r = run_cli(["apply", "flat", str(tmp / "s.wav"), str(tmp / "n.wav"),
                 "--normalize", "-20", "--json"], tmp)
    info = json.loads(r.stdout)
    assert info["normalize_gain_db"] > 6  # quiet input got lifted
    r = run_cli(["analyze", str(tmp / "n.wav"), "--json"], tmp)
    rep = json.loads(r.stdout)
    assert rep["integrated_lufs"] == pytest.approx(-20.0, abs=0.6)


def test_cli_apply_dry_run_advises(isolated_home):
    tmp = isolated_home
    run_cli(["synth", "square", str(tmp / "sq.wav"), "--duration", "2"], tmp)
    r = run_cli(["apply", "bass-boost", str(tmp / "sq.wav"),
                 str(tmp / "x.wav"), "--dry-run"], tmp)
    assert not (tmp / "x.wav").exists()
    assert "[" in r.stdout  # findings printed


def test_cli_import_autoeq(isolated_home):
    tmp = isolated_home
    src = tmp / "hd.txt"
    src.write_text(
        "Preamp: -5.5 dB\n"
        "Filter 1: ON PK Fc 105 Hz Gain 5.1 dB Q 1.1\n"
        "Filter 2: ON LSC Fc 30 Hz Gain 2.0 dB Q 0.7\n")
    run_cli(["import", str(src), "--id", "hd-test"], tmp)
    r = run_cli(["show", "hd-test", "--resolved"], tmp)
    assert '"preamp_db": -5.5' in r.stdout


def test_cli_export(isolated_home):
    tmp = isolated_home
    run_cli(["export", "gaming", str(tmp / "gaming.json")], tmp)
    d = json.loads((tmp / "gaming.json").read_text())
    assert d["id"] == "gaming"
    assert d["eq"]["filters"]


def test_cli_batch(isolated_home):
    tmp = isolated_home
    src = tmp / "music"
    src.mkdir()
    for i in range(3):
        run_cli(["synth", "sine", str(src / f"t{i}.wav"), "--duration", "1",
                 "--freq", str(400 + i * 100)], tmp)
    r = run_cli(["batch", "flat", str(src), "-o", str(tmp / "done")], tmp)
    outs = list((tmp / "done").glob("*.flac"))
    assert len(outs) == 3


def test_cli_suggest(isolated_home):
    tmp = isolated_home
    run_cli(["synth", "pink", str(tmp / "p.wav"), "--duration", "3"], tmp)
    r = run_cli(["suggest", str(tmp / "p.wav"), "--json"], tmp)
    data = json.loads(r.stdout)
    assert "deviation" in data
    r = run_cli(["suggest", str(tmp / "p.wav"), "--save-as", "auto-match"],
                tmp)
    run_cli(["validate", "auto-match"], tmp)


def test_cli_doctor(isolated_home):
    r = run_cli(["doctor"], isolated_home)
    assert "native core" in r.stdout
    assert "EQForge is healthy" in r.stdout or "checks failed" in r.stdout


def test_cli_status_without_daemon(isolated_home):
    r = run_cli(["status"], isolated_home)
    assert "not running" in r.stdout


def test_cli_missing_file_error(isolated_home):
    r = run_cli(["analyze", "/no/such/file.wav"], isolated_home,
                expect_rc=3)
    assert "not found" in r.stderr.lower() or "error" in r.stderr.lower()


def test_cli_bad_profile_error(isolated_home):
    run_cli(["synth", "sine", str(isolated_home / "a.wav"), "--duration",
             "1"], isolated_home)
    r = run_cli(["apply", "nonexistent-profile",
                 str(isolated_home / "a.wav"),
                 str(isolated_home / "b.wav")], isolated_home, expect_rc=2)
    assert "no such profile" in r.stderr.lower()


def test_cli_devices_without_pipewire_graceful(isolated_home, monkeypatch):
    # PATH stripped of pipewire tools -> must exit gracefully, not traceback
    env_backup = os.environ.get("PATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "eqforge", "devices"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "HOME": str(isolated_home),
             "XDG_RUNTIME_DIR": str(isolated_home / "run"),
             "PATH": "/nonexistent-bin"},
        cwd=str(isolated_home))
    assert r.returncode in (0, 1)
    assert "Traceback" not in r.stderr
