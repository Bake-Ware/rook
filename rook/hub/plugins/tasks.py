"""``task.*``: tasks, projects and concepts, as a hub plugin (worker ``rook``).

The work tree (concept > project > task) lives in the knowledge store, next
to the wiki pages it links to, so this plugin ``DEPENDS`` on ``knowledge``
and loads only where that one did. Its own ``enabled`` setting (default on,
env ``ROOK_TASKS``) turns the task tools off while keeping the wiki.

Caps (the ``rook_task`` / ``rook_project`` / ``rook_concept`` MCP tools route
to them; ``kind`` picks the record kind, default ``task``):

* ``task.read`` (risk ``read``): deck, search, list, get, context, status.
* ``task.write`` (risk ``write``): create, update, link, retract, claim,
  release (``review`` is for people, from the Knowledge page).

Handoffs stay in the MCP bridge's session store (``rook_handoff_*``), not
here: a handoff is a *thread* record whose id is shared with the call journal
and chat rooms, and it must work with this plugin off. Tasks only link to
handoffs (link kind ``handoff``; ``data.handoff`` on update/release saves one
through the bridge and links it). See docs/design/plugins.md, "Knowledge and
tasks".
"""
from __future__ import annotations

from ...core.plugin import Plugin, capability, place, setting
from .knowledge import ERRORS, LINKS_HELP, error_reply, reply, route

KINDS = ('task', 'project', 'concept')


class Tasks(Plugin):
    NAMESPACE = "task"
    NAME = "tasks"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    DEPENDS = ("knowledge",)
    SETTINGS = (
        setting("enabled", bool, default=True, env="ROOK_TASKS", apply="restart",
                label="Tasks, projects and concepts",
                help="Needs Knowledge on. Off keeps the wiki and removes the task tools. "
                     "Read at start: restart the hub to apply."),
    )
    GUIDANCE = {
        "tool:rook_task": "",
        "cap:task.": ("task.read/task.write take rook_task's action, id, query, data and request_id "
                      "plus kind=task|project|concept; the rook_task/rook_project/rook_concept tools "
                      "route to them."),
    }
    SKILL = ("### tasks\n"
             "Tasks, projects and concepts. Use `rook_task(action=\"deck\")` to see what is on; "
             "`claim` a task before working (your calls, consoles and handoffs then link to it); "
             "finish with `update` state done + `attrs.outcome` + an evidence `link`, or leave a "
             "handoff. Over the band: `task.read` (deck/search/list/get) and `task.write` "
             "(create/update/link/retract/claim/release) on worker `rook`, with "
             "`kind=task|project|concept`.\n")

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def _knowledge(self):
        kb = self.dependency("knowledge")
        if kb is None or kb.service is None:
            raise ValueError('Tasks need the knowledge plugin (its store is not open)')
        return kb

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in KINDS:
            raise ValueError('kind must be one of ' + ', '.join(KINDS))
        return kind

    @capability("read", risk="read")
    async def read(self, action: str = 'deck', kind: str = 'task', band: str | None = None,
                   id: str | None = None, query: str = '', data: dict | None = None) -> dict:
        """Read tasks/projects/concepts: deck|search|list|get|context|status.

        Same arguments and result as rook_task's read actions; kind picks
        task (default), project or concept. deck: id=project narrows."""
        return await self._knowledge().run('task', False, action, self._kind(kind), band, id, query,
                                           data, None)

    @capability("write", risk="write")
    async def write(self, action: str, kind: str = 'task', band: str | None = None,
                    id: str | None = None, data: dict | None = None,
                    request_id: str | None = None) -> dict:
        """Write tasks/projects/concepts: create|update|link|retract|claim|release|note|batch.

        Same arguments and result as rook_task's write actions (needs
        request_id); kind picks task (default), project or concept."""
        return await self._knowledge().run('task', True, action, self._kind(kind), band, id, '',
                                           data, request_id)

    def mcp_tools(self, invoke):
        """The action-style ``rook_concept`` / ``rook_project`` / ``rook_task``
        tools (names, arguments and replies kept), routed to ``task.read`` /
        ``task.write`` with their kind."""
        async def call(kind, action, band, id, query, data, request_id):
            cap = route('task', action)
            args = {'action': action, 'kind': kind, 'band': band, 'id': id, 'data': data}
            if cap.endswith('.write'):
                args['request_id'] = request_id
            else:
                args['query'] = query
            try:
                return reply(await invoke(cap, args))
            except ERRORS as error:
                return error_reply(error)

        async def rook_concept(action: str = 'search', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Concepts (why) above projects: search/list/get/create/update/link.
            create data {title, body, slug?}. Writes need request_id."""
            return await call('concept', action, band, id, query, data, request_id)

        async def rook_project(action: str = 'list', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Projects (outcomes) under concepts: list/search/get/create/update/link.
            create data {title, body, parent: concept, slug?}. States active|paused|done|archived
            (update data {cascade:true}: open tasks follow).
            What's on deck: rook_task(action="deck")."""
            return await call('project', action, band, id, query, data, request_id)

        async def rook_task(action: str = 'deck', band: str | None = None, id: str | None = None,
                            query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            return await call('task', action, band, id, query, data, request_id)
        rook_task.__doc__ = """Tasks. deck (id=project narrows): in progress with claimants and latest
handoff, blocked, paused, todo, recently done; rows have revision, unblocked. deck data {states,
done_days, fields, handoffs:true}. claim id before you work: your calls, consoles and handoffs
link to it.
create data {title, body, parent: project or task, attrs:{criteria, workers, dependencies}}.
update data {revision, patch:{state?, attrs?, title?, body?}}; states todo|in_progress|blocked|
paused|done|closed|cancelled|archived. done needs attrs.outcome + evidence link; closed (a person
said so) needs data.closed_by {who, quote, session}; blocked needs
attrs.blocked_reason or a blocked_by link; release/stopping needs data.handoff {goal, state, next_steps}.
note data {text, evidence?}. batch data {ops:[{action, id, data}]}.
release data {actor}: free a stale claim. get data {links:"all"}: + auto links.
""" + LINKS_HELP + """
search/list: 5/20 excerpts (data {limit, fields}). Writes need request_id."""
        return [rook_concept, rook_project, rook_task]


PLUGIN = Tasks
