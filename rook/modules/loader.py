"""Module loader — discovers, loads, and manages module lifecycle."""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

BUILTIN_MODULES_DIR = Path(__file__).parent
CUSTOM_MODULES_DIR = Path("./data/modules")


class Module(Protocol):
    """Interface every module must implement."""

    MODULE_NAME: str
    MODULE_DESCRIPTION: str
    MODULE_TYPE: str  # "channel", "service", "integration"

    async def start(self, agent: Any, config: Any) -> None: ...
    async def stop(self) -> None: ...


class ModuleLoader:
    """Discovers and manages module lifecycle."""

    def __init__(self):
        self._modules: dict[str, Any] = {}  # name -> module instance or module object
        self._running: dict[str, bool] = {}

    async def load_all(self, agent: Any, config: Any) -> None:
        """Load all builtin and custom modules."""
        # Builtin modules (rook/modules/*.py)
        for path in sorted(BUILTIN_MODULES_DIR.glob("*.py")):
            if path.stem.startswith("_") or path.stem == "loader":
                continue
            await self._load_module(path, agent, config, builtin=True)

        # Custom modules (data/modules/*.py)
        CUSTOM_MODULES_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(CUSTOM_MODULES_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            await self._load_module(path, agent, config, builtin=False)

        log.info("Modules loaded: %s", list(self._modules.keys()))

    async def _load_module(self, path: Path, agent: Any, config: Any, builtin: bool) -> None:
        """Load and start a single module."""
        try:
            if builtin:
                # Import as part of the package
                module_name = f"rook.modules.{path.stem}"
                mod = importlib.import_module(module_name)
            else:
                spec = importlib.util.spec_from_file_location(f"custom_module_{path.stem}", path)
                if not spec or not spec.loader:
                    return
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

            name = getattr(mod, "MODULE_NAME", path.stem)
            mod_type = getattr(mod, "MODULE_TYPE", "unknown")
            description = getattr(mod, "MODULE_DESCRIPTION", "")

            # Check if module is enabled in config
            module_config = config.get(f"modules.{name}", {})
            if module_config is not None and isinstance(module_config, dict):
                if not module_config.get("enabled", True):
                    log.info("Module '%s' disabled in config, skipping", name)
                    return

            start_fn = getattr(mod, "start", None)
            if not start_fn:
                log.warning("Module '%s' has no start() function, skipping", name)
                return

            self._modules[name] = mod
            await start_fn(agent, config)
            self._running[name] = True
            source = "builtin" if builtin else "custom"
            log.info("Module started: [%s] %s (%s) — %s", mod_type, name, source, description)

        except Exception as e:
            log.error("Failed to load module %s: %s", path.stem, e)

    async def stop_all(self) -> None:
        """Stop all running modules."""
        for name, mod in self._modules.items():
            if self._running.get(name):
                stop_fn = getattr(mod, "stop", None)
                if stop_fn:
                    try:
                        await stop_fn()
                    except Exception as e:
                        log.error("Error stopping module '%s': %s", name, e)
                self._running[name] = False
        log.info("All modules stopped")

    async def stop_module(self, name: str) -> bool:
        mod = self._modules.get(name)
        if not mod:
            return False
        stop_fn = getattr(mod, "stop", None)
        if stop_fn:
            await stop_fn()
        self._running[name] = False
        return True

    async def start_module(self, name: str, agent: Any, config: Any) -> bool:
        mod = self._modules.get(name)
        if not mod:
            return False
        start_fn = getattr(mod, "start", None)
        if start_fn:
            await start_fn(agent, config)
        self._running[name] = True
        return True

    def list_modules(self) -> list[dict[str, Any]]:
        result = []
        for name, mod in self._modules.items():
            result.append({
                "name": name,
                "type": getattr(mod, "MODULE_TYPE", "unknown"),
                "description": getattr(mod, "MODULE_DESCRIPTION", ""),
                "running": self._running.get(name, False),
            })
        return result
