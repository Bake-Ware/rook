# The user manual: keeping it current, shipping it, seeding it

Rook's user manual is the set of Markdown pages in `docs/manual/`: plain
language, one page per topic, for people who run Rook. Every release ships it,
the dashboard shows it under **Help**, and a hub copies it into its knowledge
base as the **Rook manual** pages. This page is for maintainers and
contributors; users read the manual itself (`docs/manual/help.md` explains it
to them).

## The rule

**Every release has a current manual.** A change that adds, removes or
deprecates something a user can see updates the manual in the same pull
request, and its CHANGELOG bullet names the pages it changed. Only the
release blocks on it, so an unrelated pull request is never held up by
someone else's missing page:

| Where | What runs | Blocks? |
|---|---|---|
| `pytest` | `tests/test_manual.py::test_the_repository_manual_pages_parse_and_link` (`manual_check --pages`): every page parses and its `[[manual-…]]` links resolve | yes |
| CI job "user manual coverage (advisory)" (`ci.yml`, `continue-on-error`) | `python tools/manual_check.py --base origin/<base branch>`: coverage, the CHANGELOG, and pages the CHANGELOG names must have changed in the branch | no: shown on the pull request |
| By hand | `python tools/manual_check.py` | no |
| Release: `tools/release_prep.py --tag vX.Y.Z` (release workflow, "Stamp version…") | `manual_check.check(release=tag)`: everything above against the previous tag, plus no page's `since` newer than the release | **yes**: the release is not built (`--skip-manual-check` overrides, for emergencies) |

`tools/manual_check.py` builds a **feature inventory** from the code and
checks the manual against it:

- every `##`/`###` heading of docs/FEATURES.md (`features/<heading-slug>`);
- every settings group of the settings schema: the core groups
  (`settings/core/<group>`) and each plugin or service namespace
  (`settings/<namespace>`);
- every hub and worker plugin module (`plugin/hub/<module>`,
  `plugin/worker/<module>`);
- every `rook` command and `rook hub` subcommand (`cli/<command>`,
  `cli/hub/<sub>`).

Each feature must be listed in some page's front matter `features`. A page
that lists a feature which no longer exists must be marked
`deprecated: true` (it then documents the removal) or drop the mapping.
`python tools/manual_check.py --list` prints the inventory with the pages
covering each feature.

`docs/manual/manifest.json` (the pages in reading order with their content
hashes, and the feature-to-page map) is generated, not committed:
`tools/release_prep.py` writes it for every release build and
`manual_check --write` writes it locally. Nothing at runtime needs it; the hub
hashes the pages itself.

### CHANGELOG lines

Under *Unreleased*, a bullet under an `Added`, `Removed` or `Deprecated`
heading (`###`, or `####` inside a workstream block) must name the manual
pages it changed. Other headings are not checked, whatever a bullet's verb.

```markdown
### Widgets
#### Added
Manual: workers, jobs
- A widget column on the Workers page.
#### Removed
- The gadget view (manual: workers).
```

A `Manual:` line covers its block; a `(manual: <page-id>, ...)` on a bullet
covers that bullet. `n/a` says the change is not user-visible.

## Front matter

```markdown
---
id: jobs                  # = the file name; the knowledge slug is manual-jobs
title: Jobs: scheduled work
order: 11                 # reading order on Help and in the knowledge copy
features:
  - features/jobs
  - plugin/hub/jobs
  - settings/job
since: 0.1.0              # the Rook version that added the page
deprecated: false         # true: the feature is going or gone
preview: false            # optional; true: not in a release yet (may list no features)
---
```

Body: `#`-`###` headings, `-` and `1.` lists, code blocks, `inline code`,
**bold**, `[[manual-<id>]]` links between pages and plain `https://` links.
Both the Help page and the knowledge page render exactly that subset. At most
18,000 characters a page (the knowledge store's limit is 20,000).

`desktop-app.md` covers the desktop app, which is still a preview
(`preview: true`, feature `features/desktop-app-preview`).

## Packaging

The wheel carries the pages as `rook/_manual/` (pyproject force-include, like
the agent skill), the Docker image copies `docs/manual/`, and
`ROOK_MANUAL_DIR` overrides both. The split-layout hub installer writes
`ROOK_MANUAL_ADMIN_URL` with the other `ROOK_*_ADMIN_URL`s.

## On a hub

The hub plugin `manual` (rook/hub/plugins/manual, always loaded on the hub)
serves the pages and keeps the knowledge copy:

- **Seed.** When the knowledge plugin is loaded (`ROOK_KNOWLEDGE=1`) and its
  store is empty, the manual is imported once: a contents page
  ("Rook manual", slug `rook-manual`) and one child per page
  (`manual-<id>`), in the primary band, written by `system:rook-manual`,
  tagged `rook-manual` and `rook-<version>`, with one `file` evidence link to
  `docs/manual/<id>.md` and marked verified (so the pages do not land in
  Review mode). A page someone else already has at `rook-manual` is never
  adopted: the contents page becomes `rook-manual-2` (and so on). A
  knowledge base that already has pages is not seeded silently: the plugin
  tries once, then leaves it to the operator's prompt (while the knowledge
  plugin is off it re-checks with a growing pause, up to every 30 minutes).
  Setting `manual.auto_seed` turns seeding off.
- **Edits.** The pages carry `attrs.verified_from {actor, label}`: the
  knowledge store marks a page unverified (with a review note) when anyone
  but that actor changes its title or body. The import record,
  `<hub state>/plugins/manual/state.json`, keeps per page (and for the
  contents page, `root_entry`) the bundled hash it came from and a
  fingerprint of what was written (title, body and tags), so text and tag
  edits both count as edits.
- **Update prompt.** When the bundled manual differs, the operator sees a
  banner on the dashboard; **Review changes** lists pages added, changed and
  removed (the contents page as "Contents page", id `_root`, when its list of
  pages changed), marking pages edited in the knowledge base since and
  pages whose slug someone else holds; those are only written when ticked,
  and a page's own extra tags are kept. **Update** applies it (removed pages
  are archived), **Not now** snoozes it for 24 hours, **Never for this
  version** records the manual's hash and asks again only when it changes.
  `manual.prompt_updates` turns the question off.
- **Agents.** The `rook_knowledge` tool's description says where the manual
  is (guidance slot `tool:rook_knowledge`, editable on Agent instructions);
  `manual.list` / `manual.get` / `manual.status` on worker `rook` read it
  without the wiki; `manual.update` (admin) does what the banner does.
- **Help** (`#help`) reads the knowledge copy for the operator when there is
  one (showing the operator's edits) and the bundled pages for everyone else
  and when there is no copy. API: `/account/manual/api` on the dashboard,
  proxied to `/manual/account-api`.
