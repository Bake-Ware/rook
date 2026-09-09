# Dashboard workspace

The dashboard is a single document with hash-routed Workers, Bands, Chat,
Sessions, Install, Account, and Tokens views. `shell.css` owns layout;
`theme.css` adopts BakeDash's olive/amber palette, square panels, DM Sans,
and IBM Plex Mono. The BakeDash project remains independent.

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
  "worker": "sojourn",
  "args": {"description": "Hermes agent host and shared operations workspace."}
}
```

Native Android applications bundle the worker core and need an APK update to
advertise these capabilities. Publishing a Python worker bundle does not update
the embedded Android runtime.
