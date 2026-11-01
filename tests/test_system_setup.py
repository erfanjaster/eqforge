"""`eqforge system setup` must never destroy a user's edits.

The audit found it called Path.write_text() unconditionally, so re-running it
silently replaced a hand-edited PipeWire drop-in. Those files are documented
as user-editable, so losing them is real data loss, not a cosmetic issue.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eqforge.system import module_config


class FakePaths:
    """Minimal stand-in for eqforge.paths with an isolated home."""

    def __init__(self, home: Path):
        self._home = home

    def home(self) -> str:
        return str(self._home)

    def active_dsp_file(self) -> Path:
        return self._home / ".config/eqforge/active-dsp.json"

    def rt_socket(self) -> Path:
        return self._home / "run/eqforge/rt.sock"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path, FakePaths(tmp_path)


def test_creates_files_when_absent(env):
    home, paths = env
    results = []
    written = module_config.write_system_setup(paths, results=results)
    assert len(written) == 2
    assert all(p.exists() for p in written)
    assert [st for _, st, _ in results] == ["created", "created"]
    assert all(b is None for _, _, b in results)


def test_second_run_is_idempotent(env):
    home, paths = env
    module_config.write_system_setup(paths)
    results = []
    module_config.write_system_setup(paths, results=results)
    assert [st for _, st, _ in results] == ["same", "same"]
    assert all(b is None for _, _, b in results)


def test_user_edit_is_backed_up_not_lost(env):
    home, paths = env
    conf = (Path(home) / ".config/pipewire/pipewire.conf.d/99-eqforge.conf")
    module_config.write_system_setup(paths)
    conf.write_text(conf.read_text() + "\n# user tweak\n")

    results = []
    module_config.write_system_setup(paths, results=results)

    status, backup = results[0][1], results[0][2]
    assert status == "updated"
    assert backup is not None and backup.exists()
    assert "# user tweak" in backup.read_text(), "the edit was not preserved"
    assert "# user tweak" not in conf.read_text(), "the file was not rewritten"


def test_backup_name_never_collides(env, monkeypatch):
    home, paths = env
    conf = Path(home) / ".config/pipewire/pipewire.conf.d/99-eqforge.conf"
    module_config.write_system_setup(paths)

    # freeze the clock so both backups would pick the same timestamp
    monkeypatch.setattr(module_config.time, "strftime",
                        lambda fmt: "20260101-000000")
    conf.write_text("edit one")
    r1 = []
    module_config.write_system_setup(paths, results=r1)
    conf.write_text("edit two")
    r2 = []
    module_config.write_system_setup(paths, results=r2)

    assert r1[0][2] != r2[0][2], "backup files collided"
    assert r1[0][2].exists() and r2[0][2].exists()
    assert "edit one" in r1[0][2].read_text()
    assert "edit two" in r2[0][2].read_text()


def test_rendered_config_points_at_the_active_dsp_file(env):
    home, paths = env
    module_config.write_system_setup(paths)
    conf = Path(home) / ".config/pipewire/pipewire.conf.d/99-eqforge.conf"
    text = conf.read_text()
    assert str(paths.active_dsp_file()) in text
    assert "eqforge.filter" in text
