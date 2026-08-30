"""Build naming: deterministic <build>.<adjective>.<noun>, and what counts as dirty."""

from __future__ import annotations

import importlib.util
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bbw", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "rook", "remote", "build_band_worker.py"))
bbw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bbw)


def test_wordlists_are_sized_for_unbiased_indexing():
    # 256 hash-byte values // 128 words == 2 exactly, so no word is favoured.
    assert len(bbw.ADJECTIVES) == len(bbw.NOUNS) == 128
    assert len(set(bbw.ADJECTIVES)) == len(set(bbw.NOUNS)) == 128
    assert all(w.isalpha() and w.islower() for w in bbw.ADJECTIVES + bbw.NOUNS)
    # Keeps the longest possible version inside the dashboard's version column.
    assert max(len(w) for w in bbw.ADJECTIVES + bbw.NOUNS) <= 8


def test_name_is_deterministic_for_a_commit():
    assert bbw.build_name("f2ba9ac") == bbw.build_name("f2ba9ac")
    assert bbw.build_name("f2ba9ac") != bbw.build_name("b17227c")


def test_name_has_the_adjective_noun_shape():
    adj, noun = bbw.build_name("deadbee").split(".")
    assert adj in bbw.ADJECTIVES
    assert noun in bbw.NOUNS


def test_dirty_replaces_the_adjective_but_keeps_the_noun():
    # The noun still identifies the commit, so two dirty builds of different
    # commits stay distinguishable.
    clean = bbw.build_name("f2ba9ac")
    dirty = bbw.build_name("f2ba9ac", dirty=True)
    assert dirty.startswith("dirty.")
    assert dirty.split(".")[1] == clean.split(".")[1]
    assert bbw.build_name("b17227c", dirty=True) != dirty


def test_names_spread_across_the_space():
    import hashlib
    names = {bbw.build_name(hashlib.sha256(str(i).encode()).hexdigest()[:7])
             for i in range(2000)}
    # Birthday collisions are fine (the build number disambiguates), but a
    # badly-mixed hash would collapse the space.
    assert len(names) > 1700
