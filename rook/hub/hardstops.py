"""Hard stops: the only things that ask the person before an agent acts
(knowledge change-management-design-20261025, docs/manual/trust.md).

1. **A change on a sacred machine** (kaiju): the trust map's ``sacred``
   entries, ruled in :mod:`rook.hub.trust`; reads stay free.
2. **Unrecoverable data destruction**: :data:`ERASE_CAPS`, and a shell or
   console command that wipes a disk or file system, drops a database or
   recursively deletes a top-level, home or service-data directory
   (:func:`destroys`). A command reaching a sacred machine over ssh, scp or
   rsync (:func:`remote_hosts`) counts as a change there.
3. **Revealing a secret value**: :data:`SECRET_CAPS` (a raw vault read), a
   message, post or speech whose arguments carry a vault value or a
   ``{{secret:...}}`` placeholder (:data:`OUTBOUND_CAPS`), and any command or
   file call on the hub's own vault and guardrail files.
4. **Speaking or sending as the person to other people**: :data:`SEND_AS_CAPS`.

Everything else goes and stays journaled. Anything worth more care is a
change request (rook/hub/plugins/changes), not a new stop here.

**No ratchet.** :func:`guardrail` names the calls that change the guardrails
themselves (who may do what, the approval and trust machinery, the advanced
security switch): an agent is refused, only a person makes them, on the
dashboard or by voiceprint-sure voice.

Shell matching is a best-effort net for mistakes, not a sandbox: a command
the patterns don't see still runs, and the journal shows it.
"""
from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

#: Caps that erase stored data with no copy and no undo (``cap`` or ``cap:action``).
ERASE_CAPS = ("memory.forget", "secret.delete", "vault.delete", "chat.delete", "role.delete",
              "job.write:delete")
#: Caps that put a secret's raw value outside the vault.
SECRET_CAPS = ("secret.get", "vault.get")
#: Caps whose arguments leave the band as a message, post or speech.
OUTBOUND_CAPS = ("notify.post", "notify.send", "notify.act", "voice.speak", "attention.send",
                 "attention.remind", "sms.send", "msg.send", "telegram.send", "discord.send",
                 "messages.send")
#: Caps that send as the person to someone else (``notify.act`` only with a reply text).
SEND_AS_CAPS = ("sms.send", "messages.send", "notify.act")
#: Shell and console caps whose command text is checked by :func:`destroys`.
SHELL_CAPS = ("shell.exec", "proc.start", "proc.spawn", "work.stream.open",
              "terminals.stream.open")
TYPED_CAPS = ("proc.write", "work.stream.write", "console.write", "terminals.stream.write",
              "stream.write")

#: Guardrail caps (an agent never calls them; ``cap`` or ``cap:<arg glob>``).
GUARDRAIL_CAPS = ("policy.set", "trust.tighten", "approvals.grant", "approvals.set",
                  "relay.set", "member.*", "token.*", "tokens.*", "guidance.write")
#: Settings keys that are guardrails (settings.set / reset by an agent).
GUARDRAIL_KEYS = ("core.approvals.*", "approvals.*", "security.*", "core.plugins.hub",
                  "trust.*", "core.policy.*", "huddle.vote_*", "role.protected_workers")
#: The hub plugin that carries the advanced guardrails.
SECURITY_PLUGIN = "security"

_DESTROY = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a file system"),
    (re.compile(r"\b(wipefs|blkdiscard|shred)\b"), "wipes a disk or file"),
    (re.compile(r"\bdd\b[^;&|\n]*\bof=/dev/"), "writes over a disk"),
    (re.compile(r"\b(fdisk|sfdisk|parted|sgdisk|gdisk)\b[^;&|\n]*/dev/"), "repartitions a disk"),
    (re.compile(r"\b(zpool\s+(destroy|labelclear)|zfs\s+destroy|lvremove|vgremove|pvremove)\b"),
     "destroys a storage pool or volume"),
    (re.compile(r"(?i)\bdrop\s+(database|schema|table)\b|\btruncate\s+table\b|\bdropdb\b"),
     "drops a database or table"),
    (re.compile(r"\b(docker|podman)\s+(volume\s+(rm|prune)|system\s+prune)\b"),
     "deletes container volumes"),
    (re.compile(r"\brclone\s+(purge|delete)\b"), "deletes remote storage"),
]
#: Where a program name may start: the text's start, a space, a shell operator,
#: a quote, a backtick or $( (``bash -c "ssh kaiju ..."``), with or without a
#: path (``/usr/bin/ssh``).
_LEAD = r"(?:^|[\s;&|(`'\"=\\])(?:[^\s;&|(`'\"=\\]*/)?"
_RM = re.compile(_LEAD + r"rm\s+([^;&|\n`]*)")
_FIND_DELETE = re.compile(r"\bfind\s+(\S+)[^;&|\n]*\s-delete\b")
#: Recursive deletes of these (and of anything one level under /) never go by
#: themselves: ``/``, a top-level directory, a home, a service's data.
_ROOTISH = re.compile(r"^(/[^/]*|/home/[^/]+|/root|/var/lib/[^/]+|/var/backups(/[^/]+)?|"
                      r"/mnt/[^/]+|/media/[^/]+|/srv/[^/]+|/opt/[^/]+|~|~/?\*?|\$HOME|\$\{HOME\}|"
                      r"\$HOME/\*|~/[^/]+)/?\*?$")


#: The hub's own secrets and guardrail files: a command or file call that
#: names one reads a secret or changes the guardrails behind the hub's back.
_HUB_FILES = re.compile(r"(vault\.(key|db)|trust(-history)?\.json(l)?|policy\.json|approvals\.db|"
                        r"settings\.db)\b")
_REMOTE = re.compile(_LEAD + r"(ssh|scp|rsync|sftp|mosh)\s+([^;&|\n`]*)")
#: ssh options that take a value.
_SSH_ARG = set("bcDEeFIiJLlmOopQRSWw")


def _match(sel: str, cap: str, action: str = "") -> bool:
    pat, _, act = sel.partition(":")
    if not fnmatch.fnmatchcase(cap, pat):
        return False
    return not act or fnmatch.fnmatchcase(action, act)


def _any(sels: tuple, cap: str, action: str = "") -> bool:
    return any(_match(s, cap, action) for s in sels)


def command_text(cap: str, args: Any) -> str:
    """The command a shell or console call would run ("" for other caps)."""
    if not isinstance(args, dict):
        return ""
    if cap in TYPED_CAPS or cap.endswith(".stream.write"):
        return str(args.get("data") or args.get("text") or "")
    if cap not in SHELL_CAPS:
        return ""
    for key in ("argv", "cmd", "command"):
        v = args.get(key)
        if isinstance(v, list) and v:
            return " ".join(str(x) for x in v)
        if isinstance(v, str) and v.strip():
            return v
    return ""


_DB_FILE = re.compile(r"\.(db|sqlite3?|kdbx)(-wal|-shm|-journal)?['\"]?$")


def _rm_rootish(rest: str) -> bool:
    words = [w.strip("'\"`()") for w in rest.split()]
    if any(_DB_FILE.search(w) and not w.strip("'\"").startswith(("/tmp/", "/var/tmp/"))
           for w in words if not w.startswith("-")):
        return True                                  # a database file, recursive or not
    flags = "".join(w[1:] for w in words if w.startswith("-") and not w.startswith("--"))
    recursive = "r" in flags.lower() or "--recursive" in words
    if not recursive:
        return False
    return any(_ROOTISH.match(w.strip("'\"")) for w in words if not w.startswith("-"))


def remote_hosts(text: str) -> set[str]:
    """Machines a command reaches over ssh, scp, rsync or sftp (lower-case,
    without the user): a change sent there is a change on that machine."""
    out: set[str] = set()
    for m in _REMOTE.finditer(str(text or "")):
        prog, words = m.group(1), m.group(2).split()
        out |= remote_hosts(m.group(2))          # a hop inside: ssh hop "ssh kaiju ..."
        words = [w.strip("'\"`()") for w in words]
        if prog in ("ssh", "mosh"):
            skip = jump = False
            for w in words:
                if skip:
                    skip = False
                    if jump:                         # -J kaiju,user@other:22
                        out |= {h.split("@")[-1].split(":")[0].lower() for h in w.split(",")}
                    continue
                if w.startswith("-") and not w.startswith("--"):
                    # -p 22, -tp 22 (grouped: the last letter takes the value), -p22 (glued)
                    letters = w[1:]
                    skip = bool(letters) and letters[-1] in _SSH_ARG and \
                        all(c not in _SSH_ARG for c in letters[:-1])
                    jump = skip and letters[-1] == "J"
                    continue
                if w.startswith("--"):
                    continue
                out.add(w.split("@")[-1].lower())
                break
        else:
            out |= {w.split(":", 1)[0].split("@")[-1].lower() for w in words
                    if ":" in w and not w.startswith("-") and "://" not in w}
    return {h for h in out if h}


def destroys(text: str) -> str:
    """Why a command destroys data for good ("" when it doesn't look like it)."""
    t = str(text or "")
    if not t:
        return ""
    for rx, why in _DESTROY:
        if rx.search(t):
            return why
    for m in _RM.finditer(t):
        if _rm_rootish(m.group(1)):
            return "deletes a database, or a top-level, home or data directory"
    for m in _FIND_DELETE.finditer(t):
        if _ROOTISH.match(m.group(1).strip("'\"")):
            return "deletes files under a top-level, home or data directory"
    return ""


def _carries_secret(args: Any) -> bool:
    from ..band_mcp.secret_mask import STUB, scrub
    try:
        raw = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    if STUB.search(raw):
        return True
    try:
        return json.dumps(scrub(args), sort_keys=True, default=str) != raw
    except Exception:  # noqa: BLE001 - no masker in this process: placeholders only
        return False


def stop(cap: str, args: Any, tags: tuple | list = ()) -> str:
    """The hard stop a call hits, as the reason shown to the person ("" for
    none). Sacred machines are the trust map's (rook/hub/trust.py)."""
    cap = str(cap or "")
    args = args if isinstance(args, dict) else {}
    action = str(args.get("action") or "")
    if _any(ERASE_CAPS, cap, action):
        return "hard stop: it erases data for good"
    if cap == "deluge.remove" and args.get("remove_data"):
        return "hard stop: it erases data for good"
    text = command_text(cap, args)
    why = destroys(text)
    if why:
        return f"hard stop: it {why}"
    paths = " ".join(str(args.get(k) or "") for k in ("path", "dest", "src")) \
        if cap.startswith("file.") else ""
    if _HUB_FILES.search(text) or _HUB_FILES.search(paths):
        return "hard stop: it touches the hub's own secrets or guardrails"
    if cap in SECRET_CAPS:
        return "hard stop: it reveals a secret's value"
    if cap in OUTBOUND_CAPS and _carries_secret(args):
        return "hard stop: the message would reveal a secret's value"
    if cap in SEND_AS_CAPS and (cap != "notify.act" or str(args.get("text") or "").strip()):
        return "hard stop: it sends a message as you to someone else"
    return ""


def guardrail(cap: str, args: Any) -> str:
    """Why a call changes the guardrails ("" when it doesn't): refused for
    agents, a person's alone (no ratchet, either way)."""
    cap = str(cap or "")
    args = args if isinstance(args, dict) else {}
    if _any(GUARDRAIL_CAPS, cap, str(args.get("action") or "")):
        return f"{cap} changes the guardrails"
    if cap in ("settings.set", "settings.reset"):
        key = str(args.get("key") or "")
        if any(fnmatch.fnmatchcase(key, k) for k in GUARDRAIL_KEYS):
            return f"setting {key} is a guardrail"
    if cap.startswith("hub.plugin.") and str(args.get("module") or "") == SECURITY_PLUGIN:
        return "the advanced security plugin is switched by a person"
    return ""
