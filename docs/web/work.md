# Work sessions

Work replaces the old Sessions view; saved `#sessions` links redirect to `#work`.
The server discovers Claude and Codex histories on connected workers every 15
seconds after each scan. Discovery requests only paginated metadata, including
source identity, title, directory, modification time, message count, and activity.
The worker's native session files remain the source of truth. Codex archives are
included alongside regular rollout files.

Imported entries persist per operator in `work.sqlite3`, but their transcripts
are not copied into the database. Opening an entry automatically reads the whole conversation from its worker.
The browser fetches bounded pages sequentially; switching entries cancels the
previous read. Refresh from host takes a new snapshot. Pages contain at most 6,000 content characters, including
partial large messages. The worker freezes a short-lived conversation snapshot so active appends cannot
shift page boundaries, and the browser holds the selected conversation. Reads require the owning administrator's login and use
`Cache-Control: no-store`. Updated workers are required for snapshot paging and live-process metadata. Older workers can still contribute catalog entries.
An offline host leaves metadata and review controls available, but its transcript
cannot be fetched until it reconnects. While selected and visible, the browser
checks the source every two seconds and reads only its changed tail in bounded
snapshot pages. Unchanged checks return a version without conversation content.
The last message is replaced to include extensions; truncated or replaced files
reset the view. Navigation cancels polling and hidden pages pause it. Failed reads
retry after five seconds from a fresh tail snapshot. Unselected entries do not
download transcripts. Existing imported transcript copies are removed from
session records when this version opens the database; native Work sessions are
migrated with acknowledgment before their web copies are removed. Resumed imported terminal output uses a bounded in-memory buffer,
not durable transcript storage.

A separate SQLite index keeps roster polling lightweight. For deployment canaries,
`ROOK_WORK_IMPORT_WORKERS` optionally limits discovery to comma-separated worker
names; unset it for fleet-wide discovery.

Session titles skip injected setup blocks such as `<environment_context>` and
use the first actual user request. Enter sends a message; Shift+Enter inserts a
newline. Refresh sits at the top right; only loading or error text appears below
the conversation.

Search the sidebar by title, host, agent, directory, or status. Each entry offers
Pending, Blocked, Closed, and Auto. Manual choices survive new activity. Auto
shows Working during activity and Ready when input is requested or a turn ends.
Live Linux sessions are identified by exact agent-owned open log files, resume
arguments, and Claude session/PID markers (including process-start validation).
Active sessions show Active on host and do not offer Resume on host. The worker
also rechecks activity before resuming, including sessions started outside Rook.
This is separate from Working/Ready, which describe turn activity. Other platforms
currently have no independent-process detection.

Imported activity is inferred from log markers; incomplete working logs older
than two minutes fall back to Pending.

Resume on host retains the former Sessions controls: Claude Remote Control or
a Codex PTY, terminal output/input, interruption, and closing the resumed process.
Closing an imported entry that has no web-managed process only closes its review
entry; it does not terminate an independently started terminal.

Active imported sessions expose a message composer when the worker can reach
their existing input channel. Codex uses its installed `codex queue --thread`
command; messages wait for the current turn to finish. Claude uses its local
authenticated peer inbox, with exact session, process-start, socket-owner, and
peer-key checks. Claude's peer protocol has no in-band acceptance acknowledgment:
Work reports **sent to inbox**, not agent acceptance. Subsequent messages appear
automatically while the session is open. **Refresh from host** rereads the whole
conversation. Older CLIs without a usable channel
explain why messaging is unavailable. Sending never resumes a second process.

The worker records imported-message command receipts in
`session_message_receipts` in its Work database before dispatch. Neither those
receipts nor the web database contain prompt bodies. Retries with the same ID
do not resend, including after an uncertain outcome or restart. Failed sends
retain the browser draft. Claude inbox behavior is version-dependent; the adapter
currently recognizes peer protocol 1 and respects the session's peer-message policy.

To create a new session, open **Work**, choose a connected Linux host with Codex installed
and authenticated, and enter an existing absolute working directory. New sessions use Codex. Leave Model blank to use the host's configured model.

Sessions belong to the signed-in operator account. Send a task, follow tool output,
review the current turn's diff under Changes, and answer approval requests inline.
Messages sent during an active turn steer that turn. Stop turn interrupts work;
Close session stops its web-managed host process. Reopen resumes the saved Codex thread.
Closing the page or navigating away does not stop work.

## Ownership and transport

All transcripts belong to their worker, including sessions started with **+ New**.
The web database (`work.sqlite3` beside enrollment.db) retains only identities,
source pointers, titles/directories, activity, review status, revisions, and
command receipts. It never persists new prompts, conversation items, diffs,
terminal output, pending question bodies, or provider protocol events.

The worker's `work.*` plugin launches and controls `codex app-server` locally
through `proc.*`. Its controller and collector run without the web service or a
browser. Worker state lives in `~/.rook-band-worker/work.sqlite3` (override with
`ROOK_WORK_DB`): projected conversation, pending approvals, process cursor, raw
protocol events (`work_events`), and command IDs/results (`work_commands`). Codex
also retains its own native rollout files. Back up worker SQLite databases using
the SQLite backup API, not by copying a live database without its WAL.

The web service polls compact `work.status` batches every two seconds. Only an
open session requests `work.view_page`: stable worker-local view snapshots are
paged in chunks of at most 6,000 characters, then reconstructed in the browser.
Subsequent reads request fields and items changed since the browser's last
revision. Pending approvals and diffs use this same on-demand path. No new
public worker listener or provider credential transfer is required.

Command IDs are deduplicated on both ends. The worker records intent before
sending to Codex; this prevents duplicate dispatch, not uncertain-delivery
failures. Accepted commands interrupted by a crash are not blindly retried.
The web keeps only submission receipts, while raw events stay on the worker.
`work_events` remains on older web databases solely to migrate their old records;
new web actions and transcript reads do not append content there.

On upgrade, older web-owned native sessions are marked for migration. The web
uploads a bounded, checksum-verified archive containing their state, events, and
command receipts to the original worker. The worker adopts the runtime and
keeps the archive in `~/.rook-band-worker/work-migrations/`. Only after its
acknowledgment does the web remove its old state bodies and event rows. Offline
or outdated workers leave migration pending and retain the existing copy; their
old session controls are unavailable until adoption completes. Existing imported
conversation copies can be discarded because their source files remain on the
worker. Removal is logical SQLite record cleanup, not secure erasure of backups
or old WAL pages.

A web-service restart leaves the worker controller running. A worker restart
ends its managed process; reopen resumes its saved Codex thread. Offline workers
leave only metadata available on the web; previously viewed browser content may
remain until navigation, but cannot be refreshed. Resumed external Claude/Codex
terminal output is read only while selected and is not persisted by the web.

This is a Rook-native implementation of the T3 work pattern, not an embedded T3
server. T3's Codex adapter/session runtime at commit
`cfeaca41ae27bdf2c203158d378c87c7308fea2a` was reviewed as an architectural
reference. Its assumption that provider execution and orchestration share a
host is deliberately split at Rook's process transport boundary. No T3 source
is vendored.

## Current scope

- Operator accounts; private sessions per account.
- Codex conversation, steering, tool activity, file diffs, one-time command/file
  approvals, and structured user questions.
- Existing directories, with optional model selection. Worktree creation,
  repository browsing, commits/PR actions, and creating new Claude sessions are future work.
- Unknown provider request types remain visible and can be canceled by stopping
  the turn; they are never implicitly approved.

## Verification

`python -m pytest -q tests/test_work_sessions.py tests/test_band_management.py tests/test_codex_history.py tests/test_claude_resume.py tests/test_session_messages.py`
covers durable projection/cursors, browser disconnect, web-service replacement, worker-local recovery,
pending approvals, duplicate commands/answers, authentication, Origin/CSRF, and
owner isolation, paginated import, stable identities, manual statuses, and resumed
terminal controls, bounded incremental views, and acknowledged legacy migration.

`python tests/browser_work_runtime.py` verifies web-initiated sessions, paged and
incremental output, approvals, close/reopen, and metadata-only web storage with
a fake worker.

`python tests/browser_work_history.py` uses a fake worker with Playwright Chromium
to verify the Sessions redirect, imported transcripts, sidebar controls, reload
persistence, and mobile layout without invoking an agent.

The optional `python tests/browser_work.py --live` requires Playwright Chromium
and a logged-in local Codex. It consumes one small real turn, edits a disposable
repository, disconnects the browser while work runs, verifies restored history
and diffs, checks desktop/mobile layouts, and reopens the native thread.
