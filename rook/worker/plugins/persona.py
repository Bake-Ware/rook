"""persona.apply / persona.status: install the band's persona in harness files.

Writes one managed, clearly delimited block into a harness instruction file
and never touches anything outside it:

    <!-- rook:persona:begin profile=<id> rev=<n> sha=<hash> (managed by Rook; edits here are replaced) -->
    ...rendered persona...
    <!-- rook:persona:end -->

| harness       | default file                                  |
|---------------|-----------------------------------------------|
| claude-code   | ``$CLAUDE_CONFIG_DIR/CLAUDE.md`` or ``~/.claude/CLAUDE.md`` |
| codex         | ``$CODEX_HOME/AGENTS.md`` or ``~/.codex/AGENTS.md`` |
| hermes        | ``$HERMES_HOME/SOUL.md`` or ``~/.hermes/SOUL.md`` |

``path`` may name a project file instead (absolute, and its file name must be
``CLAUDE.md``, ``AGENTS.md`` or ``SOUL.md``). Applying again with the same
text changes nothing; ``dry_run`` returns the diff; ``remove`` takes the block
(and the blank line that separated it) out again. A file with a damaged or
repeated marker pair is refused rather than guessed at.

The text comes from ``content`` when given, else from the hub
(``persona.render`` on worker ``rook``, a read cap callable over the band).
Stdlib only: this ships in the worker bundle.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from ..plugin import Plugin, capability

log = logging.getLogger("rook.worker.plugins.persona")

BEGIN = "<!-- rook:persona:begin"
END = "<!-- rook:persona:end -->"
# A marker is a whole line; the match stops before the line ending (LF or CRLF).
_BEGIN_RE = re.compile(r"^<!-- rook:persona:begin\b[^\r\n]*-->[ \t]*(?=\r?$)", re.M)
_END_RE = re.compile(r"^<!-- rook:persona:end -->[ \t]*(?=\r?$)", re.M)
_ATTR = re.compile(r"\b(profile|rev|sha)=([A-Za-z0-9_.-]+)")
FILES = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md", "hermes": "SOUL.md"}
ALIASES = {"claude": "claude-code", "claudecode": "claude-code"}
ALLOWED_NAMES = frozenset(FILES.values())
MAX_CONTENT = 16 * 1024
MAX_FILE = 1024 * 1024
SILENT_BACKOFF = 600.0
_silent_until = 0.0


class MarkerError(ValueError):
    pass


def harness_name(harness: str) -> str:
    h = (harness or "").strip().lower()
    h = ALIASES.get(h, h)
    if h not in FILES:
        raise ValueError(f"harness must be one of {', '.join(FILES)}")
    return h


def default_path(harness: str, environ=None) -> Path:
    env = os.environ if environ is None else environ
    h = harness_name(harness)
    home = {"claude-code": env.get("CLAUDE_CONFIG_DIR") or "~/.claude",
            "codex": env.get("CODEX_HOME") or "~/.codex",
            "hermes": env.get("HERMES_HOME") or "~/.hermes"}[h]
    return Path(os.path.expanduser(home)) / FILES[h]


def check_path(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        raise ValueError("path must be absolute")
    if p.name not in ALLOWED_NAMES:
        raise ValueError(f"path must name one of {', '.join(sorted(ALLOWED_NAMES))}")
    return p


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def find_block(text: str) -> tuple[int, int] | None:
    """(start, end) offsets of the managed block including its marker lines,
    ``None`` when absent. Raises :class:`MarkerError` on damaged markers."""
    begins = list(_BEGIN_RE.finditer(text))
    ends = list(_END_RE.finditer(text))
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0].start() < begins[0].end():
        raise MarkerError(f"found {len(begins)} begin and {len(ends)} end persona markers; "
                          "fix the file by hand (or remove the stray marker lines)")
    return begins[0].start(), ends[0].end()


def block_attrs(text: str) -> dict:
    m = _BEGIN_RE.search(text)
    return dict(_ATTR.findall(m.group(0))) if m else {}


def make_block(content: str, profile: str = "", rev: str | int = "", nl: str = "\n") -> str:
    body = content.replace("\r\n", "\n").strip("\n")
    attrs = " ".join(f"{k}={v}" for k, v in (("profile", profile), ("rev", rev),
                                              ("sha", fingerprint(body))) if v not in ("", None))
    lines = [f"{BEGIN} {attrs} (managed by Rook; edits here are replaced) -->", body, END]
    return "\n".join(lines).replace("\n", nl)


def upsert(text: str, block: str) -> str:
    """``text`` with ``block`` in place of the managed block, or appended
    after a blank line. Everything outside the block is kept byte for byte."""
    span = find_block(text)
    if span is not None:
        return text[:span[0]] + block + text[span[1]:]
    nl = "\r\n" if "\r\n" in text else "\n"
    if not text:
        return block + nl
    sep = "" if text.endswith(nl) else nl
    return text + sep + nl + block + nl


def strip(text: str) -> str:
    """``text`` without the managed block and the separator :func:`upsert` added."""
    span = find_block(text)
    if span is None:
        return text
    start, end = span
    nl = "\r\n" if "\r\n" in text else "\n"
    before, after = text[:start], text[end:]
    if after.startswith(nl):
        after = after[len(nl):]
    if not after and before.endswith(nl + nl):
        before = before[:-len(nl)]
    elif before.endswith(nl + nl) and after.startswith(nl):
        after = after[len(nl):]
    return before + after


def _write(path: Path, text: str) -> None:
    """Atomic replace that keeps the file's mode and follows a symlink."""
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = real.stat().st_mode & 0o777 if real.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".rook-persona-", dir=str(real.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=str(path), tofile=str(path)))[:8000]


def plan(path: Path, content: str | None, *, remove: bool = False, profile: str = "",
         rev: str | int = "") -> dict:
    """What applying would do: ``{action, old, new}`` (no writes)."""
    exists = path.exists()
    if exists and path.stat().st_size > MAX_FILE:
        raise ValueError(f"{path} is larger than {MAX_FILE} bytes")
    # Bytes, not text mode: universal newlines would turn CRLF files into LF.
    old = path.read_bytes().decode("utf-8") if exists else ""
    if remove:
        new = strip(old)
        action = "remove" if new != old else "absent"
    else:
        if not (content or "").strip():
            raise ValueError("no persona text to apply (nothing assigned on the hub?)")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"persona text is longer than {MAX_CONTENT} characters")
        nl = "\r\n" if "\r\n" in old else "\n"
        new = upsert(old, make_block(content, profile, rev, nl))
        action = "unchanged" if new == old else ("update" if find_block(old) else
                                                 ("create" if not exists else "insert"))
    return {"action": action, "old": old, "new": new, "exists": exists}


async def fetch_persona(worker, harness: str, profile: str = "",
                        timeout: float = 5.0, backoff: bool = True) -> dict | None:
    """Ask the hub (``persona.render``) for the persona text; ``None`` when
    there is no worker, no hub answer or an error. With ``backoff`` (work
    launches) a silent hub is not asked again for ``SILENT_BACKOFF`` s."""
    global _silent_until
    request = getattr(worker, "request", None)
    if request is None or (backoff and time.monotonic() < _silent_until):
        return None
    args = {"harness": harness}
    if profile:
        args["profile"] = profile
    try:
        reply = await request("persona.render", args, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — timeouts, closed transport
        # A hub without the persona plugin never answers: don't make every
        # launch wait for that again for a while.
        _silent_until = time.monotonic() + SILENT_BACKOFF
        log.info("persona.render from the hub failed: %s", type(e).__name__)
        return None
    if not isinstance(reply, dict) or not reply.get("ok"):
        log.info("persona.render refused: %s", (reply or {}).get("error"))
        return None
    res = reply.get("result")
    return res if isinstance(res, dict) else None


class PersonaFiles(Plugin):
    NAMESPACE = "persona"

    def __init__(self) -> None:
        super().__init__()
        self._worker = None

    def bind_worker(self, worker) -> None:
        self._worker = worker

    def _target(self, harness: str, path: str) -> Path:
        h = harness_name(harness)
        return check_path(path) if path else default_path(h)

    @capability("apply", risk="write")
    async def apply(self, harness: str, path: str = "", content: str | None = None,
                    profile: str = "", remove: bool = False, dry_run: bool = False) -> dict:
        """Write (or with ``remove=true`` take out) the persona's managed block
        in a harness file: claude-code -> ~/.claude/CLAUDE.md, codex ->
        ~/.codex/AGENTS.md, hermes -> ~/.hermes/SOUL.md, or ``path`` (a
        project CLAUDE.md/AGENTS.md/SOUL.md). Text outside the markers is
        never touched. ``content`` overrides the hub's ``persona.render``;
        ``profile`` picks a profile there. ``dry_run`` returns the diff only."""
        h = harness_name(harness)
        target = self._target(h, path)
        rev = ""
        if not remove and content is None:
            got = await fetch_persona(self._worker, h, profile, backoff=False)
            if got is None:
                raise ValueError("could not get the persona from the hub (persona.render on "
                                 "worker rook); pass content= to apply text directly")
            content, profile, rev = got.get("text") or "", got.get("profile") or "", got.get("rev") or ""
        p = plan(target, content, remove=remove, profile=profile, rev=rev)
        out = {"ok": True, "harness": h, "path": str(target), "action": p["action"],
               "dry_run": bool(dry_run)}
        if p["action"] in ("unchanged", "absent"):
            return out
        out["diff"] = _diff(p["old"], p["new"], target)
        if dry_run:
            return out
        if remove and not p["new"]:
            os.unlink(os.path.realpath(target))
            out["deleted_empty_file"] = True
        else:
            _write(target, p["new"])
        return out

    @capability("status", risk="read")
    def status(self, harness: str = "", path: str = "") -> list[dict]:
        """Whether each harness file holds a persona block, with its profile,
        rev and hash. ``harness``/``path`` narrow it to one file."""
        targets = [(harness_name(harness), self._target(harness, path))] if (harness or path) \
            else [(h, default_path(h)) for h in FILES]
        out = []
        for h, p in targets:
            row = {"harness": h, "path": str(p), "exists": p.exists(), "block": False}
            if p.exists():
                try:
                    text = p.read_bytes().decode("utf-8")
                    row["block"] = find_block(text) is not None
                    row.update(block_attrs(text))
                except MarkerError as e:
                    row["error"] = str(e)
                except OSError as e:
                    row["error"] = type(e).__name__
            out.append(row)
        return out


PLUGIN = PersonaFiles
