"""``persona.*``: one agent persona carried to every harness through Rook.

Profiles (name, voice/tone, rules, do/don't, formatting, per-harness addenda)
live on the hub with scoped assignments (default, band, agent family, user)
and an attributed history (docs/design/persona.md). Delivery:

* MCP ``initialize`` instructions: the bridge appends the caller's resolved
  persona to the ``server`` guidance slot (:meth:`PersonaPlugin.instructions_for`),
  trimmed to ``model.MCP_BUDGET`` characters. No persona assigned: the
  instructions are unchanged.
* Worker cap ``persona.apply`` (rook/worker/plugins/persona.py) writes a
  delimited marker block into CLAUDE.md / AGENTS.md / SOUL.md, fetching the
  text from ``persona.render`` here.
* Work launches (``work.stream.open``) fetch ``persona.render`` for the
  harness and pass it on the command line (terminals.persona_args).
* The skill's site overlay (``references/site.md``) gets a persona note.
* The voice service reads ``assistant_name`` / ``owner`` from
  ``settings.fetch("voice")``; this plugin fills them from the ``voice``
  family persona when they are not set explicitly.

Caps on worker ``rook`` (none has a dedicated MCP tool):

* ``persona.get`` / ``persona.list`` / ``persona.history`` / ``persona.render``: read.
* ``persona.set`` / ``persona.assign`` / ``persona.delete``: admin
  (``require_hub_admin``: band owners and operator tokens, whatever the
  policy mode).
"""

from __future__ import annotations

import logging
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place
from . import model
from .model import PersonaError

log = logging.getLogger("rook.hub.plugins.persona")


def _actor() -> str:
    ident = context.caller_identity.get()
    return str(ident) if ident else "system:rook-hub"


def _admin_gate(what: str) -> None:
    from ...authz import require_hub_admin
    refusal = require_hub_admin(what)
    if refusal:
        raise PermissionError(refusal)


def _caller_ids() -> list[str]:
    """Identifiers of the MCP caller for user-scope matching."""
    try:
        from ....band_mcp import attribution
        att = attribution.current.get()
    except Exception:
        return []
    if att is None or not att.verified:
        return []
    return [x for x in (att.agent_id, att.label) if x]


class PersonaPlugin(Plugin):
    NAMESPACE = "persona"
    NAME = "persona"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SKILL = ("### persona\n"
             "One persona for every harness. `rook_call(cap=\"persona.get\", worker=\"rook\", "
             "args={\"family\": \"claude-code\"})` returns the persona that applies to you "
             "(user > family > band > default) with its rendered `text`. To install it in a "
             "harness file on a machine: `rook_call(cap=\"persona.apply\", worker=\"<name>\", "
             "args={\"harness\": \"claude-code\", \"dry_run\": true})` (then without `dry_run`; "
             "`remove=true` takes it out). It only edits between its own markers. "
             "`persona.set` / `persona.assign` are admin: ask the user first.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self._store: model.PersonaStore | None = None

    # -- wiring --------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self) -> model.PersonaStore:
        if self._store is None:
            self._store = model.PersonaStore(self.data_dir / "persona.db", self.migrate)
        return self._store

    async def start(self) -> None:
        try:
            self.store
        except Exception:
            log.exception("persona store unavailable")

    # -- delivery helpers (in-process, used by the bridge and settings) --------
    def resolve(self, users=(), fam: str = "", band: str = "") -> tuple[dict | None, dict]:
        try:
            return self.store.resolve(users, fam, band)
        except Exception:
            log.exception("persona resolution failed")
            return None, {}

    def instructions_for(self, users=(), fam: str = "", band: str = "") -> str:
        """The persona section for MCP ``initialize`` instructions ('' = none)."""
        doc, _src = self.resolve(users, fam or "mcp", band)
        if doc is None:
            return ""
        return model.compact(model.render(doc, fam or "mcp"))

    def site_note(self) -> str:
        """Short note for the skill's site overlay (default persona only)."""
        doc, _ = self.resolve()
        if doc is None:
            return ""
        return ("# Persona\n\nThis band sets an agent persona. It arrives in the MCP "
                "instructions; `persona.get` on worker `rook` returns the version for your "
                "harness.\n\n" + model.render(doc, heading=False))

    def settings_fetch_extra(self, namespace: str, values: dict, users: dict) -> None:
        """Hook called by ``SettingsService.fetch``: for ``voice``, fill
        ``assistant_name`` / ``owner`` from the persona for family ``voice``
        where the voice settings leave them blank (hub-wide values, then each
        user's own)."""
        if namespace != "voice":
            return
        doc, _ = self.resolve((), "voice")
        if doc is not None:
            if not values.get("assistant_name") and doc.get("name"):
                values["assistant_name"] = doc["name"]
            if not values.get("owner") and doc.get("owner"):
                values["owner"] = doc["owner"]
        try:
            user_rows = [r for r in self.store.assignments() if r["scope"] == "user"]
        except Exception:
            return
        for row in user_rows:
            udoc, src = self.resolve((row["target"],), "voice")
            if udoc is None or src.get("scope") != "user":
                continue
            mine = users.setdefault(row["target"], {})
            if not mine.get("assistant_name") and udoc.get("name"):
                mine["assistant_name"] = udoc["name"]
            if not mine.get("owner") and udoc.get("owner"):
                mine["owner"] = udoc["owner"]

    # -- read caps ---------------------------------------------------------------
    @capability("get", risk="read")
    def get(self, id: str = "", family: str = "", user: str = "", band: str = "",
            harness: str = "") -> dict:
        """A persona profile with its rendered ``text``.

        ``id`` names a profile. Without it, returns the one that applies to
        you (or to ``user``) for ``family`` (claude-code, codex, hermes,
        voice, mcp) and ``band``: user > family > band > default. ``harness``
        picks the addendum rendered into ``text`` (defaults to ``family``)."""
        fam = model.family(family)
        if id:
            doc = self.store.profile(id)
            if doc is None:
                raise PersonaError(f"no profile {id!r}")
            src = {}
        else:
            users = [user] if user else _caller_ids()
            doc, src = self.store.resolve(users, fam, band)
        if doc is None:
            return {"profile": None, "text": "", "note": "No persona assigned."}
        return {"profile": doc, "source": src, "text": model.render(doc, harness or fam)}

    @capability("render", risk="read")
    def render(self, harness: str = "", profile: str = "", user: str = "", band: str = "") -> dict:
        """Just the rendered persona for a harness: ``{text, profile, rev, sha}``.

        Callable over the band (workers fetch it for ``persona.apply`` and
        launches). ``profile`` forces one profile; else scoped resolution for
        the harness family. Empty ``text`` means no persona applies."""
        fam = model.family(harness)
        if profile:
            doc, src = self.store.profile(profile), {"profile": profile}
            if doc is None:
                raise PersonaError(f"no profile {profile!r}")
        else:
            doc, src = self.store.resolve([user] if user else _caller_ids(), fam, band)
        if doc is None:
            return {"text": "", "profile": None}
        text = model.render(doc, fam)
        return {"text": text, "profile": doc["id"], "rev": doc["rev"],
                "sha": model.fingerprint(text), "source": src}

    @capability("list", risk="read")
    def list_(self) -> dict:
        """Every profile (id, name, rev) and every scoped assignment."""
        return {"profiles": self.store.profiles(), "assignments": self.store.assignments(),
                "scopes": list(model.SCOPES), "families": list(model.KNOWN_FAMILIES)}

    @capability("history", risk="read", limit=20)
    def history(self, id: str = "", scope: str = "", target: str = "") -> list[dict]:
        """Attributed changes, newest first. ``id`` for a profile's revisions;
        ``scope``/``target`` for an assignment (``scope="default"``)."""
        ref = id or (f"{scope}:{target if scope != 'default' else ''}" if scope else "")
        return self.store.history(ref, limit=200)

    # -- admin caps -----------------------------------------------------------------
    @capability("set", risk="admin")
    def set(self, profile: dict, note: str = "", dry_run: bool = False,
            expect_rev: int | None = None) -> dict:
        """Create or replace a profile (band owners and operator tokens).

        ``profile``: {id, name, owner, voice, rules[], do[], dont[],
        formatting, addenda{family: text}, description}. The rev goes up by
        one; ``expect_rev`` refuses a stale edit; ``dry_run`` validates and
        previews without saving."""
        if not dry_run:
            _admin_gate(f"persona.set {profile.get('id') if isinstance(profile, dict) else ''}")
        return self.store.save(profile, _actor(), note, expect_rev, dry_run)

    @capability("assign", risk="admin")
    def assign(self, scope: str, profile: str = "", target: str = "", note: str = "") -> dict:
        """Assign a profile at a scope: ``default`` (everyone), ``band``
        (band id), ``family`` (claude-code, codex, hermes, voice, mcp, ...) or
        ``user`` (account id, token agent_id or label). Empty ``profile``
        removes the assignment."""
        _admin_gate(f"persona.assign {scope}:{target}")
        return self.store.assign(scope, target, profile, _actor(), note)

    @capability("delete", risk="admin")
    def delete(self, id: str, note: str = "") -> dict:
        """Delete an unassigned profile (its history stays)."""
        _admin_gate(f"persona.delete {id}")
        return self.store.delete(id, _actor(), note)

    # -- settings page (operator account; called by settings_web) --------------------
    def page(self) -> dict:
        profiles = []
        for p in self.store.profiles():
            doc = self.store.profile(p["id"])
            profiles.append({**p, "doc": doc, "text": model.render(doc)})
        return {"profiles": profiles, "assignments": self.store.assignments(),
                "scopes": list(model.SCOPES), "families": list(model.KNOWN_FAMILIES),
                "history": self.store.history(limit=30), "mcp_budget": model.MCP_BUDGET}

    def page_action(self, data: dict, actor: str) -> Any:
        action = data.get("action")
        note = str(data.get("note") or "")
        if action == "persona_save":
            return self.store.save(data.get("profile"), actor, note, data.get("rev"),
                                   bool(data.get("dry_run")))
        if action == "persona_assign":
            return self.store.assign(str(data.get("scope") or ""), str(data.get("target") or ""),
                                     str(data.get("profile") or ""), actor, note)
        if action == "persona_delete":
            return self.store.delete(str(data.get("id") or ""), actor, note)
        raise PersonaError("use persona_save, persona_assign or persona_delete")


PLUGIN = PersonaPlugin
