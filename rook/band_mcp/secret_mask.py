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
  see ``SecretMasker.mask(extra=...)``.)
* Cheap encodings of each value: JSON-escaped once and twice (a value inside
  JSON inside JSON), base64 (standard and URL-safe, padded and not) of the
  whole value, and URL/percent encoding (``quote``, ``quote_plus``).
* Leftmost match wins, and at one position the longest form wins, so a secret
  that contains another is masked as the longer one.
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
import os
import re
import sqlite3
import threading
from typing import Any, Callable
from urllib.parse import quote, quote_plus

log = logging.getLogger("rook.band_mcp.secret_mask")

MIN_LEN = 8          # raw values shorter than this are not masked everywhere
MIN_EXTRA_LEN = 4    # values a call used explicitly are masked down to this
HEAD = 16            # prefix length indexed for the stream hold-back check


def stub(name: str) -> str:
    return "{{secret:%s}}" % name


def forms_for(value: str) -> list[str]:
    """The value plus the encodings we look for, longest first."""
    if not isinstance(value, str) or not value:
        return []
    forms = {value}
    raw = value.encode("utf-8", "surrogatepass")
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        b = enc(raw).decode("ascii")
        forms.add(b)
        forms.add(b.rstrip("="))
    for q in (quote(value, safe=""), quote(value), quote_plus(value)):
        forms.add(q)
    # JSON-escaped once and twice (a worker's stdout that itself holds JSON,
    # stored again as JSON).
    for _ in range(2):
        forms |= ({json.dumps(f)[1:-1] for f in forms}
                  | {json.dumps(f, ensure_ascii=False)[1:-1] for f in forms})
    return sorted((f for f in forms if f), key=len, reverse=True)


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
    """An immutable matcher over one snapshot of {name: value}."""

    def __init__(self, secrets: dict[str, str], min_len: int = MIN_LEN) -> None:
        names: dict[str, str] = {}
        # Sorted by name so a value stored under two names always maps to the
        # same (first) one.
        for name in sorted(secrets):
            value = secrets[name]
            if not isinstance(value, str) or len(value) < min_len:
                continue
            for f in forms_for(value):
                names.setdefault(f, name)
        self._names = names
        self.count = len({n for n in names.values()})
        self._pattern = None
        self._sorted: list[str] = []
        self._heads: set[str] = set()
        self.longest = 0
        self.shortest = 0
        if not names:
            return
        words = sorted(names, key=len, reverse=True)
        try:
            self._pattern = re.compile(_trie_regex(words))
        except (RecursionError, re.error, OverflowError):
            self._pattern = re.compile("|".join(re.escape(w) for w in words))
        self._sorted = sorted(words)
        # Every form's first HEAD characters (and shorter prefixes): a cheap
        # filter before the exact prefix check when holding back a stream tail.
        self._heads = {w[:k] for w in words for k in range(1, min(len(w), HEAD) + 1)}
        self.longest = len(words[0])
        self.shortest = len(words[-1])

    def __bool__(self) -> bool:
        return self._pattern is not None

    def _repl(self, m: "re.Match") -> str:
        return stub(self._names[m.group(0)])

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
            return {(self.sub(k) if isinstance(k, str) else k): self._walk(v) for k, v in o.items()}
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
        for i in range(max(0, n - self.longest + 1), n):
            if buf[i:i + HEAD] in self._heads and self._could_start(buf[i:]):
                return i
        return n

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

    ``source`` needs ``version()`` (any value that changes when secrets change)
    and ``masking_values()`` ({name: value}, read without an access-log entry:
    masking is not a use of the secret)."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._version: Any = object()
        self._matcher = EMPTY

    def matcher(self) -> Matcher:
        try:
            v = self.source.version()
        except Exception:  # noqa: BLE001 — keep the last good matcher
            log.debug("vault version check failed", exc_info=True)
            return self._matcher
        if v == self._version:
            return self._matcher
        with self._lock:
            if v != self._version:
                try:
                    self._matcher = Matcher(self.source.masking_values())
                    self._version = v
                    log.info("secret mask rebuilt: %d secret(s)", self._matcher.count)
                except Exception:  # noqa: BLE001 — never log the values
                    log.warning("secret mask rebuild failed (%s)", "vault read error")
        return self._matcher

    def mask(self, obj: Any, extra: dict[str, str] | None = None) -> Any:
        """Mask known secrets in ``obj``. ``extra`` ({name: value}) adds values
        known to be in play for this payload (e.g. those a call substituted),
        masked even when shorter than MIN_LEN."""
        obj = self.matcher().mask(obj)
        if extra:
            obj = Matcher(extra, min_len=MIN_EXTRA_LEN).mask(obj)
        return obj

    def stream(self) -> StreamMasker:
        return StreamMasker(self.matcher)


class VaultReader:
    """Read-only view of a hub vault (vault.db + vault.key) for a process that
    does not own it (the dashboard). Never creates a key or a database."""

    def __init__(self, path: str, key_path: str | None = None) -> None:
        import nacl.secret
        key_path = key_path or os.path.join(os.path.dirname(path) or ".", "vault.key")
        with open(key_path, "rb") as f:
            key = f.read()
        self._box = nacl.secret.SecretBox(key)
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self._lock = threading.Lock()

    def version(self):
        with self._lock:
            return self._db.execute(
                "SELECT count(*), max(updated), total(updated) FROM secrets").fetchone()

    def masking_values(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT name, value FROM secrets").fetchall()
        return {n: self._box.decrypt(v).decode() for n, v in rows}


# -- process default ---------------------------------------------------------------
# One hub per process: the bridge installs its vault at startup, the dashboard
# a read-only view of the same files. Stores call scrub() on what they write.

_installed: SecretMasker | None = None


def install(source: Any) -> SecretMasker | None:
    global _installed
    _installed = SecretMasker(source) if source is not None else None
    return _installed


def install_from_dir(state_dir: str) -> SecretMasker | None:
    """Install a read-only view of ``state_dir``/vault.db if it exists and is
    readable; otherwise leave masking as it is."""
    path = os.path.join(state_dir or ".", "vault.db")
    if not (os.path.exists(path) and os.path.exists(os.path.join(state_dir or ".", "vault.key"))):
        return _installed
    try:
        return install(VaultReader(path))
    except Exception as e:  # noqa: BLE001
        log.warning("secret masking unavailable in this process: %s", type(e).__name__)
        return _installed


def installed() -> SecretMasker | None:
    return _installed


def scrub(obj: Any) -> Any:
    """Mask with the process's installed vault; a no-op without one. Never
    raises: a masking failure returns the object unchanged and logs (no
    values)."""
    m = _installed
    if m is None or obj is None:
        return obj
    try:
        return m.mask(obj)
    except Exception:  # noqa: BLE001
        log.exception("secret masking failed")
        return obj


def current_matcher() -> Matcher:
    m = _installed
    return m.matcher() if m is not None else EMPTY


def stream() -> StreamMasker:
    """A stream masker that follows whatever vault is installed."""
    return StreamMasker(current_matcher)
