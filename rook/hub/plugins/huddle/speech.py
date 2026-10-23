"""Reading huddle messages aloud (the Speak button on each bubble).

A message is chat text written for a screen: markdown, code, links, record
ids. :func:`speakable` turns it into what a voice should say: no markup,
code blocks and links left out, record ids said by their three-word names
(``t_7b47df858bfc`` -> ``brave otter lamp``) or dropped. :func:`parts` cuts
it into sentence-sized chunks the voice engine reads whole (see :data:`PART`).
:func:`choose_voice` picks the voice: the speaking role's own voice, else
the band's default, else (no voice engine the hub can reach) the reader's
browser.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

#: Longest text read out for one message (about ten minutes); the rest is
#: announced as cut.
MAX_SPOKEN = 10000
#: Longest chunk sent to the voice engine in one request. Its /api/voice takes
#: 700 characters, but Chatterbox stops at about 32 seconds of audio and drops
#: the rest of a longer line silently; it reads about 16 characters a second,
#: so 280 (about 17 s) leaves room for slow voices.
PART = 280
#: The first chunk is shorter, so the voice starts sooner.
FIRST_PART = 180
#: A chunk shorter than this takes the start of a long sentence with it
#: rather than going alone.
MIN_PART = 60

_FENCE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()\s]|\([^)]*\))*\)")
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_URL = re.compile(r"(?:https?|wss?|ftp)://[^\s<>()\[\]]+|\bwww\.[^\s<>()\[\]]+", re.I)
_TAG = re.compile(r"</?[a-zA-Z][^>]{0,200}>")
_INLINE = re.compile(r"`([^`\n]*)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_RULE = re.compile(r"^\s*(?:[-*_=]\s*){3,}$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_EMPHASIS = re.compile(r"(\*\*|~~|\*)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\3(?!\w)")
#: Record ids: a prefixed id (``t_7b47df858bfc``, ``k_<32 hex>``), a UUID, or
#: bare hex of 7+ characters with a digit in it (room ids, commits).
_ID = re.compile(r"\b(?:[a-z]{1,4}_(?=[0-9a-f]*\d)[0-9a-f]{6,}"
                 r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                 r"|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64})\b", re.I)
#: A voice engine's voice id (the roles' ``voice`` field accepts the same).
VOICE_ID = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
#: A sender prefix in the text (``role:qa``, ``agent:home``, ``user:operator``).
_PRINCIPAL = re.compile(r"\b(?:role|agent|user|human):([\w.@-]+)")
_PUNCT_END = re.compile(r"[.!?:;,]$")


def speakable(text: str, name_for: Callable[[str], str | None] | None = None,
              limit: int = MAX_SPOKEN) -> str:
    """``text`` as plain sentences to read aloud.

    ``name_for(id)`` gives a record's three-word name (``brave-otter-lamp``),
    or None; ids without a name are left out.
    """
    t = str(text or "").replace("\r\n", "\n")
    t = _FENCE.sub("\n(code left out)\n", t)
    t = _IMAGE.sub(lambda m: m.group(1), t)
    t = _WIKI.sub(lambda m: (m.group(2) or m.group(1)).replace("-", " "), t)
    t = _LINK.sub(lambda m: m.group(1), t)
    t = _URL.sub("", t)
    t = _TAG.sub("", t)
    t = _INLINE.sub(lambda m: m.group(1), t)
    t = _TABLE_SEP.sub("", t)
    t = _RULE.sub("", t)
    t = _HEADING.sub("", t)
    t = _QUOTE.sub("", t)
    t = _BULLET.sub("", t)

    def rid(m: re.Match) -> str:
        name = None
        if name_for is not None:
            try:
                name = name_for(m.group(0))
            except Exception:  # noqa: BLE001 - a lookup never stops the reading
                name = None
        return name.replace("-", " ") if name else ""
    t = _ID.sub(rid, t)
    for _ in range(2):
        t = _EMPHASIS.sub(lambda m: m.group(2) or m.group(4), t)
    t = _PRINCIPAL.sub(lambda m: m.group(1), t)
    t = t.replace("|", ", ").replace("@", "")
    lines = []
    for line in t.split("\n"):
        line = re.sub(r"\s+", " ", line).strip(" ,")
        line = re.sub(r"\(\s*\)|\[\s*\]", "", line).strip()
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        if not line or not re.search(r"\w", line):
            continue
        lines.append(line if _PUNCT_END.search(line) else line + ".")
    out = " ".join(lines)
    if len(out) > limit:
        cut = out.rfind(" ", 0, limit)
        out = out[:cut if cut > limit // 2 else limit].rstrip(" ,;") + ". The rest is cut."
    return out


_SENTENCE = re.compile(r"(?<=[.!?\u2026])\s+")
_CLAUSE = (", ", "; ", ": ", " - ", " \u2014 ", " \u2013 ")


def _cut(text: str, room: int, hard: bool) -> tuple[str, str]:
    """``text`` split as ``(head, rest)`` with ``head`` at most ``room``
    characters, cut after a clause, else between words. A word longer than
    ``room`` is cut only when ``hard`` (else the head is empty)."""
    if len(text) <= room:
        return text, ""
    window = text[:room + 1]
    ends = [i + len(p.rstrip()) for p in _CLAUSE if (i := window.rfind(p)) >= 0]
    cut = max(ends, default=-1)
    if cut < room // 3:
        cut = window.rfind(" ")
    if cut <= 0:
        if not hard:
            return "", text
        cut = room
    return text[:cut].strip(), text[cut:].strip()


def parts(text: str, limit: int = PART, first: int = FIRST_PART) -> list[str]:
    """``text`` in chunks for the voice engine, in order: whole sentences
    packed up to ``limit`` characters (``first`` for the first chunk), a
    sentence longer than that cut after a clause or between words, never
    inside a word (unless one word is longer than a chunk), and a very short
    sentence carried along with the next rather than sent alone.
    ``" ".join(parts(t)) == t`` for whitespace-normalized ``t``."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    queue = [s for s in _SENTENCE.split(text) if s]
    queue.reverse()
    out: list[str] = []
    cur = ""
    while queue:
        sentence = queue.pop()
        lim = max(1, first if not out else limit)
        joined = f"{cur} {sentence}" if cur else sentence
        if len(joined) <= lim:
            cur = joined
            continue
        if len(cur) >= MIN_PART:      # a full chunk: the sentence starts the next one
            out.append(cur)
            cur = ""
            queue.append(sentence)
            continue
        room = lim - len(cur) - (1 if cur else 0)
        head, rest = _cut(sentence, room, hard=not cur)
        cur = f"{cur} {head}".strip() if head else cur
        out.append(cur)
        cur = ""
        if rest:
            queue.append(rest)
    if cur:
        out.append(cur)
    return out


def choose_voice(role_voice: str, *, unavailable: str = "", voices: Iterable[str] | None = None,
                 default: str = "") -> dict:
    """Which voice reads a message.

    The role's own voice when it has one the engine knows, else the band's
    default (``voice`` blank: the engine's starting voice, ``default``
    names it); with no engine the hub can reach (``unavailable`` says why),
    the reader's browser speaks it.
    """
    if unavailable:
        return {"mode": "browser", "voice": "", "source": "browser", "reason": unavailable}
    role_voice = str(role_voice or "").strip()
    known = None if voices is None else {str(v) for v in voices}
    if role_voice:
        bare = role_voice.split(":", 1)[1] if ":" in role_voice else role_voice
        if known is None or role_voice in known or bare in known:
            return {"mode": "hub", "voice": role_voice, "source": "role", "label": role_voice}
        return {"mode": "hub", "voice": "", "source": "default", "label": default,
                "reason": f"the role's voice {role_voice} is not on the voice engine"}
    return {"mode": "hub", "voice": "", "source": "default", "label": default}
