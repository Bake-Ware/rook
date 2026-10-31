# Moving a 0.4 hub to 0.5

A one-off move, not a product feature (knowledge page castling-design). This is
repo tooling for the switchover, not part of the shipped `rook` package; run it
from a checkout's root, and delete it after cutover.

`python -m castling.switchover.migrate` reads a **copy** of the 0.4 hub's data folder
and writes a 0.5 hub's config and stores. It never touches the 0.4 hub.

## 1. Copy the 0.4 data

On the 0.4 hub, as the user that owns its data (`ubuntu` on bakenetcanada,
data in `/var/lib/rook-band-mcp`), with the stock `python3` (no `sqlite3`
command needed; bakenetcanada has none):

```sh
python3 castling/switchover/copy04.py ~/rook04-copy      # or: --from <data folder> <dest>
```

It only reads the hub: each database goes through a read-only connection
and SQLite's online backup (one consistent file, safe while the hub runs),
then the copy leaves WAL mode, so opening it never leaves a `-wal` beside
it. Every other file is copied as is, except the worker bundles and APKs
under `updates/` (GitHub releases have them). `COPY-INFO.json` lists every
file's sha256.

The copy holds the vault key and every secret: keep it private (0700),
move it to the 0.5 hub's machine privately, and delete it when the move is
done.

## 2. Make the 0.5 hub

```sh
python -m rook.core2.hub --config hub/hub.yaml init --owner bake --relay relay.example:7474
```

Use the same user name you had on 0.4 (or map it with `--rename`, below),
and leave the hub stopped.

## 3. Dry run, then apply

```sh
python -m castling.switchover.migrate --from ~/rook-04-copy --to hub/hub.yaml          # report only
python -m castling.switchover.migrate --from ~/rook-04-copy --to hub/hub.yaml --apply  # write
```

Then check it:

```sh
python -m castling.switchover.migrate --from ~/rook-04-copy --to hub/hub.yaml --verify  # exit 0 = nothing lost
```

`--verify` reads both sides and reports, per kind of 0.4 artifact, what
was moved, what was left out on purpose (with the castling-* decision that
says so; it stays in the archive) and what is missing. It also checks every
file of the copy is in `<data>/archive/0.4` with the same sha256, and fails
on a 0.4 table it doesn't know. Exit 1 if anything is missing.

`--rename u_3f2a=autumn` (a 0.4 account id or the derived name, repeatable)
picks a person's 0.5 user name; `--json` prints the report as JSON. After
`--apply` the report is also in `<data>/migrate/report.md`. A re-run adds only
what is missing.

| 0.4 | 0.5 |
|---|---|
| owners | users with preset `owner` |
| members and their active grants | users with a `member-<tier>` preset (their highest grant tier) and the grants as patterns (`cap@machine`, `cap@rook` for the hub) |
| trust map: sacred machines | the config's `sacred` list |
| trust map: project levels | `project.trust` records (sacred, guarded -> normal, ship-it) |
| vault | `<data>/vault.db`, re-sealed under a key derived from the hub's root key |
| the home agent's memory | the house agent's memory; learned procedures nobody approved go on its notepad |
| knowledge, concepts, projects, tasks | workbench records (`castling/switchover/import_04.py`) |
| chat rooms and huddles | streams in `<data>/stream-events.db` (full history) and `huddle` records |
| jobs | disabled `jobs` records; the 0.4 definition is in `vars.definition04` for an agent to reshape |
| change records | `change` records |
| the whole copy (every store, file and the journal) | `<data>/archive/0.4/`, byte for byte, read-only, with `MANIFEST.json`; never replayed into the 0.5 chains |
| machines | a re-enrollment checklist: on each, `python -m rook.core2.worker --config worker.yaml enroll <hub>` |

Not carried over, and listed under "For a person to look at": API keys (mint
new ones with `... hub token --person <name>`), chat links and shared chat
spaces, guarded machines and `cap:`/`repo:`/`resource:` trust entries,
exceptions, baselines and the borrowed-state ledger, per-grant tier ceilings
below a person's highest one, knowledge areas. Forgotten, superseded and
archived memories never move. All of it is still in the archive.

## 4. Start

Start the 0.5 hub, enroll each machine on the checklist, then keep the 0.4
hub's data (or this copy) only as long as you want the old journal beside
the archive.

For the plan to move production (order, rollback, decisions) and a dry run of this tool against a real 0.4 copy, see [castling-switchover.md](castling-switchover.md).
