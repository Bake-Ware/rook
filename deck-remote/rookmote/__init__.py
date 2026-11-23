"""Rook remote for the Steam Deck: the firmware remote (firmware-remote/) as a
fullscreen app, driven by the Deck's buttons and touchscreen, minus IR.

The modules below the UI (keys, profiles, paths, bridge, lan) import nothing
outside the standard library: the worker plugin (rook/plugins/remote-deck)
uses them too, from the worker's own venv, which has no pygame.
"""

VERSION = "0.2.3"            # bumped with every commit that changes the app (shown on About and Baketris's menu)


def build() -> str:
    """VERSION and the commit it came from: BUILD holds the short commit when installed from
    `git archive` (export-subst, .gitattributes); in a checkout, git says; else just VERSION."""
    from pathlib import Path
    here = Path(__file__).resolve().parent
    commit = ""
    try:
        commit = (here / "BUILD").read_text().strip()
    except OSError:
        pass
    if not commit or "Format" in commit:
        import subprocess
        try:
            commit = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"], capture_output=True,
                                    text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    return f"{VERSION} ({commit})" if commit else VERSION
