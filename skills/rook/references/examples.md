# Worked examples

Generic names throughout (`worker-a`, `build-box`, `my-repo`). Replace `request_id` values with fresh unique strings (a uuid is fine); reuse one only to retry the same write.

## 1. Start of session

```
rook_whoami()
# → {"actor": "mytoken.claudecode.laptop@.home.me.repo", "kind": "agent", …}

rook_task(action="deck", data={"states": ["in_progress", "blocked"], "done_days": 1})
# Read: rows claimed by you, needs_hygiene, hygiene findings, blocked reasons.

rook_handoff_list(limit=5)                       # resuming? find the thread
rook_handoff_get(thread_id="…")                  # freshness: current / STALE / SUPERSEDED

rook_knowledge(query="nightly backup worker-a", data={"limit": 3})
rook_knowledge(query="restic snapshot schedule", data={"limit": 3})   # second phrasing

rook_chat_rooms()                                # read only rooms with unread > 0
rook_chat_read(room="…", since_seq=<last seq you saw>)
```

## 2. A task from claim to done

The user asks: "make the nightly backup on worker-a also prune old snapshots".

```
rook_task(action="search", query="backup prune worker-a", data={"limit": 3})
# nothing open → create it under the right project
rook_task(action="create", request_id="r-1", data={
  "title": "Nightly backup on worker-a prunes old snapshots",
  "body": "Asked by the user. Keep 7 daily, 4 weekly.",
  "parent": "homelab-backups",
  "attrs": {"criteria": ["prune runs after backup", "a dry run shows the expected keep set"], "workers": ["worker-a"]}})

rook_task(action="claim", id="nightly-backup-on-worker-a-prunes-old-snapshots", request_id="r-2")
# also_claimed_by is empty → it's yours

rook_knowledge(query="worker-a backup", data={"limit": 3})        # how is it set up today?
rook_call(cap="shell.exec", worker="worker-a", text=true, args={"cmd":
  "echo '== unit'; systemctl cat backup.service | head -20; echo '== timer'; systemctl list-timers backup.timer --no-pager | head -3"})

# … make the change with file.write, then prove it
rook_call(cap="shell.exec", worker="worker-a", args={"cmd": "restic forget --dry-run --keep-daily 7 --keep-weekly 4 2>&1 | tail -n 15", "timeout": 120})
# → reply id "4f9c2e…" is the evidence

rook_task(action="link", id="…", request_id="r-3",
          data={"kind": "journal", "ref": "4f9c2e…", "relation": "evidence", "note": "dry run shows keep set"})
rook_task(action="get", id="…")                                     # read revision
rook_task(action="update", id="…", request_id="r-4", data={"revision": 3, "patch": {
  "state": "done",
  "attrs": {"outcome": "backup.service now runs restic forget --keep-daily 7 --keep-weekly 4 after each backup; dry run (journal 4f9c2e…) shows the expected keep set. First real prune runs tonight."}}})

# The lasting lesson goes on the subject page, not a new dated page
rook_knowledge(action="get", id="worker-a-backups")
rook_knowledge(action="update", id="worker-a-backups", request_id="r-5", data={"revision": 7, "patch": {
  "body": "<existing body, with a Retention section added>"}})
rook_knowledge(action="link", id="worker-a-backups", request_id="r-6",
               data={"kind": "record", "ref": "nightly-backup-on-worker-a-prunes-old-snapshots", "relation": "source"})
```

## 3. Stopping before it's finished

```
rook_task(action="release", id="…", request_id="r-7", data={"handoff": {
  "goal": "Nightly backup on worker-a prunes old snapshots",
  "state": "Prune flags chosen and dry-run verified (journal 4f9c2e…). The unit file is NOT changed yet.",
  "next_steps": [
    "1. On worker-a: add ExecStartPost=/usr/local/bin/backup-prune to backup.service (draft at worker-a:/tmp/backup-prune)",
    "2. systemctl daemon-reload; run the service once; check journalctl -u backup -n 30",
    "3. Link the run as evidence and set the task done"],
  "artifacts": ["worker-a:/tmp/backup-prune", "journal 4f9c2e…", "knowledge worker-a-backups"]}})
# → task paused, handoff thread linked; the next agent finds it on the deck
```

## 4. A long build in a console

```
rook_console_search("build android apk")          # done before? read its summary first
rook_console_open(worker="build-box", task="Build release APK for the voice fix",
                  cwd="/srv/my-repo", cmd="./gradlew assembleRelease",
                  task_id="voice-fix-apk")         # claims and links the task
# → {"room": "e39f…", …}

rook_console_read(room="e39f…", tail=true, limit=40)    # later: last 40 lines
rook_console_read(room="e39f…", since_seq=<last_seq>, limit=60)
rook_console_close(room="e39f…", summary="assembleRelease OK in 6m; APK at build-box:/srv/my-repo/app/build/outputs/apk/release/. Needed JAVA_HOME=17; the 21 JDK fails kapt.")
```

## 5. A side-effecting call timed out

```
rook_call(cap="shell.exec", worker="worker-a", args={"argv": ["systemctl", "restart", "app"], "timeout": 30})
# → {"ok": false, "error": "no reply from worker 'worker-a' within 35s (the call's own 30s timeout + 5s;
#    journal id 8b1d07…). The worker may be offline, or the call may still be running …"}
# The journal id is in the error text; a timed-out reply has no "id" field.

rook_journal(call_id="8b1d07…")                    # did a reply land later?
rook_call(cap="shell.exec", worker="worker-a", text=true,
          args={"cmd": "systemctl is-active app; systemctl show app -p ActiveEnterTimestamp"})
# active, started a minute ago → it worked; do NOT restart again
# no reply at all → rook_call("info.ping", worker="worker-a"); the worker may be off the band
```

The same for knowledge and task writes: retry with the same `request_id`, which returns the original result instead of writing twice.

## 6. Asking another agent

```
rook_presence()                                   # is agent:reviewer online?
rook_chat_start(title="Schema change in my-repo: need a review", invite=["agent:reviewer"])
rook_chat_send(room="…", mention=["agent:reviewer"], expects_reply=true, text=
  "Relaying for the user. Please review branch db-schema (abc123) in my-repo: adds a nullable column and a backfill job. "
  "I need: go/no-go on running the backfill on the live database, and anything that would lock the table. "
  "Reply here; claim task db-schema-review if you start.")
# Offline and it can't wait? Check the worker can be woken, then wake once:
rook_caps(prefix="hermes.chat")
rook_chat_wake(room="…", worker="reviewer-box", note="Review request above; go/no-go needed today.")
```

## 7. Using a credential

```
rook_secret(action="list")                         # names and descriptions only
rook_call(cap="shell.exec", worker="worker-a", args={"argv":
  ["curl", "-fsS", "-H", "Authorization: Bearer {{secret:service-api-token}}", "https://service.example/api/health"]})
# The hub fills it in and masks it in the reply and the journal. Never echo it, never write it to a page.
```

## 8. Reporting a milestone to the user

One message, then stop. Use the channel your band's instructions name, for example:

```
rook_call(worker="rook", cap="notify.send", args={"text": "Backup prune is live on worker-a; first real prune tonight. Task done with evidence."})
```
