# Backup store

Keeps the hub's backups on this worker. Install it on the machine that
should hold them, then set the hub's backup target to `worker:<this
worker>` ([Backup](../../../core2/hub/backup/docs/manual.md)).

The files arrive already sealed under the owner's recovery key: this worker
never has a key and can't read them.

| Cap | What it does |
|---|---|
| `backup-store.put` | Part of a backup at `offset` (base64, up to 4 MiB). The last part carries the whole file's `sha256`: it is checked, then the file is kept. A finished backup is never overwritten. |
| `backup-store.list` | The backups here and their sizes. |
| `backup-store.get` | Part of a backup (base64) from `offset`; `eof` on the last. |
| `backup-store.delete` | Delete an old backup (the hub's `keep`). A hub's newest backup is refused, so the last copy always stays. |

Only `rook-hub-<hub>-<time>.rookbak` names in the folder are reachable.

| Setting | Where | What |
|---|---|---|
| `folder` | worker | where the backups live (default `~/.rook-backups`, made 0700) |

To restore on a new machine, copy the newest file from this folder there
and run the hub's `restore` command.
