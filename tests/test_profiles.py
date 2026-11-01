"""Profile model, schema, migration, inheritance, dsp-config tests."""
from __future__ import annotations

import json

import pytest

from eqforge.errors import ProfileError, ProfileMigrationError, \
    ProfileValidationError
from eqforge.profiles.model import FilterSpec, Profile
from eqforge.profiles.resolve import compute_auto_preamp, resolve_profile, \
    to_dsp_config
from eqforge.profiles.schema import validate_profile
from eqforge.profiles.versioning import migrate, needs_migration
from tests.conftest import write_profile


# ------------------------------------------------------------ model --

def test_profile_roundtrip():
    p = Profile(id="test", name="Test",
                filters=[FilterSpec(type="peak", freq=100, gain_db=3, q=1.2),
                         FilterSpec(type="highpass", freq=30, q=0.707)])
    d = p.to_dict()
    j = json.loads(json.dumps(d))
    p2 = Profile.from_dict(j)
    assert p2.id == "test"
    assert len(p2.filters) == 2
    assert p2.filters[0].freq == 100
    assert p2.filters[1].type == "highpass"


# ----------------------------------------------------------- schema --

def test_schema_accepts_valid():
    d = Profile(id="ok", filters=[FilterSpec(freq=1000, gain_db=3)]).to_dict()
    assert not [i for i in validate_profile(d) if i.severity == "error"]


def test_schema_rejects_bad_id():
    d = {"format_version": 2, "id": "not valid!!", "eq": {"filters": []}}
    errs = [i for i in validate_profile(d) if i.severity == "error"]
    assert any("/id" in i.path for i in errs)


def test_schema_rejects_unknown_filter_type():
    d = Profile(id="x", filters=[FilterSpec(type="magic")]).to_dict()
    errs = [i for i in validate_profile(d) if i.severity == "error"]
    assert any("magic" in i.message for i in errs)


def test_schema_rejects_bad_limiter_ceiling():
    d = Profile(id="x").to_dict()
    d["limiter"] = {"enabled": True, "ceiling_db": 3.0}
    errs = [i for i in validate_profile(d) if i.severity == "error"]
    assert errs


def test_schema_warns_extreme_boost():
    d = Profile(id="x", filters=[FilterSpec(freq=100, gain_db=30)]).to_dict()
    warns = [i for i in validate_profile(d) if i.severity == "warning"]
    assert warns


def test_schema_validates_matching_rules():
    d = Profile(id="x").to_dict()
    d["matching"] = {"devices": [{"match": {}}]}  # missing profile
    errs = [i for i in validate_profile(d) if i.severity == "error"]
    assert errs


# ------------------------------------------------------- versioning --

def test_migration_v1_to_v2():
    v1 = {
        "id": "old", "name": "Old",
        "preamp_db": -3.5,
        "filters": [
            {"type": "low_shelf", "frequency": 80, "gain": 4.0, "q": 0.7},
            {"type": "PK", "freq": 2000, "gain_db": -2, "q": 1.5},
        ],
        "device_match": [{"profile": "old", "match": {"name": "dac"}}],
    }
    assert needs_migration(v1)
    v2 = migrate(v1)
    assert v2["format_version"] == 2
    assert v2["preamp"]["mode"] == "manual"
    assert v2["preamp"]["gain_db"] == -3.5
    assert v2["eq"]["filters"][0]["type"] == "lowshelf"
    assert v2["eq"]["filters"][0]["freq"] == 80
    assert v2["eq"]["filters"][1]["type"] == "peak"
    assert v2["matching"]["devices"][0]["match"]["name"] == "dac"
    assert not needs_migration(v2)


def test_migration_rejects_future_version():
    with pytest.raises(ProfileMigrationError):
        migrate({"format_version": 99, "id": "x"})


def test_migration_is_idempotent():
    v2 = migrate({"id": "x", "filters": []})
    assert migrate(v2) == v2


# ------------------------------------------------------ inheritance --

def test_inheritance_replace_and_append(tmp_path):
    write_profile(tmp_path, "base",
                  filters=[{"type": "peak", "freq": 100, "gain_db": 3,
                            "q": 1.0, "enabled": True}],
                  limiter={"enabled": True, "ceiling_db": -2.0})
    write_profile(tmp_path, "child", extends=["base"], limiter=None,
                  filters=[{"type": "peak", "freq": 5000, "gain_db": -2,
                            "q": 1.0, "enabled": True}])
    write_profile(tmp_path, "adder", extends=["base"],
                  filters=[{"type": "peak", "freq": 9000, "gain_db": 1,
                            "q": 1.0, "enabled": True}],
                  eq_filters_op="append")

    loader = lambda pid: json.loads((tmp_path / f"{pid}.json").read_text())

    r_child = resolve_profile("child", loader)
    f = r_child["eq"]["filters"]
    assert [x["freq"] for x in f] == [5000]           # replace
    assert r_child["limiter"]["ceiling_db"] == -2.0    # inherited

    # append via eq.filters_op
    d = loader("adder")
    d["eq"]["filters_op"] = "append"
    (tmp_path / "adder.json").write_text(json.dumps(d))
    r_add = resolve_profile("adder", loader)
    freqs = sorted(x["freq"] for x in r_add["eq"]["filters"])
    assert freqs == [100, 9000]


def test_inheritance_cycle_detected(tmp_path):
    write_profile(tmp_path, "a", extends=["b"])
    write_profile(tmp_path, "b", extends=["a"])
    loader = lambda pid: json.loads((tmp_path / f"{pid}.json").read_text())
    with pytest.raises(ProfileError, match="circular"):
        resolve_profile("a", loader)


def test_inheritance_depth_limit(tmp_path):
    for i in range(12):
        parent = [f"lvl{i-1}"] if i else []
        write_profile(tmp_path, f"lvl{i}", extends=parent)
    loader = lambda pid: json.loads((tmp_path / f"{pid}.json").read_text())
    with pytest.raises(ProfileError):
        resolve_profile("lvl11", loader)


# ------------------------------------------------------ dsp-config --

def test_auto_preamp():
    filters = [
        {"freq": 100, "gain_db": 5, "enabled": True},
        {"freq": 200, "gain_db": -6, "enabled": True},
        {"freq": 300, "gain_db": 2.5, "enabled": True},
        {"freq": 400, "gain_db": 9, "enabled": False},
    ]
    assert compute_auto_preamp(filters) == pytest.approx(-7.5)


def test_to_dsp_config_manual_preamp_and_stages():
    d = {
        "id": "t", "format_version": 2,
        "preamp": {"mode": "manual", "gain_db": -2.0},
        "eq": {"filters": [{"type": "peak", "freq": 1000, "gain_db": 3,
                            "q": 1, "enabled": True},
                           {"type": "peak", "freq": 50, "gain_db": 3,
                            "q": 1, "enabled": False}]},
        "crossfeed": {"enabled": True, "level_db": -6, "fc_hz": 700},
        "limiter": {"enabled": True, "ceiling_db": -1.0},
    }
    cfg = to_dsp_config(d)
    assert cfg["preamp_db"] == -2.0
    assert len(cfg["eq"]["filters"]) == 1     # disabled dropped
    assert cfg["crossfeed"]["enabled"] is True
    assert cfg["limiter"]["ceiling_db"] == -1.0
    json.dumps(cfg)  # serializable


def test_store_builtin_presets_all_valid():
    from eqforge.profiles.store import ProfileStore
    store = ProfileStore()
    entries = store.builtin_profiles()
    assert len(entries) >= 10
    for pid in entries:
        d = store.load_raw(pid)
        errs = [i for i in validate_profile(d) if i.severity == "error"]
        assert not errs, f"{pid}: {errs}"
        resolved = store.resolve(pid)
        cfg = to_dsp_config(resolved)
        json.dumps(cfg)


def test_store_user_shadows_builtin(tmp_path, monkeypatch):
    from eqforge.profiles.store import ProfileStore
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    store = ProfileStore(user_dir=user_dir)
    prof = Profile(id="flat", name="My Flat Override",
                   filters=[FilterSpec(freq=1000, gain_db=-1)])
    store.save(prof)
    loaded = store.load_raw("flat")
    assert loaded["name"] == "My Flat Override"


def test_store_corrupt_file_skipped(tmp_path):
    from eqforge.profiles.store import ProfileStore
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "broken.json").write_text("{not json")
    store = ProfileStore(user_dir=user_dir)
    assert "broken" not in store.user_profiles()
