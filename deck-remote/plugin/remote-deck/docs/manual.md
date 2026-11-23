# Steam Deck remote

The Deck as a Rook remote: the app (`deck-remote/` in this repository) shows
the picked device profile, sends each key's action, and has the Assistant,
Games and Settings tabs; this plugin, on the Deck's worker, is its link to
the band. Install with `deck-remote/install.sh` on the Deck (it prints the
line to add to the worker's `plugins:` and the command for the Steam
shortcut).

Profiles are the firmware remote's (the same JSON for `remote.ir
profile_save`): keys are `{rook: true}` (the press is the hub's: its jobs
filter `remote.button` on profile and button), `{cap, worker, args}`,
`{lan: true | key}` to the linked TV (`roku@address`, `webos@address`), or
an IR code, kept but not sent (no IR LED on a Deck).
