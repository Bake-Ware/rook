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
import pkgutil
import shlex
from typing import Any

from ..core.paths import worker_state_dir
from ..core.pluginkit import no_window
from .plugin import Plugin

log = logging.getLogger("rook.worker.admin")

_WORKER_DIR = worker_state_dir()
_PLUGIN_STATE = _WORKER_DIR / "plugins.json"        # {"disabled": [...], "enabled": [...]}
_CUSTOM_STATE = _WORKER_DIR / "custom_caps.json"    # {name: {command, args, ...}}

_CUSTOM_NS = "cmd"      # custom caps register as cmd.<name>
_MAX_OUT = 100_000
_MAX_ERR = 20_000


def _load_state() -> dict:
    try:
        d = json.loads(_PLUGIN_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_disabled() -> set[str]:
    """Persisted set of disabled plugin module names. Read at boot by the Worker
    so disabled plugins are never loaded in the first place."""
    return set(_load_state().get("disabled", []) or [])


def load_enabled() -> set[str]:
    """Plugins enabled at runtime that ``--enable`` leaves out: the Worker adds
    them to its enable list at boot, so an enable survives a restart."""
    return set(_load_state().get("enabled", []) or [])


def _save_state(disabled: set[str] | None = None, enabled: set[str] | None = None) -> None:
    state = _load_state()
    if disabled is not None:
        state["disabled"] = sorted(disabled)
    if enabled is not None:
        state["enabled"] = sorted(enabled)
    _WORKER_DIR.mkdir(parents=True, exist_ok=True)
    _PLUGIN_STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _save_disabled(names: set[str]) -> None:
    _save_state(disabled=names)


#: Never switched off by an enrollment profile: worker control and enrollment.
_PROFILE_PROTECTED = frozenset({"selfupdate", "config", "enrollment", "info"})


def _profile_modules(v: Any) -> set[str]:
    if not isinstance(v, list):
        return set()
    return {m for m in (str(x or "").strip() for x in v)
            if m and len(m) <= 64 and m.replace("_", "").replace("-", "").isalnum()}


def apply_enrollment_profile(saved: dict | None = None) -> dict | None:
    """Apply the plugin profile the hub handed this worker at enrollment, on
    its first start only.

    ``saved`` is the enrollment file (rook.worker.enroll.load()). The profile
    (``{"enable": [...], "disable": [...]}``, a pairing code's or the band's
    defaults) is applied only while this worker has no persisted plugin state
    yet (no ``plugins.json``): a re-enrollment or a re-pairing of a worker that
    already ran never undoes an operator's per-worker switches. Its
    ``disable`` list becomes the persisted disables, exactly like
    ``worker.plugin.disable`` (protected modules excepted). Its ``enable``
    list is recorded but changes nothing: every plugin loads by default, and
    adding to the persisted ``enabled`` set would widen an ``--enable``
    allowlist. Returns what was applied, or ``None``."""
    if saved is None:
        try:
            from .enroll import load
            saved = load()
        except Exception:
            return None
    profile = (saved or {}).get("plugin_profile")
    if not isinstance(profile, dict):
        return None
    enable = _profile_modules(profile.get("enable"))
    disable = _profile_modules(profile.get("disable")) - enable - _PROFILE_PROTECTED
    if not enable and not disable:
        return None
    if _PLUGIN_STATE.exists():
        return None   # not the first start: the worker's own state wins
    import hashlib
    pid = hashlib.sha256(json.dumps({"enable": sorted(enable), "disable": sorted(disable)},
                                    sort_keys=True).encode("utf-8")).hexdigest()[:16]
    _WORKER_DIR.mkdir(parents=True, exist_ok=True)
    _PLUGIN_STATE.write_text(json.dumps({"disabled": sorted(disable), "enabled": [],
                                         "profile_applied": pid}, indent=2) + "\n",
                             encoding="utf-8")
    log.info("applied the enrollment plugin profile %s: off %s", pid, sorted(disable) or "-")
    return {"id": pid, "enable": sorted(enable), "disable": sorted(disable)}


def _load_custom() -> dict:
    try:
        d = json.loads(_CUSTOM_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_custom(defs: dict) -> None:
    _WORKER_DIR.mkdir(parents=True, exist_ok=True)
    _CUSTOM_STATE.write_text(json.dumps(defs, indent=2) + "\n", encoding="utf-8")


#: Plugins that cannot be disabled remotely: they carry worker.restart/apply
#: and the ability to re-enable anything.
_PROTECTED = frozenset({"selfupdate"})


class WorkerAdmin:
    """Runtime plugin and custom-cap administration for a live worker.
    Plugin changes go through the worker's :class:`rook.core.host.PluginHost`
    (the boot path: placement, settings, collision safety, ``bind_worker``,
    status) and are persisted so they survive a restart. Constructed and
    wired by :class:`rook.worker.core.Worker`."""

    def __init__(self, registry, plugins: list[Plugin], plugins_pkg: "str | list[str]",
                 enable_arg: list[str] | None = None, host: Any = None) -> None:
        self.registry = registry
        self.plugins = plugins            # the Worker's live list (the host's own)
        self.plugins_pkg = [plugins_pkg] if isinstance(plugins_pkg, str) else list(plugins_pkg)
        self.enable_arg = enable_arg      # --enable as given (None = all built-ins)
        self.host = host
        self._custom: dict = _load_custom()

    def excluded(self) -> list[str]:
        """Modules --enable leaves out that were not enabled at runtime."""
        if self.enable_arg is None:
            return []
        keep = set(self.enable_arg) | load_enabled()
        try:
            return [m for m in self._all_modules() if m not in keep]
        except Exception:
            return []

    def register_caps(self) -> None:
        """Register the admin capabilities and re-hydrate persisted custom caps."""
        r = self.registry
        r.register("worker.plugin.list", self.plugin_list)
        r.register("worker.plugin.enable", self.plugin_enable)
        r.register("worker.plugin.disable", self.plugin_disable)
        if self.host is not None:
            r.register("worker.plugin.reload", self.plugin_reload)
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
        if self.host is not None and self.host.candidates:
            return sorted(self.host.candidates)
        out: set[str] = set()
        for name in self.plugins_pkg:
            pkg = importlib.import_module(name)
            out.update(m.name for m in pkgutil.iter_modules(pkg.__path__)
                       if not m.name.startswith("_"))
        return sorted(out)

    def plugin_list(self) -> dict:
        """List every plugin module and whether it's currently loaded, with the
        caps each loaded plugin provides."""
        loaded = {p._module: p for p in self.plugins if getattr(p, "_module", "")}
        disabled = load_disabled()
        excluded = set(self.excluded())
        out = []
        for mod in self._all_modules():
            p = loaded.get(mod)
            row = {
                "module": mod,
                "loaded": p is not None,
                "disabled": mod in disabled,
                "namespace": p.NAMESPACE if p else None,
                "caps": sorted(p.caps().keys()) if p else [],
            }
            st = (self.host.status.get(mod) if self.host is not None else None) or {}
            cand = self.host.candidates.get(mod) if self.host is not None else None
            if cand is not None:
                row["source"] = cand.source   # package:<pkg> | entry_point:<name>
            if p is None:
                # Why it isn't loaded, so a UI can explain it (settings P14).
                if mod in disabled:
                    row["reason"] = "disabled by an operator"
                elif mod in excluded:
                    row["excluded"] = True
                    row["reason"] = "left out by --enable on the worker's command line"
                elif st.get("state") in ("not_placed", "failed", "skipped") and st.get("reason"):
                    row["reason"] = st["reason"]
                else:
                    row["reason"] = "not available on this host (backend or setting missing)"
            if st.get("state"):
                row["state"] = st["state"]
            for k in ("start_error", "bind_error"):
                if st.get(k):
                    row[k] = st[k]
            out.append(row)
        return {"ok": True, "plugins": out, "enable": self.enable_arg}

    async def plugin_disable(self, module: str) -> dict:
        """Unload a plugin now and keep it unloaded across restarts. Its caps are
        unregistered immediately."""
        if self.host is not None and self.host.loaded(module) is None:
            return {"ok": False, "error": f"plugin not loaded: {module}"}
        # Refuse to disable the admin/self-update machinery — that would strip
        # worker.restart/apply and the ability to re-enable anything remotely.
        if module in _PROTECTED:
            return {"ok": False, "error": f"refusing to disable {module} (would strip worker.* control)"}
        if self.host is None:
            return {"ok": False, "error": "this worker has no plugin host"}
        res = await self.host.disable(module)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        d = load_disabled(); d.add(module)
        e = load_enabled(); e.discard(module)
        _save_state(disabled=d, enabled=e)
        return {"ok": True, "module": module, "caps_removed": res.get("caps_removed", [])}

    async def plugin_enable(self, module: str) -> dict:
        """Load a plugin now and keep it loaded across restarts.

        Loaded the way boot loads it: placement, settings, ``available()``,
        collision checks and its worker binding. Won't load where it can't
        function."""
        if self.host is None:
            return {"ok": False, "error": "this worker has no plugin host"}
        if module not in self._all_modules():
            return {"ok": False, "error": f"no such plugin module: {module}"}
        res = await self.host.enable(module)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        d = load_disabled(); d.discard(module)
        e = load_enabled()
        if self.enable_arg is not None and module not in self.enable_arg:
            e.add(module)   # --enable leaves it out: remember, or a restart drops it
        _save_state(disabled=d, enabled=e)
        if res.get("already_loaded"):
            return {"ok": True, "module": module, "already_loaded": True}
        return {"ok": True, "module": module, "caps_added": res.get("caps", [])}

    async def plugin_reload(self, module: str) -> dict:
        """Restart one loaded plugin with its current settings.

        Stops it, loads it again (new instance, settings re-read) and starts
        it, e.g. after changing a setting marked ``reload``."""
        res = await self.host.reload(module)
        return res if res.get("ok") else {"ok": False, "error": res.get("error"),
                                          **({"unloaded": True} if res.get("unloaded") else {})}

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
        from ..core.plugin import TIMEOUT_MARGIN, CapMeta
        dotpath = f"{_CUSTOM_NS}.{name}"
        handler = self._make_handler(name, spec)
        # The command's own timeout bounds it; core's cut-off sits just past
        # that, so a long-running custom cap is not killed at the default.
        meta = CapMeta(risk="exec", scope="local", timeout=float(spec.get("timeout", 30.0)) + TIMEOUT_MARGIN)
        self.registry.register(dotpath, handler, replace=True, meta=meta)

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
                    stderr=asyncio.subprocess.PIPE, **no_window())
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
