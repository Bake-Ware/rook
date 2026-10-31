"""Chaired huddles, their table and merge / qa / deploy calls (plugin.yaml
says what they are). One runtime per hub (``_RT``), made by the start hook.

**Streams.** Each huddle is a durable ``huddle`` stream: posts are ``text``
events by whoever posted, and the plugin writes ``status`` events for pins,
calls, gates and outcomes, so the thread is the huddle's whole story.
The streams live in this plugin's own store (``<data>/huddles.db``) and are
listed in the hub's stream index (``ctx.index_streams``, ``stream.list``).

**Pins.** Only a person (no agent in the call) or the huddle's chair agent
(``agent:<chair>``) pins, at an exact version: a PR at its head SHA (a
``sha`` that isn't the head is refused), a commit or file at a SHA, a
document or link at a SHA-256. A newer pin of the same thing supersedes the
older one; calls are on the newest.

**Calls** (``huddle.vote action=call``). Anyone the hub lets call it, with an
adversarial self-review (there are no ballots on 0.5, so the review is the
check on the record). The gates run in the background, reading GitHub as
this plugin: version (the PR's head is still the pinned SHA), mergeable,
ci (every check green, waiting up to ``gate_wait`` while they run), tests (a
check named like ``test_checks`` passed) and secrets (no credential-shaped
text on the diff's added lines). Then, by the trust level (``project.trust``
of the huddle's project and of the project the ``repos`` setting names for
the artifact's repo, the stricter; normal without either): sacred asks an
owner first
(``huddle.carry`` is a hard stop, so the hub holds it as one card); else it
is carried out at once:

- merge: ``github.merge`` at the pinned SHA, as this plugin (the only writer
  github allows), so GitHub refuses if the head moved;
- qa: the ``qa_label`` label and a comment;
- deploy: ``change.submit`` as the caller (their rights, the change record's
  tests and rollback), with ``{version}`` in its steps' args filled in and
  the huddle's project on it; at sacred it is a risky change, so the change
  record's own card is the one an owner gets (the huddle doesn't ask first).

Every call through dispatch is journaled with its why (the motion's id); the
motion record keeps the gates, the self-review and the outcome. A hub restart
ends open motions as failed (holds don't outlive the hub): call it again.
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import logging
import re
import time

from rook.kit._types import parse_duration
from rook.kit.ctx import Caller, KitError
from rook.streams import SqliteStore, StreamError, Streams

log = logging.getLogger("rook.plugin.huddle")

ME = "system:plugin.huddle"
SELF_REVIEW_MIN = 80
READ_CHARS = 12000
WHY_MAX = 500
GATES = {"merge": ("version", "mergeable", "ci", "tests", "secrets"),
         "qa": ("version", "ci", "tests", "secrets"),
         "deploy": ("version", "ci", "tests", "secrets")}
OPEN = ("gating", "asked", "carrying")
PASSING = ("success", "neutral", "skipped")
MERGEABLE_TRIES = 4
_HOLD = re.compile(r"\(hold (h_[0-9a-f]+)\)")
_PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_SHA = re.compile(r"[0-9a-f]{40}")
# Credential shapes, as on 0.4 (rook/hub/plugins/huddle/table.py).
SECRET_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|xox[abposr]-[A-Za-z0-9-]{10,}|"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35})")


def short(sha: str) -> str:
    return str(sha or "")[:7]


def _line(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def secret_hits(diff: str) -> list[str]:
    """Credential-looking strings on a diff's added lines: where, never the value."""
    hits = []
    for i, line in enumerate(str(diff or "").splitlines(), 1):
        if line.startswith("+") and not line.startswith("+++"):
            m = SECRET_SHAPES.search(line)
            if m:
                hits.append(f"line {i}: looks like a credential ({m.group(0)[:4]}...)")
    return hits[:20]


def fill(value, version: str):
    """``{version}`` in a deploy step's args becomes the pinned SHA."""
    if isinstance(value, str):
        return value.replace("{version}", version)
    if isinstance(value, list):
        return [fill(v, version) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, version) for k, v in value.items()}
    return value


# -- the runtime -------------------------------------------------------------------

_RT: "Runtime | None" = None


def _rt() -> "Runtime":
    if _RT is None:
        raise KitError("huddles are not running", retry=True)
    return _RT


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.band = ctx.band
        self.sys = Caller(band=ctx.band, owner=True, plugin="huddle")
        self.sysctx = _with_caller(ctx, self.sys)
        self.streams = Streams(SqliteStore(str(ctx.data / "huddles.db")), host="hub")
        ctx.index_streams(self.streams)          # huddles in the hub's stream list (N28)
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.streams.durable.close()

    def setting(self, key: str):
        return self.ctx.settings[key]

    def me(self, why: str) -> Caller:
        return Caller(band=self.band, plugin="huddle", why=why[:WHY_MAX])

    async def gh(self, cap: str, why: str, **args):
        return await self.ctx.call_as(self.me(why), f"github.{cap}", args)

    # -- records ----------------------------------------------------------------------

    @property
    def rec(self):
        """This plugin's records, as itself (it sees and writes every row)."""
        return self.sysctx.records

    def huddle(self, hid: str, open_: bool = False) -> dict:
        try:
            h = self.rec.huddle.get(str(hid or ""))
        except KitError:
            raise KitError(f"no huddle {hid}") from None
        if open_ and h["state"] != "open":
            raise KitError(f"huddle {hid} is closed")
        return h

    def artifact(self, aid: str, h: dict) -> dict:
        try:
            a = self.rec.artifact.get(str(aid or ""))
        except KitError:
            a = None
        if a is None or a["huddle"] != h["id"]:
            raise KitError(f"no artifact {aid} on huddle {h['id']}'s table (huddle.table lists them)")
        return a

    def motion(self, mid: str) -> dict:
        return self.rec.motion.get(mid)

    def set(self, mid: str, **fields) -> dict:
        m = self.motion(mid)
        return self.rec.motion.update(mid, revision=m["revision"], **fields)

    def end(self, mid: str, state: str, outcome: str, **fields) -> dict:
        m = self.set(mid, state=state, outcome=outcome[:1000], ended=time.time(), **fields)
        self.say(m["huddle"], f"{m['kind']} {m['id']}: {state.replace('_', ' ')}. {outcome}",
                 event="motion", motion=mid, state=state)
        return m

    # -- the thread -------------------------------------------------------------------

    def say(self, hid: str, text: str, **status) -> None:
        """A status line in the huddle's thread (by the plugin)."""
        try:
            h = self.huddle(hid)
            self.streams.append(h["stream"], "status", {"text": text[:4000], **status}, by=ME)
        except (KitError, StreamError) as e:
            log.warning("huddle status not written", extra={"fields": {"huddle": hid, "error": str(e)}})

    # -- trust ------------------------------------------------------------------------

    async def level(self, project: str | None) -> str:
        """The huddle project's trust level (project.trust); normal without one."""
        if not project:
            return "normal"
        me = self.me(f"Read project {project}'s trust level for a huddle call")
        try:
            try:
                p = await self.ctx.call_as(me, "project.get", {"id": project})
            except KitError:
                found = await self.ctx.call_as(me, "project.list", {"where": {"slug": project}, "limit": 1})
                if not found:
                    raise KitError(f"no project {project}") from None
                p = found[0]
            trust = await self.ctx.call_as(me, "project.trust.list", {"where": {"project": p["id"]}, "limit": 1})
        except KitError as e:
            raise KitError(f"project {project}: {e.message}") from None
        return trust[0]["level"] if trust else "normal"

    async def strictest(self, project: str | None, a: dict) -> str:
        """The stricter of the huddle's project and the one the ``repos``
        setting names for the artifact's repo: a call can't dodge a sacred
        repo by being made in a huddle without a project."""
        repo = str((a.get("ref") or {}).get("repo") or "").lower()
        mapped = next((p for r, p in (self.setting("repos") or {}).items() if r.lower() == repo), None) if repo else None
        levels = [await self.level(p) for p in (project, mapped) if p] or ["normal"]
        return min(levels, key=("sacred", "normal", "ship-it").index)

    # -- gates ------------------------------------------------------------------------

    async def gates(self, m: dict, a: dict) -> list[dict]:
        out, checks = [], {}
        for gid in GATES[m["kind"]]:
            try:
                ok, detail = await getattr(self, f"_gate_{gid}")(m, a, checks)
            except KitError as e:
                ok, detail = False, e.message[:300]
            out.append({"id": gid, "ok": bool(ok), "detail": detail, "at": time.time()})
            if gid == "version" and not ok:
                break
        return out

    def _why(self, m: dict, what: str) -> str:
        return f"Huddle {m['kind']} {m['id']}: {what} at {short(m['version'])}"

    async def _pr(self, m: dict, a: dict) -> dict:
        r = a["ref"]
        return await self.gh("pr", self._why(m, "the version gate reads the PR"), repo=r["repo"], number=r["number"])

    async def _gate_version(self, m, a, _):
        if a["kind"] != "pr":
            return True, f"pinned at {short(a['version'])}"
        p = await self._pr(m, a)
        if p["merged"] and m["kind"] == "deploy":
            return True, "merged"
        if p["state"] != "open":
            return False, f"the pull request is {p['state']}"
        if p["draft"] and m["kind"] == "merge":
            return False, "the pull request is a draft"
        if p["head"] != m["version"]:
            return False, f"the PR moved to {short(p['head'])}; pin it again and call again"
        return True, f"head is {short(m['version'])}"

    async def _gate_mergeable(self, m, a, _):
        p = {}
        for i in range(MERGEABLE_TRIES):
            p = await self._pr(m, a)
            if p["mergeable"] is not None:
                break
            if i + 1 < MERGEABLE_TRIES:
                await asyncio.sleep(parse_duration(self.setting("poll")) / 4)
        if p["mergeable"] is False:
            return False, f"conflicts with {p['base']} ({p['mergeable_state']}); rebase it"
        if p["mergeable"] is None:
            return False, "GitHub has not worked out whether it merges cleanly yet; call again"
        return True, f"merges cleanly into {p['base']}"

    async def _checks(self, m: dict, a: dict, cache: dict) -> dict:
        """The commit's checks, waiting while they run (``gate_wait``)."""
        if "c" in cache:
            return cache["c"]
        deadline = time.monotonic() + parse_duration(self.setting("gate_wait"))
        while True:
            c = await self.gh("checks", self._why(m, "the ci gate reads the checks"),
                              repo=a["ref"]["repo"], sha=m["version"])
            running = [r for r in c["runs"] if r["status"] != "completed"] + \
                [s for s in c["statuses"] if s["state"] == "pending"]
            if not running or time.monotonic() >= deadline:
                cache["c"] = c
                return c
            await asyncio.sleep(parse_duration(self.setting("poll")))

    async def _gate_ci(self, m, a, cache):
        c = await self._checks(m, a, cache)
        if not c["runs"] and not c["statuses"]:
            if self.setting("require_ci"):
                return False, f"no CI ran on {short(m['version'])}"
            return True, "no CI on this repository (require_ci is off)"
        bad = [f"{r['name']}: {r['conclusion'] or r['status']}" for r in c["runs"]
               if r["status"] != "completed" or r["conclusion"] not in PASSING]
        bad += [f"{s['context']}: {s['state']}" for s in c["statuses"] if s["state"] != "success"]
        if bad:
            return False, "CI is not green: " + "; ".join(bad[:6])
        return True, f"{len(c['runs']) + len(c['statuses'])} checks green"

    async def _gate_tests(self, m, a, cache):
        c = await self._checks(m, a, cache)
        pats = [p.lower() for p in self.setting("test_checks") or ["*test*"]]
        runs = [r for r in c["runs"] if any(fnmatch.fnmatchcase(str(r["name"]).lower(), p) for p in pats)]
        if not runs:
            if not self.setting("require_ci") and not c["runs"]:
                return True, "no test runs (require_ci is off)"
            return False, f"no test run among the checks (test_checks: {pats})"
        bad = [r["name"] for r in runs if r["conclusion"] != "success"]
        if bad:
            return False, "tests failed: " + ", ".join(bad)
        return True, ", ".join(r["name"] for r in runs) + " passed"

    async def _gate_secrets(self, m, a, _):
        r = a["ref"]
        base = r.get("base_sha") if a["kind"] == "pr" else r.get("parent")
        if not base:
            return False, "no parent commit to diff against"
        d = await self.gh("diff", self._why(m, "the secrets gate reads the diff"),
                          repo=r["repo"], base=base, head=m["version"])
        if d.get("truncated"):
            return False, f"the diff is over {d['bytes'] // (1024 * 1024)} MB: too big to check for secrets"
        hits = secret_hits(d["text"])
        if hits:
            return False, "the diff has secrets in it: " + "; ".join(hits[:5])
        return True, "no secrets in the diff"

    # -- running a motion -------------------------------------------------------------

    async def guarded(self, mid: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:      # a broken run says so, never stays open
            log.exception("huddle motion broke", extra={"fields": {"motion": mid}})
            self.end(mid, "failed", f"it broke ({type(e).__name__}: {e})"[:300])

    async def run(self, mid: str) -> None:
        m = self.motion(mid)
        a = self.rec.artifact.get(m["artifact"])
        gates = await self.gates(m, a)
        failed = [g for g in gates if not g["ok"]]
        if failed:
            self.end(mid, "gates_failed", "stopped at the gates: "
                     + "; ".join(f"{g['id']}: {g['detail']}" for g in failed), gates=gates)
            return
        self.set(mid, gates=gates)
        self.say(m["huddle"], f"{m['kind']} {mid}: gates passed ({', '.join(g['id'] for g in gates)}).",
                 event="gates", motion=mid, ok=True)
        if m["level"] == "sacred" and m["kind"] != "deploy":     # a deploy's change record asks instead
            await self.ask(mid)
        else:
            await self.carry(mid)

    async def ask(self, mid: str) -> None:
        """Sacred: hold huddle.carry for an owner (one card)."""
        m = self.motion(mid)
        a = self.rec.artifact.get(m["artifact"])
        why = f"{m['kind'].capitalize()} {a['title']} at {short(m['version'])}: gates passed (motion {mid})"
        try:
            await self.ctx.call_as(self.me(why), "huddle.carry", {"motion": mid})
        except KitError as e:
            held = _HOLD.search(e.message)
            if held:
                self.set(mid, state="asked", hold=held.group(1))
                self.say(m["huddle"], f"{m['kind']} {mid}: the project is sacred; waiting for an owner "
                                      f"(hold {held.group(1)}).", event="asked", motion=mid)
                return
            self.end(mid, "failed", f"the owner's card could not be made ({e.message})")

    async def carry(self, mid: str, recheck: bool = False) -> dict:
        m = self.set(mid, state="carrying")
        a = self.rec.artifact.get(m["artifact"])
        if recheck:     # after an owner's yes: time has passed, so the gates again
            gates = await self.gates(m, a)
            failed = [g for g in gates if not g["ok"]]
            if failed:
                return self.end(mid, "gates_failed", "a gate failed after the owner's yes: "
                                + "; ".join(f"{g['id']}: {g['detail']}" for g in failed), gates=gates)
        r, v = a["ref"], m["version"]
        try:
            if m["kind"] == "merge":
                title = (f"Merge pull request #{r['number']} from {r.get('head_ref') or short(v)} "
                         f"(vote {mid}: no vote, {m['level']})")
                got = await self.gh("merge", self._why(m, f"merge {r['repo']}#{r['number']} after its gates"),
                                    repo=r["repo"], number=r["number"], sha=v, title=title,
                                    method=self.setting("merge_method"))
                return self.end(mid, "merged", f"merged {r['repo']}#{r['number']} at {short(v)} "
                                               f"({got.get('sha', '')[:7]})", result=got)
            if m["kind"] == "qa":
                why = self._why(m, f"mark {r['repo']}#{r['number']} ready for QA")
                await self.gh("label", why, repo=r["repo"], number=r["number"], labels=[self.setting("qa_label")])
                await self.gh("comment", why, repo=r["repo"], number=r["number"],
                              body=f"Ready for QA at {v} (motion {mid}, huddle {m['huddle']}).")
                return self.end(mid, "done", f"{r['repo']}#{r['number']} labelled {self.setting('qa_label')}")
            return await self.deploy(m, a)
        except KitError as e:
            if e.message.startswith("GitHub 409"):
                return self.end(mid, "void", f"GitHub refused: the head is no longer {short(v)}; pin it again")
            return self.end(mid, "failed", f"carrying it out failed: {e.message[:300]}")

    async def deploy(self, m: dict, a: dict) -> dict:
        """A change record as the caller: their rights, its tests and rollback."""
        h = self.huddle(m["huddle"])
        spec = fill(dict(m["change"] or {}), m["version"])
        spec.setdefault("what", f"Deploy {a['title']} at {short(m['version'])}")
        spec.setdefault("why", (m.get("reason") or f"Huddle deploy, motion {m['id']}")[:400])
        if h.get("project"):
            spec["project"] = h["project"]
        if m["level"] == "sacred":      # however it got sacred (huddle or repo): the change asks an owner
            spec["kind"] = "risky"
        creator, person = m.get("creator") or "", m.get("person")
        why = f"Deploy for huddle motion {m['id']}"
        if creator.startswith("plugin:") and not person:
            who = Caller(band=self.band, plugin=creator[7:], why=why)
        else:
            who = Caller(person=person, agent=creator if creator and creator != person else None,
                         band=self.band, why=why)
        rec = await self.ctx.call_as(who, "change.submit", spec)
        return self.end(m["id"], "done", f"change {rec['id']} submitted ({rec['state']})",
                        result={"change": rec["id"], "change_state": rec["state"]})

    def recover(self) -> None:
        """Motions a restart cut off end as failed: call again."""
        for state in OPEN:
            for m in self.rec.motion.list(where={"state": state}, limit=500):
                self.end(m["id"], "failed", "the hub restarted mid-call; check the pull request, then call again")


def _with_caller(ctx, caller):
    c = copy.copy(ctx)
    c.caller = caller
    return c


# -- hooks -----------------------------------------------------------------------------

async def start_hook(ctx):
    global _RT
    _RT = Runtime(ctx)
    _RT.recover()


async def stop_hook(ctx):
    global _RT
    if _RT is not None:
        rt, _RT = _RT, None
        await rt.close()


async def on_hold(ctx, event, payload):
    """An owner answered a sacred motion's card: no ends it; yes ran
    huddle.carry already."""
    rt = _RT
    if rt is None or not isinstance(payload, dict) or not payload.get("hold"):
        return
    found = rt.rec.motion.list(where={"hold": payload["hold"]}, limit=1)
    if found and found[0]["state"] == "asked" and payload.get("decision") == "denied":
        rt.end(found[0]["id"], "declined", f"declined by {payload.get('by') or 'an owner'}")


# -- caps ------------------------------------------------------------------------------

def _actor(ctx) -> str:
    return ctx.caller.actor or "someone"


def _mine(ctx, rec: dict) -> bool:
    c = ctx.caller
    return c.owner or (c.person == rec.get("person") if c.person else c.actor == rec.get("creator"))


async def start(ctx, title, chair=None, project=None):
    rt = _rt()
    title = _line(title, 200)
    if not title:
        raise KitError("bad args: title says what the huddle is for")
    if chair and chair != rt.setting("chair") and (ctx.caller.agent or not ctx.caller.person):
        raise KitError("denied: only a person names a huddle's chair (the chair may pin)")
    chair = chair or rt.setting("chair")
    if project:
        await rt.level(project)             # refuses a project that doesn't exist
    meta = rt.streams.open("huddle", title=title, producer="huddle", participants=[_actor(ctx)],
                           attrs={"chair": chair, **({"project": project} if project else {})})
    h = ctx.records.huddle.create(title=title, chair=chair, stream=meta.id, state="open",
                                  **({"project": project} if project else {}))
    rt.say(h["id"], f"{_actor(ctx)} opened the huddle; agent:{chair} chairs.", event="started")
    return {"huddle": h["id"], "stream": meta.id, "title": title, "chair": chair, "project": project}


async def post(ctx, huddle, text, mention=None):
    rt = _rt()
    h = rt.huddle(huddle, open_=True)
    mentions = list(dict.fromkeys(re.findall(r"@([a-z][a-z0-9_-]{0,39})", text) + ([mention] if mention else [])))
    ev = rt.streams.append(h["stream"], "text", {"text": text, "mentions": mentions}, by=_actor(ctx))
    out = {"huddle": h["id"], "seq": ev.seq}
    chair = h["chair"]
    if chair in mentions and ctx.caller.agent != f"agent:{chair}":
        note = (f"[huddle {h['id']}: {h['title']}] {_actor(ctx)} said: {text}\n"
                f"(Answer in the huddle: huddle.post huddle={h['id']}; pin with huddle.pin.)")
        try:
            await ctx.call("agents.chat.send", agent=chair, text=note, wait=False)
            out["chair"] = "told"
        except KitError as e:
            out["chair"] = f"not told: {e.message[:200]}"
            rt.say(h["id"], f"agent:{chair} did not get that ({e.message[:200]}).", event="chair")
    return out


async def read(ctx, huddle, after=0, limit=100, wait=0):
    rt = _rt()
    h = rt.huddle(huddle)
    out = await rt.streams.read(h["stream"], int(after), int(limit), wait=float(wait))
    return {"huddle": h["id"], "title": h["title"], "chair": h["chair"], "state": h["state"],
            "events": [e.wire() for e in out["events"]], "next": out["next"], "closed": out["closed"]}


def _may_pin(ctx, h: dict) -> None:
    c = ctx.caller
    if c.agent == f"agent:{h['chair']}" or (c.person and not c.agent):
        return
    raise KitError(f"denied: only a person or the chair (agent:{h['chair']}) pins; ask the chair "
                   f"(huddle.post with @{h['chair']}) to pin it")


async def pin(ctx, huddle, kind=None, repo=None, number=None, sha=None, path=None, url=None, text=None,
              sha256=None, title=None, note=None):
    rt = _rt()
    h = rt.huddle(huddle, open_=True)
    _may_pin(ctx, h)
    m = _PR_URL.search(str(url or ""))
    if m and kind in (None, "pr"):
        kind, repo, number = "pr", m.group(1), int(m.group(2))
    if not kind:
        raise KitError("bad args: kind is pr, commit, file, doc or link")
    why = f"Pin a {kind} on huddle {h['id']}'s table for {_actor(ctx)}"
    title, ref, content = _line(title, 200), {}, None
    if kind in ("pr", "commit", "file") and not repo:
        raise KitError(f"bad args: a {kind} pin needs repo")
    if kind == "pr":
        if not number:
            raise KitError("bad args: a pr pin needs number (or the PR's url)")
        p = await rt.gh("pr", why, repo=repo, number=int(number))
        if p["state"] != "open" and not p["merged"]:
            raise KitError(f"{repo}#{number} is closed without a merge")
        if sha and (len(sha) < 7 or not p["head"].startswith(sha.lower())):
            raise KitError(f"{repo}#{number}'s head is {p['head']}, not {sha}: pin the head")
        version, key = p["head"], f"pr:{repo.lower()}#{p['number']}"
        ref = {"repo": repo, "number": p["number"], "base": p["base"], "base_sha": p["base_sha"],
               "head_ref": p["head_ref"], "author": p["author"], "url": p["url"]}
        title = title or f"{repo}#{p['number']}: {p['title']}"[:200]
    elif kind in ("commit", "file"):
        if not sha:
            raise KitError(f"bad args: a {kind} pin needs sha")
        c = await rt.gh("commit", why, repo=repo, ref=sha)
        version = c["sha"]
        if kind == "commit":
            key = f"commit:{repo.lower()}@{version}"
            ref = {"repo": repo, "parent": (c["parents"] or [""])[0]}
            title = title or f"{repo}@{short(version)}: {c['message'].splitlines()[0] if c['message'] else ''}"[:200]
        else:
            if not path:
                raise KitError("bad args: a file pin needs path")
            await rt.gh("file", why, repo=repo, path=path, ref=version)       # it exists there
            key, ref = f"file:{repo.lower()}:{path}", {"repo": repo, "path": path}
            title = title or f"{repo}:{path}@{short(version)}"[:200]
    elif kind == "doc":
        if not text:
            raise KitError("bad args: a doc pin needs text")
        version, content = hashlib.sha256(text.encode()).hexdigest(), text
        title = title or _line(text.splitlines()[0], 80) or "document"
        key = f"doc:{title}"
    else:
        if not re.match(r"https?://", str(url or "")):
            raise KitError("bad args: a link pin needs an http(s) url")
        version, key, ref = sha256 or "unhashed", f"link:{url}", {"url": url}
        title = title or str(url)[:200]
    pinned = rt.rec.artifact.list(where={"huddle": h["id"], "key": key, "state": "pinned"}, limit=50)
    same = [a for a in pinned if a["version"] == version]
    if same:
        return {"artifact": same[0]["id"], "kind": kind, "title": same[0]["title"], "version": version,
                "already": True}
    for old in pinned:
        rt.rec.artifact.update(old["id"], revision=old["revision"], state="superseded")
    a = ctx.records.artifact.create(huddle=h["id"], kind=kind, title=title, version=version, key=key, ref=ref,
                                    state="pinned", **({"content": content} if content else {}),
                                    **({"note": _line(note, 500)} if note else {}))
    rt.say(h["id"], f"{_actor(ctx)} pinned {kind} {a['title']} @ {short(version)} (artifact {a['id']})"
           + (f", superseding {', '.join(o['id'] for o in pinned)}" if pinned else "") + ".",
           event="pinned", artifact=a["id"], version=version)
    return {"artifact": a["id"], "kind": kind, "title": a["title"], "version": version,
            "superseded": [o["id"] for o in pinned]}


def _page(text: str, offset: int) -> dict:
    text = str(text or "")
    end = offset + READ_CHARS
    return {"text": text[offset:end], "total": len(text), "next_offset": end if end < len(text) else None}


async def table(ctx, huddle, action="list", artifact=None, what="summary", path=None, offset=0, all=False):
    rt = _rt()
    h = rt.huddle(huddle)
    if action == "list":
        where = {"huddle": h["id"], **({} if all else {"state": "pinned"})}
        arts = rt.rec.artifact.list(where=where, limit=200, order="created")
        motions = rt.rec.motion.list(where={"huddle": h["id"]}, limit=50)
        return {"huddle": h["id"], "title": h["title"], "chair": h["chair"], "state": h["state"],
                "artifacts": [{k: a.get(k) for k in ("id", "kind", "title", "version", "state", "note", "ref")}
                              for a in arts],
                "motions": [{k: m.get(k) for k in ("id", "kind", "artifact", "version", "state", "outcome")}
                            for m in motions]}
    if not artifact:
        raise KitError("bad args: read names an artifact (huddle.table lists them)")
    a = rt.artifact(artifact, h)
    r, v = a["ref"] or {}, a["version"]
    why = f"Read {what} of artifact {a['id']} on huddle {h['id']}'s table for {_actor(ctx)}"
    head = {k: a.get(k) for k in ("id", "kind", "title", "version", "state", "note", "ref")}
    base = r.get("base_sha") if a["kind"] == "pr" else r.get("parent")
    if what == "summary":
        if a["kind"] == "pr":
            p = await rt.gh("pr", why, repo=r["repo"], number=r["number"])
            head["now"] = {k: p[k] for k in ("state", "merged", "draft", "head", "mergeable")}
            head["body"] = p["body"][:READ_CHARS]
        elif a["kind"] == "commit":
            c = await rt.gh("commit", why, repo=r["repo"], ref=v)
            head.update(message=c["message"][:READ_CHARS], files=c["files"][:200])
        return head
    if what == "content":
        if a["kind"] == "doc":
            return {**head, **_page(a.get("content"), offset)}
        if a["kind"] == "link":
            return head
        what, path = "file", path or r.get("path")
    if a["kind"] in ("doc", "link"):
        raise KitError(f"a {a['kind']} has only summary and content")
    if what == "checks":
        return {**head, "checks": await rt.gh("checks", why, repo=r["repo"], sha=v)}
    if what == "files":
        if a["kind"] == "commit":
            return {**head, "files": (await rt.gh("commit", why, repo=r["repo"], ref=v))["files"]}
        return {**head, "files": await rt.gh("files", why, repo=r["repo"], base=base or v, head=v)}
    if what == "diff":
        if not base:
            raise KitError("no parent commit to diff against")
        d = await rt.gh("diff", why, repo=r["repo"], base=base, head=v)
        return {**head, **_page(d["text"], offset), "truncated": d["truncated"]}
    path = path or r.get("path")
    if not path:
        raise KitError("bad args: what=file needs path")
    f = await rt.gh("file", why, repo=r["repo"], path=path, ref=v)
    return {**head, "path": path, **_page(f["text"], offset), "truncated": f["truncated"]}


async def vote(ctx, huddle, action="call", kind=None, artifact=None, reason=None, self_review=None,
               change=None, motion=None):
    rt = _rt()
    h = rt.huddle(huddle, open_=action == "call")
    if action == "status":
        if motion:
            m = rt.motion(motion)
            if m["huddle"] != h["id"]:
                raise KitError(f"motion {motion} is not in huddle {h['id']}")
            return m
        return {"motions": rt.rec.motion.list(where={"huddle": h["id"]}, limit=50)}
    if kind not in GATES:
        raise KitError("bad args: kind is merge, qa or deploy")
    a = rt.artifact(artifact, h)
    if a["state"] != "pinned":
        newer = rt.rec.artifact.list(where={"huddle": h["id"], "key": a["key"], "state": "pinned"}, limit=1)
        raise KitError(f"artifact {a['id']} was superseded; call on the newest pin"
                       + (f" ({newer[0]['id']})" if newer else ""))
    if kind in ("merge", "qa") and a["kind"] != "pr":
        raise KitError(f"a {kind} is on a pinned pull request")
    if kind == "deploy":
        if a["kind"] not in ("pr", "commit"):
            raise KitError("a deploy is of a pinned pull request or commit")
        if not isinstance(change, dict) or not change.get("scripts"):
            raise KitError("bad args: a deploy needs change: the change record's scripts, tests and "
                           "rollback (change.submit's args)")
    review = " ".join(str(self_review or "").split())
    if len(review) < SELF_REVIEW_MIN:
        raise KitError(f"self_review: your adversarial self-review of the full diff (at least {SELF_REVIEW_MIN} "
                       "characters: what you checked, what could break, why it is safe); it goes on the record")
    for m in rt.rec.motion.list(where={"artifact": a["id"]}, limit=100):
        if m["kind"] == kind and m["state"] in OPEN:
            raise KitError(f"{kind} {m['id']} on this is already {m['state']}")
    level = await rt.strictest(h.get("project"), a)
    m = ctx.records.motion.create(huddle=h["id"], artifact=a["id"], kind=kind, version=a["version"], level=level,
                                  self_review=str(self_review).strip(), gates=[], state="gating",
                                  **({"reason": _line(reason, 500)} if reason else {}),
                                  **({"change": change} if kind == "deploy" else {}))
    rt.say(h["id"], f"{_actor(ctx)} calls {kind} {m['id']} on {a['title']} @ {short(a['version'])} "
                    f"(trust: {level}). Self-review: {review[:4000]}", event="call", motion=m["id"])
    rt.spawn(rt.guarded(m["id"], rt.run(m["id"])))
    return {"motion": m["id"], "kind": kind, "artifact": a["id"], "version": a["version"], "level": level,
            "state": "gating", "gates": list(GATES[kind]),
            "after": "an owner's card" if level == "sacred" else "carried out at once"}


async def close(ctx, huddle):
    rt = _rt()
    h = rt.huddle(huddle)
    if not _mine(ctx, h):
        raise KitError("only the huddle's creator or an owner closes it")
    if h["state"] != "closed":
        rt.say(h["id"], f"{_actor(ctx)} closed the huddle.", event="closed")
        h = rt.rec.huddle.update(h["id"], revision=h["revision"], state="closed")
        try:
            rt.streams.close(h["stream"], by=_actor(ctx))
        except StreamError:
            pass
    return {"huddle": h["id"], "state": h["state"]}


async def carry(ctx, motion):
    rt = _rt()
    if ctx.caller.agent != ME or ctx.caller.person:
        raise KitError("only the huddle plugin carries a motion out: answer its card instead")
    m = rt.motion(motion)
    if m["state"] != "asked":
        raise KitError(f"motion {motion} is {m['state']}, not waiting for a card")
    rt.spawn(rt.guarded(motion, rt.carry(motion, recheck=True)))
    return {"motion": motion, "state": "carrying"}
