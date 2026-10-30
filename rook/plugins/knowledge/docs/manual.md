# Knowledge

The shared wiki: what the band knows, decided, does and can do. Search
before writing (`knowledge.search query=...`), so the same page isn't
written twice.

- `knowledge.get id=<id or slug>`: the page, the pages it links
  (`links_out`, and `missing` for links to pages that don't exist yet), the
  pages that link it (`backlinks`) and its child pages.
- `knowledge.create`, `knowledge.update` (needs the current `revision`),
  `knowledge.archive`, `knowledge.history`, `knowledge.list`.
- Link pages with `[[slug]]`. A folder page is a `parent`.
- `kind`: fact, decision, procedure, observation, question, summary, or
  skill (a playbook agents follow; personas and concepts name skills by slug).

**Provenance, not verification.** Every statement says where it came from
and when. Cite it as a footnote `[^key]` and give a `sources` entry with
that key: `{key, kind, ref, quote, note}`. Kinds: `user` (the person said
so; quote their words: the strongest source), `agent` (your own
conclusion), `journal`, `commit`, `pr`, `file`, `url`, `page`, `task`,
`chat`, `session`. The hub stamps who added it and when. A footnote can also
be written out in the body (`[^key]: ...`). A citation with no source is
refused. `knowledge.link` adds a source or related link to a page (or takes
one off with `remove=true`).

When a page is wrong, fix it and cite why, like a wiki edit; the history
keeps the old text.

## Importing 0.4

`scripts/import_04.py` copies a 0.4 hub's knowledge, concepts, projects and
tasks into these records. Stop the 0.5 hub and copy the 0.4 `knowledge.db`
(and, for handoffs, its sessions db) with `sqlite3 knowledge.db ".backup
copy.db"` (one consistent file; a plain copy can leave recent writes in a
`-wal` file, and the import refuses one), then:

```sh
python rook/plugins/knowledge/scripts/import_04.py --from knowledge-copy.db \
    --records hub-data/records.db --band <the 0.5 band> \
    [--from-band <a 0.4 band>] [--handoffs sessions-copy.db] [--dry-run]
```

The copy is opened read-only. Verification becomes provenance (evidence
links are sources; a person's review is a `user` source); superseded pages
are archived; task links, notes (as comments), open whiteboard lines and the latest
handoff come along; claims don't (claim again). It prints what it imported,
skipped (already there from an earlier run) and renamed (a slug another
record already had).
