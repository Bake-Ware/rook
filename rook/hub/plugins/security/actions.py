"""What the house agent treats as not easily undone (advanced security):
moved from rook/hub/plugins/home/front.py. With the plugin off the house
agent asks only for the hub's hard stops (rook/hub/hardstops.py)."""
from __future__ import annotations

import re
from typing import Any

_GONE = re.compile(r"(?:^|[._-])(delete|remove|rm|drop|purge|wipe|erase|format|destroy|reset|"
                   r"uninstall|deauth|revoke|kill|truncate|prune|forget)(?:$|[._-])", re.I)
_GONE_ARGV = re.compile(r"^(rm|rmdir|dd|mkfs\S*|shred|wipefs|fdisk|parted|shutdown|reboot|"
                        r"poweroff|halt|kill|killall|pkill|truncate)$")


#: Cap families that carry messages to other people: anything in them that is not a
#: plain read speaks for the person (ask first, on any level). Review of #188.
_MESSAGING = {"sms", "mms", "mail", "email", "gmail", "outlook", "msg", "message", "messages",
              "signal", "discord", "slack", "telegram", "whatsapp", "matrix", "messenger",
              "imessage", "teams", "chat_app", "call", "phone", "dialer"}
_READS = re.compile(r"^(list|get|read|search|status|describe|info|history|threads?|unread)"
                    r"(_\w+)?$")
#: Any cap, any family, whose action sends, replies, forwards, calls or dials.
_SPEAKS = re.compile(r"(^|[._])(send|reply|forward|respond|call|dial|post_to|message)"
                     r"(_\w+)?$")
#: A notification's own actions that change nothing for anyone else: these alone may run
#: without asking, and only when named (an index can't be checked here).
SAFE_NOTIFY_ACTIONS = {"mark as read", "mark read", "dismiss", "snooze"}

def speaks_for(cap: str, args: Any) -> str:
    """Why a call speaks or acts for the person towards others ("" when it doesn't)."""
    family, _, action = str(cap or "").partition(".")
    if cap == "notify.act":
        title = " ".join(str((args or {}).get("action") or "").lower().split()) \
            if isinstance(args, dict) else ""
        text = str((args or {}).get("text") or "").strip() if isinstance(args, dict) else ""
        if text or title not in SAFE_NOTIFY_ACTIONS:
            return "a notification's own action can reply, delete, archive or call as you"
        return ""
    if family in _MESSAGING and not _READS.match(action):
        return f"{cap} sends a message to someone as you"
    if _SPEAKS.search(action) and family not in ("notify", "voice", "chat", "huddle", "attention"):
        return f"{cap} sends a message to someone as you"
    return ""


def irreversible(cap: str, kind: str, tags: tuple | list, args: Any) -> str:
    """Why an action is not easily undone ("" when it looks reversible): an
    admin or destructive cap, a cap named for removing something, or a shell
    command that deletes, wipes, kills or powers off."""
    if kind == "admin":
        return f"{cap} is {'destructive' if 'destructive' in (tags or ()) else 'admin'}"
    spoken = speaks_for(cap, args)
    if spoken:
        # Texts, emails, calls, notification actions: they act for the person towards others.
        return spoken
    if _GONE.search(cap.split(".", 1)[-1]):
        return f"{cap} removes something"
    if isinstance(args, dict):
        argv = args.get("argv") or args.get("cmd") or args.get("command")
        words = argv if isinstance(argv, list) else str(argv or "").split()
        words = [str(w).rsplit("/", 1)[-1] for w in words if str(w).strip()]
        if words and words[0] == "sudo":
            words = words[1:]
        if words and _GONE_ARGV.match(words[0]):
            return f"`{' '.join(words[:3])}` is not easily undone"
        if words[:2] in (["git", "push"], ["git", "reset"]) and ("--force" in words or "-f" in words
                                                                 or "--hard" in words):
            return "a force push or hard reset is not easily undone"
    return ""
