# Work sessions

Open **Work** in the dashboard, choose a connected Linux host with Codex installed
and authenticated, and enter an existing absolute working directory. The first
review build supports Codex. Leave Model blank to use the host's configured model.

Sessions belong to the signed-in operator account. Send a task, follow tool output,
review the current turn's diff under Changes, and answer approval requests inline.
Messages sent during an active turn steer that turn. Stop turn interrupts work;
Close agent stops its host process. Reopen resumes the saved Codex thread.
Closing the page or navigating away does not stop work.

## Ownership and transport

The web service owns the durable session, projected conversation, raw event history,
pending approvals, command IDs, and worker output cursor. State is in
`work.sqlite3` beside the enrollment database. Back it up using SQLite's backup
API; do not copy a live database without its WAL.

Codex runs on the selected host as a separate `codex app-server` stdio process.
The web service uses the existing Rook `proc.start/read/write/signal`
capabilities to carry its structured protocol. The browser only connects to the
web service, and the server collector continues without browser subscribers.
No new public host listener, worker upgrade, or provider credential transfer is
required. A web-service restart reattaches to the saved worker process handle.

The host still owns the repository and Codex's native execution context. A
worker restart ends its managed processes; reopening uses the saved native thread
when that same worker is available. A host outage leaves the server history intact.
The existing process transport retains up to 4 MiB of output; an outage longer
than that buffer permits is reported as an error, not silently treated as a
complete transcript. Input delivery with a lost acknowledgment is not retried
blindly. Check the conversation before resubmitting an uncertain input.

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
  repository browsing, commits/PR actions, and other agent providers are future work.
- Unknown provider request types remain visible and can be canceled by stopping
  the turn; they are never implicitly approved.

## Verification

`python -m pytest -q tests/test_work_sessions.py tests/test_band_management.py`
covers durable projection/cursors, browser disconnect, collector replacement,
pending approvals, duplicate commands/answers, authentication, Origin/CSRF, and
owner isolation.

The optional `python tests/browser_work.py --live` requires Playwright Chromium
and a logged-in local Codex. It consumes one small real turn, edits a disposable
repository, disconnects the browser while work runs, verifies restored history
and diffs, checks desktop/mobile layouts, and reopens the native thread.
