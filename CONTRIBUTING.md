# Contributing to Rook

Thanks for your interest. Rook is maintained by one person, so small, focused
changes with a clear description are the easiest to review. For anything large
(a new subsystem, a protocol change, a new dependency), please open an issue
first to talk it through.

Note that the project's license has not been chosen yet (see [LICENSE](LICENSE)).
Contributions are welcome, but the terms they are made under will follow the
license the owner picks.

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

To run a hub locally, build the relay once
(`cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium`) and
use `scripts/local-hub.sh`; see the [README quickstart](README.md#quickstart).

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
- Match the surrounding code's style and comment density. Comments explain *why*.

## Pull requests

1. Branch from `master`.
2. Keep each commit to one logical change with a message that says why.
3. Run `pytest -q` before pushing.
4. Describe what changed, how you tested it, and anything reviewers should look at.
   For user-visible changes, add a line to [CHANGELOG.md](CHANGELOG.md) under
   *Unreleased*.

## Security issues

Please don't open public issues for vulnerabilities. Contact the maintainer privately
through GitHub (Bake-Ware) and allow time for a fix before disclosing.
