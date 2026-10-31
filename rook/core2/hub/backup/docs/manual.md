# Backup

Sealed backups of the hub. A backup is one file holding the hub's config
(`hub.yaml`, with the band key), its whole data folder (keys, journal,
records, tokens, the vault file, plugin data) and its plugin library. It is
sealed under a key made from the owner's **recovery key**: only that key
opens it, so a backup can sit anywhere (a USB disk, another machine, a cloud
bucket) and nobody who finds it can read it.

The hub keeps the recovery key itself, so it backs up on its own (a nightly
job) without asking anyone. You need the key only to restore, when the hub
is gone: save it when `init` shows it (`recovery-key` shows it again).

| Cap | What it does |
|---|---|
| `backup.run` | Back up now: seal, push to the target, fetch the copy back and restore it into a scratch folder to check it, then delete this hub's backups past `keep`. Returns the file's name, size and what the check found. |
| `backup.list` | This hub's backups on the target, newest first. |
| `backup.verify` | Fetch a backup (the newest, unless `name`) and restore it into scratch with this hub's recovery key: every file against its SHA-256, the root key is this hub's, the config loads, each database passes SQLite's check. |

Owners only (in person, through their agents, or a job they made).

## Settings

In `hub.yaml` under `settings: {backup: {...}}`:

| Setting | What |
|---|---|
| `target` | Where backups go (below). Nothing is backed up until it is set. |
| `keep` | How many of this hub's newest backups stay on the target (default 7). The newest is never deleted. |

Targets:

- `dir:/mnt/usb/rook`: a folder (a disk, a network mount). Files are 0600 in a 0700 folder.
- `worker:janitor`: another worker on the band, through its
  [backup-store](../../../../plugins/backup-store/docs/manual.md) plugin
  (install it there first). The default suggestion: your data stays on
  your own machines.
- `rclone:gdrive:rook-backups`: anything [rclone](https://rclone.org) reaches
  (S3, Google Drive, B2, SFTP, ...). Set the remote up with `rclone config`
  as the hub's account; rclone keeps its own credentials.

## Every night

The **nightly** job (Manage > Jobs, from the backup plugin) runs `backup.run`
at 03:40 in the jobs timezone, as the person who installs it, and catches up
after a night the hub was down. A failed run notifies you.

## What is in a backup

`hub.yaml`, `data/` and `library/`. Each database is copied with SQLite's
online backup, so it is whole while the hub runs. Left out: the asset
download cache (`data/assets/`), `-wal`/`-shm` files (folded into the
copies), half-written `.tmp` files and the scratch folder `data/.backup/`.
Library plugins linked from a checkout are copied as files.

Nothing unsealed leaves the data folder: copies and checks are made in
`data/.backup/` (0700) and removed straight away. Names on the target show
only the hub id and the time: `rook-hub-<hub>-<UTC time>.rookbak`.

## Restoring on a new machine

With the backup file (copied off the worker or the disk) and the recovery
key:

    python -m rook.core2.hub --config /srv/rook/hub.yaml restore rook-hub-....rookbak

or fetch the newest straight from a target:

    python -m rook.core2.hub --config /srv/rook/hub.yaml restore --target rclone:gdrive:rook-backups

It asks for the recovery key (or reads it from stdin), checks the backup,
and writes `/srv/rook/hub.yaml` with `data/` and `library/` beside it. It
never overwrites: the folder must not hold a hub already. The restored hub
has the same id, keys, people and workers; start it and the workers find it
on the band again. A wrong key or a damaged file is refused before anything
is written.

The same without the hub running: `python -m rook.core2.hub --config
hub.yaml backup [run | list | verify [NAME]] [--target dir:/path]` (dir and
rclone targets; a worker target needs the running hub, so use the cap).

## How it is sealed

XChaCha20-Poly1305 (libsodium's secretstream) in 1 MiB chunks, under
BLAKE2b of the recovery key's 160 bits (`person: rook-backup-1`). A file cut
short, changed or extended fails to open. The format is in
`rook/core2/hub/backup/__init__.py`.
