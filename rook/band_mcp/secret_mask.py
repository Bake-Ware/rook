"""Reverse masking: known vault values become ``{{secret:<name>}}`` stubs.

The vault's forward path substitutes ``{{secret:name}}`` in ``rook_call`` args
just before dispatch. This module is the reverse: wherever content crosses or
lands on the band through the hub (MCP replies, the call journal, chat,
handoffs, knowledge records, task attrs, console transcripts) any known
secret value is replaced with its stub, so a value that leaked into a cap's
output, a file read or an agent's own message never travels or gets stored in
plain text. An agent that sees a stub can pass it straight back in rook_call
args and the forward path resolves it again.

Matching
--------
* Every vault value of at least ``MIN_LEN`` (8) characters, as an exact
  substring. Shorter values are skipped: masking every "admin" or "1234" in
  every payload would mangle ordinary text. (Values a call used explicitly via
  a placeholder are still masked in that call's reply down to 4 characters,
  see ``SecretMasker.mask(extra=...)``; so are values typed into a console
  room, in that room's transcript.)
* Cheap encodings of each value: JSON-escaped once and twice (a value inside
  JSON inside JSON), base64 (standard and URL-safe, padded and not) of the
  whole value, and URL/percent encoding (``quote``, ``quote_plus``).
* An encoded form becomes an encoding-tagged stub, ``{{secret:name|b64}}``,
  so the forward path re-encodes the value and a read -> edit -> write of a
  file round-trips to the same bytes. Tags chain left to right
  (``{{secret:name|json|json}}`` is the value JSON-escaped twice); see
  ``ENCODERS``. The raw value keeps the plain ``{{secret:name}}``.
* Leftmost match wins, and at one position the longest form wins, so a secret
  that contains another is masked as the longer one. Text that already is a
  stub is left alone (no stub inside a stub). Dict keys are never masked.
* One compiled trie-shaped regex (a prefix tree turned into nested
  alternations), rebuilt only when the vault changes.

Not covered: base64 of a value embedded at an arbitrary offset inside a larger
base64 blob (e.g. ``Basic`` auth of ``user:password``), hex, other encodings,
case changes, and values shorter than ``MIN_LEN``. Values are never logged.
"""
from __future__ import annotations

import base64
import bisect
import codecs
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import quote, quote_plus

log = logging.getLogger("rook.band_mcp.secret_mask")

MIN_LEN = 8          # raw values shorter than this are not masked everywhere
MIN_EXTRA_LEN = 4    # values a call used explicitly are masked down to this
HEAD = 16            # prefix length indexed for the stream hold-back check
CHECK_INTERVAL = 1.0  # seconds between cross-process vault version checks


def _b64(value: str, enc=base64.b64encode) -> str:
    return enc(value.encode("utf-8", "surrogatepass")).decode("ascii")


# Encoding tags: ``{{secret:name|<tag>}}`` resolves to ENCODERS[tag](value).
ENCODERS: dict[str, Callable[[str], str]] = {
    "b64": lambda v: _b64(v),
    "b64np": lambda v: _b64(v).rstrip("="),
    "b64url": lambda v: _b64(v, base64.urlsafe_b64encode),
    "b64urlnp": lambda v: _b64(v, base64.urlsafe_b64encode).rstrip("="),
    "url": lambda v: quote(v, safe=""),
    "urlpath": lambda v: quote(v),
    "urlplus": lambda v: quote_plus(v),
    "json": lambda v: json.dumps(v, ensure_ascii=False)[1:-1],
    "jsona": lambda v: json.dumps(v)[1:-1],
}
_TAGS = "|".join(sorted(ENCODERS, key=len, reverse=True))
# Any stub, plain or tagged. Group 1: the name; group 2: "|tag|tag" or "".
STUB = re.compile(r"\{\{secret:([a-z0-9][a-z0-9._-]{0,63})((?:\|(?:%s))*)\}\}" % _TAGS)
_STUB_OPEN = "{{secret:"
_STUB_MAX = len(_STUB_OPEN) + 64 + 2 + 10 * 4  # name, braces, a few tags
_STUB_NC = r"\{\{secret:[a-z0-9][a-z0-9._-]{0,63}(?:\|(?:%s))*\}\}" % _TAGS


def stub(name: str, tags: "tuple[str, ...] | list[str]" = ()) -> str:
    return "{{secret:%s}}" % "|".join((name, *tags))


def encode(value: str, tags: "str | tuple[str, ...] | list[str]") -> str:
    """Apply encoding tags left to right (``"|b64"`` or ``("b64",)``)."""
    if isinstance(tags, str):
        tags = [t for t in tags.split("|") if t]
    for t in tags:
        value = ENCODERS[t](value)
    return value


def tagged_forms(value: str) -> list[tuple[str, tuple[str, ...]]]:
    """[(form, tags)] for the value and the encodings we look for, longest
    first. ``encode(value, tags) == form`` for every pair; a form two chains
    produce keeps the shorter chain."""
    if not isinstance(value, str) or not value:
        return []
    chains: dict[str, tuple[str, ...]] = {value: ()}
    for t in ("b64", "b64np", "b64url", "b64urlnp", "url", "urlpath", "urlplus"):
        chains.setdefault(ENCODERS[t](value), (t,))
    # JSON-escaped once and twice (a worker's stdout that itself holds JSON,
    # stored again as JSON). Escaping leaves base64/percent forms unchanged.
    for _ in range(2):
        for form, chain in list(chains.items()):
            for t in ("json", "jsona"):
                chains.setdefault(ENCODERS[t](form), chain + (t,))
    return sorted(((f, c) for f, c in chains.items() if f), key=lambda fc: len(fc[0]), reverse=True)


def forms_for(value: str) -> list[str]:
    """The value plus the encodings we look for, longest first."""
    return [f for f, _ in tagged_forms(value)]


# -- matcher -------------------------------------------------------------------

def _trie_regex(words: list[str]) -> str:
    """A regex matching any of ``words``, preferring the longest at a position.
    Built from a prefix tree, so a scan costs one walk down the tree instead of
    one try per word. Single-child chains collapse into literals, so nesting
    depth is the number of branch points, not the word length."""
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = {}

    def emit(node: dict) -> str:
        parts = []
        for ch in sorted(k for k in node if k):
            run, child = [ch], node[ch]
            while "" not in child and len(child) == 1:
                (c2, child), = child.items()
                run.append(c2)
            parts.append(re.escape("".join(run)) + emit(child))
        if not parts:
            return ""
        body = parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"
        # Greedy optional: try the longer word first, fall back to this one.
        return "(?:" + body + ")?" if "" in node else body
    return emit(trie)


class Matcher:
    """An immutable matcher over one snapshot of {name: value}. ``extra``
    ({name: value}) adds values masked down to MIN_EXTRA_LEN characters (ones
    known to be in play for one payload), in the same single pass."""

    def __init__(self, secrets: dict[str, str], min_len: int = MIN_LEN,
                 extra: "dict[str, str] | None" = None) -> None:
        stubs: dict[str, str] = {}
        named: set[str] = set()
        for src, floor in ((secrets, min_len), (extra or {}, MIN_EXTRA_LEN)):
            # Sorted by name so a value stored under two names always maps to
            # the same (first) one.
            for name in sorted(src):
                value = src[name]
                if not isinstance(value, str) or len(value) < floor:
                    continue
                named.add(name)
                for f, tags in tagged_forms(value):
                    stubs.setdefault(f, stub(name, tags))
        self._stubs = stubs
        self.count = len(named)
        self._pattern = None
        self._sorted: list[str] = []
        self._heads: set[str] = set()
        self.longest = 0
        self.shortest = 0
        if not stubs:
            return
        words = sorted(stubs, key=len, reverse=True)
        # An existing stub is matched first and kept as it is, so a value that
        # occurs inside a stub's name never nests one stub inside another.
        try:
            self._pattern = re.compile("(?P<stub>%s)|%s" % (_STUB_NC, _trie_regex(words)))
        except (RecursionError, re.error, OverflowError):
            self._pattern = re.compile("(?P<stub>%s)|%s" % (
                _STUB_NC, "|".join(re.escape(w) for w in words)))
        self._sorted = sorted(words)
        # Every form's first HEAD characters (and shorter prefixes): a cheap
        # filter before the exact prefix check when holding back a stream tail.
        self._heads = {w[:k] for w in words for k in range(1, min(len(w), HEAD) + 1)}
        self.longest = len(words[0])
        self.shortest = len(words[-1])

    def __bool__(self) -> bool:
        return self._pattern is not None

    def _repl(self, m: "re.Match") -> str:
        if m.group("stub") is not None:
            return m.group(0)
        return self._stubs[m.group(0)]

    def sub(self, text: str) -> str:
        if self._pattern is None or len(text) < self.shortest:
            return text
        return self._pattern.sub(self._repl, text)

    def mask(self, obj: Any) -> Any:
        """Mask every string (and bytes) inside a JSON-like structure."""
        if self._pattern is None:
            return obj
        return self._walk(obj)

    def _walk(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.sub(o)
        if isinstance(o, dict):
            # Keys are left alone: a masked key is never substituted back, and
            # two keys could collapse into one.
            return {k: self._walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self._walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(self._walk(v) for v in o)
        if isinstance(o, (bytes, bytearray)):
            masked = self.sub(bytes(o).decode("utf-8", "surrogateescape"))
            return masked.encode("utf-8", "surrogateescape")
        return o

    # -- streams ------------------------------------------------------------

    def _could_start(self, s: str) -> bool:
        """True if ``s`` is a proper prefix of some form (more input could
        still turn it into a match, or into a longer one)."""
        i = bisect.bisect_right(self._sorted, s)
        return i < len(self._sorted) and self._sorted[i].startswith(s)

    def holdback(self, buf: str) -> int:
        """Index where the shortest-possible held-back tail starts: the
        earliest position whose suffix could still grow into a match."""
        n = len(buf)
        # A stub cut off at the end ("{{secret:ap") is held whole, so the
        # next read cannot mask a value inside its name.
        stub_at = n
        i = buf.rfind("{{", max(0, n - _STUB_MAX))
        if i >= 0 and "}}" not in buf[i:]:
            tail = buf[i:i + len(_STUB_OPEN)]
            if _STUB_OPEN.startswith(tail) or buf[i:].startswith(_STUB_OPEN):
                stub_at = i
        for i in range(max(0, n - self.longest + 1), min(n, stub_at)):
            if buf[i:i + HEAD] in self._heads and self._could_start(buf[i:]):
                return i
        return stub_at

    def sub_stream(self, buf: str, final: bool) -> tuple[str, str]:
        """Mask what is safe to emit from ``buf``; return (emitted, carry)."""
        if self._pattern is None:
            return buf, ""
        if final:
            return self.sub(buf), ""
        h = self.holdback(buf)
        out, pos = [], 0
        for m in self._pattern.finditer(buf):
            if m.start() >= h:
                break
            out.append(buf[pos:m.start()])
            out.append(self._repl(m))
            pos = m.end()
        cut = max(h, pos)
        out.append(buf[pos:cut])
        return "".join(out), buf[cut:]


EMPTY = Matcher({})


class StreamMasker:
    """Masks a chunked text (or bytes) stream, holding back only the tail that
    could still be the start of a secret split across chunks (at most the
    longest form minus one character). Call ``flush()`` at end of stream."""

    def __init__(self, matcher: Callable[[], Matcher]) -> None:
        self._matcher = matcher
        self._carry = ""
        self._decoder = None

    def feed(self, chunk: "str | bytes", final: bool = False) -> "str | bytes":
        is_bytes = isinstance(chunk, (bytes, bytearray))
        if is_bytes:
            if self._decoder is None:
                self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
            chunk = self._decoder.decode(bytes(chunk), final=final)
        out, self._carry = self._matcher().sub_stream(self._carry + chunk, final)
        return out.encode("utf-8", "surrogateescape") if is_bytes else out

    def flush(self) -> str:
        out, self._carry = self._matcher().sub_stream(self._carry, True)
        return out

    @property
    def pending(self) -> int:
        return len(self._carry)


# -- sources ---------------------------------------------------------------------

class SecretMasker:
    """A cached Matcher over a vault, rebuilt when the vault changes.

    ``source`` needs ``version()`` (any value that changes when secrets change,
    including from another process) and ``masking_values()`` ({name: value},
    read without an access-log entry: masking is not a use of the secret).
    An optional cheap ``generation()`` (changes on every in-process write)
    lets the version query run at most once per CHECK_INTERVAL."""

    _EXTRA_CACHE = 64

    def __init__(self, source: Any) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._version: Any = object()
        self._matcher = EMPTY
        self._values: dict[str, str] = {}
        self._with_extra: "OrderedDict[tuple, Matcher]" = OrderedDict()
        self._checked = float("-inf")
        self._seen_gen: Any = object()

    def _generation(self) -> Any:
        fn = getattr(self.source, "generation", None)
        try:
            return fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            return object()  # never equal: forces the full check

    def matcher(self) -> Matcher:
        now = time.monotonic()
        gen = self._generation()
        if gen == self._seen_gen and now - self._checked < CHECK_INTERVAL:
            return self._matcher
        try:
            v = self.source.version()
        except Exception:  # noqa: BLE001 — keep the last good matcher
            log.debug("vault version check failed", exc_info=True)
            return self._matcher
        self._checked, self._seen_gen = now, gen
        if v == self._version:
            return self._matcher
        with self._lock:
            if v != self._version:
                try:
                    values = self.source.masking_values()
                    self._matcher = Matcher(values)
                    self._values = values
                    self._with_extra.clear()
                    self._version = v
                    log.info("secret mask rebuilt: %d secret(s)", self._matcher.count)
                except Exception:  # noqa: BLE001 — never log the values
                    log.warning("secret mask rebuild failed (%s)", "vault read error")
        return self._matcher

    def matcher_with(self, extra: "dict[str, str] | None") -> Matcher:
        """The vault matcher plus ``extra`` values masked down to
        MIN_EXTRA_LEN, as one matcher (one pass: no stub inside a stub)."""
        base = self.matcher()
        if not extra:
            return base
        key = tuple(sorted(extra.items()))
        with self._lock:
            got = self._with_extra.get(key)
            if got is not None:
                self._with_extra.move_to_end(key)
                return got
            values = self._values
        got = Matcher(values, extra=extra)
        with self._lock:
            self._with_extra[key] = got
            while len(self._with_extra) > self._EXTRA_CACHE:
                self._with_extra.popitem(last=False)
        return got

    def mask(self, obj: Any, extra: dict[str, str] | None = None) -> Any:
        """Mask known secrets in ``obj``. ``extra`` ({name: value}) adds values
        known to be in play for this payload (e.g. those a call substituted),
        masked even when shorter than MIN_LEN."""
        return self.matcher_with(extra).mask(obj)

    def stream(self, extra: "Callable[[], dict[str, str] | None] | None" = None) -> StreamMasker:
        return StreamMasker(lambda: self.matcher_with(extra() if extra else None))


# -- process default ---------------------------------------------------------------
# One hub per process: the bridge installs its vault at startup. Stores call
# scrub() on what they write. The dashboard holds no vault: it asks the
# bridge to mask (mask_web.py, rook/remote/mask_client.py).

_installed: SecretMasker | None = None


def install(source: Any) -> SecretMasker | None:
    global _installed
    _installed = SecretMasker(source) if source is not None else None
    return _installed


def installed() -> SecretMasker | None:
    return _installed


def scrub(obj: Any, extra: "dict[str, str] | None" = None) -> Any:
    """Mask with the process's installed vault; a no-op without one. Never
    raises: a masking failure returns the object unchanged and logs (no
    values)."""
    if obj is None:
        return obj
    try:
        return current_matcher_with(extra).mask(obj)
    except Exception:  # noqa: BLE001
        log.exception("secret masking failed")
        return obj


def current_matcher() -> Matcher:
    m = _installed
    return m.matcher() if m is not None else EMPTY


def current_matcher_with(extra: "dict[str, str] | None") -> Matcher:
    m = _installed
    if m is not None:
        return m.matcher_with(extra)
    return Matcher({}, extra=extra) if extra else EMPTY


def stream(extra: "Callable[[], dict[str, str] | None] | None" = None) -> StreamMasker:
    """A stream masker that follows whatever vault is installed, plus the
    values ``extra()`` returns at each feed (masked down to MIN_EXTRA_LEN)."""
    if extra is None:
        return StreamMasker(current_matcher)
    return StreamMasker(lambda: current_matcher_with(extra()))
