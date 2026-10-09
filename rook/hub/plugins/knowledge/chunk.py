"""Block-level chunking of record bodies, for per-block embeddings and
query-relevant search excerpts.

A body splits at markdown headings (ATX ``#`` .. ``######``, ignoring fenced
code). Each block keeps its heading path (``Hard rules > Branch``); text
before the first heading is the intro (empty path). A heading with almost
nothing under it merges into the block that follows; a block longer than
``MAX_BLOCK`` splits at paragraph breaks, then lines, then hard.

Deterministic and stdlib-only: the index and the query side chunk the same
body the same way.
"""
from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

MAX_BLOCK = 1200   # chars; the default embedder (MiniLM) reads ~256 tokens
MIN_CONTENT = 40   # a heading with less under it merges forward
HEADING = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
TERMS = re.compile(r'\w+')


class Block(NamedTuple):
    ord: int
    heading: str   # heading path, '' for the intro
    start: int     # offsets into the body
    end: int


def _sections(body):
    """(heading path, start, end, content start) per heading section."""
    out, stack, fence = [], [], None
    start, path, content = 0, '', 0
    pos = 0
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip('\r\n')
        f = FENCE.match(stripped)
        if f:
            mark = f.group(1)
            if fence is None:
                fence = mark[0] * len(mark)
            elif mark.startswith(fence):
                fence = None
        elif fence is None:
            h = HEADING.match(stripped)
            if h:
                out.append((path, start, pos, content))
                level = len(h.group(1))
                stack = [s for s in stack if s[0] < level] + [(level, h.group(2).strip())]
                path = ' > '.join(s[1] for s in stack)
                start, content = pos, pos + len(line)
        pos += len(line)
    out.append((path, start, len(body), content))
    return out


def _split(body, start, end):
    """Cut [start, end) into pieces of at most MAX_BLOCK chars, preferring
    paragraph breaks, then line breaks."""
    pieces = []
    while end - start > MAX_BLOCK:
        window = body[start:start + MAX_BLOCK]
        cut = window.rfind('\n\n')
        if cut < MAX_BLOCK // 3:
            cut = window.rfind('\n')
        if cut < MAX_BLOCK // 3:
            cut = window.rfind(' ')
        if cut < MAX_BLOCK // 3:
            cut = MAX_BLOCK
        pieces.append((start, start + cut))
        start += cut
    pieces.append((start, end))
    return pieces


def chunk(body: str) -> list[Block]:
    """The body's blocks, in order. Never empty: an empty body is one empty
    intro block (the title alone gets embedded)."""
    merged, carry = [], None
    for path, start, end, content in _sections(body or ''):
        if carry is not None:
            start, carry = carry[0], None
        if not body[start:end].strip():
            continue
        if len(body[content:end].strip()) < MIN_CONTENT and path:
            carry = (start, path)
            continue
        merged.append((path, start, end))
    if carry is not None:  # trailing heading(s) with nothing under them
        if merged:
            path, start, _ = merged.pop()
            merged.append((path, start, len(body)))
        else:
            merged.append((carry[1], carry[0], len(body)))
    blocks = []
    for path, start, end in merged:
        for s, e in _split(body, start, end):
            blocks.append(Block(len(blocks), path, s, e))
    return blocks or [Block(0, '', 0, len(body or ''))]


def block_text(body, block):
    """The block's text without its leading heading lines (the path carries
    them; a merged block can start with several)."""
    lines = body[block.start:block.end].split('\n')
    while lines and (not lines[0].strip() or HEADING.match(lines[0].rstrip('\r'))):
        lines.pop(0)
    return '\n'.join(lines).strip()


def embed_text(title, body, block, limit=2000):
    """What gets embedded for a block: the page title and heading path give
    the block its context."""
    head = title + ('\n' + block.heading if block.heading else '')
    return (head + '\n' + block_text(body, block))[:limit]


def digest(title, body, block):
    """Content key for a block's vector: an edit re-embeds only the blocks
    whose text (or the page title) changed."""
    return hashlib.sha256(embed_text(title, body, block).encode()).hexdigest()[:32]


def terms(query):
    return list(dict.fromkeys(t.lower() for t in TERMS.findall(query or '') if len(t) > 1))[:20]


def term_hits(text, words):
    """(distinct query terms present, total occurrences) in ``text``."""
    low = text.lower()
    found = [low.count(w) for w in words]
    return sum(1 for n in found if n), sum(found)


def excerpt(text, words, limit):
    """Up to ``limit`` chars of ``text`` with whitespace collapsed, windowed
    around the first query-term hit when the text is longer."""
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    low = text.lower()
    hits = [i for i in (low.find(w) for w in words) if i >= 0]
    limit -= 2  # room for the ellipses
    start = max(0, min(hits) - limit // 4) if hits else 0
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    if start and text[start - 1] != ' ':
        start = text.find(' ', start, start + 20) + 1 or start
    if end < len(text) and text[end] != ' ':
        end = text.rfind(' ', end - 20, end) if text.rfind(' ', end - 20, end) > start else end
    return ('…' if start else '') + text[start:end].strip() + ('…' if end < len(text) else '')
