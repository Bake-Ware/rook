"""``rook voice``: set up speech on this hub (voice.engine "here") without hand work.

    rook voice status [--model-dir DIR]
    rook voice setup  [--model-dir DIR] [--no-extras]
    rook voice models [--model-dir DIR] [--check] [--optional]

``setup`` installs the voice extras this Python lacks (the ``voice`` extra of
the installed rook, ``pip install 'rook[voice]'``) and downloads the speech
model files (services/voice/models.json, each checked against its SHA-256)
into the model folder. Run it as the user the hub runs as, then restart the
hub (or press Download on Intelligence sources > Voice, which fetches the same
files without a restart). The model folder is ``--model-dir``, else
``$VOICE_MODEL_DIR``, else ``$ROOK_DATA_DIR/plugins/voice`` (the voice
plugin's own folder): pass ``--model-dir`` when Settings > Voice > Model folder
says otherwise. Chatterbox (cloned voices on a GPU) stays a separate venv; see
docs/operations/voice.md.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path


def _model_dir(arg: str | None) -> str:
    d = arg or os.environ.get("VOICE_MODEL_DIR") or ""
    if not d and os.environ.get("ROOK_DATA_DIR"):
        d = str(Path(os.environ["ROOK_DATA_DIR"]) / "plugins" / "voice")
    if not d:
        raise SystemExit("rook voice: no model folder: pass --model-dir (the hub's data folder "
                         "+ /plugins/voice), or set ROOK_DATA_DIR or VOICE_MODEL_DIR")
    return d


def _fetcher():
    from ..hub.plugins.voice import engine as eng
    pkg = eng.find_engine()
    if pkg is None:
        raise SystemExit("rook voice: the voice service code is not installed with this rook")
    return importlib.import_module(pkg + ".fetch_models")


def voice_extras() -> list[str]:
    """The requirements of rook's ``voice`` extra: from the installed package's
    metadata, else (a source checkout run in place) from pyproject.toml."""
    try:
        from importlib.metadata import PackageNotFoundError, requires
        reqs = requires("rook") or []
    except PackageNotFoundError:
        reqs = []
    out = [r.split(";")[0].strip() for r in reqs
           if re.search(r"""extra\s*==\s*['"]voice['"]""", r.partition(";")[2])]
    if out:
        return out
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        import tomllib
        data = tomllib.loads(pyproject.read_text())
        return list(data.get("project", {}).get("optional-dependencies", {}).get("voice", []))
    return []


def _status(model_dir: str) -> int:
    from ..hub.plugins.voice import engine as eng
    extras = eng.missing_extras()
    models = eng.missing_models(model_dir)
    print(f"voice extras: {'missing ' + ', '.join(extras) if extras else 'installed'}")
    print(f"model folder: {model_dir}")
    print(f"model files:  {'missing ' + ', '.join(models) if models else 'all there'}")
    if extras or models:
        print("run: rook voice setup" + (f" --model-dir {model_dir}" if model_dir else ""))
    return 1 if extras or models else 0


def _install_extras() -> None:
    from ..hub.plugins.voice import engine as eng
    if not eng.missing_extras():
        print("voice extras: already installed")
        return
    reqs = voice_extras()
    if not reqs:
        raise SystemExit("rook voice: cannot tell rook's voice extras here; run "
                         f"{eng.EXTRAS_HINT} yourself")
    print(f"installing the voice extras (about 1 GB): {' '.join(reqs)}", flush=True)
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "-q", *reqs])
    if rc != 0:
        raise SystemExit(f"rook voice: pip failed ({rc})")
    importlib.invalidate_caches()
    still = eng.missing_extras()
    if still:
        raise SystemExit(f"rook voice: still missing after pip: {', '.join(still)}")


def _models(model_dir: str, check: bool = False, optional: bool = False) -> int:
    fm = _fetcher()
    argv = [model_dir] + (["--check"] if check else []) + (["--optional"] if optional else [])
    return fm.main(argv)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rook voice", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("status", "what speech on this hub still needs"),
                       ("setup", "install the voice extras and download the model files"),
                       ("models", "download (or --check) the model files only")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--model-dir", help="the model folder (default: see rook voice --help)")
        if name == "setup":
            p.add_argument("--no-extras", action="store_true", help="only the model files")
        if name == "models":
            p.add_argument("--check", action="store_true", help="check the files; download nothing")
            p.add_argument("--optional", action="store_true", help="also the speaker model")
    args = ap.parse_args(argv)
    model_dir = _model_dir(args.model_dir)
    if args.cmd == "status":
        return _status(model_dir)
    if args.cmd == "models":
        return _models(model_dir, args.check, args.optional)
    if not args.no_extras:
        _install_extras()
    rc = _models(model_dir)
    if rc == 0:
        print("voice is ready to run on this hub: restart the hub, or press Check on "
              "Intelligence sources > Voice")
    return rc


if __name__ == "__main__":
    sys.exit(main())
