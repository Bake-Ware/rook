"""Rook's Claude Code hook: the command Rook puts in Claude Code's settings.

The hub's Claude Code rules (Manage > Claude Code, docs/design/ccmod.md) are
pushed to each worker, which writes them to ``<worker state>/ccmod/rules.json``
and adds one hook entry per event to each Claude Code installation's
``settings.json``. Each entry runs this file with the worker's Python
(``-I -S``: no site, nothing from the current folder) and the event name::

    "<python>" -I -S "<worker state>/ccmod/rook-ccmod-hook.py" PreToolUse

It is copied out of the worker bundle, so it imports nothing from Rook:
stdlib only. The hub reuses its matching and template code
(``rook.worker.ccmod_hook``), so a rule matches the same way everywhere.

What one run does:

1. Reads the event Claude Code sends on stdin and the rules file.
2. Finds the rules whose trigger matches (event, tool matcher, text, and a
   project or agent scope).
3. Applies the *local* actions itself and prints Claude Code's hook reply:
   ``block`` (deny a tool call or a prompt), ``inject`` (add context, or on
   Stop, ask Claude to carry on) and ``statusline`` (the text the status
   line command shows for this session).
4. Hands the event to the worker when a matched rule has *remote* actions
   (post to a room, notify, run a capability). Those run on the hub, under
   the hub's rules and policy, never here. It goes over the worker's local
   shim socket (owner-only) and, when that is not there, as a file in the
   worker's ``ccmod/outbox`` folder.

It never fails a hook: any error ends the run quietly with exit status 0,
so Claude Code carries on as if the hook were not there.
"""

import json
import os
import re
import socket
import struct
import sys
import time

VERSION = 1
MARK = "rook-ccmod-hook"
SCRIPT_NAME = MARK + ".py"

#: Claude Code hook events a rule may trigger on.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "SubagentStop", "PreCompact", "SessionEnd")
#: Reported by the worker, not by Claude Code: a session waiting on the person.
IDLE = "Idle"
#: Events whose hook entry takes a tool-name matcher.
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
#: The events the worker needs to see to tell when a session went idle.
ACTIVITY = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
#: Local actions per event, and what each turns into in the hook's reply.
BLOCKABLE = ("PreToolUse", "UserPromptSubmit")
INJECTABLE = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop")

FIELDS = ("worker", "band", "event", "tool", "project", "cwd", "session", "agent",
          "prompt", "message", "minutes")
TEXT_CLIP = 300
MAX_STDIN = 1 << 20
MAX_FRAME = 1 << 20
CONNECT_TIMEOUT = 0.2
_TEMPLATE = re.compile(r"\{(" + "|".join(FIELDS) + r")\}")
_SESSION_FILE = re.compile(r"[^A-Za-z0-9_-]")


def clip(text, room=TEXT_CLIP):
    text = "" if text is None else str(text)
    return text if len(text) <= room else text[:room] + "..."


def render(template, ctx):
    """``{field}`` placeholders filled from ``ctx``; other braces are kept."""
    return _TEMPLATE.sub(lambda m: clip(ctx.get(m.group(1), ""), TEXT_CLIP), str(template or ""))


def project_of(cwd):
    """The folder name of the git checkout holding ``cwd``, else of ``cwd``."""
    if not cwd:
        return ""
    path = os.path.abspath(cwd)
    probe = path
    for _ in range(40):
        if os.path.exists(os.path.join(probe, ".git")):
            return os.path.basename(probe) or probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return os.path.basename(path.rstrip("/\\")) or path


def _parts(path):
    return [p for p in re.split(r"[/\\]+", path or "") if p]


def in_scope(scope, ctx):
    """Project and agent scopes are checked where the event happens; band
    and worker scopes were applied by the hub when it built this worker's
    rules, so they pass here."""
    scope = scope if isinstance(scope, dict) else {}
    kind, target = scope.get("kind") or "all", str(scope.get("target") or "")
    if kind == "project":
        if not target:
            return False
        cwd = str(ctx.get("cwd") or "")
        if os.path.isabs(target) or target.startswith("~"):
            base = os.path.expanduser(target).rstrip("/\\")
            return cwd == base or cwd.startswith(base + "/") or cwd.startswith(base + "\\")
        return ctx.get("project") == target or target in _parts(cwd)
    if kind == "agent":
        return bool(target) and ctx.get("agent") == target
    return True


def matches(rule, event, ctx, data=None):
    """Whether ``rule`` fires for this event (``data``: the raw hook input)."""
    trig = rule.get("trigger") if isinstance(rule, dict) else None
    if not isinstance(trig, dict) or trig.get("event") != event:
        return False
    if rule.get("enabled") is False:
        return False
    matcher = str(trig.get("matcher") or "").strip()
    if matcher and matcher != "*" and event in TOOL_EVENTS:
        try:
            if not re.fullmatch(matcher, str(ctx.get("tool") or "")):
                return False
        except re.error:
            return False
    needle = str(trig.get("contains") or "").strip().lower()
    if needle:
        if event in TOOL_EVENTS:
            hay = json.dumps((data or {}).get("tool_input") or {}, ensure_ascii=False)
        elif event == "Notification":
            hay = str(ctx.get("message") or "")
        else:
            hay = str((data or {}).get("prompt") or ctx.get("prompt") or "")
        if needle not in hay.lower():
            return False
    return in_scope(rule.get("scope"), ctx)


def context(data, event, cfg, environ=None):
    """The template fields of one event."""
    env = os.environ if environ is None else environ
    cwd = str(data.get("cwd") or "")
    return {"worker": str(cfg.get("worker") or ""), "band": str(cfg.get("band") or ""),
            "event": event, "tool": str(data.get("tool_name") or ""),
            "project": project_of(cwd), "cwd": cwd,
            "session": str(data.get("session_id") or "")[:64],
            "agent": str(env.get("ROOK_AGENT") or "")[:120],
            "prompt": clip(data.get("prompt")), "message": clip(data.get("message")),
            "minutes": ""}


def reply(event, rules, ctx, data):
    """Claude Code's hook reply for the local actions of the matched rules
    (``None``: nothing to say)."""
    blocks, injects = [], []
    for r in rules:
        for a in r.get("local") or ():
            kind = a.get("type")
            if kind == "block" and event in BLOCKABLE:
                blocks.append(render(a.get("message") or "Blocked by a Rook rule.", ctx))
            elif kind == "inject" and event in INJECTABLE:
                injects.append(render(a.get("text"), ctx))
    blocks = [b for b in blocks if b.strip()]
    injects = [i for i in injects if i.strip()]
    if blocks and event == "PreToolUse":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "\n".join(blocks)}}
    if blocks and event == "UserPromptSubmit":
        return {"decision": "block", "reason": "\n".join(blocks)}
    if not injects:
        return None
    text = "\n\n".join(injects)
    if event in ("Stop", "SubagentStop"):
        # A Stop reply that blocks asks Claude to keep going with the reason as
        # its next instruction. Once is enough: Claude Code marks the second
        # Stop of that chain with stop_hook_active, and then we let it stop.
        if data.get("stop_hook_active"):
            return None
        return {"decision": "block", "reason": text}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def statuses(rules, ctx):
    """The status line text the matched rules set (last one wins)."""
    text = None
    for r in rules:
        for a in r.get("local") or ():
            if a.get("type") == "statusline":
                text = render(a.get("text"), ctx)
    return text


def session_file(base, session):
    name = _SESSION_FILE.sub("", session or "")[:64] or "default"
    return os.path.join(base, "status", name + ".txt")


def _write(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def forward(cfg, payload, base):
    """Give the event to the worker: its shim socket, else the outbox."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        return False
    sock_path = cfg.get("socket")
    if sock_path and hasattr(socket, "AF_UNIX") and os.path.exists(sock_path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(sock_path)
            s.sendall(b"J" + struct.pack(">I", len(data)) + data)
            try:
                s.recv(64)  # the worker's short ack; not needed to succeed
            except OSError:
                pass
            return True
        except OSError:
            pass
        finally:
            s.close()
    outbox = cfg.get("outbox") or os.path.join(base, "outbox")
    try:
        os.makedirs(outbox, mode=0o700, exist_ok=True)
        name = "%d-%d.json" % (time.time_ns(), os.getpid())
        tmp = os.path.join(outbox, "." + name)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(outbox, name))
        return True
    except OSError:
        return False


def run(mode, raw, cfg, base, environ=None):
    """One hook run: returns the text to print (may be empty)."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if mode == "statusline":
        try:
            with open(session_file(base, str(data.get("session_id") or "")), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    event = mode or str(data.get("hook_event_name") or "")
    if event not in HOOK_EVENTS:
        return ""
    ctx = context(data, event, cfg, environ)
    matched = [r for r in cfg.get("rules") or () if isinstance(r, dict) and matches(r, event, ctx, data)]
    status = statuses(matched, ctx)
    if status is not None:
        try:
            _write(session_file(base, ctx["session"]), status)
        except OSError:
            pass
    remote = [r["id"] for r in matched if r.get("forward") and r.get("id")]
    activity = bool(cfg.get("idle")) and event in ACTIVITY
    if remote or activity:
        payload = {"op": "hook", "v": VERSION, "ts": time.time(), "event": event,
                   "matched": remote, "activity": activity}
        payload.update({k: ctx[k] for k in ("tool", "project", "cwd", "session", "agent")})
        if remote:  # the person's words leave the machine only for a rule that uses them
            payload.update(prompt=ctx["prompt"], message=ctx["message"])
        forward(cfg, payload, base)
    out = reply(event, matched, ctx, data)
    return json.dumps(out) if out else ""


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        here = os.path.dirname(os.path.abspath(argv[0] if argv else __file__))
        base = os.environ.get("ROOK_CCMOD_DIR") or here
        mode = argv[1] if len(argv) > 1 else ""
        try:
            with open(os.path.join(base, "rules.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return 0
        if not isinstance(cfg, dict):
            return 0
        raw = sys.stdin.read(MAX_STDIN) if not sys.stdin.isatty() else ""
        out = run(mode, raw, cfg, base)
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
