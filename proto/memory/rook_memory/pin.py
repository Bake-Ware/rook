"""Pinned beliefs: the one write that happens mid-turn.

A pin is set only from Bake's own words (the message he just sent, which the record stamps as his),
when he says something definitively. `pin` does its own lookup: handed his statement, it pins the
closest belief if one is close enough, or makes a new belief, already pinned, tied to his message.
Either way the pinned belief's text is his own words (an earlier wording is kept in belief_texts). `unpin` lifts it again,
and the belief drops straight back to an ordinary one. Dreams cannot move a pinned belief: their
links are recorded and shown, and the standing ignores them while the pin holds.
"""

import re

from . import search, standing
from .knobs import search as search_knobs


def _his(store, knobs, entry):
    row = store.one("SELECT * FROM entries WHERE id = ?", entry) if entry else None
    if not row:
        return None, "There is no message of his to tie a pin to; nothing was pinned."
    if row["speaker"] != knobs["people"]["person"]:
        return None, (f"That message is from {row['speaker']}, not {knobs['people']['person']}. Only his own words"
                      " can pin or unpin; nothing changed.")
    return row, None


def _closest(store, knobs, text, emb="load"):
    beliefs = store.all("SELECT * FROM beliefs ORDER BY id")
    if not beliefs:
        return None, 0.0
    row = search.pair(store, knobs, [text], beliefs, emb=emb if emb is not None else "load")[0]
    i = max(range(len(beliefs)), key=lambda j: row[j])
    return beliefs[i], row[i]


def his_words(statement, message):
    """The pin's text: the agent's quote if it is his words as he said them, else his whole message."""
    norm = lambda t: " ".join(re.findall(r"[a-z0-9']+", (t or "").lower()))
    quote = (statement or "").strip().strip('"').strip()
    return quote if quote and norm(quote) and norm(quote) in norm(message) else message.strip()


def pin(store, knobs, statement, entry, belief=None, about="world", emb=None):
    """Answers (belief id or None, what to tell the agent). The pinned belief's text is his words."""
    row, refusal = _his(store, knobs, entry)
    if refusal:
        return None, refusal
    words = his_words(statement, row["text"])
    match = None
    if belief:
        match = store.one("SELECT * FROM beliefs WHERE id = ?", int(str(belief).lstrip("bB")))
        how = "the belief named"
        if not match:
            return None, f"There is no belief {belief}; nothing was pinned."
    else:
        best, closeness = _closest(store, knobs, words, emb)
        if best is not None and closeness >= search_knobs(knobs)["pin_match"]:
            match, how = best, f"the closest held belief (closeness {closeness:.2f})"
    ep = store.add_episode(row["session"], f'{row["speaker"].capitalize()} said, definitively: "{words}"',
                           row["speaker"], row["channel"], "decision", [row["id"]], row["at"], at=row["at"])
    if match is None:
        if about not in [knobs["people"]["person"], "how", "world"]:
            about = "world"
        bid = store.add_belief(words, about, at=row["at"])
        how = "a new belief in his words"
    else:
        bid = match["id"]
        store.reword(bid, words, row["at"], "pinned in his words")
    st = standing.strength(knobs, "decision", row["speaker"], row["channel"])
    store.add_link(bid, ep, +1, st, standing.occasion(knobs, row["at"], row["session"]), why="his words when pinning",
                   at=row["at"])
    store.add_pin(bid, "pin", row["at"], row["id"], words)
    return bid, (f"Pinned b{bid} ({how}), in his words: {words}\nIf that is not what he meant, unpin it and pin"
                 f" again with belief= the right id, or without one to make a new belief.")


def unpin(store, knobs, entry, belief=None):
    row, refusal = _his(store, knobs, entry)
    if refusal:
        return None, refusal
    if belief:
        bid = int(str(belief).lstrip("bB"))
    else:
        last = store.one("SELECT belief FROM pins WHERE action = 'pin' ORDER BY id DESC LIMIT 1")
        bid = last["belief"] if last else None
    if not bid or not store.pinned(bid):
        return None, f"b{bid} is not pinned; nothing changed." if bid else "Nothing is pinned; nothing changed."
    store.add_pin(bid, "unpin", row["at"], row["id"], row["text"])
    text = store.one("SELECT text FROM beliefs WHERE id = ?", bid)["text"]
    return bid, f"Unpinned b{bid}; it is an ordinary belief again: {text}"


TOOLS = {
    "search": {
        "description": "Search the house agent's memory by meaning: the scratch pad (recent words), episodes and"
                       " beliefs, each with who said it and, for beliefs, their standing and evidence.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "pin": {
        "description": "Pin what Bake just said definitively. Give his statement in his own words (quote the"
                       " definitive part of his message); it pins the matching belief, reworded to his words, or makes"
                       " a new pinned one tied to his message. Give belief (an id like b12) only to pin that exact belief.",
        "inputSchema": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "belief": {"type": "string"},
            "about": {"type": "string", "description": "for a new belief: world (how things are), how (how things work)"
                                                       " or bake (what he wants)"}},
            "required": ["statement"]},
    },
    "unpin": {
        "description": "Lift a pin when Bake says it was not meant. Without belief, lifts the most recent pin.",
        "inputSchema": {"type": "object", "properties": {"belief": {"type": "string"}}},
    },
}
