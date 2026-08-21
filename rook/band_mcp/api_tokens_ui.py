"""Admin-gated /tokens UI for issuing long-lived API tokens.

Headless agents (cron jobs, daemons, CI) can't do an interactive login, but
they can carry a single ``Authorization: Bearer <token>`` header. This module
provides the management page where the operator (you) mints those bearers,
lists them, and revokes them. The minted tokens flow through
``TokenStore.verify_bearer`` (see :mod:`tokens`), so MCP-side verification
picks them up automatically.

Auth model:
    - A single admin password gates this page. Once entered, you get a
      short-lived signed session cookie (``rook_admin``) that lets you
      mint/revoke without re-prompting.
    - The minted API tokens themselves have no expiry by default; pass a
      ``ttl`` (seconds) on creation to bound them.
    - Secrets are shown to the operator EXACTLY ONCE, via a one-shot URL
      query param after a successful POST /tokens/create. They are never
      logged and never displayed again from the index page.
"""

from __future__ import annotations

import base64
import html
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from .chat_rooms import ChatStore
    from .tokens import TokenStore

ADMIN_COOKIE = "rook_admin"
_COOKIE_MAX_AGE = 1800  # mirror _ADMIN_SESSION_TTL in tokens.py


_LOGIN_HTML = """<!doctype html>
<html><head><title>Rook MCP — API tokens</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:24rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
h1{{font-size:1.2rem;color:#0c9}}
form{{display:flex;flex-direction:column;gap:.8rem}}
input{{padding:.6rem;background:#222;color:#eee;border:1px solid #444;border-radius:4px;font-size:1rem}}
button{{padding:.7rem;background:#0a7;color:#fff;border:0;border-radius:4px;font-size:1rem;cursor:pointer}}
.err{{color:#f55;font-size:.9rem}}
small{{color:#888;font-size:.85rem}}
</style></head>
<body>
<h1>Rook MCP — API tokens</h1>
<p><small>Issue long-lived bearer tokens for headless agents that can't
do an interactive login. Enter the admin password to continue.</small></p>
{err}
<form method="POST" action="/tokens/auth">
<input type="password" name="password" autofocus required placeholder="Admin password"/>
<button type="submit">Sign in</button>
</form>
</body></html>
"""


_PAGE_CSS = """body{font-family:system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
h1,h2{color:#0c9}
h1{font-size:1.3rem;margin:0 0 .2rem}
h2{font-size:1rem;margin:1.6rem 0 .6rem}
small{color:#888}
table{width:100%;border-collapse:collapse;margin-top:.4rem}
th,td{padding:.4rem .5rem;border-bottom:1px solid #2a2a2a;text-align:left;font-size:.92rem}
th{color:#999;font-weight:500}
form.inline{display:inline}
button{background:#0a7;color:#fff;border:0;border-radius:4px;padding:.45rem .8rem;font-size:.9rem;cursor:pointer}
button.warn{background:#a33}
input,select{padding:.45rem;background:#222;color:#eee;border:1px solid #444;border-radius:4px;font-size:.95rem}
.row{display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap;margin-top:.4rem}
.row>label{display:flex;flex-direction:column;font-size:.8rem;color:#999;gap:.2rem}
.tok-box{background:#1a2a1a;border:1px solid #0a7;border-radius:6px;padding:.8rem 1rem;margin:.6rem 0;font-family:ui-monospace,monospace;word-break:break-all;color:#0fa}
.warn-box{background:#2a1a1a;border:1px solid #a33;border-radius:6px;padding:.6rem .9rem;color:#fbb}
.empty{color:#888;font-style:italic}
.av{width:34px;height:34px;border-radius:9px;object-fit:cover;vertical-align:middle;background:#222;border:1px solid #333}
.av.ph{display:inline-flex;align-items:center;justify-content:center;color:#777;font-size:.7rem;font-weight:700;text-transform:uppercase}
form.avf{display:inline-flex;gap:.4rem;align-items:center}
form.avf input[type=file]{display:none}
button.ghost{background:#222;color:#ccc;border:1px solid #444;padding:.3rem .6rem;font-size:.8rem}
code.id{color:#9cf;font-size:.82rem}
"""


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _avatar_cell(identity: str, has_pic: bool, ver: int) -> str:
    """Picture + set/clear controls for one identity. The file never leaves
    the browser as-is: a tiny script squares + shrinks it to 96px on a canvas
    and posts the PNG data URL, so uploads stay a few KB and the server needs
    no image library."""
    eid = html.escape(identity)
    qid = html.escape(urlencode({"id": identity, "v": ver}))
    short = identity.split(":", 1)[-1]
    ini = html.escape((short[:2] or "?"))
    pic = (f"<img class=av src=\"/tokens/avatar?{qid}\" alt=\"\"/>" if has_pic
           else f"<span class=\"av ph\">{ini}</span>")
    clear = (f"<button class=ghost type=submit name=op value=clear>Clear</button>"
             if has_pic else "")
    return (f"<form class=avf method=POST action=/tokens/avatar "
            f"onsubmit=\"return avSubmit(this)\">"
            f"{pic}"
            f"<input type=hidden name=id value=\"{eid}\"/>"
            f"<input type=hidden name=data value=\"\"/>"
            f"<input type=file accept=image/* onchange=\"avPick(this)\"/>"
            f"<button class=ghost type=button "
            f"onclick=\"this.form.querySelector('[type=file]').click()\">"
            f"{'Change' if has_pic else 'Set picture'}</button>{clear}</form>")


_AVATAR_JS = """<script>
function avPick(inp){ const f=inp.files&&inp.files[0]; if(!f)return; const form=inp.form;
  const img=new Image(); const url=URL.createObjectURL(f);
  img.onload=()=>{ const S=96, c=document.createElement('canvas'); c.width=S; c.height=S;
    const x=c.getContext('2d'); const m=Math.min(img.width,img.height);
    x.drawImage(img,(img.width-m)/2,(img.height-m)/2,m,m,0,0,S,S);
    form.querySelector('[name=data]').value=c.toDataURL('image/png'); URL.revokeObjectURL(url);
    form.querySelector('[name=op]')?.remove(); form.requestSubmit(); };
  img.onerror=()=>{ alert('That file is not an image the browser can read.'); URL.revokeObjectURL(url); };
  img.src=url; }
function avSubmit(form){ const sub=form.ownerDocument.activeElement;
  if(sub&&sub.name==='op'&&sub.value==='clear')return true;   // clear button
  return !!form.querySelector('[name=data]').value; }        // only post once a picture is staged
</script>"""


def _index_html(tokens: list[dict], minted_secret: str | None,
                minted_name: str | None, avatars: dict[str, int] | None = None,
                avatars_enabled: bool = False) -> str:
    avatars = avatars or {}
    rows = []
    for t in tokens:
        ident = f"agent:{t.get('name') or t.get('id') or 'api'}"
        av = (_avatar_cell(ident, ident in avatars, avatars.get(ident, 0))
              if avatars_enabled else "<span class=empty>n/a</span>")
        rows.append(
            f"<tr>"
            f"<td>{av}</td>"
            f"<td>{html.escape(str(t.get('name') or ''))}<br>"
            f"<code class=id>{html.escape(ident)}</code></td>"
            f"<td><code>{html.escape(str(t.get('preview') or ''))}</code></td>"
            f"<td>{_fmt_ts(t.get('created_at'))}</td>"
            f"<td>{_fmt_ts(t.get('last_used_at'))}</td>"
            f"<td>{_fmt_ts(t.get('expires_at'))}</td>"
            f"<td><form class=inline method=POST action=/tokens/revoke>"
            f"<input type=hidden name=id value=\"{html.escape(str(t.get('id') or ''))}\"/>"
            f"<button class=warn type=submit "
            f"onclick=\"return confirm('Revoke {html.escape(str(t.get('name') or ''))}?')\">"
            f"Revoke</button></form></td>"
            f"</tr>"
        )
    table = ("<table><tr><th></th><th>Name / identity</th><th>Token preview</th>"
             "<th>Created</th><th>Last used</th><th>Expires</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows \
            else "<p class=empty>No API tokens yet.</p>"

    # Identities that aren't minted tokens but still show up in chat/presence.
    others = ""
    if avatars_enabled:
        fixed = [("user:operator", "the dashboard user (you, when chatting from the web UI)"),
                 ("agent:static", "the fixed ROOK_MCP_STATIC_TOKEN, if one is configured")]
        orow = "".join(
            f"<tr><td>{_avatar_cell(i, i in avatars, avatars.get(i, 0))}</td>"
            f"<td><code class=id>{html.escape(i)}</code></td>"
            f"<td><small>{html.escape(d)}</small></td></tr>" for i, d in fixed)
        # anything else that has a picture (e.g. a host-suffixed identity)
        known = {f"agent:{t.get('name') or t.get('id') or 'api'}" for t in tokens} | {i for i, _ in fixed}
        for i in sorted(avatars):
            if i in known:
                continue
            orow += (f"<tr><td>{_avatar_cell(i, True, avatars.get(i, 0))}</td>"
                     f"<td><code class=id>{html.escape(i)}</code></td><td></td></tr>")
        others = (
            "<h2>Other identities</h2>"
            "<p><small>Agents appear in chat and audit logs as "
            "<code>agent:&lt;token name&gt;</code>. If several agents share one token, "
            "have each send an <code>X-Rook-Host</code> header (e.g. in Claude Code's "
            "<code>.claude.json</code> next to <code>Authorization</code>) and they show "
            "up as <code>agent:&lt;name&gt;_&lt;host&gt;</code>, like "
            "<code>agent:claude_kaiju</code>. A picture set on the base name is used "
            "for its host variants too.</small></p>"
            f"<table><tr><th></th><th>Identity</th><th></th></tr>{orow}</table>"
            "<h2>Add a picture for another identity</h2>"
            "<form class=avf method=POST action=/tokens/avatar onsubmit=\"return avSubmit(this)\">"
            "<input name=id required placeholder=\"agent:claude_kaiju\" style=\"width:16rem\"/>"
            "<input type=hidden name=data value=\"\"/>"
            "<input type=file accept=image/* onchange=\"avPick(this)\"/>"
            "<button class=ghost type=button onclick=\"this.form.querySelector('[type=file]').click()\">"
            "Choose picture…</button></form>")

    minted_block = ""
    if minted_secret:
        minted_block = (
            f"<h2>New token issued</h2>"
            f"<p class=warn-box>Copy this now — it will not be shown again.</p>"
            f"<div class=tok-box>{html.escape(minted_secret)}</div>"
            f"<p><small>Label: <b>{html.escape(minted_name or '')}</b></small></p>"
        )

    create_form = """
<h2>Create new token</h2>
<form method=POST action="/tokens/create">
<div class=row>
  <label>Label
    <input name=name required maxlength=64 placeholder="e.g. cron-snap, ci-runner"/>
  </label>
  <label>TTL
    <select name=ttl>
      <option value="">no expiry</option>
      <option value="86400">1 day</option>
      <option value="604800">7 days</option>
      <option value="2592000">30 days</option>
      <option value="7776000">90 days</option>
      <option value="31536000">1 year</option>
    </select>
  </label>
  <button type=submit>Mint</button>
</div>
</form>
"""

    logout_form = (
        "<form class=inline method=POST action=/tokens/logout>"
        "<button class=warn type=submit>Sign out</button></form>"
    )

    return (
        "<!doctype html><html><head><title>Rook MCP — API tokens</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Rook MCP — API tokens</h1>"
        "<p><small>Use these as <code>Authorization: Bearer …</code> on "
        "<code>/mcp</code> calls from headless agents. "
        f"{logout_form}</small></p>"
        f"{minted_block}"
        "<h2>Existing tokens</h2>"
        f"{table}"
        f"{create_form}"
        f"{others}"
        f"{_AVATAR_JS}"
        "</body></html>"
    )


def _parse_data_url(u: str) -> tuple[str, bytes | None]:
    """``data:image/png;base64,…`` → (mime, bytes); (``""``, None) if malformed
    or over 256 KB decoded."""
    if not u.startswith("data:image/") or ";base64," not in u:
        return "", None
    head, b64 = u.split(";base64,", 1)
    mime = head[5:]
    if len(b64) > 360_000:
        return mime, None
    try:
        return mime, base64.b64decode(b64, validate=True)
    except Exception:
        return mime, None


def _is_admin(request: Request, provider: "TokenStore") -> bool:
    sid = request.cookies.get(ADMIN_COOKIE)
    return provider.admin_session_ok(sid)


def _redirect_with_secret(secret: str, name: str) -> Response:
    # One-shot query string — never persisted, only displayed once.
    qs = urlencode({"shown": secret, "name": name})
    return RedirectResponse(f"/tokens?{qs}", status_code=303)


def build_api_token_routes(provider: "TokenStore",
                           chat: "ChatStore | None" = None) -> list[Route]:
    av_on = chat is not None and chat.enabled

    async def index(request: Request) -> Response:
        if not _is_admin(request, provider):
            return HTMLResponse(_LOGIN_HTML.format(err=""))
        shown = request.query_params.get("shown")
        name = request.query_params.get("name")
        return HTMLResponse(_index_html(
            provider.list_api_tokens(), shown, name,
            chat.avatar_index() if av_on else {}, av_on))

    async def avatar_get(request: Request) -> Response:
        if not _is_admin(request, provider):
            return Response(status_code=401)
        ident = request.query_params.get("id", "")
        got = chat.get_avatar(ident) if av_on else None
        if not got:
            return Response(status_code=404)
        mime, data, _ = got
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "private, max-age=86400"})

    async def avatar_set(request: Request) -> Response:
        if not _is_admin(request, provider):
            return RedirectResponse("/tokens", status_code=303)
        if not av_on:
            return HTMLResponse("avatars unavailable (chat store disabled)", status_code=503)
        form = await request.form()
        ident = str(form.get("id") or "").strip()
        if str(form.get("op") or "") == "clear":
            chat.clear_avatar(ident)
            return RedirectResponse("/tokens", status_code=303)
        data_url = str(form.get("data") or "")
        mime, raw = _parse_data_url(data_url)
        if raw is None:
            return HTMLResponse("bad image upload", status_code=400)
        res = chat.set_avatar(ident, mime, raw)
        if not res.get("ok"):
            return HTMLResponse(html.escape(str(res.get("error"))), status_code=400)
        return RedirectResponse("/tokens", status_code=303)

    async def auth_submit(request: Request) -> Response:
        form = await request.form()
        password = form.get("password", "")
        sid = provider.admin_login(str(password))
        if sid is None:
            return HTMLResponse(
                _LOGIN_HTML.format(
                    err='<p class="err">Wrong password.</p>'),
                status_code=401)
        resp = RedirectResponse("/tokens", status_code=303)
        resp.set_cookie(ADMIN_COOKIE, sid, max_age=_COOKIE_MAX_AGE,
                        path="/", httponly=True, secure=True,
                        samesite="lax")
        return resp

    async def logout(request: Request) -> Response:
        sid = request.cookies.get(ADMIN_COOKIE)
        provider.admin_logout(sid)
        resp = RedirectResponse("/tokens", status_code=303)
        resp.delete_cookie(ADMIN_COOKIE, path="/")
        return resp

    async def create(request: Request) -> Response:
        if not _is_admin(request, provider):
            return RedirectResponse("/tokens", status_code=303)
        form = await request.form()
        name = str(form.get("name") or "").strip() or "unnamed"
        ttl_raw = str(form.get("ttl") or "").strip()
        ttl = int(ttl_raw) if ttl_raw.isdigit() else None
        entry = provider.mint_api_token(name=name, ttl_seconds=ttl)
        return _redirect_with_secret(entry["token"], entry["name"])

    async def revoke(request: Request) -> Response:
        if not _is_admin(request, provider):
            return RedirectResponse("/tokens", status_code=303)
        form = await request.form()
        token_id = str(form.get("id") or "").strip()
        provider.revoke_api_token(token_id)
        return RedirectResponse("/tokens", status_code=303)

    return [
        Route("/tokens", index, methods=["GET"]),
        Route("/tokens/avatar", avatar_get, methods=["GET"]),
        Route("/tokens/avatar", avatar_set, methods=["POST"]),
        Route("/tokens/auth", auth_submit, methods=["POST"]),
        Route("/tokens/logout", logout, methods=["POST"]),
        Route("/tokens/create", create, methods=["POST"]),
        Route("/tokens/revoke", revoke, methods=["POST"]),
    ]
