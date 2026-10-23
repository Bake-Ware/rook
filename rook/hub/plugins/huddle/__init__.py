"""``huddle.*``: huddles, as a hub plugin (worker ``rook``).

A huddle is a group chat room for a project (or a whole band) where agents
report what they are working on, say when they are blocked, ask the person
for decisions and work with each other in the person's view. It is an
ordinary hub chat room (the ``chat`` plugin's store), so every chat client
reads it; the structure (status strip, pinned questions) lives beside it, see
:mod:`.service`.

A **chaired** huddle (``huddle.start``, docs/design/huddles.md 5) is a
thread with the person, the house agent in the chair and a set of roles:
each post from the person starts a round (:mod:`.chair`): the chair calls on
roles, they answer in parallel, the chair closes, and action cards wait for
the person's approval.

Caps (the ``rook_huddle`` MCP tool routes to them):

* ``huddle.read`` (risk ``read``): ``read`` a huddle (statuses, questions,
  messages since a seq; for a chaired one also its rounds and cards),
  ``list`` every huddle, ``mine`` (your questions). ``huddle.list``: the
  huddles (``archived`` too on request).
* ``huddle.write`` (risk ``write``): ``status``, ``ask``, ``say``,
  ``withdraw`` (your own open question).
* ``huddle.start``, ``huddle.post``, ``huddle.card``, ``huddle.archive``
  (risk ``write``): chaired huddles. Only people approve or dismiss cards.

Answering is for the person, from the dashboard (``/huddle/account-api``,
:mod:`.web`): an answer is posted to the room, unpins the question and wakes
the asking agent the way ``rook_chat_wake`` does.
"""
from __future__ import annotations

import json
import logging

import asyncio
import contextlib
import time

from ....core.plugin import Plugin, capability, place, setting
from ..rooms import caller
from .chair import Chair, CardError
from .service import SYSTEM, HuddleService, HuddleStore
from .table import ALTERNATES, AUTO, VOTERS, GitHubError, Table, TableError

log = logging.getLogger("rook.hub.plugins.huddle")

READS = ("read", "list", "mine")
WRITES = ("status", "ask", "say", "withdraw")
CHAIRED = ("start", "post", "card", "archive")
TABLE = ("table", "pin", "vote")
ERRORS = (ValueError, KeyError, TypeError, PermissionError, GitHubError)
SWEEP_S = 3600.0


class Huddle(Plugin):
    NAMESPACE = "huddle"
    NAME = "huddle"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    DEPENDS = ("chat",)
    SETTINGS = (
        setting("enabled", bool, default=True, env="ROOK_HUDDLE", apply="restart",
                label="Huddles",
                help="Group rooms where agents post status and ask you questions, shown on Work > "
                     "Huddle. Read at start: restart the hub to apply."),
        setting("wake_on_answer", bool, default=False, label="Wake the agent when you answer",
                help="After you answer a question, start the asking agent again on the machine it "
                     "says it runs on (hermes or agent.wake), with the agent's own permissions. Off: "
                     "it sees the answer on its next call."),
        setting("reply_chars", int, 1200, min=200, max=8000, group="Chaired huddles",
                label="Role reply length",
                help="Each role is asked to keep its answer under this many characters. Its "
                     "answer is posted whole (a chat post past 8000 characters is cut, with a "
                     "[truncated: N of M chars] marker)."),
        setting("role_retries", int, 1, min=0, max=3, group="Chaired huddles",
                label="Retries for a role that fails",
                help="A role whose turn failed or ran out of tool calls is asked again this many "
                     "times in a round, with a narrower question, before the chair closes."),
        setting("max_followups", int, 2, min=0, max=5, group="Chaired huddles",
                label="Follow-ups per round",
                help="How many times in one round the chair may call on roles again when the "
                     "thread is not settled (a role failed, or a question is still open), before "
                     "it closes and says what is still open."),
        setting("max_role_turns", int, 6, min=1, max=20, group="Chaired huddles",
                label="Role turns per round",
                help="At most this many roles answer in one round (the chair picks them; "
                     "roles never call each other)."),
        setting("retention_days", int, 90, min=0, max=3650, group="Chaired huddles",
                label="Keep archived huddles (days)",
                help="Archived chaired huddles are deleted this many days after they were "
                     "archived (0: kept forever). Huddles that are not archived are kept."),
        setting("default_roles", list, ["pm", "qa", "lead"], group="Chaired huddles",
                label="Default roles",
                help="Roles a new huddle starts with when none are named (only those that "
                     "exist on the Roles tab)."),
        setting("votes", bool, True, group="Conference table and votes",
                label="Conference table and votes",
                help="The chair pins artifacts (a PR, a commit, test output, a document, a link) "
                     "at an exact version for every role to read, and calls votes on merge, "
                     "ready for QA, deploy and ship: gates first, then the required roles vote "
                     "blind; the hub carries out a passed vote (docs/manual/conference-table.md)."),
        setting("vote_voters", dict, {k: list(v) for k, v in VOTERS.items()},
                group="Conference table and votes", label="Required voters",
                help="Per action (merge, qa, deploy, ship) the seats that must all vote yes. "
                     "A seat 'critic|skeptic' is the first of those roles that exists and may "
                     "vote. Authors and the proposer never vote on their own work."),
        setting("vote_alternates", dict, {k: list(v) for k, v in ALTERNATES.items()},
                group="Conference table and votes", label="Stand-in voters",
                help="Who fills a seat when its role wrote the work (e.g. lead -> architect). "
                     "With nobody to stand in, the seat counts as couldn't verify."),
        setting("vote_auto", list, list(AUTO), group="Conference table and votes",
                label="Carry out after a passed vote",
                help="Actions the hub carries out on its own after every gate and every required "
                     "voter says yes (the trust ramp). Others (deploy, ship) ask you with one "
                     "Yes / No / Explain line. Sacred targets always ask you."),
        setting("vote_github_secret", str, "", group="Conference table and votes",
                label="Hub GitHub credential (vault secret name)",
                help="The vault secret the hub merges with (and reads GitHub with). Sessions and "
                     "roles never see it. Blank: the hub reads public repositories anonymously "
                     "and cannot merge."),
        setting("vote_merge_method", str, "merge", choices=("merge", "squash", "rebase"),
                group="Conference table and votes", label="Merge method"),
        setting("vote_require_ci", bool, True, group="Conference table and votes",
                label="Require CI",
                help="The CI and tests gates fail when no check ran on the voted commit."),
        setting("vote_test_checks", list, ["*test*"], group="Conference table and votes",
                label="Test checks",
                help="Check names (globs) that count as the test run for the tests gate."),
        setting("vote_gate_wait_minutes", int, 20, min=0, max=120,
                group="Conference table and votes", label="Wait for CI (minutes)",
                help="How long the gates wait for running checks before they fail."),
        setting("vote_deploy_targets", list, [], group="Conference table and votes",
                label="Deploy targets",
                help="Deploy and ship votes name one: {id, title, dry_run: {worker, cap, args}, "
                     "deploy: {worker, cap, args}}; '{sha}' in args is the voted commit. The "
                     "dry run is the rollback gate; both run as the house agent under the trust "
                     "map."),
        setting("speak_devices", list, [], label="Play on devices",
                help="Workers a message's Speak menu may play on (voice.speak, e.g. your "
                     "phone). Blank: every online worker with voice.speak except the "
                     "protected devices (role.protected_workers), and none when no devices are "
                     "marked protected. Protected devices are never offered."),
    )
    GUIDANCE = {
        "tool:rook_huddle": "",
        "cap:huddle.": "huddle.read/huddle.write take rook_huddle's action, room/project/band, text and "
                       "data; huddle.start/post/card/archive are chaired huddles (room = huddle).",
    }
    SKILL = ("### huddles\n"
             "A huddle is a project's (or band's) group room in the person's view. `rook_huddle(action="
             "\"status\", project=..., data={working_on, blocked_on?, needs_decision?, task?})` when you "
             "start, get stuck or finish; `action=\"ask\"` with `text` (and `data.options`) for a "
             "decision only the person can make: it stays pinned until answered, and the answer comes "
             "back as a chat message (and wakes you if your worker can be woken). `read` shows the "
             "status strip, questions and recent messages.\n"
             "A *chaired* huddle (`action=\"start\"`, data {title, roles}) is the person's standup "
             "with the house agent in the chair and roles (pm, qa, lead): your posts there are a "
             "guest's and start a round only when they mention @<role> or the chair. You are never "
             "called on. Action cards are approved by the person, not by agents.\n")

    def __init__(self) -> None:
        super().__init__()
        self.service: HuddleService | None = None
        self._node = None
        # Set by the MCP bridge: callable() -> the attributed caller (audit
        # dict, with ``host``) of the current MCP tool call, or None.
        self.attribution = None

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def bind_host(self, node) -> None:
        self._node = node
        chat = getattr(node, "chat", None)
        if chat is None:
            rooms = node.plugin("chat")
            chat = getattr(rooms, "_store", None)
        if chat is None or not getattr(chat, "enabled", False):
            log.warning("huddle: the chat store is not open; huddles are off")
            return
        try:
            store = HuddleStore(chat.path)
        except Exception:
            log.exception("huddle: could not open its tables in %s", chat.path)
            return
        self.service = HuddleService(chat, store, project=self._project, bands=self._bands,
                                     link=self._link, client=lambda: getattr(node, "client", None),
                                     origin=self._origin,
                                     wake_on_answer=lambda: bool(self.settings.get("wake_on_answer", False)))
        self.service.chair = Chair(self.service, lambda: self._node, self._chair_settings)
        self.service.table = Table(self.service, lambda: self._node, self._table_settings)
        self.service.relayed = self._relayed

    def _relayed(self, kind: str, ids: list) -> dict:
        ap = self._node.plugin("approvals") if self._node is not None else None
        return ap.relay.relayed(kind, ids) if ap is not None else {}

    def _chair_settings(self) -> dict:
        return {k: self.settings.get(k) for k in ("reply_chars", "max_role_turns", "retention_days",
                                                  "default_roles", "role_retries", "max_followups",
                                                  "votes")}

    def _table_settings(self) -> dict:
        return {k: self.settings.get(k) for k in (
            "vote_voters", "vote_alternates", "vote_auto", "vote_github_secret",
            "vote_merge_method", "vote_require_ci", "vote_test_checks", "vote_gate_wait_minutes",
            "vote_deploy_targets")}

    async def start(self) -> None:
        if self.service is None:
            return
        with contextlib.suppress(Exception):
            self.service.chair.resume()
        with contextlib.suppress(Exception):
            self.service.table.resume()
        self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                self.sweep()
            await asyncio.sleep(SWEEP_S)

    def sweep(self, now: float | None = None) -> list[str]:
        """Delete archived chaired huddles past ``huddle.retention_days``."""
        days = int(self.settings.get("retention_days") or 0)
        if self.service is None or days <= 0:
            return []
        gone = []
        for room in self.service.store.expired((now or time.time()) - days * 86400):
            self.service.chat.delete(room, None)
            self.service.store.drop(room)
            gone.append(room)
        if gone:
            log.info("huddle: deleted %d archived huddles past retention", len(gone))
        return gone

    def chaired(self, room: str) -> bool:
        """Is ``room`` a huddle the house agent chairs (its chat loop leaves it alone)?"""
        return self.service is not None and self.service.store.chaired(room)

    async def stop(self) -> None:
        sweeper = getattr(self, "_sweeper", None)
        if sweeper is not None:
            sweeper.cancel()
        if self.service is not None:
            for task in list(self.service.wakes) + list(self.service.chair.tasks):
                task.cancel()
            self.service.store.close()
            self.service = None

    # -- what the service needs from the rest of the hub ---------------------
    def _knowledge(self):
        kb = self._node.plugin("knowledge") if self._node is not None else None
        return getattr(kb, "service", None)

    def _project(self, ref: str, write: bool = True):
        """The project ``ref`` (id, slug or speakable name) names; a read
        (``write`` False) may take a near-miss name (``resolved``)."""
        svc = self._knowledge()
        if svc is None:
            return None
        store = svc.store
        band, rid, resolved = store.resolve(ref, [b["id"] for b in svc.bands()], write=write)
        with store.db(False) as db:
            r = store._get(db, band, rid)
        if r["kind"] != "project":
            raise ValueError(f"{ref!r} is a {r['kind']}, not a project")
        return {"id": r["id"], "slug": r["slug"], "title": r["title"], "band": r["band"],
                **({"resolved": resolved} if resolved else {})}

    def _bands(self):
        svc = self._knowledge()
        if svc is not None:
            return svc.bands()
        enrollment = getattr(self._node, "enrollment", None)
        if enrollment is not None:
            try:
                return [{"id": b["id"], "name": b["name"], "label": b["label"], "primary": bool(b["is_primary"])}
                        for b in enrollment.bands(active_only=True)]
            except Exception:
                log.exception("huddle: listing bands failed")
        return []

    async def _link(self, project: dict, band, room: str) -> None:
        """Record the huddle on its project (a ``chat`` link), so the project
        page lists it with the project's other rooms."""
        svc = self._knowledge()
        if svc is None:
            return
        await svc.dispatch("link", band, None, project["id"], "",
                           {"kind": "chat", "ref": room, "relation": "discussed_in", "note": "Huddle"},
                           f"huddle-link:{room}", actor=SYSTEM)

    def _origin(self):
        """The current caller's authenticated principal (the MCP wrapper's)
        and the host its attribution names. Band callers have none."""
        from ...authz import current_principal
        p = current_principal.get()
        if p is None or getattr(p, "kind", "") == "band":
            return None
        host = None
        if self.attribution is not None:
            try:
                host = (self.attribution() or {}).get("host")
            except Exception:
                log.exception("huddle: attribution lookup failed")
        return {"principal": {"id": p.id, "kind": p.kind, "role": p.role, "groups": list(p.groups),
                              "verified": bool(p.verified), "label": p.label}, "host": host}

    def _svc(self) -> HuddleService:
        if self.service is None:
            raise ValueError("huddles are off on this hub (the chat store is not open)")
        return self.service

    # -- caps ----------------------------------------------------------------
    @capability("read", risk="read")
    async def read(self, action: str = "read", room: str | None = None, project: str | None = None,
                   band: str | None = None, since_seq: int = 0, limit: int = 30) -> dict:
        """Read huddles: read (room|project|band: statuses, questions, messages after since_seq), list, mine.

        read marks the messages read for you; with since_seq 0 it returns the
        last ``limit`` messages. mine: your questions, open and recently answered."""
        svc, me = self._svc(), caller()
        if action == "list":
            return {"huddles": svc.huddles()}
        if action == "mine":
            return {"questions": svc.mine(me)}
        if action == "read":
            # A read changes nothing: no huddle is made, nobody joins.
            return await svc.view(me, since_seq=int(since_seq or 0), limit=max(1, min(int(limit), 200)),
                                  create=False, room=room, project=project, band=band)
        raise ValueError("action must be one of " + ", ".join(READS))

    @capability("write", risk="write")
    async def write(self, action: str, room: str | None = None, project: str | None = None,
                    band: str | None = None, text: str | None = None, data: dict | None = None,
                    mention=None) -> dict:
        """Post in a huddle: status (data {working_on, blocked_on?, needs_decision?, task?}), ask (text, data {options?, task?}), say (text, mention?), withdraw (data {question}).

        The huddle is room=, else project='s, else band='s (default: the
        primary band's); it is made on first use and you join it."""
        svc, me = self._svc(), caller()
        where = {"room": room, "project": project, "band": band}
        if action == "status":
            return await svc.status(me, data or {}, **where)
        if action == "ask":
            return await svc.ask(me, text or (data or {}).get("text"), data, **where)
        if action == "say":
            if isinstance(mention, str):
                mention = [m.strip() for m in mention.split(",") if m.strip()]
            return await svc.say(me, text or "", mention, **where)
        if action == "withdraw":
            return svc.withdraw(me, int((data or {}).get("question") or 0))
        raise ValueError("action must be one of " + ", ".join(WRITES))

    # -- chaired huddles ------------------------------------------------------
    @capability("list", risk="read")
    def list_(self, archived: bool = False) -> dict:
        """Every huddle: chaired ones (roles, open cards) and project/band status huddles; archived ones with archived=true."""
        return {"huddles": self._svc().huddles(archived=bool(archived))}

    @capability("start", risk="write")
    async def start_(self, title: str = "", roles=None, band: str | None = None) -> dict:
        """Start a chaired huddle: you, the house agent in the chair and roles (ids; default huddle.default_roles; [] for a 1:1 with the chair). Returns {room, title, roles}."""
        return await self._svc().start(caller(), title, roles, band)

    @capability("post", risk="write")
    async def post(self, huddle: str, text: str, mention=None, wait: bool = False) -> dict:
        """Post in a chaired huddle. A person's post starts a round (the chair calls on roles; @role calls one directly); an agent's only when it mentions @<role> or the chair. wait: return when the round ends."""
        if isinstance(mention, str):
            mention = [m.strip() for m in mention.split(",") if m.strip()]
        svc = self._svc()
        out = await svc.post(caller(), huddle, text, mention)
        if wait and out.get("round"):
            r = await svc.chair.wait(out["round"], 600.0)
            out["state"] = r.get("state")
            out["messages"] = [m for m in svc.chat.tail(out["room"], 30) if m["seq"] > out["seq"]]
            out["cards"] = svc.store.cards(out["room"], out["round"])
        return out

    @capability("card", risk="write")
    async def card(self, huddle: str, card: str = "", action: str = "approve",
                   data: dict | None = None) -> dict:
        """An action card: approve (runs its call as you, under the band's policy) or dismiss; people only. propose (data {kind, summary, worker?, cap?, args?}) adds one."""
        from ...authz import current_principal
        try:
            return await self._svc().card(huddle, card, action, caller(), current_principal.get(),
                                          data)
        except CardError as e:
            raise ValueError(str(e)) from None

    @capability("archive", risk="write")
    def archive(self, huddle: str, archived: bool = True) -> dict:
        """Archive a huddle (hidden from the list, kept; deleted after huddle.retention_days) or unarchive it."""
        return self._svc().archive(huddle, archived)

    # -- the conference table and votes (table.py) -------------------------------
    def _table(self) -> Table:
        svc = self._svc()
        if svc.table is None or not self.settings.get("votes", True):
            raise ValueError("the conference table is off on this hub (huddle.votes)")
        return svc.table

    @capability("table", risk="read")
    async def table(self, huddle: str, action: str = "list", artifact: str = "",
                    what: str = "summary", path: str = "", offset: int = 0) -> dict:
        """A chaired huddle's conference table: list (pinned artifacts, and votes with ballots sealed until all are in), read (artifact, what: summary|diff|files|file|checks|comments|content, path, offset: always at the pinned version)."""
        t = self._table()
        room = self._svc()._chaired(huddle)["room"]
        try:
            if action == "list":
                return {"room": room, **t.view(room)}
            if action == "read":
                return await t.read(room, artifact, what, path, int(offset or 0), caller())
        except TableError as e:
            raise ValueError(str(e)) from None
        raise ValueError("action must be list or read")

    @capability("pin", risk="write")
    async def pin(self, huddle: str, kind: str, repo: str = "", number: int = 0, sha: str = "",
                  path: str = "", url: str = "", text: str = "", title: str = "",
                  note: str = "", sha256: str = "") -> dict:
        """Pin an artifact on a chaired huddle's table at its exact version: pr (repo, number), commit (repo, sha), file (repo, path, sha), doc|tests (text), link (url, sha256?)."""
        me = caller()
        if str(me).startswith("role:"):
            raise PermissionError("roles do not pin; ask the chair to")
        data = {k: v for k, v in {"kind": kind, "repo": repo, "number": number, "sha": sha,
                                  "path": path, "url": url, "text": text, "title": title,
                                  "note": note, "sha256": sha256}.items() if v}
        try:
            return await self._table().pin(huddle, me, data)
        except TableError as e:
            raise ValueError(str(e)) from None

    @capability("vote", risk="write")
    async def vote(self, huddle: str, action: str = "call", kind: str = "", artifact: str = "",
                   reason: str = "", target: str = "", skip_vote: bool = False,
                   motion: str = "") -> dict:
        """Votes on a chaired huddle's table: call (kind merge|qa|deploy|ship, artifact, reason?, target? for deploy/ship, skip_vote? only on ship-it), status (motion). People and the house agent call votes; roles vote only when the hub asks them."""
        from ...authz import current_principal
        me = caller()
        t = self._table()
        room = self._svc()._chaired(huddle)["room"]
        try:
            if action == "status":
                m = t.motion(motion, room)
                return {**m, "ballots": t.ballots(m["id"])}
            if action != "call":
                raise ValueError("action must be call or status")
            p = current_principal.get()
            home = self._node.plugin("home") if self._node is not None else None
            person = str(me).startswith(("user:", "human:", "person:")) or \
                getattr(p, "kind", "") in ("user", "person", "operator")
            if not person and (home is None or me != home.identity):
                raise PermissionError("only a person or the house agent (the chair) calls a "
                                      "vote; agents ask the chair in the huddle")
            return await t.call(room, me, {"kind": kind, "artifact": artifact, "reason": reason,
                                           "target": target, "skip_vote": skip_vote})
        except TableError as e:
            raise ValueError(str(e)) from None

    # -- MCP -----------------------------------------------------------------
    def mcp_tools(self, invoke):
        async def rook_huddle(action: str = "read", project: str | None = None, text: str | None = None,
                              data: dict | None = None) -> str:
            """Huddle: a project's group room the person watches (project=; none: the band's).
            status data {working_on, blocked_on?, needs_decision?, task?}: when you start, stall or
            finish. ask text, data {options?}: a decision only the person can make; pinned until
            answered; the answer comes back as chat and wakes you. say text. read (data.since_seq,
            data.room), mine, list. Chaired (person, house agent, roles): start data {title,
            roles}; post text data {room}; card data {room, card, action}; archive data {room}.
            table data {room, artifact?, what?}; pin, vote: the conference table."""
            d = dict(data or {})

            def failed(error):
                out = {"ok": False, "error": str(error), "code": type(error).__name__}
                if getattr(error, "candidates", None):   # a name that was not exactly one record's
                    out.update({k: v for k, v in error.payload().items() if k != "error"})
                return json.dumps(out, separators=(",", ":"))
            if action in TABLE:
                room = d.pop("room", None)
                if action == "table":
                    cap, args = "huddle.table", {"huddle": room,
                                                 "action": "read" if d.get("artifact") else "list",
                                                 "artifact": d.get("artifact"),
                                                 "what": d.get("what"), "path": d.get("path"),
                                                 "offset": d.get("offset")}
                elif action == "pin":
                    cap, args = "huddle.pin", {"huddle": room, **{k: d.get(k) for k in (
                        "kind", "repo", "number", "sha", "path", "url", "title", "note",
                        "sha256")}, "text": text or d.get("text")}
                else:
                    cap, args = "huddle.vote", {"huddle": room, "action": d.get("action") or "call",
                                                **{k: d.get(k) for k in (
                                                    "kind", "artifact", "reason", "target",
                                                    "skip_vote", "motion")}}
                try:
                    result = await invoke(cap, {k: v for k, v in args.items() if v is not None})
                    return json.dumps({"ok": True, "result": result}, separators=(",", ":"),
                                      ensure_ascii=False, default=str)
                except ERRORS as error:
                    return failed(error)
            if action in CHAIRED:
                room = d.pop("room", None)
                if action == "start":
                    cap, args = "huddle.start", {"title": text or d.get("title") or "",
                                                 "roles": d.get("roles"), "band": d.get("band")}
                elif action == "post":
                    cap, args = "huddle.post", {"huddle": room, "text": text,
                                                "mention": d.get("mention")}
                elif action == "card":
                    cap, args = "huddle.card", {"huddle": room, "card": d.get("card") or "",
                                                "action": d.get("action") or "approve",
                                                "data": d.get("card_data")}
                else:
                    cap, args = "huddle.archive", {"huddle": room,
                                                   "archived": d.get("archived", True)}
                try:
                    result = await invoke(cap, {k: v for k, v in args.items() if v is not None})
                    return json.dumps({"ok": True, "result": result}, separators=(",", ":"),
                                      ensure_ascii=False)
                except ERRORS as error:
                    return failed(error)
            where = {"room": d.pop("room", None), "project": project, "band": d.pop("band", None)}
            if action in READS:
                cap, args = "huddle.read", {"action": action, **where, "since_seq": d.get("since_seq")}
            else:
                cap = "huddle.write"
                args = {"action": action, **where, "text": text, "data": d, "mention": d.pop("mention", None)}
            try:
                result = await invoke(cap, {k: v for k, v in args.items() if v is not None})
                if action == "read":
                    result = lean_view(result)
                return json.dumps({"ok": True, "result": result}, separators=(",", ":"), ensure_ascii=False)
            except ERRORS as error:
                return failed(error)
        return [rook_huddle]


def lean_view(view: dict) -> dict:
    """What an agent needs from a read, in fewer tokens: no participants,
    no read receipts, compact messages and statuses."""
    msgs = [{k: m[k] for k in ("seq", "sender", "text", "kind") if m.get(k) is not None} for m in view["messages"]]
    extra = {}
    if view.get("roles") is not None:
        extra = {"roles": view["roles"],
                 "cards": [{k: c[k] for k in ("id", "name", "kind", "by", "summary", "state", "seq", "error")
                            if c.get(k) is not None} for c in view.get("cards") or []]}
        tb = view.get("table") or {}
        if tb.get("artifacts") or tb.get("motions"):
            extra["table"] = {
                "artifacts": [{k: a.get(k) for k in ("n", "id", "kind", "title", "version", "state")}
                              for a in tb.get("artifacts") or []],
                "motions": [{k: m.get(k) for k in ("id", "name", "kind", "artifact", "version",
                                                   "state")} for m in tb.get("motions") or []]}
    named = {k: view[k] for k in ("name", "resolved") if view.get(k)}
    return {"room": view["room"], **named, "title": view["title"], **extra,
            "statuses": [{k: s[k] for k in ("identity", "state", "working_on", "blocked_on", "needs_decision",
                                            "task", "ts") if s.get(k)} for s in view["statuses"]],
            "questions": [{k: q[k] for k in ("id", "asker", "text", "state", "options", "answer", "answered_by")
                           if q.get(k)} for q in view["questions"]],
            "messages": msgs, "last_seq": view["last_seq"]}


PLUGIN = Huddle
