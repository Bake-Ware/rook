"""browser.*: throwaway headless Chromium sessions an agent drives (Playwright).

A session is a named browser of its own: its own Chromium and a fresh
incognito context, never the person's profile, cookies or logins. An agent
opens one, then navigates and acts on it over several calls; sessions idle
longer than ``idle_timeout`` close by themselves (the ``every`` hook), and at
most ``max_sessions`` run at once. One :class:`Browser` per plugin load
keeps them.

Replies are bounded for agents. ``browser.snapshot`` is a short outline of
the page with refs (``e12``) on everything clickable or typeable; the action
caps take a ref back (or a Playwright selector) as ``target``. An element
keeps its ref while it stays on the page and a session never reuses a ref,
so a stale one fails rather than hitting something else. A session belongs
to whoever opened it (the person, else the agent), and refuses local-network
addresses unless ``allow_private`` is on.

Ported from 0.4 ``rook/worker/plugins/browser.py``: args are checked by the
manifest now; ``browser.tabs`` is split into ``tabs`` (list) and ``tab``
(switch), closing a tab is ``browser.close {tab}``, and a screenshot is a
file result instead of a path plus inline base64.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REF_RE = re.compile(r"^e\d{1,6}$")
_HISTORY = ("back", "forward", "reload")
_REFUSED = ("file:", "data:", "javascript:", "blob:", "chrome:", "view-source:", "about:")
_SHOTS_KEPT = 20
_TABS_MAX = 10
_INSTALL = ("install Playwright into the worker's Python (pip install playwright), "
            "then python -m playwright install chromium")

# Walks the page once and returns one line per heading, text block and
# control, cut at maxChars. Controls are tagged with the session's own ref
# attribute (a random name the page can't guess). An element keeps its ref
# across snapshots, and new refs continue from the session's counter, so a
# ref never names a different element later, even on another page or tab.
_SNAPSHOT_JS = r"""
(opts) => {
  const ATTR = opts.attr;
  let next = Math.max(window[ATTR] || 0, opts.base);
  const root = opts.selector ? document.querySelector(opts.selector) : document.body;
  if (!root) return {error: 'selector matched nothing'};
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'svg']);
  const ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'switch', 'combobox', 'textbox',
    'searchbox', 'slider', 'spinbutton', 'treeitem']);
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"]),' +
    Array.from(ROLES).map(r => '[role=' + r + ']').join(',');
  const clip = (s, n) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };
  const shown = (el) => {
    if (el.hidden) return false;
    const st = getComputedStyle(el);
    if (st.display === 'contents') return true;
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
    return el.getClientRects().length > 0;
  };
  const interactive = (el) => {
    const t = el.tagName;
    if (t === 'A') return el.hasAttribute('href');
    if (t === 'INPUT') return el.type !== 'hidden';
    if (t === 'BUTTON' || t === 'SELECT' || t === 'TEXTAREA' || t === 'SUMMARY') return true;
    const role = el.getAttribute('role');
    if (role && ROLES.has(role)) return true;
    if (el.isContentEditable && el.hasAttribute('contenteditable')) return true;
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    return ti !== null && Number(ti) >= 0;
  };
  const kind = (el) => {
    const role = el.getAttribute('role');
    if (role) return role;
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'select';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') {
      const ty = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'text') return 'textbox';
      return ty;
    }
    if (el.isContentEditable) return 'textbox';
    return 'clickable';
  };
  const name = (el) => {
    let n = el.getAttribute('aria-label') || '';
    const by = el.getAttribute('aria-labelledby');
    if (!n && by) n = by.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    if (!n && el.labels && el.labels.length) n = Array.from(el.labels).map(l => l.innerText).join(' ');
    if (!n && el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) n = el.value;
    if (!n && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) n = el.placeholder || '';
    if (!n && el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') n = el.innerText || '';
    if (!n) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) n = img.alt; }
    if (!n) n = el.title || el.getAttribute('name') || el.id || '';
    return clip(n, 80);
  };
  const state = (el) => {
    const out = [];
    const t = el.tagName;
    if (t === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
      out.push(el.checked ? 'checked' : 'unchecked');
    } else if (t === 'INPUT' || t === 'TEXTAREA') {
      const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
      const secret = el.type === 'password' || /password|one-time-code|cc-/.test(ac);
      if (el.value) out.push('value=' + JSON.stringify(secret ? '***' : clip(el.value, 60)));
    } else if (t === 'SELECT') {
      const sel = Array.from(el.selectedOptions).map(o => clip(o.text, 40));
      out.push('selected=' + JSON.stringify(sel.join(', ')));
      const opts = Array.from(el.options).slice(0, 8).map(o => clip(o.text, 30));
      out.push('options=' + JSON.stringify(opts) + (el.options.length > 8 ? '+' + (el.options.length - 8) : ''));
    } else if (t === 'A') {
      const href = el.getAttribute('href') || '';
      if (href && !href.startsWith('javascript:')) out.push('-> ' + clip(href, 80));
    }
    if (el.disabled) out.push('disabled');
    if (el.required) out.push('required');
    return out.length ? ' ' + out.join(' ') : '';
  };
  const lines = [];
  let size = 0, refs = 0, truncated = false;
  const push = (s) => {
    if (truncated || !s) return;
    if (size + s.length + 1 > opts.maxChars) { truncated = true; return; }
    lines.push(s); size += s.length + 1;
  };
  const walk = (el) => {
    if (truncated || SKIP.has(el.tagName) || !shown(el)) return;
    const t = el.tagName;
    if (interactive(el)) {
      let ref = el.getAttribute(ATTR);
      if (!ref) { ref = 'e' + (next++); el.setAttribute(ATTR, ref); }
      refs++;
      push('[' + ref + '] ' + kind(el) + ' ' + JSON.stringify(name(el)) + state(el));
      // Its text is already its name; only nested controls still need a line.
      for (const c of el.querySelectorAll(INTERACTIVE)) if (c !== el && shown(c)) walk(c);
      return;
    }
    if (t === 'IFRAME') { push('iframe ' + clip(el.src || '', 100) + ' (contents not included)'); return; }
    if (t === 'IMG') { if (el.alt) push('img ' + JSON.stringify(clip(el.alt, 80))); return; }
    const heading = /^H([1-6])$/.exec(t);
    const hasControls = el.querySelector(INTERACTIVE) !== null;
    if (heading) {
      push('#'.repeat(Number(heading[1])) + ' ' + clip(el.innerText, 200));
      if (hasControls) for (const c of el.querySelectorAll(INTERACTIVE)) walk(c);
      return;
    }
    if (!hasControls) {
      if (t === 'LABEL' && el.control) return;
      const text = clip(el.innerText, 100000);
      if (!text) return;
      if (text.length <= 300 || el.children.length === 0) { push(clip(text, 300)); return; }
    }
    let own = '';
    // A label's text is already its control's name.
    if (!(t === 'LABEL' && el.control)) for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
    push(clip(own, 300));
    for (const c of el.children) walk(c);
  };
  walk(root);
  window[ATTR] = next;
  return {text: lines.join('\n'), refs: refs, truncated: truncated, next: next};
}
"""

_OPENS_TAB_JS = ("e => !!e.closest('a[target]:not([target=_self]):not([target=_top])"
                 ":not([target=_parent]), form[target]:not([target=_self])')")

_READ_JS = r"""
(sel) => {
  const el = sel ? document.querySelector(sel)
    : (document.querySelector('main, article, [role=main]') || document.body);
  return el ? el.innerText : null;
}
"""


class Error(Exception):
    """Stand-in until a call hands us ctx.Error (helpers below run without ctx)."""


def _clean(exc: BaseException) -> str:
    """A Playwright error as one short line: no call log, no stack."""
    text = str(exc) or type(exc).__name__
    text = text.split("Call log:")[0]
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not set(ln.strip()) <= set("=╔╗╚╝║ ")]
    return " ".join(lines)[:400] or type(exc).__name__


def check_url(url: str) -> str:
    """``url`` if it is http(s) or about:blank; a bare host gets https://."""
    url = url.strip()
    if url == "about:blank":
        return url
    if "://" not in url and not url.lower().startswith(_REFUSED):
        url = "https://" + url      # "example.com/x" is what people type
    if not url.lower().startswith(("http://", "https://")):
        raise Error("url must be http(s); file:, data:, javascript: and other schemes are refused")
    return url


def _browsers_dir() -> Path | None:
    """Where Playwright keeps its downloaded browsers on this machine."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        try:
            import playwright
        except ImportError:
            return None
        return Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def chromium_installed() -> bool:
    base = _browsers_dir()
    return base is not None and base.is_dir() and (
        any(base.glob("chromium-*")) or any(base.glob("chromium_headless_shell-*")))


def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _not_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not ip.is_global


async def private_host(host: str) -> bool:
    """Whether ``host`` is (or resolves to) a loopback, LAN, VPN, link-local or
    other non-public address. A name that doesn't resolve is left to
    Chromium, which will fail on it too."""
    host = host.strip("[]").rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        pass
    else:
        return _not_public(host)
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), 5)
    except (OSError, asyncio.TimeoutError):
        return False
    return any(_not_public(info[4][0]) for info in infos)


def _who(ctx) -> str | None:
    return ctx.person or ctx.agent


class _Session:
    def __init__(self, name: str, browser: Any, context: Any, headless: bool, owner: str | None):
        self.name = name
        self.browser = browser
        self.context = context
        self.headless = headless
        self.owner = owner
        self.page: Any = None
        self.used = time.monotonic()
        self.lock = asyncio.Lock()
        self.popped = False       # a new tab opened since the last reply
        # A ref attribute the page can't guess, and the next ref number: refs
        # are never reused within a session.
        self.attr = "data-rook-" + secrets.token_hex(4)
        self.next_ref = 1
        self.hosts: dict[str, bool] = {}     # host -> private? (the guard's cache)

    def pages(self) -> list:
        return [p for p in self.context.pages if not p.is_closed()]

    def current(self) -> Any:
        if self.page is None or self.page.is_closed():
            pages = self.pages()
            if not pages:
                raise Error(f"session {self.name} has no open tab; browser.goto with new_tab=true opens one")
            self.page = pages[-1]
        return self.page


class Browser:
    """One per plugin load: the sessions, and Playwright while any is open."""

    def __init__(self, ctx):
        self._sessions: dict[str, _Session] = {}
        self._pw: Any = None
        self._pw_lock = asyncio.Lock()

    # -- hooks -------------------------------------------------------------

    async def reap(self, ctx) -> None:
        """Every 30 s: close sessions idle past ``idle_timeout``; stop
        Playwright when none is left."""
        idle = int(ctx.settings.get("idle_timeout") or 600)
        now = time.monotonic()
        for name, s in list(self._sessions.items()):
            if now - s.used > idle and not s.lock.locked():
                await self._close(name)
        await self._stop_playwright()

    async def stop(self, ctx) -> None:
        for name in list(self._sessions):
            await self._close(name)
        await self._stop_playwright()

    async def status(self, ctx) -> dict | None:
        return {"sessions": len(self._sessions)} if self._sessions else None

    # -- sessions ----------------------------------------------------------

    async def _playwright(self) -> Any:
        async with self._pw_lock:
            if self._pw is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    raise Error("Playwright is not installed: " + _INSTALL) from None
                self._pw = await async_playwright().start()
            return self._pw

    async def _stop_playwright(self) -> None:
        async with self._pw_lock:
            if self._pw is not None and not self._sessions:
                pw, self._pw = self._pw, None
                try:
                    await pw.stop()
                except Exception:
                    pass

    def _session(self, ctx, name: str) -> _Session:
        s = self._sessions.get(name)
        if s is None:
            have = ", ".join(sorted(n for n, x in self._sessions.items() if self._yours(ctx, x))) or "none"
            raise ctx.Error(f"no browser session {name!r} (yours open: {have}); browser.open starts one")
        if not self._yours(ctx, s):
            raise ctx.Error(f"session {name} belongs to {s.owner}; open your own with browser.open")
        if not s.browser.is_connected():
            # Chromium crashed or was killed: drop the husk so the name is free again.
            self._sessions.pop(name, None)
            raise ctx.Error(f"session {name}'s browser has exited; browser.open starts a new one")
        s.used = time.monotonic()
        return s

    @staticmethod
    def _yours(ctx, s: _Session) -> bool:
        who = _who(ctx)
        return not (s.owner and who and who != s.owner)

    async def _close(self, name: str) -> bool:
        s = self._sessions.pop(name, None)
        if s is None:
            return False
        for thing in (s.context, s.browser):
            try:
                await thing.close()
            except Exception:
                pass        # already gone (crashed or closed by hand)
        return True

    # -- the local-network guard -------------------------------------------

    async def _blocked(self, ctx, s: _Session, url: str) -> bool:
        if ctx.settings.get("allow_private"):
            return False
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https", "ws", "wss"):
            return False
        host = parts.hostname or ""
        if host not in s.hosts:
            s.hosts[host] = await private_host(host)
        return s.hosts[host]

    async def _guard_route(self, ctx, s: _Session, route: Any) -> None:
        try:
            if await self._blocked(ctx, s, route.request.url):
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        except Exception:
            pass        # the page or request went away meanwhile

    async def _check_landing(self, ctx, s: _Session, page: Any) -> None:
        """Redirect hops aren't routed, so check where the tab ended up."""
        if await self._blocked(ctx, s, page.url):
            host = urlsplit(page.url).hostname or "that address"
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            raise ctx.Error(f"{host} is on a local or private network; this worker's allow_private is off")

    async def _nav(self, ctx, s: _Session, page: Any, url: str, **kw: Any) -> Any:
        if await self._blocked(ctx, s, url):
            raise ctx.Error(f"{urlsplit(url).hostname} is on a local or private network; "
                            "this worker's allow_private is off")
        try:
            resp = await page.goto(url, **kw)
        except Exception as exc:
            raise ctx.Error(_clean(exc)) from None
        await self._check_landing(ctx, s, page)
        return resp

    # -- page helpers ------------------------------------------------------

    def _target(self, s: _Session, page: Any, target: str) -> Any:
        if _REF_RE.match(target):
            return page.locator(f'[{s.attr}="{target}"]')
        return page.locator(target)

    async def _one(self, ctx, s: _Session, page: Any, target: str) -> Any:
        """The single element ``target`` names, or a plain-words failure."""
        loc = self._target(s, page, target)
        try:
            n = await loc.count()
        except Exception as exc:
            raise ctx.Error(f"bad target {target!r}: {_clean(exc)}") from None
        if n == 0:
            if _REF_RE.match(target):
                raise ctx.Error(f"ref {target} is not on this page (it changed, navigated, or the ref "
                                "is from another tab); take a new browser.snapshot")
            raise ctx.Error(f"nothing matches {target!r}")
        if n > 1:
            raise ctx.Error(f"{target!r} matches {n} elements; use a ref from browser.snapshot "
                            "or a narrower selector")
        return loc

    async def _settle(self, ctx, s: _Session, page: Any) -> None:
        """Give a click or key a moment to start a navigation, then wait
        briefly for it to load. A page that keeps loading isn't an error."""
        await asyncio.sleep(0.3)
        page = s.page if s.page is not None and not s.page.is_closed() else page
        try:
            await page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        await self._check_landing(ctx, s, page)

    async def _reply(self, ctx, s: _Session, snapshot: bool = False, **extra: Any) -> dict:
        # Read the tab and the popup flag together, before an await lets a
        # late popup event change one of them.
        popped, s.popped = s.popped, False
        page = self._current(ctx, s)
        out: dict[str, Any] = {"session": s.name, "url": page.url}
        if popped:
            out["tab"] = s.pages().index(page)
            out["note"] = "a new tab opened and is now current; browser.tabs lists them"
        try:
            out["title"] = await page.title()
        except Exception:
            out["title"] = ""
        out.update(extra)
        if snapshot:
            out["snapshot"] = (await self._snapshot(ctx, s, page, 6000))["text"]
        return out

    async def _snapshot(self, ctx, s: _Session, page: Any, max_chars: int, selector: str = "") -> dict:
        try:
            res = await page.evaluate(_SNAPSHOT_JS, {"attr": s.attr, "base": s.next_ref,
                                                     "maxChars": max_chars, "selector": selector or None})
        except Exception as exc:
            raise ctx.Error(f"snapshot failed: {_clean(exc)}") from None
        if res.get("error"):
            raise ctx.Error(res["error"])
        s.next_ref = max(s.next_ref, int(res.get("next") or 0))
        return res

    async def _act(self, ctx, coro: Any) -> Any:
        try:
            return await coro
        except ctx.Error:
            raise
        except Exception as exc:
            raise ctx.Error(_clean(exc)) from None

    @staticmethod
    def _current(ctx, s: _Session) -> Any:
        try:
            return s.current()
        except Error as e:
            raise ctx.Error(str(e)) from None

    # -- caps --------------------------------------------------------------

    async def open(self, ctx, url=None, session=None, headless=None, viewport="1280x800",
                   timeout=30, snapshot=False) -> dict:
        name = session or next(f"b{n}" for n in range(1, 1000) if f"b{n}" not in self._sessions)
        try:
            target = check_url(url) if url else ""
        except Error as e:
            raise ctx.Error(str(e)) from None
        width, height = (int(x) for x in viewport.split("x"))
        ms = timeout * 1000
        if name in self._sessions:
            existing = self._session(ctx, name)
            async with existing.lock:
                if target:
                    await self._nav(ctx, existing, self._current(ctx, existing), target, timeout=ms)
                return await self._reply(ctx, existing, snapshot, reused=True)
        most = int(ctx.settings.get("max_sessions") or 3)
        if len(self._sessions) >= most:
            raise ctx.Error(f"{len(self._sessions)} browser sessions are open (the most allowed); "
                            "browser.close one first", retry=True)
        head = ctx.settings.get("headless") if headless is None else bool(headless)
        if not head and not _has_display():
            raise ctx.Error("headless=false needs a desktop session on this machine, and it has none")
        exe = ctx.settings.get("executable")
        if exe and not Path(exe).is_file():
            raise ctx.Error(f"the executable setting names {exe}, which doesn't exist")
        if not exe and not chromium_installed():
            raise ctx.Error("Playwright's Chromium is not installed on this worker: " + _INSTALL)
        try:
            pw = await self._playwright()
        except Error as e:
            raise ctx.Error(str(e)) from None
        opts: dict[str, Any] = {"headless": head, "chromium_sandbox": bool(ctx.settings.get("sandbox"))}
        if exe:
            opts["executable_path"] = exe
        try:
            browser = await pw.chromium.launch(**opts)
        except Exception as exc:
            await self._stop_playwright()
            raise ctx.Error(f"Chromium did not start: {_clean(exc)}") from None
        try:
            # Service workers would answer requests without passing the route below.
            context = await browser.new_context(viewport={"width": width, "height": height},
                                                accept_downloads=False, service_workers="block")
        except Exception as exc:
            await browser.close()
            raise ctx.Error(f"browser context failed: {_clean(exc)}") from None
        s = _Session(name, browser, context, head, _who(ctx))

        def popped(page: Any) -> None:
            if len(s.pages()) > _TABS_MAX:
                asyncio.ensure_future(page.close())     # a page spawning popups in a loop
                return
            s.page = page
            s.popped = True

        context.on("page", popped)
        if not ctx.settings.get("allow_private"):
            guard_ctx = ctx      # the route outlives this call; it only reads settings
            try:
                await context.route("**/*", lambda route: self._guard_route(guard_ctx, s, route))
            except Exception as exc:
                await browser.close()
                raise ctx.Error(f"browser context failed: {_clean(exc)}") from None
        self._sessions[name] = s
        async with s.lock:
            s.page = await context.new_page()
            s.popped = False
            if target:
                await self._nav(ctx, s, s.page, target, timeout=ms)
            return await self._reply(ctx, s, snapshot)

    async def goto(self, ctx, session, url, wait="load", new_tab=False, timeout=30, snapshot=False) -> dict:
        s = self._session(ctx, session)
        ms = timeout * 1000
        async with s.lock:
            if new_tab:
                s.page = await s.context.new_page()
                s.popped = False
            page = self._current(ctx, s)
            if url in _HISTORY:
                nav = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[url]
                resp = await self._act(ctx, nav(wait_until=wait, timeout=ms))
                await self._check_landing(ctx, s, page)
            else:
                try:
                    url = check_url(url)
                except Error as e:
                    raise ctx.Error(str(e)) from None
                resp = await self._nav(ctx, s, page, url, wait_until=wait, timeout=ms)
            return await self._reply(ctx, s, snapshot, status=resp.status if resp else None)

    async def snapshot(self, ctx, session, max_chars=6000, selector=None) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            res = await self._snapshot(ctx, s, self._current(ctx, s), max_chars, selector or "")
            out = await self._reply(ctx, s)
            out.update(refs=res["refs"], truncated=res["truncated"], text=res["text"])
            return out

    async def click(self, ctx, session, target, double=False, snapshot=False) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            page = self._current(ctx, s)
            loc = await self._one(ctx, s, page, target)
            try:
                new_tab = await loc.evaluate(_OPENS_TAB_JS)
            except Exception:
                new_tab = False
            await self._act(ctx, loc.dblclick(timeout=10000) if double else loc.click(timeout=10000))
            if new_tab:
                for _ in range(30):         # the popup event can trail the click
                    if s.popped:
                        break
                    await asyncio.sleep(0.1)
            await self._settle(ctx, s, page)
            return await self._reply(ctx, s, snapshot)

    async def type(self, ctx, session, target, text, clear=True, submit=False, snapshot=False) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            page = self._current(ctx, s)
            loc = await self._one(ctx, s, page, target)
            if clear:
                await self._act(ctx, loc.fill(text, timeout=10000))
            else:
                await self._act(ctx, loc.press_sequentially(text, timeout=10000))
            if submit:
                await self._act(ctx, loc.press("Enter", timeout=10000))
                await self._settle(ctx, s, page)
            return await self._reply(ctx, s, snapshot)

    async def press(self, ctx, session, key, target=None, snapshot=False) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            page = self._current(ctx, s)
            if target:
                loc = await self._one(ctx, s, page, target)
                await self._act(ctx, loc.press(key, timeout=10000))
            else:
                await self._act(ctx, page.keyboard.press(key))
            await self._settle(ctx, s, page)
            return await self._reply(ctx, s, snapshot)

    async def select(self, ctx, session, target, option) -> dict:
        if not option or not all(option):
            raise ctx.Error("option needs at least one non-empty value or label")
        s = self._session(ctx, session)
        async with s.lock:
            page = self._current(ctx, s)
            loc = await self._one(ctx, s, page, target)
            chosen = await self._act(ctx, loc.select_option(option if len(option) > 1 else option[0],
                                                            timeout=10000))
            return await self._reply(ctx, s, selected=chosen)

    async def wait(self, ctx, session, text=None, target=None, state="visible", timeout=10) -> dict:
        if bool(text) == bool(target):
            raise ctx.Error("pass text or target (one of them)")
        s = self._session(ctx, session)
        async with s.lock:
            page = self._current(ctx, s)
            loc = page.get_by_text(text).first if text else self._target(s, page, target).first
            try:
                await loc.wait_for(state=state, timeout=timeout * 1000)
            except Exception as exc:
                raise ctx.Error(f"still waiting after {timeout}s: {_clean(exc)}") from None
            return await self._reply(ctx, s)

    async def read(self, ctx, session, selector=None, max_chars=8000, offset=0) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            text = await self._act(ctx, self._current(ctx, s).evaluate(_READ_JS, selector or None))
            if text is None:
                raise ctx.Error(f"nothing matches {selector!r}")
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            out = await self._reply(ctx, s, text=text[offset:offset + max_chars], total=len(text))
            if offset + max_chars < len(text):
                out["next_offset"] = offset + max_chars
            return out

    async def screenshot(self, ctx, session, target=None, full_page=False):
        s = self._session(ctx, session)
        shots = ctx.data / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{s.name}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.png"
        async with s.lock:
            page = self._current(ctx, s)
            thing = await self._one(ctx, s, page, target) if target else page
            kw: dict[str, Any] = {} if target else {"full_page": bool(full_page)}
            try:
                await thing.screenshot(path=str(path), timeout=20000, **kw)
            except Exception as exc:
                raise ctx.Error(f"screenshot failed: {_clean(exc)}") from None
        for old in sorted(shots.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-_SHOTS_KEPT]:
            old.unlink(missing_ok=True)
        return ctx.file(path, mime="image/png")

    async def eval(self, ctx, session, js, max_chars=4000) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            value = await self._act(ctx, self._current(ctx, s).evaluate(js))
            text = json.dumps(value, default=str)
            extra: dict[str, Any] = {"value": value}
            if len(text) > max_chars:
                extra = {"value": text[:max_chars], "truncated": True}
            return await self._reply(ctx, s, **extra)

    async def tabs(self, ctx, session) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            return await self._tabs(s)

    async def tab(self, ctx, session, index) -> dict:
        s = self._session(ctx, session)
        async with s.lock:
            pages = s.pages()
            if index >= len(pages):
                raise ctx.Error(f"no tab {index}: the session has {len(pages)}")
            s.page = pages[index]
            await self._act(ctx, s.page.bring_to_front())
            return await self._tabs(s)

    async def _tabs(self, s: _Session) -> dict:
        s.popped = False
        items = []
        for i, p in enumerate(s.pages()):
            try:
                title = await p.title()
            except Exception:
                title = ""
            items.append({"index": i, "url": p.url, "title": title, "current": p is s.page})
        return {"session": s.name, "tabs": items}

    async def sessions(self, ctx) -> dict:
        now = time.monotonic()
        items = []
        for s in self._sessions.values():
            page = s.page if s.page is not None and not s.page.is_closed() else None
            items.append({"session": s.name, "url": page.url if page else "", "tabs": len(s.pages()),
                          "headless": s.headless, "opened_by": s.owner, "idle_s": int(now - s.used)})
        return {"sessions": items, "max": int(ctx.settings.get("max_sessions") or 3),
                "idle_timeout": int(ctx.settings.get("idle_timeout") or 600)}

    async def close(self, ctx, session=None, tab=None, all=False) -> dict:
        if all:
            if session or tab is not None:
                raise ctx.Error("all=true closes every session of yours; leave session and tab out")
            names = [n for n, s in self._sessions.items() if self._yours(ctx, s)]
        elif not session:
            raise ctx.Error("name the session (or all=true)")
        elif tab is not None:
            s = self._session(ctx, session)
            async with s.lock:
                pages = s.pages()
                if tab >= len(pages):
                    raise ctx.Error(f"no tab {tab}: the session has {len(pages)}")
                await self._act(ctx, pages[tab].close())
                pages = s.pages()
                if s.page not in pages:
                    s.page = pages[-1] if pages else None
                return await self._tabs(s)
        else:
            names = [self._session(ctx, session).name]
        for name in names:
            await self._close(name)
        await self._stop_playwright()
        return {"closed": names}
