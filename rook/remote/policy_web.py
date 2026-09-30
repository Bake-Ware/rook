"""Dashboard Permissions page: a minimal policy view, editor and explainer.

Until the settings framework owns the policy (``docs/design/settings.md``),
this page edits the same ``policy.json`` the MCP bridge reads (see
:mod:`rook.hub.policy`), through the same store:

* ``GET /permissions``: the page (mode, revision, lint, JSON editor, explain).
* ``GET /api/policy``: document, revision, mode, source, lint, load error,
  ticket status and recent non-allow decisions from the journal.
* ``POST /api/policy``: ``{"policy": {...}, "note"?}``. Band owners (and the
  shared dashboard password, an owner for compatibility) only; same-origin
  only; refuses a document that leaves nobody with admin on ``rook``;
  journaled as ``audit.policy``.
* ``POST /api/policy/explain``: ``{"principal", "cap", "worker"?, "role"?}``.

These routes sit behind the dashboard's auth middleware like every other
``/api`` route; the principal middleware supplies the caller.
"""

from __future__ import annotations

import html
import json
import logging
from urllib.parse import urlparse

from aiohttp import web

log = logging.getLogger("rook.remote.policy_web")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Permissions</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--warn:#a15c00;--bad:#b00020}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#aaa;--line:#333;--warn:#e0a040;--bad:#ff6b81}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:16px}
textarea{width:100%;min-height:420px;font:12px/1.4 ui-monospace,monospace;background:var(--bg);color:var(--fg);border:1px solid var(--line);box-sizing:border-box}
input{background:var(--bg);color:var(--fg);border:1px solid var(--line);padding:4px;max-width:100%}
.mut{color:var(--mut)}.warn{color:var(--warn)}.bad{color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;border:1px solid var(--line);padding:8px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
</style></head><body>
<h1>Permissions</h1>
<p class="mut">Policy <b id="mode">__MODE__</b> · rev <b id="rev">__REV__</b> · <span id="src">__SRC__</span>.
In <b>audit</b> mode nothing is denied: would-be denials are journaled so you can check them before
setting <code>"mode": "enforce"</code>. See docs/design/permissions.md.</p>
<div id="status">__STATUS__</div>
<h2>Explain</h2>
<div class="row">
 <input id="xp" placeholder="principal (role:agent, human:owner, token:&lt;agent_id&gt;, integration:telegram)" size="42">
 <input id="xc" placeholder="cap (shell.exec)" size="16">
 <input id="xw" placeholder="worker (name or id)" size="16">
 <button id="xb">Explain</button>
</div>
<pre id="xo" class="mut">-</pre>
<h2>Policy document</h2>
<textarea id="doc" spellcheck="false">__DOC__</textarea>
<div class="row"><input id="note" placeholder="change note" size="40"><button id="save">Save</button>
<span id="saved" class="mut"></span></div>
<h2>Recent non-allow decisions</h2>
<pre id="recent" class="mut">__RECENT__</pre>
<script>
async function post(url, body){
  const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return [r.ok, await r.json()];
}
document.getElementById('xb').onclick = async () => {
  const [, j] = await post('/api/policy/explain',{principal:xp.value,cap:xc.value,worker:xw.value||null});
  document.getElementById('xo').textContent = JSON.stringify(j,null,2);
};
document.getElementById('save').onclick = async () => {
  let doc; try { doc = JSON.parse(document.getElementById('doc').value); }
  catch(e){ saved.textContent = 'Not valid JSON: '+e.message; saved.className='bad'; return; }
  if (!confirm('Save a new policy revision?')) return;
  const [ok, j] = await post('/api/policy',{policy:doc,note:note.value});
  saved.textContent = ok ? ('Saved rev '+j.rev+(j.lint&&j.lint.length?' - lint: '+j.lint.join('; '):'')) : (j.error||'failed');
  saved.className = ok ? 'mut' : 'bad';
  if (ok){ document.getElementById('rev').textContent=j.rev; document.getElementById('mode').textContent=j.mode; }
};
</script></body></html>"""


class PolicyWeb:
    def __init__(self, server) -> None:
        self.server = server

    def install(self, app: web.Application) -> None:
        app.router.add_get("/permissions", self.page)
        app.router.add_get("/api/policy", self.get)
        app.router.add_post("/api/policy", self.save)
        app.router.add_post("/api/policy/explain", self.explain)

    def _authz(self):
        band = getattr(self.server, "_band", None)
        authz = getattr(band, "authz", None) or getattr(self.server, "_authz", None)
        if authz is None:
            authz = self.server._authorizer()
        return authz

    def _recent(self, limit: int = 20) -> list[dict]:
        try:
            from ..band_mcp.journal import Journal
            from ..paths import data_path
            import os
            path = data_path("journal.db", "/var/lib/rook-band-mcp/journal.db")
            if not os.path.exists(path):
                return []
            j = Journal(path)
            try:
                rows = j.query(decision="deny", limit=limit) + j.query(decision="would_deny",
                                                                        limit=limit)
            finally:
                j.close()
            rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
            return rows[:limit]
        except Exception:
            log.debug("recent decisions unavailable", exc_info=True)
            return []

    def _status(self, authz) -> dict:
        signer = authz.signer
        enabled = bool(getattr(signer, "enabled", False))
        store = authz.store
        policy = store.current()
        return {"rev": policy.rev, "mode": policy.mode, "source": store.source,
                "error": store.error, "lint": policy.lint(), "tickets": enabled,
                "op_kid": getattr(signer, "kid", None) if enabled else None}

    async def page(self, request: web.Request) -> web.Response:
        authz = self._authz()
        if authz is None:
            return web.Response(status=503, text="permissions unavailable on this hub")
        st = self._status(authz)
        notes = []
        if st["error"]:
            notes.append(f'<p class="bad">policy file invalid, last good rev kept: {html.escape(st["error"])}</p>')
        for m in st["lint"]:
            notes.append(f'<p class="warn">{html.escape(m)}</p>')
        if not st["tickets"]:
            notes.append('<p class="warn">No root signing key on this hub: calls carry no tickets.</p>')
        page = (_PAGE.replace("__MODE__", html.escape(st["mode"]))
                .replace("__REV__", str(st["rev"]))
                .replace("__SRC__", html.escape(st["source"] or ""))
                .replace("__STATUS__", "".join(notes))
                .replace("__DOC__", html.escape(json.dumps(authz.store.current().doc, indent=2)))
                .replace("__RECENT__", html.escape(json.dumps(self._recent(), indent=2)) or "-"))
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def get(self, request: web.Request) -> web.Response:
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        return web.json_response({**self._status(authz), "policy": authz.store.current().doc,
                                  "recent": self._recent()}, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl with the dashboard password)
        return urlparse(origin).netloc == request.host

    async def save(self, request: web.Request) -> web.Response:
        from ..hub.authz import current_principal, require_hub_admin
        from ..hub.policy import PolicyError, summarize_diff
        if not self._same_origin(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        refused = require_hub_admin("policy.set")
        if refused:
            return web.json_response({"error": refused}, status=403)
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        try:
            data = await request.json()
            doc = data.get("policy")
            if not isinstance(doc, dict):
                raise PolicyError("policy must be an object")
            old = authz.store.current()
            new = authz.store.save(doc)
        except (ValueError, PolicyError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except OSError as e:
            return web.json_response({"error": f"could not write policy: {e}"}, status=500)
        p = current_principal.get()
        authz.record_event("audit.policy", "rook", {
            "actor": p.id if p else None, "old_rev": old.rev, "new_rev": new.rev,
            "diff": summarize_diff(old.doc, new.doc), "note": str(data.get("note") or "")[:200]})
        return web.json_response({"ok": True, "rev": new.rev, "mode": new.mode, "lint": new.lint()})

    async def explain(self, request: web.Request) -> web.Response:
        from ..hub.authz import target_from_entry
        from ..hub.plugins.policy import _principal
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        try:
            data = await request.json()
            principal = _principal(str(data.get("principal") or ""), data.get("role"))
            cap = str(data.get("cap") or "").strip()
            if not cap:
                raise ValueError("cap is required")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        worker = str(data.get("worker") or "").strip() or None
        roster = getattr(getattr(self.server, "_band", None), "workers", {}) or {}
        wid, entry = worker, {"name": worker} if worker else None
        if worker and worker not in roster:
            named = [k for k, w in roster.items() if (w.get("name") or "").lower() == worker.lower()]
            if len(named) == 1:
                wid, entry = named[0], roster[named[0]]
        elif worker:
            entry = roster[worker]
        d = authz.store.current().evaluate([principal], cap, target_from_entry(wid, entry))
        return web.json_response(d.explain())
