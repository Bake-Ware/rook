# Moving from 0.4

0.5 is a clean cut: a 0.4 machine can't talk to a 0.5 one. Moving is a
one-off: copy the 0.4 hub's data, make a new 0.5 hub, convert the copy
into it, and enroll every machine again. The 0.4 hub is never touched, so
you can keep it running until the 0.5 band works.

## The steps

1. **Copy the 0.4 data** with SQLite's online backup (safe while the hub
   runs), plus `trust.json` and `vault.key`. The copy holds every secret:
   keep it private and delete it when you are done.
2. **Make the 0.5 hub** with `init` ([Install](install.md#by-hand)), using
   the same user name you had on 0.4 (or map it with `--rename`), and leave
   it stopped.
3. **Dry run, then apply:**

   ```sh
   python -m rook.core2.migrate --from ~/rook-04-copy --to hub/hub.yaml          # report only
   python -m rook.core2.migrate --from ~/rook-04-copy --to hub/hub.yaml --apply  # write
   ```

   `--rename OLD=NEW` picks a person's 0.5 name; `--json` prints the report
   as JSON. After `--apply` the report is also in `<data>/migrate/report.md`.
   Running it again adds only what is missing.
4. **Start the hub** and enroll each machine on the report's checklist
   ([Adding machines](enrollment.md)), then mint new tokens for people and
   agents ([People](people.md#adding-a-person)).

## What moves

- Owners become `owner` users; members become users with a preset for
  their 0.4 tier and their grants as patterns.
- The trust map's sacred machines become `sacred:`; project levels become
  project trust records.
- The vault is re-sealed into `<data>/vault.db` for when 0.5's vault lands.
- The home agent's memory becomes the house agent's.
- Knowledge, concepts, projects and tasks become workbench records.
- The 0.4 journal is kept read-only in `<data>/archive/0.4/journal.db`,
  outside the 0.5 chains.

Not carried over, and listed in the report under "For a person to look
at": API keys, chat links, guarded machines and finer trust entries,
exceptions and baselines, and knowledge areas.

The full guide, with the copy commands: `docs/castling-migrate.md`.
