"""Approval asks in plain words: the one shape every ask reaching the person
takes, wherever it is shown (the dashboard's Approvals page, the phone's
decision card, ``rook_approvals``, huddle cards and questions, job holds,
trust and hub-host holds, the house agent's asks).

Bake (2026-10-23): "those need to be better formatted before sending to me.
A simple title, for what task(project) and like I want to xyz, the risk is
abc." So every ask is::

    Title:      a few plain words              ("Read hub approvals database")
    For:        the task, project, huddle or job it serves ("no task" if none)
    Who:        the agent or person asking, by its readable name
    I want to:  one plain sentence of what will happen
    Why:        the asker's own reason, when it gave one (rook_call why=)
    Risk:       one plain sentence, from the tier, the trust level and
                whether the call only reads, changes or destroys
    Explain:    the technical detail (cap, args, trust entry, hold id and its
                three-word name), shown only on demand

:func:`describe` builds that from one waiting item as
:meth:`rook.hub.relay.Relay.items` lists it (or a hold row, via
:func:`for_hold`). It never resolves a secret: it reads the args the hold
kept, which were redacted when the hold was made.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

TITLE_MAX, QUESTION_MAX, LINE_MAX, EXPLAIN_MAX = 60, 100, 220, 900
#: Words for a tier, when nothing more specific is known.
TIER_RISK = {
    "read": ("Low", "it only reads"),
    "write": ("Medium", "it changes data"),
    "exec": ("Medium", "it runs a command that can change things"),
    "admin": ("High", "it changes who can do what or band-wide settings"),
}

# -- small helpers -----------------------------------------------------------------


def short(text: Any, limit: int) -> str:
    """One line of at most ``limit`` characters, cut at a word with an
    ellipsis (these are read by a person, not parsed)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _sentence(text: str) -> str:
    text = short(text, LINE_MAX)
    return text[:1].upper() + text[1:] if text else text


def _base(path: str) -> str:
    return os.path.basename(str(path).rstrip("/")) or str(path)


# -- who ---------------------------------------------------------------------------

_ROLE = re.compile(r"role:([a-z][a-z0-9_-]{0,31})")


def who_name(who: Any = "", *, principal: Any = "", role: str | None = None,
             job: str | None = None) -> str:
    """The asker by a readable name: ``Claude web``, ``architect (huddle
    role)``, ``the house agent``, ``job nightly-backup``."""
    if job:
        return f"job {job}"
    text = str(who or "").strip()
    pid = str(principal or "").strip()
    m = _ROLE.search(text) or _ROLE.search(pid)
    role = role or (m.group(1) if m else None)
    if role:
        return f"{role} (huddle role)"
    for raw in (text, pid):
        low = raw.lower()
        if low.startswith("the house agent"):
            return raw
        if low in ("agent:home", "home"):
            return "the house agent"
    if text.startswith("human:"):
        return "a person"
    if text.startswith(("agent:", "user:")):
        text = text.split(":", 1)[1]
    if not text or re.fullmatch(r"(token:|device:)?(agent_)?[0-9a-f]{12,}", text):
        return "an agent"
    return text


# -- for ---------------------------------------------------------------------------

def for_text(ctx: dict | None, frm: dict | None = None) -> str:
    """The task, project, huddle, job or console the ask serves; ``no task``."""
    ctx, frm = ctx or {}, frm or {}
    task = str(ctx.get("task_title") or "").strip()
    project = str(ctx.get("project") or "").strip()
    if task:
        return short(f"{task} ({project})" if project and project != task else task, 120)
    if project:
        return short(f"project {project}", 120)
    huddle = str(frm.get("huddle_title") or ctx.get("huddle") or frm.get("huddle") or "").strip()
    huddle = re.sub(r"^huddle:\s*", "", huddle, flags=re.I)
    if huddle:
        return short(f"huddle {huddle}", 120)
    job = str(frm.get("job") or ctx.get("job") or "").strip()
    if job:
        return short(f"job {job}", 120)
    room = str(ctx.get("room_title") or "").strip()
    if room:
        return short(f"console {room}", 120)
    if ctx.get("task"):
        return short(f"task {ctx['task']}", 120)
    if frm.get("house"):
        return HOUSE_FOR.get(frm["house"], "the house agent's front door")
    return "no task"


#: What a house agent's ask is for, by its kind (rook/hub/plugins/home/front.py).
HOUSE_FOR = {
    "action": "something an agent asked the house agent to do",
    "dispute": "settling a disagreement between agents",
    "gap": "a gap in the trust map (nothing covers this yet)",
    "playbook": "the house agent's playbook",
    "remember": "remembering your answer for next time",
    "restore": "putting a borrowed machine back",
    "review": "reviewing a new project's setup",
    "vote": "a huddle vote that did not pass on its own",
    "change": "a change request the huddle could not make a standard change",
}


# -- shell commands ------------------------------------------------------------------
#
# Core never guesses that a command only reads: the read-only allowlists live
# in the advanced security plugin (rook/hub/plugins/security/shell.py), which
# words the title and, with the plugin on, the Risk line as before. One that
# looks like it destroys data for good (rook/hub/hardstops.py) is said so.

def _argv(args: Any) -> list[str]:
    if not isinstance(args, dict):
        return []
    v = args.get("argv")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    for key in ("cmd", "command"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return ["sh", "-c", v.strip()]
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return []


def shell_summary(args: Any) -> dict:
    """``{kind, title, want, command}`` for a shell call. The wording comes
    from rook/hub/plugins/security/shell.py; core never says a command only
    reads (``kind`` is ``command``, or ``destroy`` when it erases data)."""
    from .hardstops import destroys
    from .plugins.security import current
    from .plugins.security.shell import shell_summary as guess
    out = guess(args)
    if current():
        return out
    if destroys(out.get("command") or " ".join(_argv(args))):
        return {**out, "kind": "destroy"}
    return {**out, "kind": "command"} if out["kind"] == "read" else out


# -- what a call does --------------------------------------------------------------

def _data(args: Any) -> dict:
    d = args.get("data") if isinstance(args, dict) else None
    return d if isinstance(d, dict) else {}


def _on(worker: str, hub: bool) -> str:
    if hub or str(worker or "").lower() in ("rook", "hub"):
        return "on the hub"
    return f"on {worker}" if worker else ""


def call_summary(cap: str, args: Any, worker: str = "", *, hub: bool = False,
                 tier: str = "", tags: tuple | list = ()) -> dict:
    """``{title, want, kind}`` for one cap call; ``kind`` is read, change,
    destroy or admin (feeds :func:`risk_text`)."""
    cap = str(cap or "")
    args = args if isinstance(args, dict) else {}
    on = _on(worker, hub)
    where = f" {on}" if on else ""
    act = str(args.get("action") or "")
    data = _data(args)
    destructive = "destructive" in (tags or ())
    if cap in ("shell.exec", "proc.spawn", "work.stream.open") and _argv(args):
        s = shell_summary(args)
        want = s["want"] + where
        return {"title": s["title"], "want": want, "kind": s["kind"], "command": s["command"]}
    if cap in ("proc.write", "work.stream.write", "console.write"):
        # Typed text can be an answer to a password prompt: it never goes into
        # the title or the sentence, only under Explain.
        return {"title": "Type into a console", "want": f"type a line into a console{where}",
                "kind": "change", "command": short(str(args.get("data") or ""), 300)}
    if cap.startswith("file."):
        path = str(args.get("path") or args.get("dest") or "")
        verb = cap.split(".", 1)[1]
        if verb in ("read", "stat", "list", "ls", "exists", "hash"):
            return {"title": f"Read {_base(path) or 'a file'}", "want": f"read {path or 'a file'}{where}",
                    "kind": "read"}
        if verb in ("delete", "remove", "rm", "rmdir"):
            return {"title": f"Delete {_base(path) or 'a file'}",
                    "want": f"delete {path or 'a file'}{where}", "kind": "destroy"}
        return {"title": f"Write {_base(path) or 'a file'}", "want": f"write {path or 'a file'}{where}",
                "kind": "change"}
    if cap == "task.write":
        title = str(data.get("title") or "").strip()
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
        if act == "create":
            return {"title": "Create a task", "want": f'create the task "{short(title, 80)}"'
                    + (f" under {data['parent']}" if data.get("parent") else ""), "kind": "change"}
        ref = args.get("id") or "a task"
        if patch.get("state"):
            return {"title": f"Mark a task {patch['state']}",
                    "want": f"set task {ref} to {patch['state']}", "kind": "change"}
        return {"title": f"{(act or 'update').capitalize()} a task",
                "want": f"{act or 'update'} task {ref}", "kind": "change"}
    if cap == "knowledge.write":
        title = str(data.get("title") or args.get("id") or "").strip()
        verb = {"create": "write a new knowledge page", "update": "edit the knowledge page",
                "retract": "retract the knowledge page", "link": "link evidence to the page"}.get(
            act, f"{act or 'change'} knowledge")
        return {"title": "Write knowledge" if act != "retract" else "Retract knowledge",
                "want": f'{verb}{(" " + chr(34) + short(title, 70) + chr(34)) if title else ""}',
                "kind": "change"}
    if cap in ("home.session_note",):
        role = args.get("role") or "house"
        return {"title": "Leave a note for the house agent" if role == "house"
                else f"Leave a note for {role}",
                "want": f'leave a background note for the {role} session: "{short(args.get("text"), 90)}"',
                "kind": "change"}
    if cap in ("home.session_ask", "home.ask"):
        return {"title": "Ask the house agent", "want":
                f'send the house agent: "{short(args.get("text") or args.get("question"), 90)}"',
                "kind": "change"}
    if cap == "home.session_flow":
        return {"title": f"Steer the house agent ({args.get('tool') or 'flow'})",
                "want": f"run the voice flow tool {args.get('tool') or ''} on the house agent".strip(),
                "kind": "change"}
    if cap == "home.session_compact":
        return {"title": "Compact the house agent's memory",
                "want": f"compact the house agent's {args.get('lane') or 'main'} session",
                "kind": "change"}
    if cap.startswith(("home.session", "role.session")) or cap in ("sessions.send",
                                                                    "work.stream.open"):
        if "start" in cap or cap == "work.stream.open":
            return {"title": "Start a coding session",
                    "want": f"start a coding session{where}"
                    + (f" on branch {args['branch']}" if args.get("branch") else "")
                    + (f" in {args['cwd']}" if args.get("cwd") else ""), "kind": "change"}
        return {"title": "Message a coding session",
                "want": f'send a coding session: "{short(args.get("text") or args.get("instructions"), 90)}"',
                "kind": "change"}
    if cap.startswith("huddle."):
        verb = cap.split(".", 1)[1]
        return {"title": {"vote": "Vote in a huddle", "pin": "Pin to the conference table",
                          "start": "Start a huddle", "post": "Post in a huddle",
                          "card": "Decide a huddle card"}.get(verb, "Change a huddle"),
                "want": f"{verb} in a huddle", "kind": "change"}
    if cap.startswith("trust."):
        change = args.get("change") if isinstance(args.get("change"), dict) else _data(args).get("change")
        op = (change or {}).get("op") if isinstance(change, dict) else None
        if "propose" in cap or act == "propose":
            return {"title": "Propose a trust map change",
                    "want": f"propose a trust map change{(' (' + op + ')') if op else ''} for you to accept",
                    "kind": "change"}
        return {"title": "Change the trust map", "want": f"change the trust map ({act or cap})",
                "kind": "admin"}
    if cap.startswith(("secret.", "vault.")):
        name = args.get("name") or ""
        return {"title": "Change a secret" if "get" not in cap else "Read a secret",
                "want": f"{cap.split('.', 1)[1]} the secret {name}".strip(), "kind": "admin"}
    if cap in ("worker.update", "worker.apply", "ota.push", "update.push"):
        return {"title": f"Update {worker or 'a worker'}",
                "want": f"install a new Rook build{where}", "kind": "admin"}
    if cap in ("worker.deauth", "band.deauth"):
        return {"title": f"Remove {worker or 'a worker'} from the band",
                "want": f"take {worker or 'a worker'} off the band", "kind": "destroy"}
    if cap.startswith(("config.", "settings.", "policy.")):
        key = args.get("key") or args.get("path") or ""
        return {"title": "Change settings", "want": f"change {cap.split('.', 1)[0]}"
                + (f" {key}" if key else "") + where, "kind": "admin"}
    if cap.startswith(("job.", "jobs.")):
        name = args.get("name") or args.get("job") or args.get("id") or ""
        verb = cap.split(".", 1)[1].split(":", 1)[0]
        return {"title": f"{'Run' if verb == 'run' else 'Change'} a job",
                "want": f"{verb} the job {name}".strip(), "kind": "change"}
    if cap in ("notify.post", "notify.send", "voice.speak", "attention.send", "sms.send",
               "messages.send", "telegram.send"):
        return {"title": "Send you a message" if cap.startswith(("notify", "voice", "attention"))
                else "Send a message", "want": f"send a message{where}", "kind": "change"}
    if cap.startswith(("chat.", "msg.")):
        verb = cap.split(".", 1)[1]
        return {"title": "Delete chat" if "delete" in verb else "Post in chat",
                "want": f"{verb} in chat", "kind": "destroy" if "delete" in verb else "change"}
    if cap.startswith(("info.", "caps.", "device.info", "battery.")) and tier in ("", "read"):
        return {"title": f"Check {worker or 'a machine'}",
                "want": f"read {cap.split('.', 1)[1].replace('_', ' ')}{where}", "kind": "read"}
    if "deploy" in cap or act == "deploy":
        return {"title": "Deploy", "want": f"deploy{where}", "kind": "admin"}
    # Unknown cap: say it by its parts.
    verb = cap.split(".")[-1].replace("_", " ")
    noun = cap.split(".")[0]
    kind = "destroy" if destructive else "admin" if tier == "admin" else \
        "read" if tier == "read" else "change"
    return {"title": short(f"{verb.capitalize()} {noun}", TITLE_MAX),
            "want": f"use {cap}{(' (' + act + ')') if act else ''}{where}", "kind": kind}


# -- risk --------------------------------------------------------------------------

_TRUST_ENTRY = re.compile(r'entry "([^"]+)" \((ship-it|guarded|sacred)\)')
_TRUST_UNLISTED = re.compile(r"unlisted \((ship-it|balanced|guarded|sacred)\)")


def trust_of(reason: str) -> dict:
    """``{entry, level}`` from a trust hold's cite ("" when not a trust hold)."""
    reason = str(reason or "")
    m = _TRUST_ENTRY.search(reason)
    if m:
        return {"entry": m.group(1), "level": m.group(2)}
    m = _TRUST_UNLISTED.search(reason)
    if m:
        return {"entry": "", "level": m.group(1)}
    return {}


def risk_text(kind: str, tier: str = "", *, hold_kind: str = "", reason: str = "",
              where: str = "", hub: bool = False) -> str:
    """``Low|Medium|High: <why>`` in one sentence."""
    tier = str(tier or "")
    if kind == "read":
        head = ("Low", "looks read-only" if tier and tier != "read" else "it only reads")
    elif kind == "command":
        head = ("Medium", "it runs a command")
    elif kind == "destroy":
        head = ("High", "it can delete or overwrite things")
    elif kind == "admin" or tier == "admin":
        head = ("High", TIER_RISK["admin"][1])
    elif kind == "question":
        head = ("None by itself", "it is a question; your answer goes back to the asker")
    elif kind == "record":
        head = ("Low", "it only adds or edits a record you can change back")
    else:
        head = TIER_RISK.get(tier, ("Medium", "it changes things"))
        if tier == "read":
            head = ("Medium", "it changes things")
    level, why = head
    extra = ""
    t = trust_of(reason) if hold_kind == "trust" or "trust map" in str(reason) else {}
    place = where or ("the hub" if hub else "")
    if t:
        name = t.get("entry") or place or "this"
        if t["level"] == "sacred":
            extra = f"{name} is sacred, so only you change it"
        elif t["level"] == "guarded":
            extra = f"{name} is guarded, so changes there ask you"
        elif t["level"] == "balanced":
            extra = "admin actions ask you"
    elif hold_kind == "hub-host":
        extra = (f"{place or 'it'} runs on the hub's own machine, where a command could "
                 "rewrite the hub's approvals and settings")
    elif hold_kind == "spread":
        m = re.search(r"(\d+\w*) machine in (\d+) minutes", str(reason))
        extra = (f"it would be the {m.group(1)} machine this agent touched in {m.group(2)} "
                 "minutes" if m else "this agent is reaching many machines quickly")
    elif str(reason).startswith("hard stop:"):
        extra = str(reason)[len("hard stop:"):].strip().rstrip(".") + ", which always asks you"
    elif hold_kind == "default" and level != "High":
        extra = "it is an admin or destructive action, which always asks you"
    if kind == "read" and extra:
        return f"{level}: {why}, but {extra}."
    return f"{level}: {why}" + (f"; {extra}." if extra else ".")


# -- the whole ask -----------------------------------------------------------------

def _args_line(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(args)


def explain_text(item: dict, call: dict | None = None) -> str:
    """The technical detail behind Explain: cap, worker, command or args,
    why it was held (the trust entry), rule, and the hold's id and name."""
    parts = []
    cap, worker = item.get("cap"), item.get("worker")
    if cap:
        parts.append(f"{cap} on {worker or 'the band'}")
        cmd = (call or {}).get("command")
        parts.append(f"command: `{short(cmd, 300)}`" if cmd else
                     (f"args: {short(_args_line(item.get('args')), 300)}" if item.get("args") else ""))
    if item.get("tier"):
        parts.append(f"tier {item['tier']}" + (f", tags {', '.join(item['tags'])}"
                                               if item.get("tags") else ""))
    if item.get("reason"):
        parts.append(f"held: {short(item['reason'], 300)}")
    if item.get("hold_kind") and item.get("hold_kind") not in ("trust",):
        parts.append(f"{item['hold_kind']} hold")
    if item.get("rule"):
        parts.append(f"rule {item['rule']}")
    if item.get("principal"):
        parts.append(f"caller {item['principal']}")
    via = item.get("via") or []
    if via:
        parts.append("for " + ", ".join(str(v) for v in via))
    ctx = item.get("context") or {}
    if ctx.get("task"):
        parts.append(f"task {ctx['task']}")
    if ctx.get("room"):
        parts.append(f"console {ctx['room']}")
    if item.get("card_kind"):
        parts.append(f"{item['card_kind']} card")
    if item.get("room"):
        parts.append(f"huddle room {item['room']}")
    ident = item.get("id")
    if ident:
        parts.append(f"id {ident}" + (f" ({item['name']})" if item.get("name") else ""))
    out = "; ".join(p for p in parts if p)
    return short(out, EXPLAIN_MAX)


def describe(item: dict) -> dict:
    """``{title, for, who, want, why?, risk, explain}`` for one waiting item
    (a :meth:`rook.hub.relay.Relay.items` row, or :func:`hold_item` of a
    hold row)."""
    kind = item.get("kind") or "hold"
    frm = item.get("from") if isinstance(item.get("from"), dict) else {}
    ctx = item.get("context") if isinstance(item.get("context"), dict) else {}
    who = who_name(item.get("who_label") or item.get("who"), principal=item.get("principal"),
                   role=frm.get("role"), job=frm.get("job"))
    why = short(ctx.get("why") or "", 200)
    call = None
    if kind == "ask":
        q = str(item.get("question_text") or "").strip()
        opts = item.get("options") or []
        out = {"title": short(q or "A question for you", QUESTION_MAX),
               "want": _sentence(f'get your answer to: "{short(q, 160)}"'
                                 + (f" ({' / '.join(opts)})" if opts else "")),
               "risk": risk_text("question")}
    elif kind == "house":
        line = str(item.get("line") or "").strip()
        cap = item.get("cap")
        if cap:
            call = call_summary(cap, item.get("args") or {}, str(item.get("worker") or ""),
                                hub=bool(item.get("hub")), tier=item.get("tier") or "",
                                tags=item.get("tags") or ())
        out = {"title": short(line or (call or {}).get("title") or "A decision for you",
                              QUESTION_MAX),
               "want": _sentence(call["want"] if call else f"ask you: {line}"),
               "risk": risk_text(call["kind"], item.get("tier") or "", hub=bool(item.get("hub")))
               if call else risk_text("question")}
        who = who_name("the house agent")
    elif kind == "card" and not item.get("cap"):
        summ = str(item.get("summary") or "").strip()
        if item.get("card_kind") == "ask_bake":
            out = {"title": short(summ.rstrip("."), QUESTION_MAX) or "A question for you",
                   "want": _sentence(f'get your answer to: "{short(summ, 160)}"'),
                   "risk": risk_text("question")}
        else:
            out = {"title": f"Note from {who}", "want": _sentence(f"tell you: {short(summ, 180)}"),
                   "risk": "None: a note runs nothing."}
    else:
        call = call_summary(item.get("cap") or "", item.get("args") or {},
                            str(item.get("worker") or ""), hub=bool(item.get("hub")),
                            tier=item.get("tier") or "", tags=item.get("tags") or ())
        title = call["title"]
        data = _data(item.get("args"))
        if item.get("cap") == "task.write" and item.get("args", {}).get("action", "create") == \
                "create" and data.get("title"):
            title = f'Create task "{short(data["title"], 44)}"'
        elif kind == "card" and item.get("card_kind") == "task.create":
            title = "Create a task"
        rk = call["kind"]
        if rk == "change" and item.get("cap") in ("task.write", "knowledge.write") or \
                item.get("cap") == "home.session_note":
            rk = "record"
        where = str(item.get("worker") or "")
        out = {"title": short(title, TITLE_MAX), "want": _sentence(call["want"]),
               "risk": risk_text(rk, item.get("tier") or "", hold_kind=item.get("hold_kind") or "",
                                 reason=item.get("reason") or "", where=where,
                                 hub=bool(item.get("hub")))}
        if kind == "card" and item.get("summary") and not why:
            why = short(item["summary"], 200)
    out["for"] = for_text(ctx, frm)
    out["who"] = who
    if why:
        out["why"] = why
    out["explain"] = explain_text(item, call) if kind not in ("house", "ask") else short(
        (str(item.get("explain") or "") + ("; " if item.get("explain") else "")
         + f"id {item.get('id')}" + (f" ({item['name']})" if item.get("name") else "")),
        EXPLAIN_MAX)
    return out


def lines(p: dict, *, explain: bool = False) -> list[str]:
    """The ask as text lines (phone card body, voice, plain clients)."""
    rows = [f"For: {p['for']}", f"Who: {p['who']}", f"I want to: {p['want']}"]
    if p.get("why"):
        rows.append(f"Why: {p['why']}")
    rows.append(f"Risk: {p['risk']}")
    if explain:
        rows.append(f"Explain: {p['explain']}")
    return rows


def hold_item(hold: dict) -> dict:
    """A hold row (``approvals.db``) as the item :func:`describe` reads."""
    via = [str(v) for v in hold.get("via") or []]
    job = next((v[4:] for v in via if v.startswith("job:")), None)
    ctx = hold.get("context") if isinstance(hold.get("context"), dict) else {}
    frm = {}
    if job:
        frm["job"] = ctx.get("job") or job
    m = _ROLE.search(" ".join([str(hold.get("principal") or ""),
                               str(hold.get("principal_label") or ""), *via]))
    if m:
        frm["role"] = m.group(1)
    return {"kind": "job" if job else "hold", "id": hold.get("id"), "name": hold.get("name"),
            "who": hold.get("principal_label") or hold.get("principal") or "",
            "principal": hold.get("principal"), "from": frm, "context": ctx,
            "cap": hold.get("cap"), "args": hold.get("args") or {},
            "worker": hold.get("target_name") or hold.get("target_id") or "",
            "tier": hold.get("tier"), "tags": list(hold.get("tags") or []),
            "hub": bool(hold.get("is_hub")), "hold_kind": hold.get("kind"),
            "reason": hold.get("reason") or "", "rule": hold.get("rule"), "via": via}


def for_hold(hold: dict) -> dict:
    return describe(hold_item(hold))


def card_item(card: dict, huddle_title: str = "") -> dict:
    """A huddle action card as the item :func:`describe` reads (the huddle
    page; :meth:`rook.hub.relay.Relay._cards` adds the roster's tier and
    hub-host facts for the phone and ``rook_approvals``)."""
    from ..core import authz as core_authz
    call = card.get("call") or {}
    cap = str(call.get("cap") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    tier = core_authz.effective_tier(cap, None) if cap else "write"
    return {"kind": "card", "id": card.get("id"), "name": card.get("name"),
            "who": card.get("by") or "", "cap": cap, "args": args, "worker": call.get("worker"),
            "tier": tier, "tags": list(core_authz.builtin_tags(cap)) if cap else [],
            "hub": str(call.get("worker") or "").lower() in ("rook", "hub"),
            "card_kind": card.get("kind"), "summary": card.get("summary") or "",
            "room": card.get("room"),
            "context": {"huddle": huddle_title or card.get("room") or ""}}


def safe_title(cap: str, worker: str = "", *, hub: bool = False) -> str:
    """A title from the cap and the machine only, never the arguments: what
    leaves the dashboard and the phone (Telegram, notify, voice, the spoken
    summary) uses this, so nothing an agent typed or passed travels there."""
    cap = str(cap or "")
    on = _on(worker, hub)
    where = f" {on}" if on else ""
    if cap in ("shell.exec", "proc.spawn", "work.stream.open"):
        return f"Run a command{where}"
    if cap in ("proc.write", "work.stream.write", "console.write"):
        return f"Type into a console{where}"
    if cap.startswith("file."):
        verb = cap.split(".", 1)[1]
        what = ("Read" if verb in ("read", "stat", "list", "ls", "exists", "hash") else
                "Delete" if verb in ("delete", "remove", "rm", "rmdir") else "Write")
        return f"{what} a file{where}"
    title = call_summary(cap, {}, worker, hub=hub)["title"]
    return title if " on " in title or not where or worker in title else f"{title}{where}"


def outside(item: dict) -> dict:
    """``{title, for, who, risk}`` with an argument-free title, for channels
    that leave the dashboard and the phone (no why, no explain)."""
    p = describe(item)
    title = p["title"] if item.get("kind") in ("ask", "house") or (
        item.get("kind") == "card" and not item.get("cap")) else safe_title(
        item.get("cap") or "", str(item.get("worker") or ""), hub=bool(item.get("hub")))
    return {"title": title, "for": p["for"], "who": p["who"], "risk": p["risk"]}
