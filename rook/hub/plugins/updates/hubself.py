"""Updating the hub itself, through the way it was installed.

* ``rook-hub``: a hub deployed with ``rook hub deploy`` (``ROOK_RELEASE`` is
  set and the deploy config exists). The verified release sdist is repacked
  as a ``<build>.<adjective>.<noun>/`` tree, its manifest is signed with this
  hub's update key, and ``rook hub deploy`` runs detached (``systemd-run``,
  outside the hub's own cgroup, since it restarts the hub): preflight, DB
  backups, health checks and the dead-man rollback all apply as for a hand
  deploy. Rollback is ``rook hub rollback``.
* ``installer``: a hub from scripts/install-hub.sh (``install.env`` beside
  its virtualenv). A ``--user`` install re-runs the release's own verified
  install-hub.sh with ``--version <tag> --package <verified wheel>``; it
  keeps the data and secrets and restarts the services. A ``--system`` install runs as the unprivileged
  ``rook`` user, so the hub cannot do it: it shows the command to run.
* ``docker``, ``source`` and anything else: explained, never attempted.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("rook.hub.plugins.updates.hubself")

DEPLOY_CONFIG_DEFAULT = Path("/etc/rook/hub-deploy.json")
INSTALL_URL = "https://raw.githubusercontent.com/{repo}/{tag}/scripts/install-hub.sh"


def current_hub() -> dict:
    """What this hub runs: package version, build number and build name."""
    import rook
    from rook import _release
    release = os.environ.get("ROOK_RELEASE", "").strip()
    build = int(_release.BUILD or 0)
    name = _release.NAME or ""
    if not build and release.split(".", 1)[0].isdigit():
        build, name = int(release.split(".", 1)[0]), release
    if not build:
        try:
            from rook.worker._build_info import BUILD as wb
            build = int(wb or 0)
        except Exception:
            build = 0
    # ``version`` is the release version ("" when this is not a release:
    # a source checkout or a `rook hub release build` tree); ``package`` is
    # the version pyproject declares, which says nothing about the build.
    return {"version": _release.VERSION, "build": build, "name": name or release,
            "package": getattr(rook, "__version__", ""), "relay": _release.RELAY,
            "release": release, "python": sys.executable}


def _install_env() -> tuple[Path, dict] | None:
    """install-hub.sh's ``install.env`` for the venv we run in, if any
    (``$ROOK_INSTALL_ENV`` names it for a layout the installer did not make)."""
    env = os.environ.get("ROOK_INSTALL_ENV", "").strip()
    for cand in ([Path(env).expanduser()] if env else [Path(sys.prefix).parent / "install.env"]):
        try:
            text = cand.read_text(encoding="utf-8")
        except OSError:
            continue
        vals = {}
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
        return cand, vals
    return None


def _deploy_config_path() -> Path | None:
    env = os.environ.get("ROOK_HUB_DEPLOY_CONFIG", "").strip()
    p = Path(env).expanduser() if env else DEPLOY_CONFIG_DEFAULT
    return p if p.is_file() else None


def _in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("ROOK_IN_DOCKER") == "1"


def _source_checkout() -> bool:
    import rook
    return (Path(rook.__file__).resolve().parents[1] / ".git").exists()


def detect(repo: str = "Bake-Ware/rook") -> dict:
    """How this hub was installed, and whether it can update itself.

    ``{"kind", "can_apply", "why", "manual"}``: ``manual`` is the command an
    operator runs when the hub cannot do it (with ``{tag}`` to fill in)."""
    cfg_path = _deploy_config_path()
    if os.environ.get("ROOK_RELEASE") and cfg_path is not None:
        try:
            from rook.hubdeploy import config as cfgmod
            cfg = cfgmod.load(str(cfg_path))
        except Exception as e:
            return {"kind": "rook-hub", "can_apply": False, "config": str(cfg_path),
                    "why": f"the deploy config could not be read: {e}", "manual": ""}
        manual = (f"sudo {shlex.quote(sys.executable)} -m rook hub --config {shlex.quote(str(cfg_path))} "
                  "deploy <manifest from Manage > Updates> --yes")
        root_ok = os.access(cfg.root, os.W_OK)
        if cfg.mode == "systemd" and cfg.scope == "system" and os.geteuid() != 0:
            return {"kind": "rook-hub", "can_apply": False, "config": str(cfg_path),
                    "why": "this hub's services are system units, and the hub does not run "
                           "as root, so it cannot switch them itself. Run the command shown "
                           "on the hub machine.", "manual": manual}
        if not root_ok:
            return {"kind": "rook-hub", "can_apply": False, "config": str(cfg_path),
                    "why": f"the hub cannot write its release directory {cfg.root}.",
                    "manual": manual}
        return {"kind": "rook-hub", "can_apply": True, "config": str(cfg_path), "why": "",
                "manual": manual, "scope": cfg.scope, "mode": cfg.mode}
    inst = _install_env()
    if inst is not None:
        path, vals = inst
        mode = vals.get("MODE", "user")
        url = INSTALL_URL.format(repo=repo, tag="{tag}")
        manual = (f"curl -fsSL {url} | bash -s -- --{mode} --version {{tag}} --yes")
        if mode == "system":
            return {"kind": "installer", "can_apply": False, "install_env": str(path),
                    "mode": mode, "manual": manual.replace("| bash", "| sudo bash"),
                    "why": "this hub was installed system-wide and runs as the unprivileged "
                           "'rook' user, so it cannot reinstall itself. Run the command shown "
                           "on the hub machine (your data and passwords are kept)."}
        return {"kind": "installer", "can_apply": True, "install_env": str(path), "mode": mode,
                "systemd": vals.get("SYSTEMD", "1") != "0", "manual": manual, "why": ""}
    if _in_docker():
        return {"kind": "docker", "can_apply": False,
                "manual": "docker compose pull && docker compose up -d   "
                          "(or: docker pull ghcr.io/bake-ware/rook:{version})",
                "why": "this hub runs in a container: the image is the release. Pull the new "
                       "image and recreate the container."}
    if _source_checkout():
        return {"kind": "source", "can_apply": False,
                "manual": "git fetch --tags && git checkout {tag}, then restart the hub",
                "why": "this hub runs from a source checkout, which the update service "
                       "does not change under you."}
    return {"kind": "unknown", "can_apply": False, "manual": "",
            "why": "the update service cannot tell how this hub was installed (no install-hub.sh "
                   "install.env, no `rook hub` deploy config, not a container)."}


# -- the rook-hub path: sdist -> signed hub release ---------------------------------

def repack_sdist(sdist: Path, name: str, out_dir: Path) -> Path:
    """Re-root a release sdist (``rook-0.3.0/...``) as ``<name>/...``, the
    layout ``rook hub deploy`` unpacks. Only regular files and directories
    are copied; anything else in the archive is refused."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"rook-hub-{name}.tar.gz"
    tmp = dest.with_name(dest.name + ".part")
    with tarfile.open(sdist, "r:gz") as src, tarfile.open(tmp, "w:gz") as dst:
        top = None
        for m in src.getmembers():
            parts = Path(m.name).parts
            if not parts or m.name.startswith("/") or ".." in parts:
                raise ValueError(f"unsafe sdist member {m.name!r}")
            top = top or parts[0]
            if parts[0] != top:
                raise ValueError("the sdist has more than one top-level directory")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"the sdist has a link or device ({m.name!r})")
            new = tarfile.TarInfo("/".join((name, *parts[1:])))
            new.size, new.mode, new.mtime = m.size, m.mode, m.mtime
            new.type = m.type
            if m.isfile():
                dst.addfile(new, src.extractfile(m))
            else:
                dst.addfile(new)
    os.replace(tmp, dest)
    return dest


def hub_release_manifest(tarball: Path, meta: dict, signing_key=None) -> dict:
    """A ``rook hub`` release manifest for ``tarball``, signed with this hub's
    update key (the signature means "this hub verified it", nothing more)."""
    import datetime
    from rook.hubdeploy import manifest as mf
    if signing_key is None:
        from rook.remote.update_keys import load_signing_key
        signing_key = load_signing_key()
        if signing_key is None:
            raise RuntimeError("this hub has no update signing key")
    m = {
        "schema": mf.SCHEMA, "typ": mf.TYP, "build": int(meta["build"]), "version": meta["name"],
        "commit": meta.get("commit") or "unknown",
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "filename": tarball.name, "sha256": mf.sha256_file(tarball),
        "size": tarball.stat().st_size, "github_tag": meta.get("tag", ""),
    }
    mf.check_fields({**m, "sig": ""})
    return mf.sign(m, signing_key)


# -- running the update outside our own process ---------------------------------------

class LaunchError(RuntimeError):
    """The update could not be started where it would survive the hub restart."""


# Where the hub's keys and data are: a transient unit starts with a clean
# environment, so `rook hub deploy` (and the installer) would otherwise look
# in the default places and miss a key under ROOK_DATA_DIR or ROOK_UPDATE_KEY.
PASS_ENV = ("ROOK_DATA_DIR", "ROOK_UPDATE_KEY", "ROOK_UPDATE_PUBKEY", "ROOK_HUB_DEPLOY_CONFIG",
            "ROOK_INSTALL_ENV")


def launch(argv: list[str], *, unit: str, log_path: Path, env: dict | None = None,
           user_scope: bool = True, cwd: str | None = None) -> dict:
    """Start ``argv`` so that it survives the hub restarting under it:
    a transient systemd unit when ``systemd-run`` exists (a child of the hub
    would die with the hub's cgroup), else a new session. Raises
    :class:`LaunchError` instead of falling back to a new session when the
    hub itself runs under systemd (``INVOCATION_ID``): that child would be
    killed with the hub's unit halfway through the switch.
    ``ROOK_UPDATES_NO_SYSTEMD_RUN=1`` always uses a new session."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**{k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}, **(env or {})}
    full_env = {**os.environ, **env}
    sd = shutil.which("systemd-run")
    forced_session = os.environ.get("ROOK_UPDATES_NO_SYSTEMD_RUN") == "1"
    if sd and not forced_session:
        setenv = [f"--setenv={k}={v}" for k, v in env.items()]
        cmd = [sd, *(["--user"] if user_scope else []), f"--unit={unit}", "--collect",
               "--description=Rook hub update", *setenv,
               *([f"--working-directory={cwd}"] if cwd else []),
               "--property=StandardOutput=append:" + str(log_path),
               "--property=StandardError=append:" + str(log_path), *argv]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return {"how": "systemd-run", "unit": unit}
        why = (r.stderr or r.stdout).strip()[:200]
        if os.environ.get("INVOCATION_ID"):
            raise LaunchError(f"systemd-run could not start the update ({why}); the hub runs "
                              "under systemd, so a plain child process would be stopped with it")
        log.warning("systemd-run failed (%s); starting the update in a new session", why)
    elif os.environ.get("INVOCATION_ID") and not forced_session:
        raise LaunchError("systemd-run is not installed and the hub runs under systemd, so the "
                          "update would be stopped with the hub; update by hand")
    with open(log_path, "ab") as out:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=full_env, cwd=cwd, start_new_session=True, close_fds=True)
    return {"how": "session", "pid": p.pid}


def log_tail(path: Path, lines: int = 30) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def deploy_history(config: str, n: int = 20) -> list[dict]:
    try:
        from rook.hubdeploy import config as cfgmod
        from rook.hubdeploy import deploy as dp
        return dp.read_history(cfgmod.load(config))[-n:]
    except Exception:
        return []


def rook_hub_argv(config: str, *args: str) -> tuple[list[str], dict]:
    """``python -m rook hub --config C <args>`` run from the release the hub
    runs now (its PYTHONPATH), so the deploy tool is the one we tested with."""
    env = {}
    rel = os.environ.get("ROOK_RELEASE_DIR", "").strip()
    if rel:
        env["PYTHONPATH"] = rel
    return [sys.executable, "-m", "rook", "hub", "--config", config, *args], env


def installer_argv(script: Path, info: dict, tag: str, package: Path | None = None) -> list[str]:
    """The release's install-hub.sh for ``tag``; ``package`` is the release
    wheel the hub already downloaded and verified, so the installer installs
    exactly that file instead of fetching its own copy."""
    argv = ["bash", str(script), f"--{info.get('mode', 'user')}", "--yes", "--version", tag]
    if package is not None:
        argv += ["--package", str(package)]
    if info.get("systemd") is False:
        argv.append("--no-systemd")
    return argv


def release_wheel(rel: dict) -> str | None:
    """The name of the release's Rook wheel (``rook-<version>-py3-none-any.whl``)."""
    import re
    names = [n for n in rel.get("assets", {})
             if re.fullmatch(r"rook-[0-9][A-Za-z0-9.+]*-py3-none-any\.whl", n)]
    return names[0] if len(names) == 1 else None


def describe_apply(kind: dict, meta: dict, tag: str) -> str:
    """One plain sentence for the wizard: what applying will do here."""
    k = kind.get("kind")
    if k == "rook-hub":
        return (f"Deploys {meta.get('name') or tag} with `rook hub deploy`: preflight checks and "
                "database backups first; if the hub does not come back healthy it rolls itself back.")
    if k == "installer":
        return (f"Re-runs the hub installer for {tag}. Your data, keys and passwords are kept; the "
                "hub restarts, so this page reconnects after a minute.")
    return kind.get("why") or "This hub cannot update itself."


def json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def now() -> float:
    return time.time()
