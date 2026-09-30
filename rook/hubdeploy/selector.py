"""The single release selector per service, and how to snapshot/restore it.

Exactly one thing decides which release a service runs:

* ``systemd`` mode: ``<unit>.d/90-release.conf``. Any OTHER drop-in that sets
  ``PYTHONPATH``, ``ROOK_RELEASE``, ``WorkingDirectory`` or ``ExecStart`` is a
  stray selector: systemd applies drop-ins in lexical order, so a later one
  silently overrides ours. Deploy refuses while strays exist (or moves them
  into the deploy's backup with ``--adopt-strays``; rollback puts them back).
* ``symlink`` mode: ``current-<service>`` -> ``releases/<version>``, switched
  atomically with rename.

A snapshot is a list of entries ``{"kind": "file"|"symlink", "path", "content"
| "target"}`` (``None`` = absent). ``restore_script`` turns one into plain
shell, so a rollback never depends on the code being rolled back.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from .config import DROPIN_NAME, Config, Service

_STRAY_RE = re.compile(
    r"^\s*(Environment\s*=.*\b(PYTHONPATH|ROOK_RELEASE(_DIR)?)=|WorkingDirectory\s*=|ExecStart\s*=)",
    re.M)
_RELEASE_RE = re.compile(r"\bROOK_RELEASE=([^\s\"']+)")


def systemctl_argv(cfg: Config, *args: str) -> list[str]:
    return ["systemctl", *(["--user"] if cfg.scope == "user" else []), *args]


def _systemctl(cfg: Config, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(systemctl_argv(cfg, *args), capture_output=True, text=True,
                          check=check, timeout=120)


def dropin_text(cfg: Config, version: str) -> str:
    d = cfg.release_dir(version)
    return (
        "# Managed by `rook hub deploy`. This is the ONLY drop-in that selects the\n"
        "# release this unit runs. Do not add other drop-ins that set PYTHONPATH,\n"
        "# ROOK_RELEASE, WorkingDirectory or ExecStart: a later one silently wins.\n"
        "[Service]\n"
        f"Environment=ROOK_RELEASE={version}\n"
        f"Environment=ROOK_RELEASE_DIR={d}\n"
        f"Environment=PYTHONPATH={d}\n"
        f"WorkingDirectory={d}\n")


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _atomic_symlink(p: Path, target: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, p)


class Selector:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- reading ---------------------------------------------------------
    def current(self, svc: Service) -> str | None:
        """The release the selector points at (what the service runs after its
        next restart)."""
        if self.cfg.mode == "systemd":
            m = _RELEASE_RE.search(_read(self.cfg.dropin_path(svc)) or "")
            return m.group(1) if m else None
        link = self.cfg.link_path(svc)
        if not link.is_symlink():
            return None
        return Path(os.readlink(link)).name

    def effective(self, svc: Service) -> str | None:
        """systemd only: the ROOK_RELEASE systemd would actually apply (after all
        drop-ins). Differs from current() exactly when a stray overrides us."""
        if self.cfg.mode != "systemd":
            return None
        try:
            r = _systemctl(self.cfg, "show", "-p", "Environment", "--value", svc.unit)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = _RELEASE_RE.findall(r.stdout or "")
        return found[-1] if found else None

    def strays(self, svc: Service) -> list[Path]:
        if self.cfg.mode != "systemd":
            return []
        ours = self.cfg.dropin_path(svc)
        cands = set(ours.parent.glob("*.conf")) if ours.parent.is_dir() else set()
        try:
            r = _systemctl(self.cfg, "show", "-p", "DropInPaths", "--value", svc.unit)
            cands |= {Path(p) for p in (r.stdout or "").split() if p}
        except (OSError, subprocess.TimeoutExpired):
            pass
        out = []
        for p in sorted(cands):
            if p == ours:
                continue
            if _STRAY_RE.search(_read(p) or ""):
                out.append(p)
        return out

    # -- snapshot / restore ---------------------------------------------
    def snapshot(self, svc: Service, extra: list[Path] = ()) -> list[dict]:
        if self.cfg.mode == "systemd":
            paths = [self.cfg.dropin_path(svc), *extra]
            return [{"kind": "file", "path": str(p), "content": _read(p)} for p in paths]
        link = self.cfg.link_path(svc)
        target = os.readlink(link) if link.is_symlink() else None
        return [{"kind": "symlink", "path": str(link), "target": target}]

    def restore(self, entries: list[dict]) -> None:
        for e in entries:
            p = Path(e["path"])
            if e["kind"] == "file":
                if e["content"] is None:
                    p.unlink(missing_ok=True)
                else:
                    _atomic_write(p, e["content"])
            else:
                if e["target"] is None:
                    if p.is_symlink():
                        p.unlink()
                else:
                    _atomic_symlink(p, e["target"])

    # -- switching -------------------------------------------------------
    def select(self, svc: Service, version: str) -> None:
        if self.cfg.mode == "systemd":
            _atomic_write(self.cfg.dropin_path(svc), dropin_text(self.cfg, version))
        else:
            _atomic_symlink(self.cfg.link_path(svc), str(self.cfg.release_dir(version)))

    def reload(self) -> None:
        if self.cfg.mode == "systemd":
            _systemctl(self.cfg, "daemon-reload", check=True)


def restore_script_lines(cfg: Config, entries: list[dict]) -> list[str]:
    """Plain-shell equivalent of Selector.restore(entries)."""
    q = shlex.quote
    lines = []
    for e in entries:
        p = e["path"]
        if e["kind"] == "file":
            if e["content"] is None:
                lines.append(f"rm -f {q(p)}")
            else:
                lines.append(f"mkdir -p {q(str(Path(p).parent))}")
                lines.append(f"printf '%s' {q(e['content'])} > {q(p + '.rb')} && mv -f {q(p + '.rb')} {q(p)}")
        else:
            if e["target"] is None:
                lines.append(f"rm -f {q(p)}")
            else:
                lines.append(f"ln -sfn {q(e['target'])} {q(p + '.rb')} && mv -Tf {q(p + '.rb')} {q(p)}")
    if cfg.mode == "systemd":
        lines.append(" ".join(q(a) for a in systemctl_argv(cfg, "daemon-reload")))
    return lines


def restart_argv(cfg: Config, svc: Service) -> list[str]:
    if svc.restart:
        return ["/bin/sh", "-c", svc.restart]
    return systemctl_argv(cfg, "restart", svc.unit)


__all__ = ["Selector", "dropin_text", "restore_script_lines", "restart_argv",
           "systemctl_argv", "DROPIN_NAME"]
