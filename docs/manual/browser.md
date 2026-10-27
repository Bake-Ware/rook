---
id: browser
title: Web browser for agents
order: 18
features:
  - plugin/worker/browser
  - settings/browser
since: 0.4.1
deprecated: false
---
# Web browser for agents

A worker can lend agents a web browser. An agent opens a **session**, loads
pages in it, looks at what is on them, clicks, types and reads, over as many
calls as it needs, and closes it when done. The browser runs out of sight
(headless) on that machine.

Each session is a fresh, throwaway Chromium: no history, no cookies, no saved
passwords, and never your own browser or its profile. When it closes,
everything in it is gone.

## Turning it on

The browser is off until you install it on the machine that should run it.
In the worker's own Python (`~/.rook-band-worker/venv` on Linux and macOS,
`%USERPROFILE%\.rook-band-worker\venv` on Windows):

```
~/.rook-band-worker/venv/bin/pip install playwright
~/.rook-band-worker/venv/bin/python -m playwright install chromium
```

(on Windows, `venv\Scripts\pip` and `venv\Scripts\python`), then restart the
worker. The download is about 150 MB. On Linux, if Chromium complains about
missing system libraries, `python -m playwright install-deps chromium` (as an
administrator) installs them. Until both steps are done, **Settings > Plugins**
shows the browser plugin as *not available* on that machine with the command
that is missing. To switch it off again on a machine, use the same page.

## What agents can do

- `browser.open` starts a session (optionally with a page), `browser.goto`
  loads another page or goes back, forward or reloads.
- `browser.snapshot` gives a short outline of the page: headings, text, and
  every link, button and field with a short reference such as `e12`.
- `browser.click`, `browser.type`, `browser.press` and `browser.select` act
  on one of those references (or on a selector).
- `browser.read` gives the page's text, `browser.wait` waits for something to
  appear, `browser.screenshot` takes a picture (kept on the machine, last 20).
- `browser.tabs`, `browser.sessions` and `browser.close` keep track of
  sessions and tabs. `browser.eval` runs JavaScript in the page; it counts as
  running code (the `exec` tier), so it is trusted like a shell.

Pages load only over `http` and `https`; local files and other kinds of
address are refused, and downloads are blocked. By default the browser also
refuses your own network: `localhost`, addresses on your LAN, VPN addresses
(such as Tailscale's `100.x`) and the like, so an agent can't use it to look
at your router or other machines. Turn on **Allow local network** for a
machine whose browser should reach those, for example a dev server.

## Settings

Under **Settings** (worker scope, per machine):

| Setting | Default | What it does |
|---|---|---|
| Headless | on | Off shows the browser window on the machine's screen. |
| Idle timeout (s) | 600 | A session nobody uses for this long closes by itself. |
| Most sessions at once | 3 | Each session is its own Chromium (150-300 MB). |
| Chromium executable | blank | Use an installed Chromium or Chrome binary instead of Playwright's own. It still gets a fresh throwaway profile. |
| Allow local network | off | On lets sessions load `localhost`, LAN and VPN addresses. |
| Chromium sandbox | on | Turn off only where Chromium cannot start its sandbox (some containers). |

## Good to know

- A session belongs to whoever opened it; others are refused.
  `browser.sessions` lists every session on the machine and who opened it.
  Don't log into your personal accounts in one; for a test login an agent
  uses a vault secret (`{{secret:name}}`) in a password field, never the
  password itself.
- Sessions live in the worker: restarting or updating the worker closes them.
- A reference from a snapshot keeps pointing at the same thing while it is on
  the page and is never reused for something else. After a click that loads
  a new page, the agent takes a new snapshot.
- Frames inside a page and some custom web components don't show in the
  outline; `browser.read`, a selector or a screenshot covers most of those.
