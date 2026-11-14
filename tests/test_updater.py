"""The one-script installer/updater (`scripts/eqforge.sh`).

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

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "eqforge.sh"


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


def test_updater_is_plain_bash_not_an_html_document():
    """A GitHub file-view page must never ship in place of the updater."""
    data = SCRIPT.read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"\x00" not in data
    assert b"\r" not in data, "ship Unix line endings, not CRLF"
    assert not re.search(
        rb"<!doctype\s+html|<html(?:\s|>)|<head(?:\s|>)|<body(?:\s|>)",
        data, re.IGNORECASE,
    ), "the updater contains an HTML document"
    data.decode("utf-8")


@pytest.mark.parametrize("document", [
    "README.md", "docs/installation.md", "docs/quickstart-fa.md",
])
def test_install_instructions_download_raw_bash(document):
    text = (REPO_ROOT / document).read_text(encoding="utf-8")
    raw = ("https://raw.githubusercontent.com/erfanjaster/eqforge/"
           "main/scripts/eqforge.sh")
    assert f"curl -fsSL {raw} -o eqforge.sh" in text
    assert "head -n 1 eqforge.sh" in text
    assert "bash eqforge.sh --install" in text
    assert f"https://github.com/erfanjaster/eqforge/blob/main/{SCRIPT.name}" not in text


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
    for flag in ("--install", "--update", "--check", "--dry-run",
                 "--rollback", "--uninstall", "--self-test", "--ref",
                 "--source-dir"):
        assert flag in r.stdout

    r = run_script("--version", home=tmp_path)
    assert r.returncode == 0
    assert "eqforge.sh" in r.stdout


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
    assert not (tmp_path / "data/eqforge").exists()
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
    assert not (tmp_path / "data/eqforge").exists()
    assert not (tmp_path / ".local/bin/eqforge").exists()
    assert not (tmp_path / ".local/lib/eqforge").exists()


def test_install_flag_dry_run_plans_full_build_without_prompting(tmp_path):
    r = run_script("--install", "--dry-run", "--ref", "v1.2.3",
                   "--source-dir", str(REPO_ROOT), home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "full install / repair" in r.stdout
    assert "would: make -C" in r.stdout
    assert "would: create" in r.stdout and "venv" in r.stdout
    assert "desktop launcher" in r.stdout
    assert "system autostart on" in r.stdout
    assert "nothing changed" not in r.stdout
    assert not (tmp_path / "data/eqforge").exists()
    assert not (tmp_path / ".local/bin/eqforge").exists()


def test_update_flag_requires_an_existing_install(tmp_path):
    r = run_script("--update", "--ref", "v9.9.9", home=tmp_path)
    assert r.returncode != 0
    assert "not installed" in (r.stdout + r.stderr)
    assert not (tmp_path / ".local/bin/eqforge").exists()
    assert not (tmp_path / "data/eqforge/venv").exists()


def test_update_flag_runs_without_prompt_for_installed_version(tmp_path):
    data = tmp_path / "data/eqforge"
    data.mkdir(parents=True)
    (data / "install.json").write_text(json.dumps({
        "version": "1.0.0", "stamp": "v1.0.0", "ref": "v1.0.0",
        "source": "/nonexistent", "prefix": str(tmp_path / ".local"),
        "venv": str(data / "venv"),
    }))
    r = run_script("--update", "--dry-run", "--ref", "v1.2.3",
                   "--source-dir", str(REPO_ROOT), home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would: make -C" in r.stdout
    assert "desktop launcher" in r.stdout
    assert "system autostart on" not in r.stdout
    assert "nothing changed" not in r.stdout


def test_install_and_update_flags_are_mutually_exclusive(tmp_path):
    r = run_script("--install", "--update", home=tmp_path)
    assert r.returncode != 0
    assert "cannot be combined" in (r.stdout + r.stderr)


@pytest.mark.parametrize(("args", "message"), [
    (("--check", "--dry-run"), "one primary action"),
    (("--purge",), "only works with --uninstall"),
    (("--ref", "v1.0.0", "--channel", "main"), "either --ref or --channel"),
    (("--system", "--prefix", "/tmp/custom"), "either --system or --prefix"),
    (("--check", "--force"), "cannot be used with --check"),
    (("--rollback", "--no-native"), "cannot be used with --rollback"),
    (("--uninstall", "--no-service"), "do not apply to --uninstall"),
])
def test_conflicting_options_fail_with_a_clear_message(tmp_path, args, message):
    r = run_script(*args, home=tmp_path)
    assert r.returncode != 0
    assert message in (r.stdout + r.stderr)


def test_uninstall_requires_explicit_confirmation_without_a_terminal(tmp_path):
    desktop_file = tmp_path / "data/applications/eqforge.desktop"
    desktop_file.parent.mkdir(parents=True)
    desktop_file.write_text("keep me unless uninstall is confirmed")
    r = run_script("--uninstall", home=tmp_path)
    assert r.returncode != 0
    assert "pass --yes" in (r.stdout + r.stderr)
    assert desktop_file.exists()
    assert not (tmp_path / "data/eqforge").exists()


def test_purge_requires_explicit_confirmation_without_a_terminal(tmp_path):
    config = tmp_path / "config/eqforge/profiles"
    config.mkdir(parents=True)
    profile = config / "keep.json"
    profile.write_text("profile data")
    r = run_script("--uninstall", "--purge", home=tmp_path)
    assert r.returncode != 0
    assert "pass --yes" in (r.stdout + r.stderr)
    assert profile.exists()


def test_legacy_updater_name_delegates_to_eqforge_sh(tmp_path):
    legacy = REPO_ROOT / "scripts" / "eqforge-update.sh"
    assert legacy.is_file() and os.access(legacy, os.X_OK)
    env = dict(os.environ, HOME=str(tmp_path), NO_COLOR="1")
    r = subprocess.run(["bash", str(legacy), "--version"], capture_output=True,
                       text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "eqforge.sh" in r.stdout


def test_desktop_launcher_uses_the_installed_binary_path(tmp_path):
    import shlex

    definitions = SCRIPT.read_text().rsplit('\nmain "$@"', 1)[0]
    prefix = tmp_path / "custom prefix"
    xdg_data = tmp_path / "xdg data"
    harness = definitions + f'''\nMODE=update
SRC_DIR={shlex.quote(str(REPO_ROOT))}
PREFIX={shlex.quote(str(prefix))}
XDG_DATA_HOME={shlex.quote(str(xdg_data))}
BIN_DIR="$PREFIX/bin"
install_desktop_entry
'''
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                       timeout=30, env={**os.environ, "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    entry = (xdg_data / "applications/eqforge.desktop").read_text()
    binary = f'"{prefix}/bin/eqforge"'
    assert f"Exec={binary} gui" in entry
    assert f"TryExec={binary}" in entry


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


def test_rollback_keeps_native_build_from_previous_source(tmp_path):
    """Rollback must not overwrite the rebuilt old native files with its backup."""
    old_source = tmp_path / "old-source"
    old_source.mkdir()
    marker = tmp_path / "install.json"
    marker.write_text(json.dumps({
        "previous_version": "1.0.0",
        "previous_source": str(old_source),
        "previous_ref": "v1.0.0",
    }))
    calls = tmp_path / "calls.log"
    # Load the script's function definitions without invoking its normal main,
    # then stub side-effecting phases so this regression test stays offline.
    definitions = SCRIPT.read_text().rsplit('\nmain "$@"', 1)[0]
    harness = definitions + f'''\nMARKER={str(marker)!r}
CALLS={str(calls)!r}
log_call() {{ printf '%s\\n' "$1" >> "$CALLS"; }}
backup_native() {{ log_call backup; }}
stop_units() {{ log_call stop; }}
fetch_source() {{ log_call fetch; }}
build_native() {{ log_call build; }}
install_python() {{ log_call python; }}
restore_native() {{ log_call restore; }}
autostart_step() {{ log_call autostart; }}
start_units() {{ log_call start; }}
verify() {{ log_call verify; }}
do_rollback
'''
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.read_text().splitlines() == [
        "backup", "stop", "fetch", "build", "python", "autostart", "start", "verify"
    ]


def test_uninstall_is_safe_on_a_clean_machine(tmp_path):
    desktop_file = tmp_path / "data/applications/eqforge.desktop"
    desktop_file.parent.mkdir(parents=True)
    desktop_file.write_text("EQForge launcher")
    r = run_script("--uninstall", "--yes", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "managed files removed" in r.stdout
    assert not desktop_file.exists()


def test_confirmed_purge_removes_user_profiles_and_settings(tmp_path):
    profile = tmp_path / "config/eqforge/profiles/custom.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("profile data")
    r = run_script("--uninstall", "--purge", "--yes", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "profiles, config and logs removed" in r.stdout
    assert not profile.exists()


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
