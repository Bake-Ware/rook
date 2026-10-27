"""The security scan: what is open on the band and what the logs say, compared
with the last scan. Deterministic and cheap (local stores only, no band calls,
no model); it reports only what changed.

Sources (each optional: one this hub does not have is listed as skipped):

* ``workers``: band workers by id. A worker id never seen before is new.
* ``consoles``: live console rooms (named terminal sessions on the band). One
  that opened since the last scan is reported.
* ``agents``: identities seen over the MCP (presence). One never seen before
  is new.
* ``logins``: dashboard sign-ins (new account sessions) and account changes
  (account_audit: new accounts, merges, member changes, operator credentials).
* ``journal``: calls since the last scan that the authorizer denied
  (``decision = deny``), MCP calls refused for a missing or invalid key
  (``audit.denied``) and calls whose attribution could not be verified
  (``audit.unverified``).
* ``keys``: API keys created or removed.
* ``access``: access.db history since the last scan: roles, grants, revokes,
  key owners, chat links, bots and spaces.

The first scan (or one after the state was lost) is a baseline: it records
what is there and reports nothing. Findings are ``info`` or ``warn``; a burst
of refused keys (``urgent_refusals`` or more since the last scan) is
``alert``, the only urgent case.
"""
from __future__ import annotations

import collections
import logging
import time
from typing import Any

from .common import clip, plural

log = logging.getLogger("rook.hub.plugins.routines.security")

#: Account changes worth a line (google_login is covered by the session).
ACCOUNT_ACTIONS = {
    "register": "new dashboard account", "bootstrap": "operator account created",
    "operator_credentials_updated": "operator password changed", "account_merge": "accounts merged",
    "google_link": "Google sign-in linked", "google_unlink": "Google sign-in unlinked",
    "member_owner": "made a band owner", "member_member": "made a band member",
    "invite_accept": "band invite accepted", "device_login_approve": "device sign-in approved",
    "band_create": "band created", "band_delete": "band deleted",
}
ACCESS_ACTIONS = {
    "role": "role set", "grant": "access granted", "revoke": "access revoked",
    "token": "API key owner set", "link": "chat account linked", "unlink": "chat account unlinked",
    "bot": "own chat bot changed", "space": "chat space added", "space_removed": "chat space removed",
}
MAX_NAMES = 5
JOURNAL_LIMIT = 500


def _names(counter: collections.Counter) -> str:
    items = counter.most_common(MAX_NAMES)
    out = ", ".join(f"{k} x{n}" if n > 1 else str(k) for k, n in items)
    rest = len(counter) - len(items)
    return out + (f" and {rest} more" if rest > 0 else "")


class Scan:
    """One scan against ``node``'s stores. ``collect`` reads them;
    ``compare`` turns (previous state, snapshot) into findings and the next
    state."""

    def __init__(self, node: Any, clock=time.time) -> None:
        self.node = node
        self.clock = clock
        self.skipped: dict[str, str] = {}

    # -- reading -----------------------------------------------------------------
    def _try(self, name: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one unreadable source never stops the scan
            log.debug("security scan: %s unreadable", name, exc_info=True)
            self.skipped[name] = f"unreadable ({type(e).__name__})"
            return None

    def workers(self) -> dict | None:
        client = getattr(self.node, "client", None)
        if client is None:
            self.skipped["workers"] = "no band client"
            return None
        return {str(wid): str(w.get("name") or wid)
                for wid, w in dict(getattr(client, "workers", {}) or {}).items()}

    def consoles(self) -> dict | None:
        store = getattr(self.node, "consoles", None)
        if store is None:
            self.skipped["consoles"] = "no console store on this hub"
            return None
        got = store.list(state="live", limit=200)
        return {r["room"]: {"title": clip(r.get("title"), 80), "worker": r.get("worker") or "",
                            "name": r.get("name") or ""}
                for r in (got.get("rooms") or [])}

    def agents(self, since: float) -> list | None:
        chat = getattr(self.node, "chat", None)
        if chat is None or not hasattr(chat, "online"):
            self.skipped["agents"] = "no presence store on this hub"
            return None
        now = self.clock()
        return sorted({r["identity"] for r in chat.online()
                       if now - float(r.get("last_seen_age_secs") or 0) >= since - 1})

    def _accounts(self):
        a = getattr(self.node, "accounts", None)
        if a is not None and not hasattr(a, "db") and callable(a):
            a = a()
        return a if a is not None and hasattr(a, "db") else None

    def logins(self, since: float) -> dict | None:
        store = self._accounts()
        if store is None:
            self.skipped["logins"] = "no dashboard account store on this hub"
            return None
        with store.db() as db:
            sessions = db.execute(
                "SELECT u.username AS username, u.name AS name, s.authenticated AS at "
                "FROM user_sessions s LEFT JOIN users u ON u.id = s.user_id "
                "WHERE s.authenticated > ? ORDER BY s.authenticated", (since,)).fetchall()
            audit = db.execute(
                "SELECT a.ts AS ts, a.action AS action, u.username AS username "
                "FROM account_audit a LEFT JOIN users u ON u.id = a.actor "
                "WHERE a.ts > ? ORDER BY a.ts", (since,)).fetchall()
        return {"signins": [str(r["username"] or r["name"] or "someone") for r in sessions],
                "changes": [(str(r["action"]), str(r["username"] or "someone")) for r in audit
                            if str(r["action"]) in ACCOUNT_ACTIONS]}

    def journal(self, mark: dict) -> dict | None:
        """Rows since ``mark`` (``{ts, ids}``: the newest row the last scan
        saw and the call ids at that instant). The journal keeps ts to the
        millisecond, so a time cut alone would miss or repeat rows written
        around a scan; the mark does neither."""
        j = getattr(self.node, "journal", None)
        if j is None or not getattr(j, "enabled", True):
            self.skipped["journal"] = "no call journal on this hub"
            return None
        since = float(mark.get("ts") or 0)
        seen = set(mark.get("ids") or ())
        rows = {k: [r for r in j.query(since=since, limit=JOURNAL_LIMIT, **q)
                    if r.get("call_id") not in seen]
                for k, q in (("denied", {"decision": "deny"}), ("refused", {"cap_prefix": "audit.denied"}),
                             ("unverified", {"cap_prefix": "audit.unverified"}))}
        every = [r for v in rows.values() for r in v]
        top = max((float(r.get("ts") or 0) for r in every), default=None)
        nxt = {"ts": since, "ids": sorted(seen)} if top is None else \
            {"ts": top, "ids": sorted({r.get("call_id") for r in every if float(r.get("ts") or 0) == top}
                                      | (seen if top == since else set()))}
        return {
            "mark": nxt,
            "denied": collections.Counter(f"{r.get('identity') or '?'} -> {r.get('cap') or '?'}"
                                          for r in rows["denied"]),
            "refused": collections.Counter(str(r.get("identity") or "unauthenticated")
                                           for r in rows["refused"]),
            "unverified": collections.Counter(str(r.get("identity") or "?") for r in rows["unverified"]),
        }

    def keys(self) -> dict | None:
        tokens = getattr(self.node, "tokens", None)
        if tokens is None or not hasattr(tokens, "list_api_tokens"):
            self.skipped["keys"] = "no API key store on this hub"
            return None
        return {str(t.get("id")): clip(t.get("name") or t.get("agent_id") or "unnamed", 60)
                for t in tokens.list_api_tokens() if t.get("id")}

    def access(self, since: float) -> list | None:
        store = getattr(getattr(getattr(self.node, "client", None), "authz", None), "access", None)
        if store is None or not hasattr(store, "history"):
            self.skipped["access"] = "no access store on this hub"
            return None
        return [(str(r.get("action")), str(r.get("actor") or "?"), str(r.get("user_id") or ""))
                for r in reversed(store.history(limit=200))
                if float(r.get("ts") or 0) > since and str(r.get("action")) in ACCESS_ACTIONS]

    def collect(self, since: float, mark: dict | None = None) -> dict:
        mark = mark if isinstance(mark, dict) else {"ts": since, "ids": []}
        return {
            "workers": self._try("workers", self.workers),
            "consoles": self._try("consoles", self.consoles),
            "agents": self._try("agents", lambda: self.agents(since)),
            "logins": self._try("logins", lambda: self.logins(since)),
            "journal": self._try("journal", lambda: self.journal(mark)),
            "keys": self._try("keys", self.keys),
            "access": self._try("access", lambda: self.access(since)),
        }

    # -- comparing ---------------------------------------------------------------
    @staticmethod
    def compare(prev: dict, snap: dict, *, urgent_refusals: int = 10) -> tuple[list[dict], dict]:
        """``(findings, next state)``. Each finding: ``{kind, level, text}``."""
        out: list[dict] = []

        def add(kind: str, level: str, text: str) -> None:
            out.append({"kind": kind, "level": level, "text": text})

        state: dict = {}
        baseline = not prev.get("at")
        workers = snap.get("workers")
        known_w = dict(prev.get("workers") or {})
        if workers is not None:
            new = {w: n for w, n in workers.items() if w not in known_w}
            if new and not baseline and known_w:
                add("workers", "warn", "New on the band: " + ", ".join(sorted(new.values())[:MAX_NAMES])
                    + (f" and {len(new) - MAX_NAMES} more" if len(new) > MAX_NAMES else ""))
            known_w.update(workers)
        state["workers"] = known_w

        consoles = snap.get("consoles")
        if consoles is not None:
            before = prev.get("consoles") or {}
            opened = [c for rid, c in consoles.items() if rid not in before]
            if opened and not baseline:
                lines = [f"{c['title']} on {c['worker']}" for c in opened[:MAX_NAMES]]
                add("consoles", "info", f"{plural(len(opened), 'console')} opened: " + "; ".join(lines)
                    + (f"; and {len(opened) - MAX_NAMES} more" if len(opened) > MAX_NAMES else ""))
            state["consoles"] = consoles
        else:
            state["consoles"] = prev.get("consoles") or {}

        agents = snap.get("agents")
        known_a = set(prev.get("agents") or [])
        if agents is not None:
            new_a = [a for a in agents if a not in known_a]
            if new_a and not baseline and known_a:
                add("agents", "warn", "New identity over the MCP: " + ", ".join(new_a[:MAX_NAMES])
                    + (f" and {len(new_a) - MAX_NAMES} more" if len(new_a) > MAX_NAMES else ""))
            known_a.update(agents)
        state["agents"] = sorted(known_a)[:2000]

        logins = snap.get("logins")
        if logins is not None and not baseline:
            if logins["signins"]:
                add("logins", "info", "Dashboard sign-in: " + _names(collections.Counter(logins["signins"])))
            for action, who in logins["changes"]:
                add("logins", "warn", f"Account change: {ACCOUNT_ACTIONS[action]} ({who})")

        journal = snap.get("journal")
        if journal is not None:
            state["journal"] = journal["mark"]
        elif prev.get("journal"):
            state["journal"] = prev["journal"]
        if journal is not None and not baseline:
            refused = journal["refused"]
            n = sum(refused.values())
            if n:
                level = "alert" if urgent_refusals and n >= urgent_refusals else "warn"
                add("refused", level, f"{plural(n, 'MCP call')} refused for a missing or invalid key"
                    + (f" ({_names(refused)})" if refused else ""))
            denied = journal["denied"]
            n = sum(denied.values())
            if n:
                add("denied", "warn", f"{plural(n, 'call')} denied: {_names(denied)}")
            unverified = journal["unverified"]
            n = sum(unverified.values())
            if n:
                add("unverified", "info", f"{plural(n, 'call')} with unverified attribution: "
                    f"{_names(unverified)}")

        keys = snap.get("keys")
        if keys is not None:
            before = prev.get("keys")
            if before is not None and not baseline:
                made = [keys[k] for k in keys if k not in before]
                gone = [before[k] for k in before if k not in keys]
                if made:
                    add("keys", "warn", "New API key: " + ", ".join(made[:MAX_NAMES]))
                if gone:
                    add("keys", "info", "API key removed: " + ", ".join(gone[:MAX_NAMES]))
            state["keys"] = keys
        elif prev.get("keys") is not None:
            state["keys"] = prev["keys"]

        changes = snap.get("access")
        if changes and not baseline:
            c = collections.Counter(f"{ACCESS_ACTIONS[a]} by {actor}" for a, actor, _ in changes)
            add("access", "warn", "Access changed: " + _names(c))
        return out, state


def summary_line(findings: list[dict]) -> str:
    """Counts only, for the log: "Security scan: 1 new API key, 3 denied calls"."""
    c = collections.Counter(f["kind"] for f in findings)
    words = {"workers": "new worker", "consoles": "console opened", "agents": "new identity",
             "logins": "sign-in or account change", "refused": "refused-key burst",
             "denied": "denied calls", "unverified": "unverified calls", "keys": "API key change",
             "access": "access change"}
    parts = [f"{n} {words.get(k, k)}" if k not in ("denied", "unverified") else words[k]
             for k, n in c.most_common()]
    return "Security scan: " + ", ".join(parts)


def report_text(findings: list[dict]) -> str:
    order = {"alert": 0, "warn": 1, "info": 2}
    rows = sorted(findings, key=lambda f: order.get(f["level"], 3))
    return "\n".join(("! " if f["level"] == "alert" else "- ") + f["text"] for f in rows)
