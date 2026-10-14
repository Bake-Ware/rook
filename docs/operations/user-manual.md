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
request, and its CHANGELOG line names the pages it changed. The build
enforces it:

| Where | What runs | Fails when |
|---|---|---|
| `pytest` | `tests/test_manual.py::test_the_repository_manual_passes_the_release_check` | anything below, offline |
| By hand | `python tools/manual_check.py` | same |
| Pull request CI (suggested) | `python tools/manual_check.py --base origin/master` | also: a page the CHANGELOG names did not change in this branch |
| Release (`tools/release_prep.py`, PR #104) | `python tools/manual_check.py --release <tag>` | also: against the previous tag, and a page's `since` is newer than the release |

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

`docs/manual/manifest.json` is generated (`--write`): the pages in reading
order with their content hashes, and the feature-to-page map. A page edited
without regenerating it fails the check, so a reviewer always sees the
manifest change next to the page change.

### CHANGELOG lines

Under *Unreleased*, a bullet that adds, removes or deprecates something (it
is under an `### Added` / `### Removed` / `### Deprecated` heading, or
starts with Added, Adds, New, Removed, Remove, Deprecated, Retired...) must
name the manual pages it changed:

```markdown
### Widgets
Manual: workers, jobs
- Added a widget column to the Workers page.
- Removed the gadget view (manual: workers).
```

A `Manual:` line under the `###` heading covers the whole block; a
`(manual: <page-id>, ...)` on a bullet covers that bullet. `n/a` says the
change is not user-visible (an internal module, a test helper).

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
---
```

Body: `#`-`###` headings, `-` and `1.` lists, code blocks, `inline code`,
**bold**, `[[manual-<id>]]` links between pages and plain `https://` links.
Both the Help page and the knowledge page render exactly that subset. At most
18,000 characters a page (the knowledge store's limit is 20,000).

## The release hook (for PR #104)

`tools/release_prep.py` is not on this branch yet. When the release pipeline
lands, it should refuse to stamp a release with a stale manual by calling the
check before anything is modified:

```python
# tools/release_prep.py, in main(), right after the tag is validated:
import manual_check                       # tools/ is on sys.path (ROOT/tools)
problems = manual_check.check(release=args.tag)
if problems:
    sys.exit("the user manual is out of date:\n  - " + "\n  - ".join(problems))
```

or, from the workflow, a step before `release_prep.py` in the `dist` job
(`fetch-depth: 0` is already set, so the previous tag is available):

```yaml
- name: The user manual is current
  run: python tools/manual_check.py --release "$TAG"
```

The CI workflow (`ci.yml`) should run `python tools/manual_check.py --base
origin/${{ github.base_ref }}` on pull requests. The pytest above already
runs in CI without git.

The wheel carries the pages as `rook/_manual/` (pyproject force-include, like
the agent skill), the Docker image copies `docs/manual/`, and
`ROOK_MANUAL_DIR` overrides both.

## On a hub

The hub plugin `manual` (rook/hub/plugins/manual, always loaded on the hub)
serves the pages and keeps the knowledge copy:

- **Seed.** When the knowledge plugin is loaded (`ROOK_KNOWLEDGE=1`) and its
  store is empty, the manual is imported once: a folder page `rook-manual`
  and one child per page (`manual-<id>`), in the primary band, written by
  `system:rook-manual`, tagged `rook-manual` and `rook-<version>`, each with a
  `file` evidence link to `docs/manual/<id>.md` and marked verified (so the
  pages do not land in Review mode). Setting `manual.auto_seed` turns it off.
  A knowledge base that already has pages is not seeded silently: the
  operator is asked, as for an update.
- **Update prompt.** The import is recorded in
  `<hub state>/plugins/manual/state.json` (per page: the bundled hash it came
  from and the hash of what was written). When the bundled manual differs,
  the operator sees a banner on the dashboard; **Review changes** lists pages
  added, changed and removed, marking pages edited in the knowledge base
  since (those are only replaced when ticked). **Update** applies it (removed
  pages are archived), **Not now** snoozes it for 24 hours, **Never for this
  version** records the manual's hash and asks again only when it changes.
  `manual.prompt_updates` turns the question off.
- **Agents.** The `rook_knowledge` tool's description says where the manual
  is (guidance slot `tool:rook_knowledge`, editable on Agent instructions);
  `manual.list` / `manual.get` / `manual.status` on worker `rook` read it
  without the wiki; `manual.update` (admin) does what the banner does.
- **Help** (`#help`) reads the knowledge copy when there is one (showing the
  operator's edits) and the bundled pages otherwise. API:
  `/account/manual/api` on the dashboard, proxied to `/manual/account-api`.
