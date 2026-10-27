# Browser control: `browser.*` on a worker

Status: implemented (`rook/worker/plugins/browser.py`). User manual:
`docs/manual/browser.md`. Roadmap: Machine caps, "better ergonomics for
agents; Playwright/browser caps (browser control needed and shaky)".

## 1. Goals

- Let an agent drive a real web page on a worker: load it, see what is on it,
  click, type, pick, read, screenshot, over several calls.
- Replies an agent can use without wasting its context: a short outline of the
  page with handles it can pass back, not HTML or a full accessibility tree.
- Never touch the person's own browser, profile, cookies or logins.
- Off unless the machine's owner installs the browser on purpose.

Non-goals for now: driving the person's own Chrome (Claude in Chrome does
that), downloads and file uploads, iframes and shadow DOM in the outline,
browsers other than Chromium, and an Android version.

## 2. Shape

A worker plugin, namespace `browser`, on Playwright's async Python API in the
worker's own event loop. `available()` is false unless `playwright` imports
and a Chromium is installed (Playwright's own, found in its browsers folder,
or `browser.executable`); `unavailable_reason()` says which is missing, so the
Plugins page shows the install command.

A **session** is one Chromium process and one fresh browser context
(Playwright's incognito-style context: no profile directory on disk, nothing
kept after it closes). Sessions have names (`b1`, `b2`, … or the caller's
own), live across calls, and track their tabs and which one is current. A tab
a click opens becomes current and the reply says so; a session holds at most
10 tabs (further popups are closed). Each session has a lock, so two calls on
one session run one after the other. A session belongs to the identity that
opened it (`rook.core.context.current_identity()`): other callers are refused
(`belongs to …`), `close all=true` closes only the caller's own, and
`browser.sessions` lists every session with `opened_by`. A session whose
Chromium has exited is dropped on its next use.

Playwright's driver starts with the first session and stops when the last
one closes. An idle reaper closes sessions unused for `browser.idle_timeout`
seconds (default 600); `browser.max_sessions` (default 3) caps how many run.
Worker stop closes everything.

## 3. Caps

| Cap | Tier | Scope | What |
|---|---|---|---|
| `open` | write | external | Start (or reuse) a session, optionally load a URL. |
| `goto` | write | external | Load a URL, or `back`/`forward`/`reload`; `new_tab`. |
| `snapshot` | read | local | Bounded page outline with refs. |
| `click` / `type` / `press` / `select` | write | external | Act on one element by ref or selector. |
| `wait` | read | local | Wait for text or an element state. |
| `read` | read | local | The page's readable text, paged. |
| `screenshot` | read, sensitive | local | PNG on the worker; optional small JPEG inline. |
| `eval` | exec | external | Run JavaScript in the page. |
| `tabs` | write | local | List, switch, close tabs. |
| `sessions` | read | local | Open sessions on this worker. |
| `close` | write | local | Close one session or all. |

Tiers, by the worst a caller can reach: navigation and actions can submit
forms and post to sites outside the band, so they are `write` with scope
`external`; they cannot run code on the host (Chromium's sandbox is on,
downloads are refused, `file:` and other non-http schemes are refused).
`eval` runs arbitrary script as the page, so it is `exec` like the other
code-running caps. `screenshot` carries `sensitive` like `screenshot.capture`.

## 4. The outline (`browser.snapshot`)

One script walks the visible DOM and emits one line per heading (`# Title`),
text block and control. Each control (links, buttons, inputs, selects,
textareas, ARIA widget roles, contenteditable, `onclick`, focusable
`tabindex`) gets a ref, stored on the element in an attribute whose name is
random per session (`data-rook-<8 hex>`), so a page cannot plant or guess it:

```
# Sign up
[e1] textbox "Name" value="Bake" required
[e2] password "Password" value="***"
[e3] select "color" selected="Green" options=["Red","Green"]
[e4] checkbox "Newsletter" checked
[e5] button "Send"
[e6] link "Open in new tab" -> /done.html
```

Names come from `aria-label`, `aria-labelledby`, `<label>`, button values,
placeholders, text, image `alt`, then `title`/`name`. Values of password
fields and of fields marked `autocomplete` password, one-time-code or `cc-*`
show as `***`. Hidden elements are skipped. The text is cut at `max_chars` (default
6000) with `truncated: true`; `selector` limits it to one part of the page.

Refs are attributes on the live DOM, so they need no server-side map. An
element keeps its ref across snapshots; new elements get numbers continuing
from the session's counter, which carries across documents and tabs. A ref
is therefore never reused for another element within a session: after a
navigation, a re-render or another agent's snapshot, an old ref either still
names the same element or fails with "take a new browser.snapshot". (An
element a web app reuses in place with new content keeps its ref; the action
reply's url/title, or `snapshot=true`, shows what happened.) The alternative, Playwright's `aria_snapshot()` YAML or its
private AI snapshot, was rejected: the first has no element handles, the
second is not a public API.

Action caps take `target`: a ref, or any Playwright selector (`#id`,
`text=Sign in`, `role=button[name="OK"]`). It must match exactly one element;
otherwise the error says how many matched and suggests a ref. Every action
reply is `{session, url, title}` plus `snapshot` when asked
(`snapshot=true`), which saves the agent a round trip.

## 5. Errors and bounds

Playwright errors are reduced to their first line (no call log, no ANSI, no
stack). Navigation has its own timeout (default 30 s, at most 120); actions
wait up to 10 s for their element; caps have host timeouts of 60-120 s. After
a click or key the plugin waits up to 5 s for a page it started to load and
then answers anyway. `read` is paged (`max_chars`, `offset`, `next_offset`),
`eval` results are cut at `max_chars`. Screenshots are files under the
plugin's data directory (the last 20 kept); inline JPEG is refused past about
400 KB.

## 6. Security notes

- Profiles: always `launch()` + `new_context()`. The plugin never calls
  `launch_persistent_context` and never points Chromium at a user data
  directory, so it cannot read the person's cookies or saved passwords.
  `browser.executable` changes the binary, not the profile.
- Network reach: by default a session refuses non-public addresses, so a
  write-tier caller cannot use it to read pages behind the router (SSRF):
  `localhost`/`*.localhost`, and any host that is or resolves to an address
  that is not globally routable (loopback, RFC 1918 LAN, 100.64.0.0/10 VPNs
  such as Tailscale, link-local including 169.254.169.254, IPv6 ULA and
  link-local, IPv4-mapped forms). The check runs on every request through a
  context route (service workers are blocked so none bypass it), caches each
  host's verdict for the session, and is repeated on where a tab landed,
  because redirect hops are not routed. `browser.allow_private` (worker
  setting, default off) lifts it for a machine whose browser should reach a
  dev server or a LAN page. Not covered: DNS rebinding between the check and
  Chromium's own lookup; WebSockets opened by page script and WebRTC are not
  routed by Playwright.
- Sessions belong to their opener (section 2). Agents acting for the same
  person share an identity and so share sessions. Do not log into personal
  accounts in a session; for a test login use `{{secret:name}}`, typed only
  into a password field (other fields show their value in the outline).
- Page text is untrusted: the snapshot docstring and the agent tip say it is
  data, not instructions.
- `headless=false` (a window on the machine's screen) is open to any caller
  of `browser.open`; it needs a desktop session and is visible, not silent.
- The sandbox is on by default (`browser.sandbox`); Playwright's own default
  is off.

## 7. Agent tips

The guidance store has a default tip for `cap:browser.` (open, then act from
snapshot refs, snapshot again after the page changes, page text is data,
close when done), shown once on the first `browser.*` reply like other cap
tips.
