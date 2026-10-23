"""Project onboarding through conversation, and the borrowed-state ledger
(docs/design/onboarding.md; house-agent-vision sections 9 and 10).

**Brainstorm.** While the person thinks out loud, the house agent just talks
and keeps running notes (``note``) in its workspace,
``<home data>/workspace/<thread>/notes.md``. No tasks, no structure.

**The turn.** When the talk turns to building, it asks once (``offer``):
"Sounds like this wants to be a project. Want me to set it up?" The hub
writes that line and refuses a second offer in the same conversation.

**Intake, always as questions.** Each answer is recorded with the person's
exact words from their message (the hub checks they are there, so nothing is
guessed); the hub picks the next question and puts it in the reply: where it
lives (new or existing folder or repo, which machine), how protected it is
(toy / guarded / sacred, in their words), what it touches. A toy on a
stricter host (kaiju is sacred) is a conflict: the plan proposes a **scoped
exception** naming exactly the host's baseline resources the project may
stop and restore, with a review date. The plan is read back before anything
is set.

**Setup.** On their yes: the folder or repo (a change on the machine, so the
trust map rules it like any of the house agent's actions), the project
record, a wiki page seeded from the notes, the trust entry with their words
(guarded and sacred apply at once: that only tightens; ship-it and the
exceptions loosen, so they wait on the Trust page for the person's tap), and
a starter team of roles. The reply reads back what was set.

**Borrowed state.** ``borrow`` stops a baseline resource for a project (its
exact recorded stop call) and records a lease in the trust map's ledger:
what changed, why, the person's words, how to restore it, until when. Under
an active exception that names the resource the call just goes; otherwise it
is an ask, and the person's yes runs it. ``restore`` ("bring Strata back")
runs the recorded restore call and closes the lease. The sweep offers to
restore, once per reason, when the project finishes or goes quiet or its
exception ends, and asks "still a toy?" when a ship-it project's review date
comes.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from typing import Any

from ....core.text import clip

log = logging.getLogger("rook.hub.plugins.home.onboard")

STAGES = ("brainstorm", "offered", "intake", "readback", "done", "declined", "cancelled")
OPEN_STAGES = ("brainstorm", "offered", "intake", "readback")
OFFER_LINE = "Sounds like this wants to be a project. Want me to set it up?"
QUESTIONS = {
    "where": "Where do you want it to live: a new folder or repo, or an existing one, and on "
             "which machine?",
    "protection": "How do you feel about deployments for it: a toy experiment agents can just "
                  "ship, guarded so they ask you first, or sacred so nothing changes without you?",
    "touches": "What will it touch besides its own folder: any shared machines, GPUs, services "
               "or data?",
}
ORDER = ("where", "protection", "touches")
LEVEL_OF = {"toy": "ship-it", "ship-it": "ship-it", "ship it": "ship-it", "shipit": "ship-it",
            "guarded": "guarded", "sacred": "sacred"}
LEVEL_WORDS = {"ship-it": "a toy (ship-it: agents just go)",
               "guarded": "guarded (agents ask you first)",
               "sacred": "sacred (nothing changes without you)"}
#: The starter team by protection; the first existing id of each group is used.
TEAMS = {"ship-it": [["lead"]],
         "guarded": [["lead"], ["qa"], ["critic", "skeptic"]],
         "sacred": [["lead"], ["qa"], ["security"], ["critic", "skeptic"]]}
#: A conversation's thread is current while it was touched this recently.
THREAD_IDLE = 12 * 3600
NOTES_MAX = 20000
#: Phrases that usually mean the talk turned to building (a hint to the router).
BUILD_CUE = re.compile(
    r"\b(let'?s|we should|i want to|time to|ready to|can you)\s+(start\s+)?(build|code|write|"
    r"prototype|implement|spin (it )?up|make it|set (it|this) up|get (it|this) going)|"
    r"\bwrite some code\b|\bstart coding\b|\bnew (repo|project)\b", re.I)
_SLUG = re.compile(r"[^a-z0-9]+")
SETUP_SCRIPT = ('d="$1"; case "$d" in "~"|"~/"*) d="$HOME${d#\\~}";; esac; '
                'mkdir -p -- "$d" && cd -- "$d" && '
                'if [ "$2" = repo ] && [ ! -d .git ]; then git init -q; fi && '
                '{ [ -e README.md ] || printf "%s\\n" "$3" > README.md; } && pwd')


class OnboardError(ValueError):
    """Refused; the text says what to do instead."""


def _now() -> float:
    return time.time()


def _line(text: Any, limit: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def slugify(text: Any) -> str:
    s = _SLUG.sub("-", str(text or "").lower()).strip("-")[:48].strip("-")
    return s or "project"


def heard(words: str, said: str) -> bool:
    """The words are in what the person said (case and spacing aside)."""
    w = " ".join(str(words or "").lower().split()).strip(" .,!?\"'")
    s = " ".join(str(said or "").lower().split())
    return len(w) >= 2 and w in s


def _days(seconds: float) -> int:
    return max(1, round(seconds / 86400))


def _date(ts: float) -> str:
    return time.strftime("%b %-d", time.localtime(ts))


class Onboarding:
    """Brainstorm threads, intake, setup and the borrowed-state ledger for
    one house agent's front door (:class:`front.FrontDoor`)."""

    def __init__(self, front: Any) -> None:
        self.front = front

    # -- plumbing ------------------------------------------------------------
    @property
    def home(self) -> Any:
        return self.front.home

    @property
    def store(self) -> Any:
        return self.front.store

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        return self.store._q(sql, args)

    def _x(self, sql: str, args: tuple = ()) -> int:
        return self.store._x(sql, args)

    def setting(self, key: str, default: Any) -> Any:
        v = self.home.settings.get(key)
        return default if v in (None, "") else v

    def workspace(self, thread: dict) -> Any:
        d = self.home.data_dir / "workspace" / (thread.get("slug") or thread["id"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def key(source: str, asker: str, room: str) -> str:
        """One conversation: its room, else the client and who is asking."""
        return f"room:{room}" if room else f"{source or 'ask'}:{asker}"

    # -- threads -------------------------------------------------------------
    @staticmethod
    def _row(r: dict) -> dict:
        for k in ("answers", "plan", "result"):
            r[k] = json.loads(r.get(k) or "{}")
        return r

    def current(self, key: str) -> dict | None:
        rows = self._q("SELECT * FROM onboard_threads WHERE key=? AND updated>=? "
                       "ORDER BY updated DESC LIMIT 1", (key, _now() - THREAD_IDLE))
        return self._row(rows[0]) if rows else None

    def thread(self, tid: str) -> dict | None:
        rows = self._q("SELECT * FROM onboard_threads WHERE id=?", (tid,))
        return self._row(rows[0]) if rows else None

    def threads(self, limit: int = 20) -> list[dict]:
        return [self._row(r) for r in self._q(
            "SELECT * FROM onboard_threads ORDER BY updated DESC LIMIT ?", (limit,))]

    def _new(self, key: str, asker: str, source: str, room: str, title: str = "") -> dict:
        tid = "ob_" + uuid.uuid4().hex[:12]
        now = _now()
        self._x("INSERT INTO onboard_threads(id, created, updated, key, asker, source, room, "
                "stage, title) VALUES (?,?,?,?,?,?,?, 'brainstorm', ?)",
                (tid, now, now, key, asker, source, room, _line(title, 120)))
        return self.thread(tid)

    def _save(self, t: dict, **fields) -> dict:
        fields["updated"] = _now()
        for k in ("answers", "plan", "result"):
            if k in fields:
                fields[k] = json.dumps(fields[k], default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE onboard_threads SET {cols} WHERE id=?", (*fields.values(), t["id"]))
        return self.thread(t["id"])

    def open_thread(self, key: str, asker: str, source: str, room: str, title: str = "") -> dict:
        t = self.current(key)
        if t is None or t["stage"] in ("done", "cancelled"):
            t = self._new(key, asker, source, room, title)
        elif title and not t["title"]:
            t = self._save(t, title=_line(title, 120))
        return t

    # -- 1. brainstorm notes -------------------------------------------------
    def note(self, key: str, text: str, *, title: str = "", asker: str = "", source: str = "",
             room: str = "") -> dict:
        text = _line(text, 1500)
        if len(text) < 3:
            raise OnboardError("text: the point to note, in a sentence")
        t = self.open_thread(key, asker, source, room, title)
        stamp = time.strftime("%H:%M", time.localtime())
        notes = (t["notes"] + f"\n- {stamp} {text}").strip()[-NOTES_MAX:]
        t = self._save(t, notes=notes)
        with contextlib.suppress(OSError):
            (self.workspace(t) / "notes.md").write_text(
                f"# {t['title'] or 'Brainstorm'}\n\nRunning notes ({t['source']}, "
                f"{t['asker']}).\n\n{notes}\n", encoding="utf-8")
        return {"ok": True, "thread": t["id"], "notes": notes.count("\n- ") + 1,
                "note": "Noted. Keep talking with them; no tasks or structure yet."}

    # -- 2. the turn ---------------------------------------------------------
    def offer(self, key: str, *, title: str = "", asker: str = "", source: str = "",
              room: str = "") -> dict:
        t = self.current(key)
        if t is not None and t["offered"]:
            raise OnboardError("you already offered to set this up in this conversation; don't "
                               "ask again (they can say \"set it up\" any time)")
        if t is not None and t["stage"] not in ("brainstorm",):
            raise OnboardError(f"this conversation's project is at {t['stage']}; no offer needed")
        t = self.open_thread(key, asker, source, room, title)
        t = self._save(t, stage="offered", offered=_now())
        return {"ok": True, "thread": t["id"], "say": OFFER_LINE,
                "note": "The hub asks them that line; wait for their answer."}

    # -- 3. intake -----------------------------------------------------------
    def next_question(self, t: dict) -> str:
        a = t["answers"]
        for field in ORDER:
            if field not in a:
                return field
        return ""

    def intake(self, key: str, args: dict, said: str, *, asker: str = "", source: str = "",
               room: str = "") -> dict:
        """One answer in the person's words; returns the next question, or the
        plan read back."""
        answer = str(args.get("answer") or "").strip().lower()
        words = " ".join(str(args.get("words") or "").split())
        if answer not in ("start", "decline", "cancel", "where", "protection", "touches",
                          "review", "title"):
            raise OnboardError("answer: start, decline, cancel, where, protection, touches, "
                               "review or title")
        if not heard(words, said):
            raise OnboardError("words must be the person's exact words from this message: ask "
                               "them, never answer for them")
        t = self.current(key)
        if answer == "start":
            if t is not None and t["stage"] in ("intake", "readback"):
                return self._progress(t)
            t = self.open_thread(key, asker, source, room, str(args.get("title") or ""))
            a = dict(t["answers"])
            a["start"] = {"words": words, "by": asker, "at": _now()}
            t = self._save(t, stage="intake", answers=a)
            if args.get("title"):
                t = self._save(t, title=_line(args["title"], 120))
            return self._progress(t)
        if t is None or t["stage"] not in OPEN_STAGES:
            raise OnboardError("no project is being set up in this conversation; offer first "
                               "(offer_project) or they say \"set it up\"")
        if answer == "decline":
            self._save(t, stage="declined")
            return {"ok": True, "say": "", "note": "Fine: no project. Keep talking; don't offer "
                                                   "again in this conversation."}
        if answer == "cancel":
            self._save(t, stage="cancelled")
            return {"ok": True, "say": "Okay, I've dropped the setup. Nothing was created."}
        if t["stage"] in ("brainstorm", "offered"):
            a = dict(t["answers"])
            a["start"] = {"words": words, "by": asker, "at": _now(), "implied": True}
            t = self._save(t, stage="intake", answers=a)
        a = dict(t["answers"])
        if answer == "title":
            title = _line(args.get("title") or words, 120)
            t = self._save(t, title=title)
            return self._progress(t)
        if answer == "where":
            a["where"] = self._where(args, words, t)
        elif answer == "protection":
            level = LEVEL_OF.get(str(args.get("level") or "").strip().lower())
            if level is None:
                raise OnboardError("level: toy, guarded or sacred (from their words); if their "
                                   "words don't say, ask again")
            a["protection"] = {"level": level, "words": words}
        elif answer == "touches":
            a["touches"] = self._touches(args, words)
        elif answer == "review":
            days = int(args.get("review_days") or 0)
            if not 1 <= days <= 365:
                raise OnboardError("review_days: 1 to 365 (from their words)")
            a["review"] = {"days": days, "words": words}
        t = self._save(t, answers=a, stage="intake")
        return self._progress(t)

    def _where(self, args: dict, words: str, t: dict) -> dict:
        machine = str(args.get("machine") or "").strip()
        if not machine:
            raise OnboardError("machine: which machine it lives on (ask them if they didn't say)")
        roster, hub_id = self.front._roster()
        names = {str(e.get("name") or k).lower(): str(e.get("name") or k)
                 for k, e in roster.items()}
        if machine.lower() in ("rook", "hub"):
            raise OnboardError("projects live on a worker, not inside the hub; which machine?")
        if machine.lower() not in names:
            raise OnboardError(f"no machine {machine!r} on the band (known: "
                               f"{', '.join(sorted(names.values())[:20])}); ask which one")
        kind = "repo" if str(args.get("kind") or "").lower() in ("repo", "git") \
            or args.get("repo") else "folder"
        repo = str(args.get("repo") or "").strip()
        new = bool(args.get("new", not repo))
        path = str(args.get("path") or "").strip()
        if path and not re.match(r"^(~|/)[\w./ -]{0,200}$", path):
            raise OnboardError("path: an absolute path or one under ~ (letters, digits, . _ - /)")
        return {"machine": names[machine.lower()], "kind": kind, "new": new, "path": path,
                "repo": repo[:200], "words": words}

    def _touches(self, args: dict, words: str) -> dict:
        if args.get("nothing"):
            return {"items": [], "words": words}
        items = []
        roster, _ = self.front._roster()
        names = {str(e.get("name") or k).lower(): str(e.get("name") or k)
                 for k, e in roster.items()}
        for raw in (args.get("touches") or [])[:8]:
            if not isinstance(raw, dict):
                continue
            host = str(raw.get("host") or "").strip()
            what = _line(raw.get("what") or raw.get("resource") or "", 200)
            if host and host.lower() not in names:
                raise OnboardError(f"no machine {host!r} on the band; ask which machine")
            host = names.get(host.lower(), host)
            b = self.find_baseline(str(raw.get("resource") or what), host)
            items.append({"host": host, "what": what or (b or {}).get("title") or "",
                          "baseline": b["id"] if b else "",
                          "entry": b["entry"] if b else ""})
        if not items:
            raise OnboardError("touches: [{host, resource?, what}] from their answer, or "
                               "nothing=true when it touches nothing shared")
        return {"items": items, "words": words}

    def _progress(self, t: dict) -> dict:
        field = self.next_question(t)
        if field:
            q = QUESTIONS[field]
            if field == "where" and not t["title"]:
                q = "What should we call it, and where do you want it to live: a new folder " \
                    "or repo, or an existing one, and on which machine?"
            return {"ok": True, "thread": t["id"], "say": q, "asking": field,
                    "note": "Recorded only: nothing is set until they agree to the plan. The "
                            "hub asks them this; don't say you set anything."}
        if not t["title"]:
            return {"ok": True, "thread": t["id"], "say": "What should we call it?",
                    "asking": "title"}
        plan = self.plan(t)
        t = self._save(t, plan=plan, stage="readback", slug=plan["slug"])
        return {"ok": True, "thread": t["id"], "say": plan["readback"], "asking": "confirm",
                "note": "The hub reads the plan back; when they agree, call project_setup "
                        "with their words."}

    # -- the plan --------------------------------------------------------------
    def trust(self) -> Any:
        return self.front.trust()

    def _host_level(self, host: str) -> tuple[str, dict | None]:
        st = self.trust()
        if st is None:
            return "balanced", None
        r = st.rule("shell.exec", "exec", (), worker=host)
        return r.level, r.entry

    def _unique_slug(self, title: str) -> str:
        base = slugify(title)
        st = self.trust()
        taken = {e["id"] for e in (st.current().get("entries") or [])} if st is not None else set()
        taken |= {r["slug"] for r in self._q("SELECT slug FROM onboard_projects")}
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def team(self, level: str) -> list[str]:
        rp = self.front.plugin("role")
        have: set[str] = set()
        with contextlib.suppress(Exception):
            have = {r["id"] for r in rp.store.list() if r.get("enabled", True)}
        out = []
        for group in TEAMS.get(level, TEAMS["guarded"]):
            pick = next((g for g in group if g in have), None) if have else group[0]
            if pick:
                out.append(pick)
        return out or ["lead"]

    def plan(self, t: dict) -> dict:
        a = t["answers"]
        where, prot, touches = a["where"], a["protection"], a["touches"]
        level = prot["level"]
        slug = t.get("slug") or self._unique_slug(t["title"])
        days = int((a.get("review") or {}).get("days")
                   or self.setting("onboard_review_days", 14))
        review_at = _now() + days * 86400
        path = where["path"] or (f"~/{slug}" if where["new"] else "")
        lines, conflicts, notes = [], [], []
        if where["new"]:
            lines.append(f"a new {'git repo' if where['kind'] == 'repo' else 'folder'} at "
                         f"{path} on {where['machine']}")
        else:
            lines.append(f"the existing {where['repo'] or path or 'folder'} on "
                         f"{where['machine']}")
        lines.append(f'{LEVEL_WORDS[level]}, in your words "{_line(prot["words"], 120)}"')
        if touches["items"]:
            lines.append("it touches " + ", ".join(
                f"{i['what']} on {i['host']}" if i["host"] else i["what"]
                for i in touches["items"]))
        else:
            lines.append("it touches nothing shared")
        from ...trust import rank
        hosts: dict[str, list[dict]] = {}
        for i in touches["items"]:
            if i["host"]:
                hosts.setdefault(i["host"], []).append(i)
        hosts.setdefault(where["machine"], [])
        for host, items in hosts.items():
            hl, entry = self._host_level(host)
            if entry is None or rank(hl) <= rank(level):
                continue
            known = [i for i in items if i["baseline"]]
            unknown = [i for i in items if not i["baseline"]]
            c = {"host": host, "level": hl, "entry": entry["id"], "resources": [
                i["baseline"] for i in known]}
            if known:
                titles = " and ".join(self.find_baseline(i["baseline"])["title"] for i in known)
                xid = slugify(f"{slug}-{host}")[:60]
                c["exception"] = {
                    "id": xid, "title": f"{t['title']} on {host}",
                    "applies_to": [entry["id"]],
                    "targets": [f"resource:{i['baseline']}" for i in known],
                    "kinds": ["change", "admin"], "tier": "go",
                    "expires": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(review_at)),
                    "project": slug,
                    "description": f"{t['title']} may stop and restore only: {titles}. "
                                   f"Everything else on {host} stays {hl}."}
                notes.append(f"{host}'s {hl}, but {t['title']} may stop and restore {titles} "
                             f"until {_date(review_at)} (an exception you accept on the Trust "
                             f"page); anything else on {host} still comes to you.")
            for i in unknown:
                notes.append(f"I don't know how to stop and restore {i['what']} on {host} yet, "
                             f"so that stays with you each time ({host} is {hl}).")
            if host == where["machine"] and where["new"]:
                notes.append(f"Creating its folder on {host} needs your yes once ({host} is "
                             f"{hl}).")
            conflicts.append(c)
        team = self.team(level)
        if level == "ship-it":
            notes.append("Ship-it loosens the trust map, so that entry waits for your tap on "
                         "Manage > Trust.")
        readback = (f"Here's the plan for {t['title']}: " + "; ".join(lines) + ". "
                    + " ".join(notes) + (" " if notes else "")
                    + f"Team: {', '.join(team)}. I'll create the project, a wiki page from my "
                    "notes and the trust entry. Set it up?")
        return {"slug": slug, "title": t["title"], "where": {**where, "path": path},
                "level": level, "words": prot["words"], "touches": touches["items"],
                "conflicts": conflicts, "team": team, "review_at": review_at,
                "review_days": days, "readback": readback}

    # -- 4. setup ----------------------------------------------------------------
    async def setup(self, key: str, words: str, said: str, *, asker: str, source: str,
                    room: str, request: str, principal: Any = None) -> dict:
        if not heard(words, said):
            raise OnboardError("words must be the person's exact words from this message")
        t = self.current(key)
        if t is None or t["stage"] != "readback":
            raise OnboardError("nothing to set up yet: finish the questions and read the plan "
                               "back first")
        plan = t["plan"]
        where = plan["where"]
        steps: list[dict] = []
        asked: list[dict] = []
        slug, title = plan["slug"], plan["title"]
        # The folder or repo: a change on that machine, ruled like any action.
        path = where["path"]
        if where["new"]:
            readme = f"# {title}\n\nSet up by the house agent from a conversation with {asker}."
            out = await self._act({
                "worker": where["machine"], "cap": "shell.exec",
                "args": {"argv": ["sh", "-c", SETUP_SCRIPT, "sh", path, where["kind"], readme],
                         "timeout": 30},
                "summary": f"create {path} on {where['machine']} for {title}"},
                source=source, room=room, request=request, asker=asker, principal=principal)
            if out.get("asked"):
                asked.append(out)
                steps.append({"step": "folder", "ok": False, "asked": out["asked"]})
            else:
                got = _stdout(out)
                if out["ok"] and got:
                    path = got.splitlines()[-1].strip() or path
                steps.append({"step": "folder", "ok": out["ok"], "path": path,
                              **({"error": out.get("error")} if not out["ok"] else {})})
        # The wiki page from the brainstorm notes, then the project linking it.
        notes = t["notes"] or "(no brainstorm notes: it went straight to setup)"
        page = await self._hub("knowledge.write", {
            "action": "create", "request_id": f"onboard-{t['id']}-page",
            "data": {"title": f"{title}: notes from the brainstorm", "body": (
                f"Set up by the house agent on {time.strftime('%Y-%m-%d')} from a conversation "
                f"with {asker} ({source}).\n\n## Notes\n{notes}\n\n## Intake (their words)\n"
                + "\n".join(f"- {k}: \"{v.get('words')}\"" for k, v in t["answers"].items()
                            if isinstance(v, dict) and v.get("words"))
                + f"\n\n## Where\n{where['machine']}: {path or where['repo']}"),
                "attrs": {"knowledge_kind": "summary", "tags": ["onboarded", slug]}}})
        page_slug = _field(page, "slug")
        steps.append({"step": "wiki", "ok": bool(page_slug), "slug": page_slug,
                      **({"error": page.get("error")} if not page_slug else {})})
        concept = await self._concept()
        project = await self._hub("task.write", {
            "action": "create", "kind": "project", "request_id": f"onboard-{t['id']}-project",
            "data": {"title": title, "parent": concept, "body": (
                f"{LEVEL_WORDS[plan['level']]}, in {asker}'s words: \"{plan['words']}\".\n"
                f"Lives on {where['machine']}: {path or where['repo']}.\n"
                + (f"Notes: [[{page_slug}]]\n" if page_slug else "")
                + f"Team: {', '.join(plan['team'])}.\n"
                + "".join(f"Touches {i['what']} on {i['host']}.\n" for i in plan["touches"])),
                "attrs": {"tags": ["onboarded", slug], "workers": [where["machine"]]}}}) \
            if concept else {"ok": False, "error": "no concept to file it under"}
        pid = _field(project, "id")
        pname = _field(project, "name") or _field(project, "slug") or pid
        steps.append({"step": "project", "ok": bool(pid), "id": pid, "name": pname,
                      **({"error": project.get("error")} if not pid else {})})
        # The trust entry, in their words.
        st = self.trust()
        entry = {"id": slug, "title": title, "kind": "project", "level": plan["level"],
                 "targets": [f"project:{slug}"] + ([f"repo:{where['repo']}"]
                                                   if "/" in where["repo"] else []),
                 "description": f"Set up through the house agent: lives on {where['machine']} "
                                f"({path or where['repo']}). {asker}: \"{plan['words']}\""}
        trust_note = ""
        proposals: list[str] = []
        if st is not None:
            from ...trust import Actor, TrustError
            try:
                if plan["level"] == "ship-it":
                    prop = st.propose({"op": "set_entry", "data": entry}, by=self.home.identity,
                                      principal=self.front.principal().id,
                                      why=f"{asker} set up {title} as a toy: \"{plan['words']}\"")
                    proposals.append(prop["id"])
                    trust_note = f"ship-it waits for your tap on Trust ({prop['id']})"
                else:
                    st.change("set_entry", entry, Actor(
                        "house", self.home.identity, principal=self.front.principal().id,
                        words=f"{asker}: \"{plan['words']}\"", session=f"onboarding {t['id']}"))
                    trust_note = f"{plan['level']} is set"
                steps.append({"step": "trust", "ok": True, "note": trust_note})
            except TrustError as e:
                steps.append({"step": "trust", "ok": False, "error": str(e)})
            for c in plan["conflicts"]:
                if not c.get("exception"):
                    continue
                try:
                    prop = st.propose({"op": "add_exception", "data": c["exception"]},
                                      by=self.home.identity, principal=self.front.principal().id,
                                      why=f"{asker} set up {title} as {plan['level']} on "
                                          f"{c['host']} ({c['level']}): \"{plan['words']}\"; "
                                          f"touches: \"{t['answers']['touches']['words']}\"")
                    proposals.append(prop["id"])
                    steps.append({"step": "exception", "ok": True, "id": c["exception"]["id"],
                                  "proposal": prop["id"]})
                except TrustError as e:
                    steps.append({"step": "exception", "ok": False, "error": str(e)})
        self._x("INSERT OR REPLACE INTO onboard_projects(slug, created, thread, project, title, "
                "level, words, host, exceptions, review_at, state, data) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?)",
                (slug, _now(), t["id"], pid or "", title, plan["level"], plan["words"],
                 where["machine"], json.dumps([c["exception"]["id"] for c in plan["conflicts"]
                                               if c.get("exception")]),
                 plan["review_at"], json.dumps({"path": path, "team": plan["team"],
                                                "page": page_slug, "proposals": proposals,
                                                "asker": asker, "source": source})))
        result = {"steps": steps, "path": path, "project": pid, "project_name": pname,
                  "page": page_slug, "proposals": proposals, "asked": [a["asked"] for a in asked]}
        t = self._save(t, stage="done", result=result)
        with contextlib.suppress(OSError):
            (self.workspace(t) / "setup.json").write_text(json.dumps(
                {"plan": plan, "result": result}, indent=2, default=str), encoding="utf-8")
        self.home.journal("home.onboard.setup", {"thread": t["id"], "slug": slug},
                          {"ok": True, "result": {"steps": [s["step"] + (":ok" if s["ok"] else
                                                                          ":no")
                                                            for s in steps]}})
        said_parts = []
        folder = next((s for s in steps if s["step"] == "folder"), None)
        if folder is None:
            said_parts.append(f"it uses {path or where['repo']} on {where['machine']}")
        elif folder["ok"]:
            said_parts.append(f"{'repo' if where['kind'] == 'repo' else 'folder'} {path} on "
                              f"{where['machine']}")
        elif folder.get("asked"):
            said_parts.append(f"the folder on {where['machine']} waits for your yes "
                              f"({folder['asked']})")
        else:
            said_parts.append(f"the folder failed ({folder.get('error')})")
        if pid:
            said_parts.append(f"project {pname}")
        if page_slug:
            said_parts.append(f"wiki page {page_slug} from my notes")
        if trust_note:
            said_parts.append(f"trust entry {slug}: {trust_note}")
        for s in steps:
            if s["step"] == "exception" and s["ok"]:
                said_parts.append(f"exception {s['id']} waits for your tap on Trust "
                                  f"({s['proposal']})")
        said_parts.append(f"team: {', '.join(plan['team'])}")
        failed = [s for s in steps if not s["ok"] and not s.get("asked")]
        say = f"Set up {title}: " + "; ".join(said_parts) + "."
        if failed:
            say += " Not done: " + "; ".join(f"{s['step']} ({s.get('error')})" for s in failed) \
                + "."
        return {"ok": True, "say": say, "slug": slug, "steps": steps,
                "asked": asked, "note": "The hub read this back to them."}

    async def _act(self, action: dict, *, source: str, room: str, request: str,
                   asker: str, principal: Any = None) -> dict:
        """One machine change as the house agent: runs when the trust map
        says go (and it is reversible, not a family device), else an ask."""
        front = self.front
        w = front.resolve_worker(action["worker"], action["cap"])
        r = front.ruling(action["cap"], w)
        from ...trust import classify
        kind = classify(action["cap"], w["tier"], w["tags"], w["on_hub"])
        full = {**action, "worker": w["name"], "wid": w["wid"], "tier": w["tier"],
                "tags": list(w["tags"]), "on_hub": w["on_hub"], "kind": kind}
        reasons = []
        if not self.home.may_call(principal, action["cap"]):
            reasons.append(f"{asker} may not run {action['cap']} themselves")
        if w["name"].lower() in front._protected():
            reasons.append(f"{w['name']} is a family device")
        if r is not None and r.tier != "go":
            reasons.append(r.cite)
        if reasons:
            a = front.make_ask("action", f"{action['summary'][:1].upper()}"
                               f"{action['summary'][1:]}? ({action['cap']} on {w['name']})",
                               f"{asker} is setting up a project. I need your yes because "
                               + "; and ".join(_line(x, 220) for x in reasons[:2]) + ".",
                               more=f"Call: {action['cap']} on {w['name']} with "
                                    f"{clip(json.dumps(action.get('args'), default=str), 1500)}",
                               cite=front.cite_of(r), action=full, source=source, room=room,
                               request=request, asker=asker)
            return {"ok": False, "asked": a["name"], "line": a["line"]}
        return await front.run_action(full)

    async def _hub(self, cap: str, args: dict) -> dict:
        """A hub record write as the house agent (tasks, knowledge: free on
        any trust level)."""
        node = self.front.node
        try:
            with self.home._as_self():
                res = await node.invoke(cap, args, self.home.identity)
        except Exception as e:  # noqa: BLE001 - the step reports it
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        self.home.journal(cap, {k: v for k, v in args.items() if k != "data"},
                          {"ok": True})
        return res if isinstance(res, dict) else {"ok": True, "result": res}

    async def _concept(self) -> str:
        want = str(self.setting("onboard_concept", "") or "house-projects")
        got = await self._hub("task.read", {"action": "get", "kind": "concept", "id": want})
        cid = _field(got, "id")
        if cid:
            return cid
        if want != "house-projects":
            return ""
        made = await self._hub("task.write", {
            "action": "create", "kind": "concept", "request_id": "onboard-concept-house-projects",
            "data": {"title": "Projects set up by the house agent", "slug": "house-projects",
                     "body": "Projects the house agent set up from a conversation "
                             "(docs/design/onboarding.md). Each says where it lives, how "
                             "protected it is in the person's words, and what it touches."}})
        return _field(made, "id")

    # -- 5. borrowed state -------------------------------------------------------
    def baseline(self) -> list[dict]:
        st = self.trust()
        return list((st.current().get("baseline") or [])) if st is not None else []

    def find_baseline(self, ref: str, host: str = "") -> dict | None:
        ref = " ".join(str(ref or "").lower().split())
        if not ref:
            return None
        rows = self.baseline()
        if host:
            st = self.trust()
            hl_entries = set()
            if st is not None:
                hl_entries = {e["id"] for e in st.rule("shell.exec", "exec", (), worker=host)
                              .entries}
            rows = [b for b in rows if b["entry"] in hl_entries
                    or b["stop"]["worker"].lower() == host.lower()]
        for b in rows:
            if ref == b["id"] or ref == b["title"].lower():
                return b
        for b in rows:
            if b["id"] in ref or b["title"].lower() in ref or ref in b["title"].lower() \
                    or ref in b["normal"].lower():
                return b
        # "the Strata engine on GPU 1" for "Strata A on GPU 1": every word that
        # matters is in the resource's id, title or normal state.
        want = _words(ref)
        best = [b for b in rows if want and want <= _words(f"{b['id']} {b['title']} "
                                                           f"{b['normal']}")]
        return best[0] if len(best) == 1 else None

    def ledger(self, host: str = "") -> dict:
        st = self.trust()
        doc = st.current() if st is not None else {}
        rows = [r for r in doc.get("ledger") or () if r.get("state") == "open"]
        base = doc.get("baseline") or []
        if host:
            ids = {e["id"] for e in st.rule("shell.exec", "exec", (), worker=host).entries} \
                if st is not None else set()
            rows = [r for r in rows if r["resource"] in ids]
            base = [b for b in base if b["entry"] in ids]
        if rows:
            spoken = "Borrowed right now: " + "; ".join(
                f"{r['what']} for {r.get('project') or '?'} (restore: "
                f"{_line(r['restore'], 80)})" for r in rows) + "."
        else:
            spoken = "Nothing is borrowed" + (f" on {host}" if host else "") + \
                "; everything is in its normal state."
        return {"spoken": spoken, "open": rows, "baseline": [
            {k: b[k] for k in ("id", "entry", "title", "normal")} for b in base]}

    def record_baseline(self, args: dict, words: str) -> dict:
        st = self.trust()
        if st is None:
            raise OnboardError("no trust map on this hub")
        from ...trust import Actor, TrustError
        try:
            return st.change("set_baseline", {k: args.get(k) for k in (
                "id", "entry", "title", "normal", "stop", "restore", "check")},
                Actor("house", self.home.identity, principal=self.front.principal().id,
                      words=words or "recorded by the house agent"))
        except TrustError as e:
            raise OnboardError(str(e)) from None

    def _exception_for(self, baseline_id: str, project: str = "") -> dict | None:
        from ...trust import exception_active
        st = self.trust()
        if st is None:
            return None
        for x in st.current().get("exceptions") or ():
            if exception_active(x, _now()) and f"resource:{baseline_id}" in (x.get("targets")
                                                                            or ()) \
                    and (not project or x.get("project") in (None, "", project)):
                return x
        return None

    def _call_action(self, call: dict, summary: str, borrow: str) -> dict:
        w = self.front.resolve_worker(call["worker"], call["cap"])
        from ...trust import classify
        return {"worker": w["name"], "wid": w["wid"], "cap": call["cap"],
                "args": dict(call.get("args") or {}), "summary": summary, "tier": w["tier"],
                "tags": list(w["tags"]), "on_hub": w["on_hub"],
                "kind": classify(call["cap"], w["tier"], w["tags"], w["on_hub"]),
                "borrow": borrow}

    def _project_of(self, key: str, project: str) -> str:
        if project:
            return slugify(project)
        t = self.current(key)
        return (t or {}).get("slug") or ""

    async def borrow(self, key: str, resource: str, words: str, said: str, *, project: str = "",
                     asker: str, source: str, room: str, request: str,
                     state: dict | None = None, principal: Any = None) -> dict:
        """Stop a baseline resource for a project, recorded as a lease."""
        if not heard(words, said):
            raise OnboardError("words must be the person's exact words from this message")
        b = self.find_baseline(resource)
        if b is None:
            raise OnboardError(f"I don't know {resource!r}'s normal state or how to restore it; "
                               "record_baseline first (the exact stop and restore calls)")
        st = self.trust()
        if any(r.get("baseline") == b["id"] and r.get("state") == "open"
               for r in st.current().get("ledger") or ()):
            raise OnboardError(f"{b['title']} is already borrowed (ledger shows it)")
        project = self._project_of(key, project)
        x = self._exception_for(b["id"], project)
        project = project or (x or {}).get("project") or "a request"
        action = self._call_action(b["stop"], f"stop {b['title']} for {project}", b["id"])
        lease = {"id": slugify(f"{project}-{b['id']}-{uuid.uuid4().hex[:4]}")[:60],
                 "resource": b["entry"], "baseline": b["id"], "project": project if project !=
                 "a request" else "", "what": f"{b['title']} stopped",
                 "why": f"for {project}", "words": words,
                 "restore": _call_text(b["restore"]), "restore_call": b["restore"],
                 **({"exception": x["id"], "until": x.get("expires")} if x else {})}
        action["after"] = {"lease": lease, "words": words, "by": asker}
        st_rule = None
        if st is not None:
            st_rule = st.rule(action["cap"], action["tier"], action["tags"],
                              worker=action["worker"], worker_id=action["wid"] or "",
                              resource=b["id"])
        allowed = self.home.may_call(principal, action["cap"])
        if st_rule is not None and st_rule.tier == "go" and allowed and \
                action["worker"].lower() not in self.front._protected():
            out = await self.front.run_action(action)
            if state is not None:
                state["did"].append({"summary": action["summary"], **out})
            if out["ok"]:
                rec = self.apply_after(action, asker, words)
                self.front.decide("did", f"{action['summary']} (lease {lease['id']})",
                                  self.front.cite_of(st_rule), source=source, room=room,
                                  request=request, data={"subject": action["summary"],
                                                         "lease": lease["id"]})
                return {"ok": True, "lease": lease["id"], "restore": lease["restore"], **rec}
            return out
        a = self.front.make_ask(
            "action", f"Stop {b['title']} for {project}? ({b['stop']['cap']} on "
                      f"{action['worker']})",
            f"{asker} asked to borrow it ({b['normal']}). I need your yes because "
            + (_line(st_rule.cite, 300) if st_rule is not None and st_rule.tier != "go" else
               f"{asker} may not run {action['cap']} themselves" if not allowed else
               "no exception covers it")
            + ". It goes in the ledger with how to restore it.",
            more=f"Stop: {_call_text(b['stop'])}. Restore: {_call_text(b['restore'])}.",
            cite=self.front.cite_of(st_rule), action=action, source=source, room=room,
            request=request, asker=asker)
        if state is not None:
            state["asked"].append(a)
        return {"ok": True, "asked": a["name"], "line": a["line"]}

    def find_lease(self, ref: str) -> dict | None:
        st = self.trust()
        rows = [r for r in (st.current().get("ledger") or []) if r.get("state") == "open"] \
            if st is not None else []
        ref = " ".join(str(ref or "").lower().split())
        base = {b["id"]: b for b in self.baseline()}
        for r in rows:
            title = (base.get(r.get("baseline") or "") or {}).get("title", "").lower()
            if ref in (r["id"], r.get("baseline"), r.get("project"), r["resource"]) or (
                    title and (title in ref or ref in title)) or ref in r["what"].lower():
                return r
        if len(rows) == 1 and not ref:
            return rows[0]
        return None

    async def restore(self, key: str, what: str, words: str, said: str, *, asker: str,
                      source: str, room: str, request: str, state: dict | None = None) -> dict:
        if not heard(words, said):
            raise OnboardError("words must be the person's exact words from this message")
        lease = self.find_lease(what)
        if lease is None:
            raise OnboardError(f"nothing borrowed matches {what!r} (ledger lists what is)")
        return await self.restore_lease(lease, by=asker, words=words, source=source, room=room,
                                        request=request, state=state)

    async def restore_lease(self, lease: dict, *, by: str, words: str, source: str = "",
                            room: str = "", request: str = "", state: dict | None = None,
                            approved: str = "") -> dict:
        call = lease.get("restore_call")
        if not call:
            raise OnboardError(f"{lease['id']} has no recorded restore call: {lease['restore']}")
        b = self.find_baseline(lease.get("baseline") or "") or {"title": lease["what"]}
        action = self._call_action(call, f"bring {b['title']} back", lease.get("baseline") or "")
        action["after"] = {"close_lease": lease["id"], "words": words, "by": by}
        st = self.trust()
        rule = st.rule(action["cap"], action["tier"], action["tags"], worker=action["worker"],
                       worker_id=action["wid"] or "", resource=lease.get("baseline") or "") \
            if st is not None else None
        if approved or rule is None or rule.tier == "go":
            out = await self.front.run_action(action, approved_by=approved)
            if state is not None:
                state["did"].append({"summary": action["summary"], **out})
            if out["ok"]:
                self.apply_after(action, by, words)
                return {"ok": True, "restored": lease["id"]}
            return out
        a = self.front.make_ask(
            "restore", f"Bring {b['title']} back? ({call['cap']} on {action['worker']})",
            f"{by} asked to restore it: {lease['what']} {lease['why']}. The exception that "
            "let me do it alone has ended, so I need your yes.",
            more=f"Restore: {lease['restore']}. Lease {lease['id']}.",
            cite=self.front.cite_of(rule), action=action, source=source, room=room,
            request=request, asker=by)
        if state is not None:
            state["asked"].append(a)
        return {"ok": True, "asked": a["name"], "line": a["line"]}

    def pending_restore(self, lease_id: str) -> bool:
        """An open ask already offers to restore this lease."""
        return any(((a.get("action") or {}).get("after") or {}).get("close_lease") == lease_id
                   for a in self.store.asks("open"))

    def lease_open(self, lease_id: str) -> bool:
        st = self.trust()
        return st is not None and any(r["id"] == lease_id and r.get("state") == "open"
                                      for r in st.current().get("ledger") or ())

    def apply_after(self, action: dict, by: str, words: str = "") -> dict:
        """What a borrowed-state call leaves behind once it ran: a lease
        opened, a lease closed, an exception ended."""
        after = action.get("after") or {}
        st = self.trust()
        if st is None or not after:
            return {}
        from ...trust import Actor, TrustError
        actor = Actor("house", self.home.identity, principal=self.front.principal().id,
                      words=f"{after.get('by') or by}: \"{after.get('words') or words}\"")
        out: dict = {}
        try:
            if after.get("lease"):
                st.change("add_lease", after["lease"], actor)
                out["lease"] = after["lease"]["id"]
            if after.get("close_lease"):
                st.change("close_lease", {"id": after["close_lease"], "restored": True}, actor)
                out["closed"] = after["close_lease"]
            if after.get("end_exception"):
                st.change("end_exception", {"id": after["end_exception"]}, actor)
                out["ended"] = after["end_exception"]
        except TrustError as e:
            log.warning("onboard: ledger update failed: %s", e)
            out["error"] = str(e)
        return out

    # -- proactive offers -----------------------------------------------------------
    def _offered(self, key: str) -> bool:
        return bool(self._q("SELECT 1 FROM onboard_offers WHERE key=?", (key,)))

    def _mark(self, key: str, ask: str, kind: str) -> None:
        self._x("INSERT OR IGNORE INTO onboard_offers(key, created, ask, kind) VALUES (?,?,?,?)",
                (key, _now(), ask, kind))

    async def _project_state(self, ref: str) -> dict:
        if not ref:
            return {}
        got = await self._hub("task.read", {"action": "get", "kind": "project", "id": ref})
        rec = got.get("record") if isinstance(got.get("record"), dict) else got
        return {"state": rec.get("state"), "updated": rec.get("updated")} \
            if isinstance(rec, dict) else {}

    async def sweep(self, now: float | None = None) -> list[str]:
        """Offer, once per reason, to restore borrowed state when a project
        finished, went quiet or its exception ended; ask "still a toy?" when
        a project's review date comes. Returns the asks made."""
        now = _now() if now is None else now
        st = self.trust()
        if st is None:
            return []
        from ...trust import exception_active
        doc = st.current()
        exceptions = {x["id"]: x for x in doc.get("exceptions") or ()}
        quiet_days = int(self.setting("onboard_quiet_days", 14))
        made: list[str] = []
        projects = {r["slug"]: r for r in self._q("SELECT * FROM onboard_projects")}
        for lease in [r for r in doc.get("ledger") or () if r.get("state") == "open"]:
            proj = projects.get(lease.get("project") or "")
            pstate = await self._project_state((proj or {}).get("project") or "")
            x = exceptions.get(lease.get("exception") or "")
            b = self.find_baseline(lease.get("baseline") or "") or {"title": lease["what"]}
            name = (proj or {}).get("title") or lease.get("project") or "that experiment"
            reason, line, end = "", "", ""
            if x is not None and not exception_active(x, now):
                reason = "ended"
                line = f"The {name} exception has ended. Want me to bring {b['title']} back up?"
            elif pstate.get("state") in ("done", "archived", "closed", "cancelled"):
                reason = "finished"
                line = f"{name} ran to completion. Want me to bring {b['title']} back up?"
            else:
                last = max(float(pstate.get("updated") or 0),
                           float(((lease.get("authorized_by") or {}).get("at")) or 0))
                if last and now - last >= quiet_days * 86400:
                    reason = "quiet"
                    host = lease["resource"]
                    line = (f"{name} has been quiet {_days(now - last)} days. Close the {host} "
                            f"exception and restore {b['title']}?")
                    end = (x or {}).get("id") if x is not None and exception_active(x, now) \
                        else ""
            if not reason or self._offered(f"lease:{lease['id']}:{reason}") \
                    or self.pending_restore(lease["id"]):
                continue
            if not lease.get("restore_call"):
                continue
            action = self._call_action(lease["restore_call"], f"bring {b['title']} back",
                                       lease.get("baseline") or "")
            action["after"] = {"close_lease": lease["id"], "words": f"yes to: {line}",
                               "by": self.front.person_name(),
                               **({"end_exception": end} if end else {})}
            a = self.front.make_ask(
                "restore", line,
                f"{lease['what']} {lease['why']}, in the words \"{lease.get('words') or '?'}\". "
                f"Yes runs the recorded restore ({_line(lease['restore'], 120)}) and closes the "
                "lease" + (f" and the exception {end}." if end else "."),
                more=f"Lease {lease['id']}; baseline: {b.get('normal') or '?'}.",
                cite={"note": f"lease {lease['id']} ({reason})"}, action=action,
                source="sweep")
            self._mark(f"lease:{lease['id']}:{reason}", a["id"], "restore")
            made.append(a["name"])
        for p in projects.values():
            if p["state"] != "active" or not p["review_at"] or float(p["review_at"]) > now:
                continue
            key = f"review:{p['slug']}:{int(float(p['review_at']))}"
            if self._offered(key):
                continue
            ended = (await self._project_state(p["project"])).get("state")
            if ended in ("done", "archived", "closed", "cancelled"):
                # Finished or archived: no more check-ins.
                self._x("UPDATE onboard_projects SET state=? WHERE slug=?", (ended, p["slug"]))
                continue
            n = _days(now - float(p["created"]))
            q = {"ship-it": "Still a toy?", "guarded": "Still guarded, not core?",
                 "sacred": "Still sacred?"}.get(p["level"], "Still the same?")
            a = self.front.make_ask(
                "review", f"{p['title']} has been {p['level']} for {n} days. {q}",
                f"You set it up as {LEVEL_WORDS.get(p['level'], p['level'])}: \"{p['words']}\". "
                "Yes keeps it and asks again later. No tightens it one level (and ends any "
                "exception it has); loosening is yours on the Trust page.",
                change={"review": p["slug"]}, source="sweep",
                cite={"note": f"onboarded project {p['slug']}"})
            self._mark(key, a["id"], "review")
            made.append(a["name"])
        return made

    def answer_review(self, a: dict, yes: bool, by: str) -> dict:
        slug = (a.get("change") or {}).get("review") or ""
        rows = self._q("SELECT * FROM onboard_projects WHERE slug=?", (slug,))
        if not rows:
            return {"ok": False, "note": f"No onboarded project {slug!r}."}
        p = rows[0]
        days = int(self.setting("onboard_review_days", 14))
        if yes:
            self._x("UPDATE onboard_projects SET review_at=? WHERE slug=?",
                    (_now() + days * 86400, slug))
            return {"ok": True, "note": f"Kept {p['title']} as {p['level']}; I'll ask again in "
                                        f"{days} days."}
        st = self.trust()
        effects = []
        if st is not None:
            from ...trust import LEVELS, Actor, TrustError
            actor = Actor("house", self.home.identity, principal=self.front.principal().id,
                          words=f"{by}: no to \"{a['line']}\"")
            entry = next((e for e in st.current().get("entries") or [] if e["id"] == slug), None)
            if entry is not None and entry["level"] != "sacred":
                up = LEVELS[LEVELS.index(entry["level"]) + 1]
                new = {k: v for k, v in entry.items() if k not in ("set_by", "rev", "confirmed",
                                                                   "confirmed_by")}
                new["level"] = up
                with contextlib.suppress(TrustError):
                    st.change("set_entry", new, actor)
                    effects.append(f"{slug}: {entry['level']} -> {up}")
                    self._x("UPDATE onboard_projects SET level=? WHERE slug=?", (up, slug))
            elif entry is None and p["level"] == "ship-it":
                # The ship-it proposal was never accepted: record it guarded.
                with contextlib.suppress(TrustError):
                    st.change("set_entry", {"id": slug, "title": p["title"], "kind": "project",
                                            "level": "guarded",
                                            "targets": [f"project:{slug}"]}, actor)
                    effects.append(f"{slug}: guarded")
                    self._x("UPDATE onboard_projects SET level='guarded' WHERE slug=?", (slug,))
            for xid in json.loads(p["exceptions"] or "[]"):
                with contextlib.suppress(TrustError):
                    st.change("end_exception", {"id": xid}, actor)
                    effects.append(f"ended exception {xid}")
        self._x("UPDATE onboard_projects SET review_at=? WHERE slug=?",
                (_now() + days * 86400, slug))
        return {"ok": True, "note": "Tightened: " + ("; ".join(effects) or "nothing to change")
                + ". Borrowed state stays until you say restore."}

    # -- what the router sees ------------------------------------------------------------
    def prompt_text(self, key: str, said: str) -> str:
        t = self.current(key)
        parts = [ONBOARD_RULES]
        if t is not None and t["stage"] in OPEN_STAGES + ("declined",):
            parts.append(f"This conversation: stage {t['stage']}"
                         + (f", project \"{t['title']}\"" if t["title"] else "")
                         + (", you already offered to set it up (never offer again here)"
                            if t["offered"] else "") + ".")
            if t["notes"]:
                parts.append("Your running notes so far:\n" + clip(t["notes"], 3000))
            got = {k: v.get("words") for k, v in t["answers"].items() if isinstance(v, dict)}
            if t["stage"] in ("intake", "readback"):
                parts.append("Answers so far (their words): " + json.dumps(got, ensure_ascii=False)
                             + ". Waiting for: " + (self.next_question(t) or "their yes to the "
                                                    "plan (then project_setup)") + ".")
        if (t is None or t["stage"] == "brainstorm") and not (t or {}).get("offered") \
                and BUILD_CUE.search(said or ""):
            parts.append("Hint: this message may be the turn to building. If it is, and "
                         "there is an idea to build, call offer_project.")
        st = self.trust()
        if st is not None:
            led = [r for r in st.current().get("ledger") or () if r.get("state") == "open"]
            if led:
                parts.append("Borrowed state now: " + "; ".join(
                    f"{r['id']}: {r['what']} for {r.get('project') or '?'}" for r in led[:6]))
            base = self.baseline()
            if base:
                parts.append("Known normal states (baseline): " + "; ".join(
                    f"{b['id']} on {b['entry']}: {b['normal']}" for b in base[:10]))
        return "\n".join(parts)


ONBOARD_RULES = """New projects and borrowed state:
- Brainstorming (the person thinking out loud about an idea): just talk with them (path answer) and call note with the gist of each new point, so your running notes keep up. No tasks, sessions or structure yet.
- When the talk turns to building it ("let's write some code", "let's build it"), call offer_project once with a short title. The hub asks "Sounds like this wants to be a project. Want me to set it up?" Never ask it again in the same conversation.
- When the hub asks a question for you, don't ask one of your own: at most one short line before it.
- Their answer: yes -> project_intake answer=start; no -> answer=decline. Then set it up only by asking: each time they answer, call project_intake with what they said (answer where|protection|touches|review|title, the fields, and words = their exact words from this message). The hub asks the next question and reads the plan back. Never fill in an answer they did not give.
- When they agree to the plan read back, call project_setup with their words.
- "Shut down X for it" (a known normal state from the baseline): borrow. "Bring X back" / "restore X": restore. "What's different on <machine>?" / "what's borrowed?": ledger. A host's normal state you learn: record_baseline (exact stop and restore calls)."""


_STOP = {"the", "a", "an", "on", "in", "of", "for", "to", "it", "this", "that", "and", "my",
         "our", "its", "we", "can", "down", "up", "shut", "stop", "use", "experiment"}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w and w not in _STOP}


def _field(res: Any, key: str) -> str:
    """A record field from a hub write's reply ({record: {...}} or flat)."""
    if not isinstance(res, dict):
        return ""
    for d in (res, res.get("record"), res.get("result")):
        if isinstance(d, dict) and d.get(key):
            return str(d[key])
        if isinstance(d, dict) and isinstance(d.get("record"), dict) and d["record"].get(key):
            return str(d["record"][key])
    return ""


def _stdout(out: dict) -> str:
    raw = out.get("result")
    with contextlib.suppress(ValueError, TypeError):
        raw = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(raw, dict):
        return str(raw.get("stdout") or "").strip()
    return ""


def _call_text(call: dict) -> str:
    args = call.get("args") or {}
    argv = args.get("argv")
    shown = " ".join(str(a) for a in argv) if isinstance(argv, list) else \
        (args.get("cmd") or json.dumps(args, default=str))
    return f"{call['cap']} on {call['worker']}: {shown}"
