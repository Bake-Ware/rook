"""``knowledge.*``: the shared wiki, as a hub plugin (worker ``rook``).

One store (``knowledge.db``) holds every record kind: wiki pages
(``knowledge``) and the work tree (``concept`` > ``project`` > ``task``),
with links, claims, events and idempotency receipts. This plugin owns that
store, its migrations, search and the settings for both; the ``task.*``
plugin (:mod:`rook.hub.plugins.tasks`) serves the work kinds on the same
store and depends on this one.

Caps (``rook_call(cap=..., worker="rook")``; the ``rook_knowledge`` MCP tool
routes to them):

* ``knowledge.read`` (risk ``read``): search, get, list, context, status,
  bands, deck. Reachable from the band within the hub's band risk ceiling.
* ``knowledge.write`` (risk ``write``): create, update, link, retract.

Both take the ``rook_knowledge`` arguments (``action``, ``band``, ``id``,
``query``, ``data``, ``request_id``) and return the same result, with the lean
MCP defaults for search/list (5/20 rows, a few fields; ``data.limit`` and
``data.fields`` override).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ....core.context import current_identity
from ....core.plugin import Plugin, capability, place, resource, setting
from .hygiene import NOTIFY_KINDS, HygieneEngine
from .search import DEFAULT_MODEL, Search
from .service import WRITES, KnowledgeService
from .store import LINK_KINDS, RELATIONS

log = logging.getLogger("rook.hub.plugins.knowledge")

#: Read actions (the rest of ``KnowledgeService.dispatch``'s actions are WRITES).
READS = ('bands', 'deck', 'hygiene', 'list', 'get', 'search', 'context', 'status')
ERRORS = (ValueError, KeyError, TypeError, PermissionError)


def reply(result) -> str:
    """The MCP tools' reply shape (unchanged from the pre-plugin tools)."""
    return json.dumps({'ok': True, 'result': result}, separators=(',', ':'), ensure_ascii=False)


def error_reply(error: Exception) -> str:
    out = {'ok': False, 'error': str(error), 'code': type(error).__name__}
    if getattr(error, 'revision', None) is not None:
        out['current_revision'] = error.revision
    return json.dumps(out, separators=(',', ':'), ensure_ascii=False)


def check_action(namespace: str, action: str, write: bool) -> None:
    """Keep reads and writes on their own caps, so a band caller held to
    ``read`` cannot write through ``*.read``."""
    if write and action not in WRITES:
        raise ValueError(f'{action!r} is not a write; use {namespace}.read')
    if not write and action in WRITES:
        raise ValueError(f'{action!r} is a write; use {namespace}.write')


def route(namespace: str, action: str) -> str:
    return f'{namespace}.write' if action in WRITES else f'{namespace}.read'


# Full relation list is in the error a bad value gets; list the common ones.
LINKS_HELP = ('link data {kind, ref, relation?, note?}: kind '
              + '|'.join(k for k in LINK_KINDS if k != 'human')
              + '; relation evidence (default)|produced|blocked_by|supersedes|touched|… '
              + 'retract id=<link id>.')
assert {'evidence', 'produced', 'blocked_by', 'supersedes', 'touched'} <= set(RELATIONS)


class Knowledge(Plugin):
    NAMESPACE = "knowledge"
    NAME = "knowledge"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SETTINGS = (
        setting("enabled", bool, default=False, env="ROOK_KNOWLEDGE", apply="restart",
                group="General", label="Knowledge and tasks",
                help="Shared wiki plus concept/project/task records and the hygiene nudges. "
                     "Read at start: restart the hub to apply."),
        setting("db_path", "path", default="", env="ROOK_KNOWLEDGE_DB", apply="restart",
                group="General", advanced=True, label="Database file",
                help="Empty: knowledge.db beside the hub's other stores (the journal's directory)."),
        setting("semantic", bool, default=True, env="ROOK_KNOWLEDGE_SEMANTIC",
                apply="restart", group="Search", label="Semantic search",
                help="Blend embedding similarity into search when an embedding service is set; "
                     "off = keyword search only."),
        resource("embedder", default=None, env="ROOK_EMBED_URL", label="Embedding service",
                 apply="restart", group="Search",
                 help="http(s)://host:port/embed (services/knowledge-embeddings) or a band cap "
                      "such as cap://any/embed.text; both take {texts} and return {model, vectors}. "
                      "Empty: keyword search only."),
        setting("embed_model", str, default=DEFAULT_MODEL, env="ROOK_EMBED_MODEL",
                apply="restart", group="Search", label="Embedding model",
                help="Must match the model the embedding service reports."),
        # Hygiene triggers (hygiene.py, docs/design/hygiene.md). All live.
        setting("hygiene_enabled", bool, default=True, group="Hygiene", order=1,
                label="Hygiene triggers",
                help="Findings when work looks finished, idle or unrecorded: shown as _hygiene on the "
                     "actor's next MCP reply and on the deck. Never changes a task's state."),
        setting("hygiene_idle_minutes", int, default=30, min=5, max=10080, group="Hygiene", order=2,
                label="Idle claim nudge (minutes)",
                help="A claimed task idle this long with work since its last handoff gets a nudge "
                     "(into the agent's session when possible, else on its next reply)."),
        setting("hygiene_dirty_hours", float, default=4.0, min=0.1, max=720, group="Hygiene", order=3,
                label="Mark needs_hygiene after (hours)"),
        setting("hygiene_release_hours", float, default=24.0, min=1, max=2160, group="Hygiene", order=4,
                label="Propose releasing an idle claim after (hours)"),
        setting("hygiene_renotify_hours", float, default=6.0, min=0.25, max=720, group="Hygiene", order=5,
                label="Repeat an open nudge after (hours)",
                help="Also the rate limit: the same finding is not raised again within this period."),
        setting("hygiene_signal_hours", float, default=24.0, min=1, max=720, group="Hygiene", order=6,
                label="Work-signal findings expire after (hours)", advanced=True),
        setting("hygiene_done_window_days", float, default=3.0, min=0, max=365, group="Hygiene", order=7,
                label="Check done tasks for knowledge (days back)",
                help="Tasks finished longer ago than this are not checked by the scan."),
        setting("hygiene_project_idle_hours", float, default=24.0, min=0, max=2160, group="Hygiene", order=8,
                label="Propose closing a finished project after (hours)"),
        setting("hygiene_hints_per_reply", int, default=1, min=0, max=5, group="Hygiene", order=9,
                label="Nudges per MCP reply", help="0 = deck and rook_task(action=\"hygiene\") only."),
        setting("hygiene_scan_seconds", int, default=300, min=30, max=86400, group="Hygiene", order=10,
                label="Scan interval (seconds)", advanced=True),
        setting("hygiene_notify_people", bool, default=False, group="Hygiene", order=11,
                label="Also notify people",
                help="Post release proposals, ended sessions and finished projects through notify.send "
                     "(Telegram/Discord)."),
    )
    GUIDANCE = {
        "tool:rook_knowledge": "",
        "cap:knowledge.": ("knowledge.read/knowledge.write take rook_knowledge's action, id, query, "
                           "data and request_id; the rook_knowledge tool routes to them."),
    }
    SKILL = ("### knowledge\n"
             "The shared wiki. Use the `rook_knowledge` tool: `search` before starting "
             "(5 excerpts; `data.limit`/`data.fields` for more), `get` a page by id or slug, "
             "`create` a page with a unique `request_id`. Over the band the same actions are "
             "`knowledge.read` (search/get/list/context/status/bands) and `knowledge.write` "
             "(create/update/link/retract) on worker `rook`; band callers reach only the read cap "
             "by default. Semantic search needs an embedding service (setting `embedder`).\n")

    def __init__(self) -> None:
        super().__init__()
        self.service: KnowledgeService | None = None
        # Set by the MCP bridge: callable() -> the attributed caller (audit
        # dict) of the current MCP tool call, or None outside one.
        self.principal = None
        self._node = None
        self._maintain: asyncio.Task | None = None
        self._hygiene: asyncio.Task | None = None

    # -- setup -------------------------------------------------------------
    def db_path(self) -> Path:
        configured = self.settings.get("db_path")
        if configured:
            return Path(configured)
        root = self.__dict__.get("_data_root")
        # Pre-plugin location: beside the hub's other stores (<state>/plugins
        # is the plugin data root, so its parent is the state directory).
        # Kept so an existing knowledge.db is used in place, and a rollback
        # to an older release still finds it.
        return (Path(root).parent if root else self.data_dir) / "knowledge.db"

    def _search(self, store) -> Search:
        res = None
        url = ""
        try:
            res = self.resource("embedder")
        except ValueError:
            log.warning("knowledge: embedder setting is not a valid connection string; keyword search only")
        if res is not None and res.scheme in ("http", "https"):
            url, res = res.url, None
        elif res is not None and res.scheme != "cap":
            log.warning("knowledge: embedder must be http(s):// or cap://, not %s://", res.scheme)
            res = None
        return Search(store, url=url, model=self.settings.get("embed_model") or DEFAULT_MODEL,
                      resource=res, semantic=bool(self.settings.get("semantic", True)))

    def available(self) -> bool:
        if not self.settings.get("enabled", False):
            return False
        try:
            self.service = KnowledgeService(self.db_path(), self._principal, search=self._search)
            self.service.hygiene = HygieneEngine(self.service.store, conf=self._hygiene_conf)
        except Exception:
            log.exception("knowledge store unavailable; knowledge and task caps disabled")
            return False
        return True

    def bind_host(self, node) -> None:
        self._node = node

    async def start(self) -> None:
        if self.service is not None and self._maintain is None:
            self._maintain = asyncio.get_running_loop().create_task(self.service.maintain())
        if self.service is not None and self._hygiene is None:
            self._hygiene = asyncio.get_running_loop().create_task(self._hygiene_loop())

    async def stop(self) -> None:
        for name in ('_maintain', '_hygiene'):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, name, None)

    # -- hygiene -------------------------------------------------------------
    def _hygiene_conf(self) -> dict:
        return {s.name: self.settings.get(s.name) for s in self.SETTINGS if s.name.startswith('hygiene_')}

    async def hygiene_tick(self, now=None) -> list:
        """One scan, then notify people of the new findings that warrant it."""
        engine = self.service.hygiene if self.service is not None else None
        if engine is None:
            return []
        raised = await asyncio.to_thread(engine.scan, now)
        if raised and engine.cfg('hygiene_notify_people') and self._node is not None:
            notify = self._node.plugin('notify')
            for f in raised:
                if notify is None or f['kind'] not in NOTIFY_KINDS or not f.get('text'):
                    continue
                try:
                    await notify.send('Rook hygiene: ' + f['text'].replace('[[', '').replace(']]', ''))
                except Exception:
                    log.exception('hygiene notify failed')
        return raised

    async def _hygiene_loop(self) -> None:
        while True:
            try:
                await self.hygiene_tick()
            except Exception:
                log.exception('hygiene scan failed')
            engine = self.service.hygiene if self.service is not None else None
            await asyncio.sleep(max(30, int(engine.cfg('hygiene_scan_seconds') if engine else 300)))

    # -- attribution -------------------------------------------------------
    def _principal(self):
        """The MCP bridge's attributed caller when there is one; otherwise the
        authenticated principal of the call (permissions.md 1): a dashboard
        account, in-process hub code, or ``band:unauthenticated`` for a band
        caller, whose self-stamped identity is never taken as who it is."""
        p = None
        if self.principal is not None:
            try:
                p = self.principal()
            except Exception:
                log.exception("knowledge: principal lookup failed")
        if p:
            return p
        from ...authz import current_principal
        pr = current_principal.get()
        if pr is not None:
            return {'identity': pr.id, 'kind': pr.kind}
        # In-process hub code without a principal: the identity it passed.
        ident = current_identity()
        return {'identity': ident, 'kind': 'system'} if ident else None

    # -- caps --------------------------------------------------------------
    async def run(self, namespace: str, write: bool, action: str, kind, band, rid, query,
                  data, request_id):
        if self.service is None:
            raise ValueError('The knowledge store is not open')
        check_action(namespace, action, write)
        return await self.service.dispatch(action, band, kind, rid, query, data, request_id, lean=True)

    @capability("read", risk="read")
    async def read(self, action: str = 'search', band: str | None = None, id: str | None = None,
                   query: str = '', data: dict | None = None) -> dict:
        """Read the shared wiki: search|get|list|context|status|bands|deck.

        Same arguments and result as rook_knowledge's read actions: search
        returns 5 excerpts (data {limit, fields}); get takes an id or slug."""
        return await self.run('knowledge', False, action, None, band, id, query, data, None)

    @capability("write", risk="write")
    async def write(self, action: str, band: str | None = None, id: str | None = None,
                    data: dict | None = None, request_id: str | None = None) -> dict:
        """Write the shared wiki: create|update|link|retract (needs request_id).

        Same arguments and result as rook_knowledge's write actions."""
        return await self.run('knowledge', True, action, 'knowledge' if action == 'create' else None,
                              band, id, '', data, request_id)

    # -- MCP ---------------------------------------------------------------
    def mcp_tools(self, invoke):
        """Hand-shaped MCP tools (hub hook, see rook.hub.mcp_tools): the
        action-style ``rook_knowledge`` tool predates caps, and its name,
        arguments and replies are kept. It routes each action to
        ``knowledge.read`` or ``knowledge.write`` through ``invoke``."""
        async def rook_knowledge(action: str = 'search', band: str | None = None, id: str | None = None,
                                 query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Shared wiki: search|get|list|context|status|create|update|link|retract|bands.
            search: 5 excerpts (data {limit, fields}); get id-or-slug: full page + backlinks.
            Search before creating. create data {title, body, slug?, parent?, attrs:{knowledge_kind,
            tags, supersedes}}; parent = folder page (move: update patch {parent}). Link pages with
            [[slug]]. Correct a fact with a new page, attrs.supersedes=[old]. Set
            attrs.verification=verified only after linking traceable evidence; attrs.dispute_reason
            says what to fix. Writes need a unique request_id. Link kinds: see rook_task.
            """
            cap = route('knowledge', action)
            args = {'action': action, 'band': band, 'id': id, 'data': data}
            if cap.endswith('.write'):
                args['request_id'] = request_id
            else:
                args['query'] = query
            try:
                return reply(await invoke(cap, args))
            except ERRORS as error:
                return error_reply(error)
        return [rook_knowledge]


PLUGIN = Knowledge
