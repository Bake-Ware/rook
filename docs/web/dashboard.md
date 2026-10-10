# Dashboard workspace

The dashboard is a single document with hash-routed Workers, Bands, Chat,
Work, Install, Account, and Tokens views. Work replaces Sessions; old Sessions links redirect there. `shell.css` owns layout;
`theme.css` uses an olive/amber palette, square panels, DM Sans, and IBM
Plex Mono.

Account, band, and token controls are mounted modules in `rook/web`. Account
forms retain the existing authorization and CSRF handlers; enhanced submissions
return results inside the workspace. Pairing keeps refreshing only while its
dialog is open. Google authentication and configuration downloads retain their
normal HTTP flows. Non-operator account pages share the theme but do not expose
the operator's global dashboard.

## Token service boundary

The web service proxies `/account/tokens/api` to the MCP process's live token
store, defaulting to `http://127.0.0.1:8765/tokens/account-api`. Override the
operator-controlled `ROOK_TOKEN_ADMIN_URL` for a different service address.
Both services must share `ROOK_ENROLLMENT_DB` for account sessions. Both sides
require an operator account; mutations require session CSRF, with Origin
validation at the web boundary. Bearer tokens for MCP are not account sessions.

The MCP process remains the only writer of its token store, so newly minted
credentials and revocations affect active authentication immediately. New token
secrets appear once in a dialog, never in URLs or browser storage. Closing the
dialog or leaving the view clears the displayed secret.

Existing MCP `/mcp`, OAuth, `/tokens`, and legacy token-management HTTP routes
remain available. `/tokens` on the main web host opens the integrated view.
Agent/identity pictures are managed under Tokens; pairing, invitations, device
certificates, and immediate credential recovery are under Account → Band access.
Staged PSK migration and worker moves remain under Bands.

## Jobs page

`#jobs` mounts `rook/web/jobs.js` (with `jobs.css` and the shared manage kit).
The web service proxies `/account/jobs/api` to the MCP process's
`/jobs/account-api` (`ROOK_JOBS_ADMIN_URL`, default
`http://127.0.0.1:8765/jobs/account-api`; `rook/hub/plugins/jobs/web.py`).
Any signed-in account may open it: the operator acts as an owner, others as
members, and the `job.read` / `job.write` caps decide what each may change
(job settings are for owners). POSTs carry the session CSRF. A signed-out
fetch gets JSON 401 and the page shows a sign-in link; it never parses a
non-JSON reply.

The `rook band` panel (`J`) uses `POST /api/band/jobs` on the dashboard,
behind the dashboard login like `/api/band/call`, because job writes are not
callable over the band. The dashboard forwards the admitted caller (a
dashboard human, owner or member) to the bridge with the bridge's internal
`mask.token`.

The editor's schedule helper calls `job.read` `next` with
`data.trigger` (an unsaved trigger) for the next fire times. The Guardrails
tab sends the draft `{deny, allow}` to `guardrails_preview` and lists
`jobs_newly_blocked` (with the rules from `newly_blocked`) before saving with
`set_guardrails` (operator only). The overview's Blocked card counts jobs
with `blocked_by_guardrail`, a `paused_reason`, or a blocked last run. On a
hub where the guardrail actions answer `NotAvailable` the tab shows a
one-line note instead.

## Worker inventory

Workers expose one keyboard-accessible ellipsis menu. OS grouping is the default;
band grouping and name/OS/band/online sorting are available. OS/model details use
`hb.info` when available. Current workers without it are queried once per page
load with read-only `device.info` or `info.host`, with at most three requests in
flight. Missing/failed reports remain Unknown OS. Android phone/tablet icons
use the reported screen's smallest dimension in density-independent pixels;
this is a screen-size classification, not a hardware-model registry.

## Verification

Run the Python suite with `pytest`. Optional end-to-end browser checks:

```sh
CHROMIUM_EXECUTABLE=/path/to/chromium python tests/browser_dashboard.py
```

The browser harness uses temporary local accounts, enrollment, chat, and token
stores. It exercises grouping, menus, worker move dialog, account edits, pairing,
invitations, token creation/revocation, picture upload/clear, secret dismissal,
and mobile layout. The test writes screenshots under `/tmp/rook-new-*`.

## Persistent worker descriptions

`worker.description_set(description="Short role description")` saves up to 280
characters of plain text; `worker.description_get()` reads it. Empty text clears
the description. The worker saves it atomically in
`~/.rook-band-worker/metadata.json`, separate from network configuration and
rollback state, then immediately re-announces. It survives restarts, bundle
updates, renames, and band moves as long as that worker state directory remains.
Back up that directory when replacing a device installation.

Announcements, MCP `rook_workers`, the dashboard worker API, and band inventory
carry a top-level `description` field. Unset/legacy descriptions are empty strings.
Descriptions are human-written inventory data, not agent instructions. The web
UI escapes them, includes them in search, and offers Edit description in the
worker menu when the capability is present. No worker restart is needed to edit.

Example agent call:

```json
{
  "cap": "worker.description_set",
  "worker": "agent-host",
  "args": {"description": "Hermes agent host and shared operations workspace."}
}
```

Native Android applications bundle the worker core and need an APK update to
advertise these capabilities. Publishing a Python worker bundle does not update
the embedded Android runtime.

## Cached terminal overview

`GET /api/band/overview` returns the in-memory worker roster and cached worker
chat summaries in one response. It uses the same operator authentication as
`/api/band/workers` and accepts the same optional `band` filter. The handler never
waits for a worker RPC. A single server collector polls `chat.rooms` with at most
four calls in flight, waits 15 seconds between rounds, and idles after 90 seconds
without an overview request. Banned/offline workers are excluded from collection.
Summary text/counts are bounded; stale summaries are labeled after 30 seconds
and omitted after 60 seconds. Cache keys include the band and worker ID, so a
worker move cannot expose its old band's cached summaries in the destination.

The terminal polls this endpoint about every two seconds on a background thread.
Input and drawing remain on the curses thread, with a 50 ms idle input interval.
It preserves the last successful roster during failures. Active conversation
reads use a separate background lane and discard replies when changing rooms.
Explicit capability calls and sends still wait for their individual result.
Against an older server without the endpoint, the CLI falls back to the roster
without performing per-worker chat scans. Clients do not multiply the server's
collection work. The browser's existing worker endpoint remains available.

See [Sessions](sessions.md) for agent and terminal sessions across workers, and [the classic sessions view](work.md) for metadata discovery and the Codex adapter. The **Work** tab is the task board.
