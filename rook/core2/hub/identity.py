"""Who a call is for and who makes it (spec 8.1, 8.5 of docs/spec/core-v2.md).

A door authenticates its caller into a :class:`Caller`: ``person`` (the
person the call is for, ``person:<name>``) and/or ``agent`` (``agent:<name>``
or ``system:<component>``). :meth:`Identities.layers` turns a caller into the
rights layers dispatch intersects: the person's preset plus their grants, and
the agent's own grants. An agent acting for a person carries both, so it gets
only what both may do; acting alone it has only its own.

In-memory hub state; persistence is the caller's business.
"""
from __future__ import annotations

import copy
import hashlib
import secrets
from dataclasses import dataclass, field

from rook.kit._types import parse_duration

from .. import rights

NONE = {"grants": []}
# system: identities are hub internals; they only call core caps (spec 13).
SYSTEM_RIGHTS = {"grants": ["core.*"]}
PREFIXES = {"person": ("person:",), "agent": ("agent:", "system:")}


@dataclass(frozen=True)
class Caller:
    person: str | None = None
    agent: str | None = None

    def problem(self) -> str | None:
        """Why this isn't a caller dispatch can act for, or None."""
        if not (self.person or self.agent):
            return "no identity"
        for kind in ("person", "agent"):
            v = getattr(self, kind)
            if v is not None and not (isinstance(v, str) and v.startswith(PREFIXES[kind])
                                      and v.partition(":")[2]):
                return f"bad {kind}"
        return None


@dataclass
class User:
    name: str
    preset: str
    grants: list = field(default_factory=list)
    created: float = 0
    used: set = field(default_factory=set)    # caps called, for approve_new


class Identities:
    """People (with presets), agents (with grants) and the tokens doors
    authenticate with."""

    def __init__(self, presets: dict | None = None):
        self.presets = copy.deepcopy(presets or rights.PRESETS)
        self.plugin_grants: dict[str, dict[str, list]] = {}   # preset -> {plugin: [pattern]}
        self.users: dict[str, User] = {}
        self.agents: dict[str, dict] = {}                     # name -> rights
        self.components: dict[str, dict] = {}                 # system:<name> -> rights beyond core.*
        self._tokens: dict[str, Caller] = {}                  # sha256(token) -> caller

    # -- setup -----------------------------------------------------------

    def add_user(self, name: str, preset: str, grants: list = (), created: float = 0) -> User:
        if preset not in self.presets:
            raise ValueError(f"unknown preset {preset!r}")
        self.users[name] = User(name, preset, list(grants), created)
        return self.users[name]

    def add_agent(self, name: str, r: dict) -> None:
        if not rights.valid_rights(r):
            raise ValueError(f"malformed rights for agent {name!r}")
        self.agents[name] = r

    def add_component(self, name: str, r: dict) -> None:
        """Rights for ``system:<name>``, a hub component acting on its own (a
        hub plugin's hooks). Unregistered components get only ``core.*``."""
        if not rights.valid_rights(r):
            raise ValueError(f"malformed rights for system:{name}")
        self.components[name] = r

    def add_plugin_presets(self, plugin: str, presets: dict) -> None:
        """A manifest's ``presets:`` section ({preset: {grants: [...]}}).
        Patterns outside the plugin's namespace are dropped when rights are
        computed (rights.preset_rights)."""
        for preset, body in (presets or {}).items():
            self.plugin_grants.setdefault(preset, {})[plugin] = list((body or {}).get("grants", ()))

    def add_token(self, caller: Caller) -> str:
        """A new door token for ``caller``; only its hash is kept."""
        if caller.problem():
            raise ValueError(caller.problem())
        token = secrets.token_urlsafe(32)
        self._tokens[digest(token)] = caller
        return token

    def add_token_digest(self, token_digest: str, caller: Caller) -> None:
        """A token stored earlier, by its :func:`digest` (the hub keeps only those)."""
        if caller.problem():
            raise ValueError(caller.problem())
        self._tokens[token_digest] = caller

    def revoke_token(self, token: str) -> None:
        self._tokens.pop(digest(token), None)

    def authenticate(self, token: str) -> Caller | None:
        return self._tokens.get(digest(token)) if isinstance(token, str) else None

    # -- questions dispatch asks ------------------------------------------

    def user(self, person: str | None) -> User | None:
        if not person or not person.startswith("person:"):
            return None
        return self.users.get(person.partition(":")[2])

    def is_owner(self, person: str | None) -> bool:
        u = self.user(person)
        return bool(u and u.preset == "owner")

    def expired(self, person: str, now: float) -> bool:
        """True when the person's preset ``expires`` (guests: 7d after the
        account was created) has passed."""
        u = self.user(person)
        exp = u and self.presets[u.preset].get("expires")
        return bool(exp) and now >= u.created + parse_duration(exp)

    def person_rights(self, person: str) -> dict:
        u = self.user(person)
        if u is None:
            return NONE
        return rights.preset_rights(self.presets[u.preset], self.plugin_grants.get(u.preset), u.grants)

    def agent_rights(self, agent: str) -> dict:
        kind, _, name = agent.partition(":")
        if kind == "system":
            return self.components.get(name, SYSTEM_RIGHTS)
        return self.agents.get(name, NONE) if kind == "agent" else NONE

    def layers(self, caller: Caller) -> list:
        """[[layer, rights], ...] for the intersection rule (spec 8.3)."""
        out = []
        if caller.person:
            out.append(["person", self.person_rights(caller.person)])
        if caller.agent:
            out.append(["agent", self.agent_rights(caller.agent)])
        return out

    def needs_approval(self, person: str | None, cap: str) -> bool:
        """``approve_new``: the person's first call of a cap waits for an owner."""
        u = self.user(person)
        return bool(u and self.presets[u.preset].get("approve_new") and cap not in u.used)

    def mark_used(self, person: str | None, cap: str) -> None:
        u = self.user(person)
        if u:
            u.used.add(cap)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
