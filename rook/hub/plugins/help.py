"""``help``: the agent skill, served in layers when an agent needs it.

Instead of front-loading every rule in the skill, agents ask:

* ``help`` — the core model (what Rook is, the session loop, the rules that
  always apply) and a menu of topics, one line each;
* ``help topic=<name>`` — one subsystem: when to use it, how, its hygiene
  and common mistakes. ``topic`` may also be a tool (``rook_task``), a cap
  (``shell.exec``), a hub plugin with a skill fragment (``voice``) or a few
  words (``timed out``);
* ``help topic=<name> detail=true`` — the same plus its reference pages.

The text is ``skills/rook/help/*.md`` (:mod:`rook.band_mcp.help_topics`),
the same files the skill package ships. Read-only; no MCP tool of its own, so
connects pay nothing for it: ``rook_call(worker="rook", cap="help")``.
"""
from __future__ import annotations

from typing import Any

from ...band_mcp import help_topics
from ...core.plugin import Plugin, capability, place
from ...core.pluginkit import CapError

NEXT = ('rook_call(worker="rook", cap="help", args={"topic": "<topic>"}); '
        'add "detail": true for the reference pages')


class Help(Plugin):
    NAMESPACE = "help"
    NAME = "help"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### help\n"
             "`rook_call(worker=\"rook\", cap=\"help\")` returns the core model and the topic menu; "
             "`args={\"topic\": …}` one topic (a name, a tool, a cap, a hub plugin or a few words); "
             "`\"detail\": true` adds its reference pages.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    def _fragments(self) -> dict[str, Any]:
        """Hub plugins with a skill fragment, by namespace and by name."""
        out: dict[str, Any] = {}
        if self._node is None:
            return out
        for p in self._node.host.plugins:
            if p.SKILL and p is not self:
                out.setdefault(p.NAMESPACE, p)
                if p.NAME:
                    out.setdefault(p.NAME, p)
        return out

    @capability("", risk="read", scope="self")
    def help(self, topic: str = "", detail: bool = False) -> dict:
        """Rook help for agents: no topic gives the core model and the topic
        menu; ``topic`` (a name, a tool, a cap, a hub plugin or a few words)
        gives that subject; ``detail=true`` adds its reference pages."""
        lib = help_topics.library()
        fragments = self._fragments()
        query = (topic or "").strip()
        if not query:
            return {"core": lib.core.body, "topics": lib.menu(),
                    "plugins": sorted({p.NAMESPACE for p in fragments.values()}), "next": NEXT}
        found = lib.resolve(query)
        if found is not None and found.name != help_topics.CORE:
            out = {**found.brief(), "text": found.body, "tools": list(found.tools)}
            if detail:
                out["reference"] = lib.reference(found)
            elif found.refs:
                out["more"] = 'add "detail": true for ' + ", ".join(found.refs)
            return out
        if found is not None:
            return {**found.brief(), "text": found.body, "topics": lib.menu(), "next": NEXT}
        plugin = fragments.get(query.lower())
        if plugin is not None:
            return {"topic": plugin.NAMESPACE, "title": plugin.NAME or plugin.NAMESPACE,
                    "text": plugin.SKILL.strip() + "\n", "caps": sorted(plugin.caps())}
        matches = lib.search(query)
        if matches:
            return {"query": query, "matches": [t.brief() for t in matches], "next": NEXT}
        raise CapError(f"no help topic {query!r}. Topics: "
                       + ", ".join(t["topic"] for t in lib.menu())
                       + "; or a hub plugin: " + ", ".join(sorted({p.NAMESPACE for p in fragments.values()})))


PLUGIN = Help
