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
