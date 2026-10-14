# Contributing to Rook

Thanks for your interest. Rook is maintained by one person, so small, focused
changes with a clear description are the easiest to review. For anything large
(a new subsystem, a protocol change, a new dependency), please open an issue
first to talk it through.

Rook is licensed under the [Apache License 2.0](LICENSE). Unless you say
otherwise, a contribution you submit is licensed under the same terms, as
section 5 of the license describes.

## Development setup

```sh
git clone https://github.com/Bake-Ware/rook
git clone https://github.com/Bake-Ware/telesthete   # optional, next to rook/
cd rook
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

`telesthete` (the transport) is installed from GitHub as a dependency. If a checkout
exists at `../telesthete`, the tests and the worker-bundle builder use it instead, so
you can work on both at once.

The pin is one commit, in two places that must agree: `pyproject.toml` (the
`telesthete @ git+...@<sha>` dependency) and the `Dockerfile`'s `TELESTHETE_REV`
(the relay the hub image compiles with `RELAY_SOURCE=source`; by default it uses
the prebuilt relay from a telesthete release); a test checks they match. The bundle builders
(`rook/remote/build_band_worker.py`, `android/stage_worker.py`) refuse to build
from a telesthete git checkout at any other commit, so a bundle never ships a
different transport than the hub installs; set `ROOK_TELESTHETE_ANY_REV=1` to
build anyway (a warning) while changing both repos. The pin may point at a
telesthete PR branch while a change is in review; when that PR merges, move both
to the merged commit on telesthete `main`.

To run a hub locally, get the relay once (`python3 scripts/fetch-relay.py --dest
.venv/bin` downloads the prebuilt one; or
`cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium`) and
use `scripts/local-hub.sh`; see the [README quickstart](README.md#c-from-source).

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.12 with the voice
service's light deps (`tests/requirements-voice.txt`), the generators' `--check` modes,
`node --check` and shellcheck. Releases are cut by pushing a `vX.Y.Z` tag; see
[Releases and versions](docs/operations/install.md#releases-and-versions).

## Tests

```sh
pytest -q
```

The suite runs offline in about a minute; it needs no hub, relay or network. Add a
test with any behaviour change, especially in `rook/worker`, `rook/band_mcp` and
`rook/remote`. Some browser checks (`tests/browser_*.py`) need Playwright and are not
part of the default run.

The Android app (`android/`) and the ESP32 firmware (`firmware/`) have their own build
steps; see [android/README.md](android/README.md) and the PlatformIO project in
`firmware/`.

## Conventions

- **Python 3.11+**, standard library first. The worker in particular must stay light:
  it runs on Raspberry Pis, phones (via Chaquopy) and Windows. New worker dependencies
  need a good reason and must be optional where possible.
- **Plugins** live in `rook/worker/plugins/`, export `PLUGIN`, claim one namespace, and
  mark methods with `@capability`. Implement `available()` so the plugin only loads
  where it can actually work.
- **Windows is a first-class worker.** Open text files with `encoding="utf-8"`, and keep
  PowerShell installer text in `rook/remote/bootstrap.py` pure ASCII.
- **Configuration goes through flags and `ROOK_*` environment variables**, documented in
  `--help` and [.env.example](.env.example). Never commit keys, tokens, passwords,
  hostnames or IP addresses of real machines; use `example.com` and placeholders.
- **Peer compatibility has a floor.** `rook/core/compat.py` names the oldest worker
  build, Android app and hub this release still talks to. A fallback that serves only
  peers older than that can go; one the newest Android app or the ESP32/RP2040
  firmware still needs stays, and so does code that reads files or databases an older
  release wrote. Raise the floor deliberately, in its own change.
- Match the surrounding code's style and comment density. Comments explain *why*.

## The user manual

`docs/manual/` is Rook's user manual: plain-language pages for people who run
Rook, shown under **Help** and copied into each hub's knowledge base. It is
part of the product, so it ships current with **every release**:

- A change that adds, removes or deprecates a user-visible feature, setting,
  plugin or command updates the manual pages that cover it in the same pull
  request. A new feature goes into a page's front matter `features`; a removed
  one is dropped from it, or its page is marked `deprecated: true`.
- A bullet under an `Added`, `Removed` or `Deprecated` heading in the
  CHANGELOG names the pages it changed: `(manual: jobs, workers)` on the
  bullet, or a `Manual: jobs` line in its block (`n/a` for a change users
  cannot see).
- Run `python tools/manual_check.py` before you push. The test suite only
  checks that pages parse and their links resolve; CI reports coverage and
  CHANGELOG findings without blocking your pull request; the release tooling
  (`tools/release_prep.py`) runs the same check with `--release` and refuses
  to cut a release with a stale manual, so whoever cuts it has to fix what
  was left. `docs/manual/manifest.json` is generated, never committed.

See docs/manual/README.md for how to write a page and
docs/operations/user-manual.md for the checks and the hub side.

## Pull requests

1. Branch from `master`.
2. Keep each commit to one logical change with a message that says why.
3. Run `pytest -q` before pushing (it includes the user-manual check).
4. Describe what changed, how you tested it, and anything reviewers should look at.
   For user-visible changes, add a line to [CHANGELOG.md](CHANGELOG.md) under
   *Unreleased* and update the user manual (above).

## Security issues

Please don't open public issues for vulnerabilities. Contact the maintainer privately
through GitHub (Bake-Ware) and allow time for a fix before disclosing.
