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

import html
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
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
"""


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _index_html(tokens: list[dict], minted_secret: str | None,
                minted_name: str | None) -> str:
    rows = []
    for t in tokens:
        rows.append(
            f"<tr>"
            f"<td>{html.escape(str(t.get('name') or ''))}</td>"
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
    table = ("<table><tr><th>Name</th><th>Token preview</th><th>Created</th>"
             "<th>Last used</th><th>Expires</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows \
            else "<p class=empty>No API tokens yet.</p>"

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
        "</body></html>"
    )


def _is_admin(request: Request, provider: "TokenStore") -> bool:
    sid = request.cookies.get(ADMIN_COOKIE)
    return provider.admin_session_ok(sid)


def _redirect_with_secret(secret: str, name: str) -> Response:
    # One-shot query string — never persisted, only displayed once.
    qs = urlencode({"shown": secret, "name": name})
    return RedirectResponse(f"/tokens?{qs}", status_code=303)


def build_api_token_routes(provider: "TokenStore") -> list[Route]:
    async def index(request: Request) -> Response:
        if not _is_admin(request, provider):
            return HTMLResponse(_LOGIN_HTML.format(err=""))
        shown = request.query_params.get("shown")
        name = request.query_params.get("name")
        return HTMLResponse(_index_html(
            provider.list_api_tokens(), shown, name))

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
        Route("/tokens/auth", auth_submit, methods=["POST"]),
        Route("/tokens/logout", logout, methods=["POST"]),
        Route("/tokens/create", create, methods=["POST"]),
        Route("/tokens/revoke", revoke, methods=["POST"]),
    ]
