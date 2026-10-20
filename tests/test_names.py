"""Speakable record names (rook.core.names): the word list rules, generation,
uniqueness, backfill, resolution and fuzzy safety."""
from __future__ import annotations

import random
import re
import sqlite3
from pathlib import Path

import pytest

from rook.core import names

PRON = Path(__file__).parent / "data" / "name_words_pron.txt"

# Words that sound like an instruction to a voice assistant or a Rook command.
COMMANDS = set("""
stop delete cancel yes no yeah nope start go run kill end quit exit open close
undo redo back next help save send play pause skip clear reset wait abort halt
okay ok sure confirm deny remove erase wipe restart done finish call read write
show hide new all none up down on off fire launch approve reject accept hold
release lock unlock repeat again ready empty blank secret enter submit select
""".split())

# Offensive, violent or sexual words, and brand names, that must never appear.
BANNED = set("""
kill murder gun rifle pistol bomb blood knife sword dagger war warrior soldier
bullet grenade weapon slay slaughter corpse dead death die suicide poison
sex sexy nude naked kinky horny cock dick pussy booty boob tit titmouse ass butt
beaver cougar fairy pansy gay queer slut whore tramp hooker
eskimo gypsy tribal native wigwam squaw redskin
apple amazon google tesla jaguar puma dove tide target shell oracle uber visa
gap subway nike ford dodge mustang corona galaxy echo frisbee thermos civic
fiesta sonic kleenex xerox lego pepsi coke
""".split())

# Rook's own vocabulary: a name made of these would read like a command.
ROOK_WORDS = set("""
rook band card key signal channel model menu dashboard journal folder root
pipe log string branch hub worker task job room huddle console handoff
""".split())


def _pron() -> dict[str, list[str]]:
    out = {}
    for line in PRON.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        w, *ph = line.split()
        out[w] = ph
    return out


def _sound_key(word: str, ph: list[str]) -> str:
    """Stress dropped, AO/AA and AX/AH merged, a final -s dropped and the last
    consonant devoiced: words with the same key are homophones or near
    homophones (to/two, sea/see, cup/cub, ridge/rich, deer/dear)."""
    ph = [re.sub(r"\d", "", p) for p in ph]
    ph = [{"AO": "AA", "AX": "AH"}.get(p, p) for p in ph]
    while word.endswith("s") and ph and ph[-1] in ("S", "Z"):
        ph = ph[:-1]
    dev = {"D": "T", "B": "P", "G": "K", "V": "F", "DH": "TH", "JH": "CH", "Z": "S", "ZH": "SH"}
    if ph:
        ph[-1] = dev.get(ph[-1], ph[-1])
    return " ".join(ph)


# -- the word list --------------------------------------------------------------

def test_word_list_shape():
    adj, noun = names.adjectives(), names.nouns()
    allw = adj + noun
    assert 1900 <= len(allw) <= 2300, len(allw)
    assert len(set(allw)) == len(allw), "a word is in both lists or twice"
    for w in allw:
        assert re.fullmatch(r"[a-z]{3,9}", w), w
    assert list(adj) == sorted(adj) and list(noun) == sorted(noun)


def test_word_list_has_no_command_banned_or_rook_words():
    allw = set(names.words())
    assert not allw & COMMANDS, sorted(allw & COMMANDS)
    assert not allw & BANNED, sorted(allw & BANNED)
    assert not allw & ROOK_WORDS, sorted(allw & ROOK_WORDS)


def test_word_list_is_easy_to_say():
    pron = _pron()
    allw = names.words()
    missing = sorted(w for w in allw if w not in pron)
    assert not missing, f"no pronunciation for {missing[:10]}"
    assert set(pron) == set(allw), "pronunciation file out of step with the list"
    for w in allw:
        syllables = sum(1 for p in pron[w] if p[-1].isdigit())
        assert 1 <= syllables <= 3, w


def test_word_list_has_no_homophones_or_near_homophones():
    pron = _pron()
    seen: dict[str, str] = {}
    clashes = []
    for w in sorted(names.words()):
        k = _sound_key(w, pron[w])
        if k in seen:
            clashes.append((seen[k], w))
        seen[k] = w
    assert not clashes, clashes


def test_sound_key_catches_known_pairs():
    pairs = {"sea": ["S", "IY1"], "see": ["S", "IY1"], "cup": ["K", "AH1", "P"],
             "cub": ["K", "AH1", "B"], "ridge": ["R", "IH1", "JH"], "rich": ["R", "IH1", "CH"]}
    k = {w: _sound_key(w, ph) for w, ph in pairs.items()}
    assert k["sea"] == k["see"] and k["cup"] == k["cub"] and k["ridge"] == k["rich"]


# -- generation -----------------------------------------------------------------

def test_generate_shape():
    rng = random.Random(7)
    for _ in range(200):
        n = names.generate(rng)
        a, b, c = n.split("-")
        assert a in names.adjectives() and b in names.nouns() and c in names.nouns()
        assert b != c
        assert names.is_name(n)


def test_normalize_accepts_spaces_hyphens_underscores_and_case():
    for text in ("brave otter lamp", "Brave-Otter-Lamp", "brave_otter_lamp",
                 "  BRAVE   otter-lamp ", "brave.otter.lamp"):
        assert names.normalize(text) == "brave-otter-lamp"
    for text in ("", "brave otter", "a b c d", "3f2a9c1d00aa11bb", "t_0123", "brave otter l4mp", None, 5):
        assert names.normalize(text) is None


# -- storage --------------------------------------------------------------------

def _db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    names.ensure_table(conn)
    return conn


def test_assign_is_idempotent_and_unique_per_kind_and_band():
    conn = _db()
    a = names.assign(conn, "task", "t_1")
    assert names.assign(conn, "task", "t_1") == a
    assert names.name_of(conn, "task", "t_1") == a
    # Force collisions: a generator that repeats is retried until a free name.
    seq = iter([a, a, a, "calm-otter-lamp"])

    class Rng:
        def choice(self, opts):
            return opts[0]
    names_gen = names.generate
    try:
        names.generate = lambda rng=None: next(seq)
        assert names.assign(conn, "task", "t_2") == "calm-otter-lamp"
        # The same name is fine for another kind or band.
        seq = iter([a])
        assert names.assign(conn, "job", "j_1") == a
        seq = iter([a])
        assert names.assign(conn, "task", "t_3", band="other") == a
    finally:
        names.generate = names_gen
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO record_names(kind,id,name,band,created) VALUES ('task','t_9',?, '', 0)", (a,))


def test_assign_gives_up_after_attempts():
    conn = _db()
    names.assign(conn, "task", "t_1")
    taken = names.name_of(conn, "task", "t_1")
    gen = names.generate
    try:
        names.generate = lambda rng=None: taken
        with pytest.raises(RuntimeError):
            names.assign(conn, "task", "t_2", attempts=3)
    finally:
        names.generate = gen


def test_backfill_names_every_record_once_and_is_reversible():
    conn = _db()
    ids = [f"r{i:04d}" for i in range(500)]
    assert names.backfill(conn, "room", ids) == 500
    assert names.backfill(conn, "room", ids) == 0
    got = names.names_of(conn, "room", ids)
    assert set(got) == set(ids)
    assert len(set(got.values())) == 500
    assert names.backfill(conn, "task", [("t_a", "b1"), ("t_b", "b2")]) == 2
    names.drop(conn)
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name='record_names'").fetchall()
    names.ensure_table(conn)
    assert names.backfill(conn, "room", ids[:3]) == 3


# -- resolution -----------------------------------------------------------------

def _seed():
    conn = _db()
    for rid, n in (("aa11", "brave-otter-lamp"), ("bb22", "calm-otter-lamp"),
                   ("cc33", "brave-walrus-kettle"), ("dd44", "quiet-maple-harbor")):
        conn.execute("INSERT INTO record_names VALUES ('room', ?, ?, '', 0)", (rid, n))
    return conn


def test_resolve_by_id_and_exact_name_any_spelling():
    conn = _seed()
    ids = {"aa11", "bb22", "cc33", "dd44"}
    ex = ids.__contains__
    assert names.resolve(conn, "room", "aa11", exists=ex).via == "id"
    assert names.resolve(conn, "room", "aa11", exists=ex).name == "brave-otter-lamp"
    for text in ("brave otter lamp", "BRAVE_OTTER_LAMP", "Brave-Otter lamp"):
        r = names.resolve(conn, "room", text, exists=ex, write=True)
        assert (r.id, r.via) == ("aa11", "name")
    # Not a name and not an id: handed back for the caller's own not-found.
    assert names.resolve(conn, "room", "zz99", exists=ex).id == "zz99"


def test_fuzzy_read_uses_one_clear_near_miss():
    conn = _seed()
    r = names.resolve(conn, "room", "quiet maple harbour", exists=lambda _: False)
    assert (r.id, r.via, r.name) == ("dd44", "fuzzy", "quiet-maple-harbor")
    r = names.resolve(conn, "room", "brave walrus kettles", exists=lambda _: False)
    assert r.id == "cc33" and r.via == "fuzzy"


def test_fuzzy_never_resolves_a_write():
    conn = _seed()
    with pytest.raises(names.NameError_) as e:
        names.resolve(conn, "room", "quiet maple harbour", exists=lambda _: False, write=True)
    assert ("dd44", "quiet-maple-harbor") in e.value.candidates
    assert "exact name" in str(e.value)
    p = e.value.payload()
    assert p["candidates"][0] == {"id": "dd44", "name": "quiet-maple-harbor"}


def test_ambiguous_near_miss_returns_candidates():
    conn = _seed()
    conn.execute("INSERT INTO record_names VALUES ('room', 'ee55', 'brave-otter-ramp', '', 0)")
    # "brave otter damp" is as close to brave-otter-lamp as to -ramp: two
    # candidates, so no guess even for a read.
    with pytest.raises(names.NameError_) as e:
        names.resolve(conn, "room", "brave otter damp", exists=lambda _: False)
    assert e.value.ambiguous
    got = {i for i, _ in e.value.candidates}
    assert {"aa11", "ee55"} <= got


def test_exact_name_ambiguous_across_bands():
    conn = _db()
    conn.execute("INSERT INTO record_names VALUES ('task','t_1','brave-otter-lamp','b1',0)")
    conn.execute("INSERT INTO record_names VALUES ('task','t_2','brave-otter-lamp','b2',0)")
    with pytest.raises(names.NameError_) as e:
        names.resolve(conn, "task", "brave otter lamp")
    assert e.value.ambiguous and len(e.value.candidates) == 2
    assert names.resolve(conn, "task", "brave otter lamp", band="b2").id == "t_2"


def test_unknown_name_with_no_near_miss():
    conn = _seed()
    with pytest.raises(names.NameError_) as e:
        names.resolve(conn, "room", "lofty zebra yacht", exists=lambda _: False)
    assert not e.value.candidates


def test_label():
    assert names.label("brave-otter-lamp", "aa11") == "brave-otter-lamp (aa11)"
    assert names.label(None, "aa11") == "aa11"
