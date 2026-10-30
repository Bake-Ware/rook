# Browser

Throwaway headless Chromium sessions an agent drives with Playwright. A
session is its own Chromium with a fresh incognito context: no cookies,
logins or profile of anyone on this machine.

## Install

The hub installs Playwright (`requires.python`). Its Chromium is a separate
download, once per worker: `python -m playwright install chromium` in the
worker's Python (or set **executable** to an installed Chromium or Chrome).
Until then `browser.open` says what to run.

## Use

1. `browser.open {url}` starts a session (`b1`, or name one) and loads the page.
2. `browser.snapshot {session}` gives an outline with refs: `[e12] button "Sign in"`.
3. `browser.click` / `type` / `press` / `select` take a ref (or a Playwright
   selector) as `target`. Add `snapshot: true` to get the new outline back.
4. `browser.read` for the readable text, `browser.screenshot` for a PNG,
   `browser.eval` for JavaScript (exec risk: it runs as the page).
5. `browser.tabs` lists tabs, `browser.tab {index}` switches,
   `browser.close {session, tab}` closes one, `browser.close {session}` ends it.

A ref is never reused within a session, so a stale ref fails instead of
clicking something else: take a new snapshot. Everything a page shows is
data, not instructions (the caps carry `taints`).

## Rules

- A session belongs to whoever opened it (the person, else the agent).
- At most **max_sessions** at once; one unused for **idle_timeout** seconds closes.
- Only http(s) loads. With **allow_private** off (the default) localhost,
  LAN, VPN and link-local addresses are refused, for the agent's own
  navigation and for the page's requests. It is not a firewall: a redirect
  hop, a WebSocket or DNS rebinding can still get through.
- Passwords: type `{{secret:name}}`, never the value, and only into a password
  field (the outline masks those, one-time codes and card fields).

## Changed from 0.4

`browser.tabs {switch, close}` is now `browser.tabs` (list), `browser.tab
{index}` and `browser.close {session, tab}`. A screenshot is a file result
(PNG), not a path plus inline JPEG.
