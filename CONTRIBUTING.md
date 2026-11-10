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

The pin is one commit, in `pyproject.toml` (the `telesthete @ git+...@<sha>`
dependency). The bundle builders
(`rook/remote/build_band_worker.py`, `android/stage_worker.py`) refuse to build
from a telesthete git checkout at any other commit, so a bundle never ships a
different transport than the hub installs; set `ROOK_TELESTHETE_ANY_REV=1` to
build anyway (a warning) while changing both repos. The pin may point at a
telesthete PR branch while a change is in review; when that PR merges, move it
to the merged commit on telesthete `main`.

To run a hub locally, see [Install and first run](docs/manual-0.5/install.md).

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.12 with the voice
service's light deps (`tests/requirements-voice.txt`), the generators' `--check` modes,
`node --check` and shellcheck. Releases are cut by pushing a `vX.Y.Z` tag; see
[Releases and versions](docs/operations/install.md#releases-and-versions).

## Tests

```sh
pytest -q
```

The suite runs offline in about a minute; it needs no hub, relay or network. Add a
test with any behaviour change, especially in `rook/core2`, `rook/kit` and
`rook/plugins`. Some browser checks (`tests/browser_*.py`) need Playwright and are not
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
- **Windows is a first-class worker.** Open text files with `encoding="utf-8"`.
- **Configuration goes through flags and `ROOK_*` environment variables**, documented in
  `--help`. Never commit keys, tokens, passwords,
  hostnames or IP addresses of real machines; use `example.com` and placeholders.
- **Peer compatibility has a floor.** `rook/core/compat.py` names the oldest worker
  build, Android app and hub this release still talks to. A fallback that serves only
  peers older than that can go; one the newest Android app or the ESP32/RP2040
  firmware still needs stays, and so does code that reads files or databases an older
  release wrote. Raise the floor deliberately, in its own change.
- Match the surrounding code's style and comment density. Comments explain *why*.

## The user manual

`docs/manual-0.5/` is Rook's user manual, with each plugin's own
`docs/manual.md`. A change that adds, removes or changes something users see
updates the pages that cover it in the same pull request. Run
`python tools/manual05_check.py` before you push (tests/test_manual05.py runs
the same checks).

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
