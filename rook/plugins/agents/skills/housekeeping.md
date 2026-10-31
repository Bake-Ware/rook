---
description: Raise the owner's loose ends about their hub (backups, the recovery key) at a natural moment, once, and respect a no.
when: on a quiet moment with the owner (start of a conversation, after a task wraps up), at most once per loose end until something changes.
---
# Housekeeping

The hub holds the owner's journal, memory, vault and knowledge. Two loose
ends matter if it dies. Notice them and offer to help; never force,
never nag.

**Backups.** Call `backup.list`. It fails when there's nowhere to put them:
by default backups go to another of the owner's machines running the
`backup-store` plugin, and none is online. Or the list is empty, or the
newest backup is days old, or the nightly job isn't installed (`jobs.list`).
Say it plainly, e.g. "Your hub isn't backed up anywhere yet. Want me to set
up nightly backups to your NAS?", and offer what fits: install
`backup-store` on a machine that's always on, install the backup plugin's
nightly job, or set `target` to a disk or a cloud remote (rclone). Then run
`backup.run` once and check the result with `backup.verify`.

**The recovery key.** Only the recovery key opens a backup. The hub shows it
to owners in person (the hub site, or `recovery-key` on the hub's machine).
Ask once: "Did you save a copy of your Rook recovery key somewhere off this
machine?" Never read it out or write it into chat, notes or memory.

**Their answer wins.** "Not now" means not today. "I'm just trying it out,
not bothering with backups" means drop it until the setup changes, for
example a new hub or real data moving in. Remember the answer in your
memory so you don't ask twice.
