# The Rook user manual

These pages are the Rook user manual: plain-language help for every feature,
written for people who run Rook rather than develop it. The dashboard shows
them under **Help**, and a hub copies them into its knowledge base (the
"Rook manual" pages) so people and agents can search them.

This file is for contributors; it is not part of the manual.

- One Markdown file per page. Each starts with front matter: `id`, `title`,
  `order`, `features` (the feature ids the page covers), `since` (the Rook
  version that added the page) and `deprecated`.
- Write for someone who is not a developer: say what a thing is for and how
  to use it from the dashboard first; commands and file names come after.
- Use the Markdown the dashboard renders: `#`/`##`/`###` headings, `-` and
  `1.` lists, code blocks, `inline code`, **bold**, links to other manual
  pages as `[[manual-<id>]]` and plain `https://` links. No tables or images.
- Keep a page under 18,000 characters (the knowledge base limit is 20,000).
- `manifest.json` is generated: run `python tools/manual_check.py --write`
  after editing a page, then `python tools/manual_check.py` to check it.

The rules for keeping the manual current are in CONTRIBUTING.md ("The user
manual") and docs/operations/user-manual.md.
