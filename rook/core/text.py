"""Cutting text to a budget without hiding that it was cut.

Every place that shortens text an agent or a person will read (a tool
result, a role's sources, a posted reply, a transcript) cuts with
:func:`clip`, which leaves a marker at the cut saying how much was kept of
how much: ``[truncated: 6000 of 23817 chars]``. A reader can then tell a
partial result from a whole one and ask for the rest (a narrower read, the
next page).
"""
from __future__ import annotations


def marker(kept: int, total: int) -> str:
    return f"[truncated: {kept} of {total} chars]"


def clip(text: str, limit: int, *, tail: bool = False) -> str:
    """``text`` if it fits in ``limit`` characters, else its first ``limit``
    characters followed by the marker on its own line (``tail``: the last
    ``limit`` characters, the marker first; for transcripts, where the newest
    part matters)."""
    text = "" if text is None else str(text)
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if tail:
        return marker(limit, len(text)) + "\n" + text[len(text) - limit:]
    return text[:limit] + "\n" + marker(limit, len(text))
