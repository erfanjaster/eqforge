"""The one-script installer/updater (`scripts/eqforge-update.sh`).

The script is the only thing a new user is handed, so its contract is narrow
and worth testing without a network, a build, or a real install:

  * `--self-test` passes (version comparison, state-file JSON roundtrip);
  * `--dry-run` plans a full install from a local checkout and writes nothing;
  * `--check` on an empty home says "not installed" and exits 0;
  * unknown options and missing values fail loudly instead of doing something
    surprising.

A real end-to-end install (build + venv + pip) is exercised separately, by
running the script by hand; it needs a compiler, network for wheels, and a
minute, none of which belong in the unit suite.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "eqforge-update.sh"


@pytest.fixture(scope="module", autouse=True)
def _script_exists():
    assert SCRIPT.is_file(), "the installer must ship with the repo"
    assert os.access(SCRIPT, os.X_OK), "the installer must be executable"


def run_script(*args: str, home: Path, timeout: float = 120.0):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "NO_COLOR": "1",
        # Never let a test reach GitHub for a version query.
        "GIT_TERMINAL_PROMPT": "0",
    })
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=timeout, env=env)


def test_bash_syntax_is_valid():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_self_test_passes(tmp_path):
    r = run_script("--self-test", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout


def test_help_and_version(tmp_path):
    r = run_script("--help", home=tmp_path)
    assert r.returncode == 0
    for flag in ("--check", "--dry-run", "--rollback", "--uninstall",
                 "--self-test", "--ref", "--source-dir"):
        assert flag in r.stdout

    r = run_script("--version", home=tmp_path)
    assert r.returncode == 0
    assert "eqforge-update" in r.stdout


def test_unknown_option_fails_loudly(tmp_path):
    r = run_script("--not-an-option", home=tmp_path)
    assert r.returncode != 0
    assert "unknown option" in (r.stdout + r.stderr)


def test_check_on_empty_home_reports_not_installed(tmp_path):
    r = run_script("--check", "--ref", "v9.9.9", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "none" in r.stdout            # installed: none
    assert "not installed" in r.stdout
    # a check must not create the install tree
    assert not (tmp_path / "data/eqforge/install.json").exists()
    assert not (tmp_path / ".local/bin/eqforge").exists()


def test_dry_run_plans_an_install_and_changes_nothing(tmp_path):
    r = run_script("--dry-run", "--yes", "--no-autostart", "--no-service",
                   "--ref", "1.2.3", "--source-dir", str(REPO_ROOT),
                   home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "would: make -C" in out
    assert "would: create" in out and "venv" in out
    assert "native core" in out and "python package" in out
    # nothing may be installed, built or recorded by a dry run
    assert not (tmp_path / "data/eqforge/install.json").exists()
    assert not (tmp_path / ".local/bin/eqforge").exists()
    assert not (tmp_path / ".local/lib/eqforge").exists()


def test_dry_run_reports_the_target_version(tmp_path):
    r = run_script("--dry-run", "--yes", "--ref", "v1.4.0",
                   "--source-dir", str(REPO_ROOT), home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1.4.0" in r.stdout


def test_source_dir_must_be_a_checkout(tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    r = run_script("--dry-run", "--yes", "--source-dir", str(empty),
                   "--ref", "1.0.0", home=tmp_path)
    assert r.returncode != 0
    assert "does not look like an EQForge checkout" in (r.stdout + r.stderr)


def test_rollback_without_state_errors(tmp_path):
    r = run_script("--rollback", home=tmp_path)
    assert r.returncode != 0
    assert "nothing to roll back" in (r.stdout + r.stderr)


def test_uninstall_is_safe_on_a_clean_machine(tmp_path):
    r = run_script("--uninstall", "--yes", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "managed files removed" in r.stdout


def test_installed_state_is_read_back(tmp_path):
    """A state file written by an older run drives the next decision."""
    data = tmp_path / "data/eqforge"
    data.mkdir(parents=True)
    (data / "install.json").write_text(
        '{"version": "1.0.0", "stamp": "v1.0.0", "ref": "v1.0.0",'
        ' "source": "/nonexistent", "prefix": "x", "venv": "y"}')
    r = run_script("--check", "--ref", "v1.0.0", home=tmp_path)
    assert r.returncode == 0
    assert "up to date" in r.stdout
    assert "1.0.0" in r.stdout

    r = run_script("--check", "--ref", "v2.0.0", home=tmp_path)
    assert r.returncode == 0
    assert "update available" in r.stdout
