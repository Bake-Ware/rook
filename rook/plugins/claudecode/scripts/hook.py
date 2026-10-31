"""Rook's Claude Code hook: the command claudecode.install puts in Claude
Code's settings (0.4 rook/worker/ccmod_hook.py, its local half).

claudecode.install writes this worker's rules to ``<plugin data>/rules.json``,
copies this file beside them as ``rook-ccmod-hook.py`` and adds one hook
entry per event to each Claude Code installation's ``settings.json``. Each
entry runs this file with the worker's Python (``-I -S``: no site, nothing
from the current folder) and the event name::

    "<python>" -I -S "<plugin data>/rook-ccmod-hook.py" PreToolUse || exit 0

(``|| exit 0``: if this file or that Python is gone, the command still ends
with status 0. Claude Code reads status 2 as "block", and Python exits 2
when it can't open its script.) It imports nothing from Rook: stdlib only.

What one run does: reads the event Claude Code sends on stdin and the rules
file, finds the rules whose trigger matches (event, tool matcher, text, a
project or agent scope), and prints Claude Code's hook reply for their
actions: ``block`` (deny a tool call or a prompt), ``inject`` (add context,
or on Stop, ask Claude to carry on) and ``statusline`` (the text the status
line command shows for this session). 0.4's remote actions (posts,
notifications, caps run by the hub) and Idle wait for events to travel to
the hub (castling/coverage.md N21).

It never fails a hook: any error ends the run quietly with exit status 0,
so Claude Code carries on as if the hook were not there.
"""

import json
import os
import re
import sys

MARK = "rook-ccmod-hook"
SCRIPT_NAME = MARK + ".py"

#: Claude Code hook events a rule may trigger on.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "SubagentStop", "PreCompact", "SessionEnd")
#: Events whose hook entry takes a tool-name matcher.
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
#: Local actions per event, and what each turns into in the hook's reply.
BLOCKABLE = ("PreToolUse", "UserPromptSubmit")
INJECTABLE = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop")

FIELDS = ("worker", "band", "event", "tool", "project", "cwd", "session", "agent",
          "prompt", "message")
TEXT_CLIP = 300
#: Hook input read at most (a large Write or Edit can carry megabytes). Past
#: this, the fields that can still be read are taken from the head of the
#: text and ``contains`` looks in the raw text (:func:`partial`).
MAX_STDIN = 8 << 20
_TEMPLATE = re.compile(r"\{(" + "|".join(FIELDS) + r")\}")
_SESSION_FILE = re.compile(r"[^A-Za-z0-9_-]")
_HEAD_FIELDS = ("hook_event_name", "session_id", "cwd", "tool_name", "stop_hook_active")


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
    """Project and agent scopes are checked where the event happens; a
    worker scope was applied by claudecode.install, so it passes here."""
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
        if data and "_raw" in data:   # too large to read whole: look in the text itself
            hay = data["_raw"]
        elif event in TOOL_EVENTS:
            hay = json.dumps((data or {}).get("tool_input") or {}, ensure_ascii=False)
        elif event == "Notification":
            hay = str(ctx.get("message") or "")
        else:
            hay = str((data or {}).get("prompt") or ctx.get("prompt") or "")
        if needle not in hay.lower():
            return False
    return in_scope(rule.get("scope"), ctx)


def partial(raw):
    """What can be read from hook input too large to read whole (cut at
    :data:`MAX_STDIN`, so not valid JSON): the short fields from the head of
    the text, and the raw text itself for ``contains`` (a rule that blocks
    "Write containing X" still sees X in a huge Write)."""
    data = {"_raw": raw}
    for key in _HEAD_FIELDS:
        m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.){0,4096}"|true|false)' % key, raw[:65536])
        if m:
            try:
                data[key] = json.loads(m.group(1))
            except ValueError:
                pass
    return data


def context(data, event, cfg, environ=None):
    """The template fields of one event."""
    env = os.environ if environ is None else environ
    cwd = str(data.get("cwd") or "")
    return {"worker": str(cfg.get("worker") or ""), "band": str(cfg.get("band") or ""),
            "event": event, "tool": str(data.get("tool_name") or ""),
            "project": project_of(cwd), "cwd": cwd,
            "session": str(data.get("session_id") or "")[:64],
            "agent": str(env.get("ROOK_AGENT") or "")[:120],
            "prompt": clip(data.get("prompt")), "message": clip(data.get("message"))}


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


def run(mode, raw, cfg, base, environ=None):
    """One hook run: returns the text to print (may be empty)."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = partial(raw) if len(raw) > MAX_STDIN else {}
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
    out = reply(event, matched, ctx, data)
    return json.dumps(out) if out else ""


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        here = os.path.dirname(os.path.abspath(argv[0] if argv else __file__))
        base = here
        mode = argv[1] if len(argv) > 1 else ""
        try:
            with open(os.path.join(base, "rules.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return 0
        if not isinstance(cfg, dict):
            return 0
        # One character past the limit tells run() the input was cut.
        raw = sys.stdin.read(MAX_STDIN + 1) if not sys.stdin.isatty() else ""
        out = run(mode, raw, cfg, base)
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
