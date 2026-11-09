"""The hub site (castling-dashboard): a shell that hosts pages, served by the
HTTP door.

- **Sign-in.** A person signs in with their user name and password
  (:mod:`rook.core2.hub.passwords`; set by ``init`` or ``passwd``) and gets
  a session cookie (``HttpOnly``, ``SameSite=Strict``). An app holding the
  person's own door token (the desktop app) may send ``{token}`` instead; an
  agent's token never signs in. Requests the shell makes with the cookie
  also carry the session's CSRF token in ``X-Rook-CSRF``.
- **The shell** (``static/``) lists pages (``page.list``) and opens one at a
  time, the start page first (``page.site``). Its own calls are limited to
  ``page.*``. A plugin adds to the shell itself with ``shell:`` in its
  manifest: script and style files the shell's pages load (served at
  ``/web/plugin/<plugin>/<name>``), and the caps they may call through the
  shell, as the signed-in person.
- **Pages** run sandboxed: each is its own document served with
  ``Content-Security-Policy: sandbox allow-scripts`` (an opaque origin: no
  cookies, no hub API) and ``connect-src 'none'``. Opening one is a
  ``page.view`` call; it mints a page token the shell keeps and a one-time
  frame URL. The page talks to the shell with ``postMessage``
  (``window.rook.call``/``rook.on``, from ``static/bridge.js``); the shell
  forwards each call with the page token, and the hub runs it as the signed-in
  person with the page's declared ``caps`` as an extra layer (spec 8.3), plus
  the author's rights for a page an agent made. Events reach a page only when
  its ``events`` and the viewer's rights both allow them.

Routes (everything else is the JSON API in ``doors/http.py``)::

    GET  /                       the shell (signed in) or a redirect to /login
    GET  /login, POST /login     {user, password} (or an app's {token})
    POST /logout
    GET  /web/static/<file>
    GET  /web/plugin/<plugin>/<name>   a plugin's shell part (manifest ``shell:``)
    GET  /web/api/me             {person, csrf}
    POST /web/api/call           {cap, args?, why?}           page.* (and plugins' shell caps), as the person
    POST /web/api/open           {page}                       -> {token, frame, page}
    GET  /web/frame/<nonce>      the page's document, once
    POST /web/api/page           {token, cap, args?, why?, worker?}
    GET  /web/api/events?token=&since=   long poll, up to 20 s
    GET  /web/api/activity?since=        every call starting and ending, long poll
"""
from .site import Web

__all__ = ["Web"]
