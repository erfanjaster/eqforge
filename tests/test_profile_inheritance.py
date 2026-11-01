"""Profile inheritance: shared ancestors and "did the child mention bands?".

Two defects found while validating the workflows:

  1. `seen` was a set of everything ever visited rather than the recursion
     stack, so a shared ancestor - which is legal and common - was reported as
     circular: D extends B, C; both extend A.
  2. Every profile serialises `"eq": {"filters": [...]}`, so a child written to
     change only the preamp stored `"filters": []`, which resolve() read as
     "replace my parents' bands with nothing". The child silently lost the
     entire inherited EQ.
"""
import json

import pytest

from eqforge.profiles.resolve import resolve_profile
from eqforge.profiles.store import ProfileStore


@pytest.fixture()
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def _put(store, pid, doc):
    doc = dict(doc)
    doc.setdefault("id", pid)
    doc.setdefault("name", pid)
    store.save_raw(doc, pid)


BANDS = [{"type": "peak", "freq": 100, "gain_db": 1.0, "q": 1.0},
         {"type": "highpass", "freq": 30, "gain_db": 0.0, "q": 0.707}]


def _nbands(store, pid):
    return len(resolve_profile(pid, store.load_raw)["eq"]["filters"])


def test_shared_ancestor_is_not_a_cycle(store):
    """D -> B, C -> A. Reaching A twice is legal, not circular."""
    _put(store, "a", {"eq": {"filters": BANDS}})
    _put(store, "b", {"extends": ["a"], "preamp": {"mode": "manual", "gain_db": -1}})
    _put(store, "c", {"extends": ["a"], "preamp": {"mode": "manual", "gain_db": -2}})
    _put(store, "d", {"extends": ["b", "c"]})
    resolved = resolve_profile("d", store.load_raw)
    assert len(resolved["eq"]["filters"]) == len(BANDS)


def test_real_cycle_still_rejected(store):
    _put(store, "x", {"extends": ["y"]})
    _put(store, "y", {"extends": ["x"]})
    with pytest.raises(Exception, match="circular"):
        resolve_profile("x", store.load_raw)


def test_self_cycle_rejected(store):
    _put(store, "s", {"extends": ["s"]})
    with pytest.raises(Exception, match="circular"):
        resolve_profile("s", store.load_raw)


def test_child_that_ignores_bands_inherits_them(store):
    _put(store, "base", {"eq": {"filters": BANDS}})
    _put(store, "child", {"extends": ["base"],
                          "preamp": {"mode": "manual", "gain_db": -3}})
    assert _nbands(store, "child") == len(BANDS)


def test_explicit_empty_list_is_honoured(store):
    """An author who really wants no bands says so, and gets no bands."""
    _put(store, "base", {"eq": {"filters": BANDS}})
    _put(store, "wipe", {"extends": ["base"], "eq": {"filters": []}})
    assert _nbands(store, "wipe") == 0


def test_append_adds_to_inherited_bands(store):
    _put(store, "base", {"eq": {"filters": BANDS}})
    _put(store, "more", {"extends": ["base"], "eq": {
        "filters_op": "append",
        "filters": [{"type": "peak", "freq": 5000, "gain_db": 2.0, "q": 1.0}]}})
    assert _nbands(store, "more") == len(BANDS) + 1


def test_replace_drops_inherited_bands(store):
    _put(store, "base", {"eq": {"filters": BANDS}})
    _put(store, "own", {"extends": ["base"], "eq": {
        "filters": [{"type": "peak", "freq": 900, "gain_db": 2.0, "q": 1.0}]}})
    assert _nbands(store, "own") == 1


def test_deep_chain_resolves_in_order(store):
    """A -> B -> C, each adding preamp, keeps the band closest to the child."""
    _put(store, "a", {"eq": {"filters": BANDS}})
    _put(store, "b", {"extends": ["a"], "preamp": {"mode": "manual", "gain_db": -2}})
    _put(store, "c", {"extends": ["b"], "preamp": {"mode": "manual", "gain_db": -5}})
    resolved = resolve_profile("c", store.load_raw)
    assert resolved["preamp"]["gain_db"] == -5
    assert len(resolved["eq"]["filters"]) == len(BANDS)
