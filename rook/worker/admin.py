"""Runtime capability administration for a worker.

Two operator-facing feature sets, both driven from the `rook band` CLI:

* **Plugin enable/disable** — load or unload a plugin on a *live* worker and
  persist the choice so it survives restarts. Granularity is per-plugin (the
  unit that owns a namespace of caps), exposed as ``worker.plugin.*``.

* **Custom command-caps** — define a capability that runs a shell command with
  parameter substitution, e.g. ``cmd.deploy`` → ``systemctl restart {svc}``.
  Definitions persist per-worker and re-register on boot. Exposed as
  ``customcap.*`` for management; the caps themselves land under ``cmd.<name>``.

Security: a custom cap is a *parameterised* ``shell.exec`` — and any band member
can already call ``shell.exec`` — so this adds no new privilege, only structure.
The command *template* is operator-authored (trusted); the *argument values*
supplied at call time are ``shlex.quote``-escaped, so a caller can't break out of
the template into arbitrary shell.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import pkgutil
import shlex
from pathlib import Path
from typing import Any

from .plugin import Plugin, load_plugins

log = logging.getLogger("rook.worker.admin")

_WORKER_DIR = Path(os.path.expanduser("~")) / ".rook-band-worker"
_PLUGIN_STATE = _WORKER_DIR / "plugins.json"        # {"disabled": [module, ...]}
_CUSTOM_STATE = _WORKER_DIR / "custom_caps.json"    # {name: {command, args, ...}}

_CUSTOM_NS = "cmd"      # custom caps register as cmd.<name>
_MAX_OUT = 100_000
_MAX_ERR = 20_000


def load_disabled() -> set[str]:
    """Persisted set of disabled plugin module names. Read at boot by the Worker
    so disabled plugins are never loaded in the first place."""
    try:
        return set(json.loads(_PLUGIN_STATE.read_text(encoding="utf-8")).get("disabled", []))
    except Exception:
        return set()


def _save_disabled(names: set[str]) -> None:
    _WORKER_DIR.mkdir(parents=True, exist_ok=True)
    _PLUGIN_STATE.write_text(json.dumps({"disabled": sorted(names)}, indent=2) + "\n",
                             encoding="utf-8")


def _load_custom() -> dict:
    try:
        d = json.loads(_CUSTOM_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_custom(defs: dict) -> None:
    _WORKER_DIR.mkdir(parents=True, exist_ok=True)
    _CUSTOM_STATE.write_text(json.dumps(defs, indent=2) + "\n", encoding="utf-8")


class WorkerAdmin:
    """Holds live references to the worker's registry + plugin list so runtime
    changes take effect immediately, and persists them so they survive restart.
    Constructed and wired by :class:`rook.worker.core.Worker`."""

    def __init__(self, registry, plugins: list[Plugin], plugins_pkg: str) -> None:
        self.registry = registry
        self.plugins = plugins            # the Worker's live list (mutated in place)
        self.plugins_pkg = plugins_pkg
        self._custom: dict = _load_custom()

    def register_caps(self) -> None:
        """Register the admin capabilities and re-hydrate persisted custom caps."""
        r = self.registry
        r.register("worker.plugin.list", self.plugin_list)
        r.register("worker.plugin.enable", self.plugin_enable)
        r.register("worker.plugin.disable", self.plugin_disable)
        r.register("customcap.list", self.customcap_list)
        r.register("customcap.add", self.customcap_add)
        r.register("customcap.remove", self.customcap_remove)
        # Re-register every persisted custom cap.
        for name, spec in list(self._custom.items()):
            try:
                self._register_custom(name, spec)
            except Exception:
                log.exception("failed to register custom cap %s", name)

    # -- plugin enable / disable --------------------------------------------

    def _all_modules(self) -> list[str]:
        pkg = importlib.import_module(self.plugins_pkg)
        return sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)
                      if not m.name.startswith("_"))

    def plugin_list(self) -> dict:
        """List every plugin module and whether it's currently loaded, with the
        caps each loaded plugin provides."""
        loaded = {p._module: p for p in self.plugins if getattr(p, "_module", "")}
        disabled = load_disabled()
        out = []
        for mod in self._all_modules():
            p = loaded.get(mod)
            out.append({
                "module": mod,
                "loaded": p is not None,
                "disabled": mod in disabled,
                "namespace": p.NAMESPACE if p else None,
                "caps": sorted(p.caps().keys()) if p else [],
            })
        return {"ok": True, "plugins": out}

    async def plugin_disable(self, module: str) -> dict:
        """Unload a plugin now and keep it unloaded across restarts. Its caps are
        unregistered immediately."""
        p = next((x for x in self.plugins if getattr(x, "_module", "") == module), None)
        if p is None:
            return {"ok": False, "error": f"plugin not loaded: {module}"}
        # Refuse to disable the admin/self-update machinery — that would strip
        # worker.restart/apply and the ability to re-enable anything remotely.
        if module == "selfupdate":
            return {"ok": False, "error": "refusing to disable selfupdate (would strip worker.* control)"}
        removed = []
        for dotpath in p.caps():
            if self.registry.unregister(dotpath):
                removed.append(dotpath)
        # best-effort teardown
        try:
            res = p.stop()
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("plugin %s stop() failed", module)
        self.plugins[:] = [x for x in self.plugins if x is not p]
        d = load_disabled(); d.add(module); _save_disabled(d)
        log.info("disabled plugin %s (%d caps removed)", module, len(removed))
        return {"ok": True, "module": module, "caps_removed": removed}

    async def plugin_enable(self, module: str) -> dict:
        """Load a plugin now and keep it loaded across restarts. Honours the
        plugin's own ``available()`` gate (won't load where it can't function)."""
        if any(getattr(x, "_module", "") == module for x in self.plugins):
            # Already loaded — just clear any persisted disable.
            d = load_disabled()
            if module in d:
                d.discard(module); _save_disabled(d)
            return {"ok": True, "module": module, "already_loaded": True}
        if module not in self._all_modules():
            return {"ok": False, "error": f"no such plugin module: {module}"}
        # Reuse the loader for one module so available()/registration match boot.
        before = {id(p) for p in self.plugins}
        loaded = load_plugins(self.plugins_pkg, self.registry, enabled=[module])
        new = [p for p in loaded if id(p) not in before]
        if not new:
            return {"ok": False,
                    "error": f"{module} did not load (available() false or no PLUGIN)"}
        for p in new:
            self.plugins.append(p)
            try:
                res = p.start()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("plugin %s start() failed", module)
        d = load_disabled(); d.discard(module); _save_disabled(d)
        caps = sorted(c for p in new for c in p.caps())
        log.info("enabled plugin %s (%d caps)", module, len(caps))
        return {"ok": True, "module": module, "caps_added": caps}

    # -- custom command-caps -------------------------------------------------

    def customcap_list(self) -> dict:
        """List defined custom command-caps (name, command template, args)."""
        return {"ok": True, "custom": self._custom}

    def customcap_add(self, name: str, command: str,
                      args: list | None = None, description: str = "",
                      timeout: float = 30.0) -> dict:
        """Define (or replace) a custom cap ``cmd.<name>`` that runs ``command``.

        ``command`` is a shell template; ``{arg}`` placeholders are filled from
        the call's keyword args, each shell-escaped. ``args`` names the caps's
        parameters (so the CLI/dashboard can build a form). Persists + registers
        immediately.
        """
        name = str(name or "").strip()
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            return {"ok": False, "error": "name must be alphanumeric (-/_ allowed)"}
        if not command or not str(command).strip():
            return {"ok": False, "error": "command required"}
        args = [str(a).strip() for a in (args or []) if str(a).strip()]
        for a in args:
            if not a.isidentifier():
                return {"ok": False, "error": f"arg name not a valid identifier: {a}"}
        spec = {"command": str(command), "args": args,
                "description": str(description or ""), "timeout": float(timeout)}
        try:
            self._register_custom(name, spec)     # validates placeholders too
        except Exception as e:
            return {"ok": False, "error": f"register failed: {e}"}
        self._custom[name] = spec
        _save_custom(self._custom)
        return {"ok": True, "cap": f"{_CUSTOM_NS}.{name}", "spec": spec}

    def customcap_remove(self, name: str) -> dict:
        """Delete a custom cap and unregister it."""
        name = str(name or "").strip()
        if name not in self._custom:
            return {"ok": False, "error": f"no such custom cap: {name}"}
        self.registry.unregister(f"{_CUSTOM_NS}.{name}")
        del self._custom[name]
        _save_custom(self._custom)
        return {"ok": True, "removed": name}

    def _register_custom(self, name: str, spec: dict) -> None:
        dotpath = f"{_CUSTOM_NS}.{name}"
        handler = self._make_handler(name, spec)
        self.registry.register(dotpath, handler, replace=True)

    def _make_handler(self, name: str, spec: dict):
        argnames: list[str] = list(spec.get("args", []))
        template: str = spec["command"]
        timeout = float(spec.get("timeout", 30.0))
        # Validate placeholders up front: every {x} in the template must be a
        # declared arg (so a typo is caught at definition, not at call time).
        import string
        fields = {fn for _, fn, _, _ in string.Formatter().parse(template) if fn}
        unknown = fields - set(argnames)
        if unknown:
            raise ValueError(f"template uses undeclared args: {sorted(unknown)}")

        async def handler(**kwargs):
            missing = [a for a in argnames if a not in kwargs]
            if missing:
                return {"ok": False, "error": f"missing args: {missing}"}
            safe = {a: shlex.quote(str(kwargs[a])) for a in argnames}
            cmd = template.format(**safe)
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"timed out after {timeout}s", "cmd": cmd}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}", "cmd": cmd}
            return {"ok": proc.returncode == 0, "code": proc.returncode, "cmd": cmd,
                    "stdout": out.decode(errors="replace")[:_MAX_OUT],
                    "stderr": err.decode(errors="replace")[:_MAX_ERR]}

        # Give the handler a real signature + doc so caps.describe builds a form.
        params = [inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY, annotation=str)
                  for a in argnames]
        handler.__signature__ = inspect.Signature(params)
        handler.__doc__ = spec.get("description") or f"custom command: {template}"
        handler.__name__ = f"custom_{name}"
        return handler
