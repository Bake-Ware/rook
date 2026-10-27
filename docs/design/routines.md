# Ready-made jobs and the routines behind them

Status: built 2026-10-27 (roadmap round 2, task
`canned-jobs-security-scan-attention-aggregation-camera-review`). Code:
`rook/hub/plugins/jobs/catalog.py` (templates, `templates` / `install`) and
`rook/hub/plugins/routines/` (the `routine.*` caps).

## 1. What Bake asked for

Rook knowledge `rook-roadmap-bake-2026-10-26`: "Canned jobs: 5-minute
security scan (open sessions, logs); attention aggregation (email, phone
notifications, chats); security camera review (Ring, others)". The jobs
engine already ran scheduled, templated jobs; none shipped ready-made.

## 2. Design calls

- **A template is one job.** Each template is a single `cap` step on worker
  `rook` calling a `routine.*` cap with the template's config as its args.
  The work (reading stores, comparing with the last run, wording the report)
  is deterministic hub code, not an agent prompt: cheap, testable, and no
  model call per run (camera review calls a model only for changed frames).
  The jobs engine adds the schedule, overlap (`skip`), missed runs (`skip`),
  run history, the switch-off rules and the Jobs page.
- **Install is explicit.** Nothing is installed by default.
  `job.write install` (`rook_jobs action=install id=<template> data={config,
  name?, schedule?, enabled?}`) builds the job and saves it through
  `create`, so the caller owns it, it runs as its creator (access v2:
  rechecked each run) and the default guardrails apply unchanged (the
  `routine.*` caps are `write` risk, which the default list allows). No
  guardrail, hold or trust entry is added. `vars.template` records the
  template; `templates` lists which jobs came from which.
- **Owners only.** The routines read the band's security picture, the
  person's phone notifications, chats and cameras. Every `routine.*` cap
  calls `require_hub_admin`, and `install` refuses non-owners up front (a
  member's job would otherwise fail every run).
- **Quiet delivery.** A report is one row in the attention log
  (`attention.log`, outcome `tell`) plus one alert on the phone's `digest`
  channel through `attention.send`: a normal notification, no pop-up, no
  sound, never a card or a question. Urgent (`ask_now` in the log, the
  `urgent` channel) only for something clearly bad: a burst of refused keys,
  or a camera event the model calls urgent when the job sets `may_ring`. The
  log line holds counts and names of things (cameras, kinds of finding), never
  message content; the phone alert holds the detail. With no phone set up
  (`attention.devices` empty) the log line and the job run's output remain.
- **Only on change.** Each routine keeps a small JSON state per `state` key
  (`<hub state>/plugins/routine/state/`). The security scan's first run is a
  silent baseline; the digest's first run looks back 2 hours; a camera's
  first frame is its baseline. `dry_run: true` reports without saving or
  sending.
- **Band calls as the caller.** A routine's worker calls (`attention.feed`,
  `notify.list`, `camera.snap`, `shell.exec` for ffmpeg, an email cap) go
  through the band client as the routine's caller (the job's identity), so
  the authorizer and the person's grants apply. They are journaled without
  their (sensitive) results. The caps themselves are tagged `sensitive`, so
  the hub does not journal their results either.

## 3. Templates

| Template | Schedule | Needs | Cap |
|---|---|---|---|
| `security-scan` | `*/5 * * * *` | nothing | `routine.security_scan` |
| `attention-digest` | `0 8-20/2 * * *` | nothing (email optional) | `routine.attention_digest` |
| `camera-review` | `*/10 * * * *` | `cameras`, `provider` | `routine.camera_review` |

Run history: 3, 14 and 7 days. `install` refuses unknown config keys and
says what a missing one should be.

## 4. The security scan

Sources, each skipped (and listed in `skipped`) when the hub has none:
band workers (a never-seen id is new), live console rooms (`node.consoles`,
the bridge's console store; one opened since the last scan), identities seen
over the MCP (presence; a never-seen one is new), dashboard sign-ins (new
`user_sessions`) and account changes (`account_audit`), the call journal
since the last scan (`decision = deny`, `audit.denied` refused keys,
`audit.unverified`), API keys added or removed (the token store) and
access.db history (roles, grants, revokes, key owners, chat links, bots,
spaces). `urgent_refusals` (10) refused calls since the last scan make it
urgent; 0 never.

Not covered: failed dashboard password sign-ins (the dashboard's throttle
keeps them in memory in another process), terminal sessions that are not
console rooms (listing them means a call to every worker every 5 minutes).

## 5. The attention digest

Phones (`phones`, else `attention.devices`, else every worker with the cap):
the quiet feed (`attention.feed`, summaries only), else `notify.list`. Mail
apps' entries count as email. Chats: rooms with unread messages for
`chat_as` (`user:operator`, the dashboard's chat identity) and activity since
the last digest; Telegram and Discord are bridged into rooms. Email
(optional): `{"imap": {host, user, password: "{{secret:name}}", port?,
folder?}}` (stdlib `imaplib`, `EXAMINE`-style read-only select, headers only,
nothing marked read; the first run records the mailbox position) or
`{"cap": {worker, cap, args?}}` returning `{messages: [{from, subject}]}`.
Rook has no mail connector; credentials are vault placeholders, resolved by
the jobs runtime at the call. Nothing new: nothing is sent.

## 6. Camera review

Cameras: `{name, url, headers?}` (the hub fetches a JPEG snapshot, at most
8 MB), `{name, worker, rtsp}` (`shell.exec` runs ffmpeg on that worker, the
address passed as an argument, never in the shell text) or `{name, worker,
camera}` (`camera.snap`). Each frame is shrunk to 64x36 greyscale and
compared with the last (mean absolute difference); at `threshold` (0.06) or
more, the previous and current frames go to the provider's vision model
(`llm.client(provider, model)`, OpenAI-style `image_url` parts) with a fixed
prompt asking for `{"level": "nothing|notable|urgent", "what"}`. Only
`notable` and `urgent` are reported. The last frame per camera is kept as a
small JPEG beside the state. Ring: no local API; bring a bridge (ring-mqtt
with go2rtc, Scrypted) that exposes RTSP or a snapshot URL. A run where no
camera answered fails, so the job's `max_failures` switches it off.
