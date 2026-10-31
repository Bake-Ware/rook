# Moving a 0.4 hub to 0.5

A one-off move, not a product feature (knowledge page castling-design):
`python -m rook.core2.migrate` reads a **copy** of the 0.4 hub's data folder
and writes a 0.5 hub's config and stores. It never touches the 0.4 hub.

## 1. Copy the 0.4 data

On the 0.4 hub (data in `/var/lib/rook-band-mcp`, or `$ROOK_DATA_DIR`), copy
each database with SQLite's online backup (safe while the hub runs; a plain
`cp` of a live database can miss recent writes and is refused), plus the
trust map and the vault key:

```sh
src=/var/lib/rook-band-mcp; dst=~/rook-04-copy
cd "$src" && find . -name '*.db' | while read -r f; do
  mkdir -p "$dst/$(dirname "$f")" && sqlite3 "$f" ".backup '$dst/$f'"
done
cp -p trust.json vault.key "$dst/"
```

The copy holds the vault key and every secret: keep it private and delete it
when the move is done.

## 2. Make the 0.5 hub

```sh
python -m rook.core2.hub --config hub/hub.yaml init --owner bake --relay relay.example:7474
```

Use the same user name you had on 0.4 (or map it with `--rename`, below),
and leave the hub stopped.

## 3. Dry run, then apply

```sh
python -m rook.core2.migrate --from ~/rook-04-copy --to hub/hub.yaml          # report only
python -m rook.core2.migrate --from ~/rook-04-copy --to hub/hub.yaml --apply  # write
```

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
| knowledge, concepts, projects, tasks | workbench records (the workbench's `import_04.py`) |
| journal | `<data>/archive/0.4/journal.db`, read-only, not part of the 0.5 chains |
| machines | a re-enrollment checklist: on each, `python -m rook.core2.worker --config worker.yaml enroll <hub>` |

Not carried over, and listed under "For a person to look at": API keys (mint
new ones with `... hub token --person <name>`), chat links and shared chat
spaces, guarded machines and `cap:`/`repo:`/`resource:` trust entries,
exceptions, baselines and the borrowed-state ledger, per-grant tier ceilings
below a person's highest one, knowledge areas. Forgotten, superseded and
archived memories never move.

## 4. Start

Start the 0.5 hub, enroll each machine on the checklist, then keep the 0.4
hub's data (or this copy) only as long as you want the old journal beside
the archive.

For the plan to move production (order, rollback, decisions) and a dry run of this tool against a real 0.4 copy, see [castling-switchover.md](castling-switchover.md).
