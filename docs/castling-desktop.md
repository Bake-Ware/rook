# The desktop app on Rook 0.5

Castling wave 6. The Tauri desktop app (`desktop/`) talks to a 0.4 hub as
before, or to a 0.5 hub: Settings > Your hub > **Rook version** (0.4 until
changed). The 0.4 code paths are unchanged; the switch goes when every hub is
on 0.5, and with it `hub.rs`, `worker.rs` and the 0.4 halves of `lib.rs`.
User guide: [desktop-app.md](desktop-app.md#a-rook-05-hub-preview).

## What changed

| | 0.4 | 0.5 |
|---|---|---|
| Sign-in | dashboard cookie (window, account login or dashboard password) | the person's door token; the site's `/login` checks it once and gives the dashboard window its `rook_web` session |
| Hub calls | `/api/band/call`, read-risk caps | HTTP door `POST /call` as the person, each with a why (journaled) |
| Waiting on you | `approvals.list` | `GET /holds` |
| Work, Knowledge, Sessions tabs | `task.read` deck, `knowledge.read` search, `/account/work/sessions` | none: the panel shows holds, the hub's pages and the house agent, and an agent opens a page when one is needed (castling-attention: "No built-in views needed for things like knowledge or tasks"; drift #29) |
| Tray Dashboard menu | `ui/sections.json` | the hub's `page.list`, by section, refreshed every 20 polls |
| Quick ask | private chat room, or `home.ask` over MCP | `agent.chat.send` to `house`, in the person's own session (the dock's) |
| Ask about this page | the @home dock | the site's dock (`#dock-btn`) |
| Notifications | chat rooms + approvals | holds; agents reach the screen through the worker's `notification.post` |
| This computer's worker | the 0.4 band worker (service, task or bundle) | `python -m rook.core2.worker`, run by the app from `<settings>/worker05/` |
| Adding it | the hub's Install page | **Add this computer**: the worker's `enroll` asks for a device code, the app approves it with the person's token (`POST /enroll/approve`), the bundle configures the worker; its voice service fills empty voice settings |

Code: `desktop/src-tauri/src/hub05.rs` (door client, pure parsers),
`worker05.rs` (config, enrollment, child process), the `v05()` branches in
`lib.rs`, the token form and worker Python field in `ui/`.

## Choices

- **Token, not a new sign-in flow.** The hub site signs people in with a
  door token today (TODO(castling:enrollment) there: sign-in without pasting
  one). The app uses the same token, so it needs nothing new on the hub. A
  device-code sign-in that hands the app a person token would widen what a
  phished device code gets (a person's full rights, holds included), so it
  waits for the enrollment lane.
- **No MCP door.** The MCP door is for agents: its tokens are an agent's,
  acting for a person. The app acts as the person, and the HTTP door runs
  everything the panel and quick ask need, so the 0.4 agent-token option is
  hidden on 0.5.
- **No built-in lists beyond holds.** Tasks and knowledge are workbench
  plugins, not core (castling-dashboard), so the app doesn't assume them: the
  hub's pages menu reaches them, and the dashboard dock shows the person's own
  conversation with the house agent.
- **The app approves its own device code.** The person clicked **Add this
  computer** while signed in as themselves, which is the approval spec 12
  asks for; the code never leaves the app.
- **Update check on 0.5** works like on 0.4: the 0.5 hub serves
  `/updates/desktop.json` (no token) from Rook's latest GitHub release, cached
  for six hours (docs/castling-updates.md). The installers still come only
  from Rook's GitHub releases and aren't signed yet.

## Tests

- `cargo test --features wake-word --lib`: parsers for holds, tasks,
  search hits, agents and pages; the enroll line; the worker config; the
  0.4/0.5 credential split.
- `tests/live_hub05.rs` (ignored): against a real 0.5 hub it signs in,
  reads every list, adds a worker by device code, sees it online with
  `notification-desktop`, stops it, signs out. Passed against a throwaway
  0.5 band (relay, `init` hub with the workbench and agents plugins) on
  cachyrig.
- `tests/test_desktop_contract05.py`: the routes, cookie, page ids, caps,
  dock markup and enroll output the app relies on, against the castling code.
- By hand: the app in a headless sway session against that hub, settings in
  0.5 mode, the panel (pages and holds), the dashboard window opening
  signed in.

Windows: the 0.5 worker is untested there (the 0.5 e2e suite is POSIX only);
stopping it is a hard stop there, a clean SIGTERM elsewhere.
