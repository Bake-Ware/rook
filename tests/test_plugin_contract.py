"""The plugin contract across every built-in plugin: lazy optional imports,
declared risk tiers, docstrings, timeouts (pluginhost audit)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A sitecustomize that makes aiohttp and PyNaCl unimportable, as on a light
# worker or a hub installed without the optional extras.
_BLOCK = textwrap.dedent("""
    import sys, importlib.abc
    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in ("aiohttp", "nacl"):
                raise ImportError("blocked for the test: " + name)
    sys.meta_path.insert(0, _Block())
""")


def _run_blocked(tmp_path, code: str) -> subprocess.CompletedProcess:
    (tmp_path / "sitecustomize.py").write_text(_BLOCK, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(ROOT)]))
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


def test_plugins_and_settings_schema_load_without_optional_deps(tmp_path):
    """claude_history/codex_history (aiohttp via codex_input), enrollment
    (PyNaCl via device_key) and knowledge (aiohttp via search) used to fail to
    import, and the settings schema silently dropped their keys."""
    code = textwrap.dedent("""
        import importlib, pkgutil
        for pkg in ("rook.worker.plugins", "rook.hub.plugins"):
            mod = importlib.import_module(pkg)
            for info in pkgutil.iter_modules(mod.__path__):
                importlib.import_module(pkg + "." + info.name)
        try:
            import aiohttp
        except ImportError:
            pass
        else:
            raise SystemExit("blocker did not work")
        from rook.hub.settings_schema import Schema
        s = Schema()
        assert s.import_errors == [], s.import_errors
        assert any(k.startswith("knowledge.") for k in s.entries), "knowledge settings missing"
        print("ok")
    """)
    res = _run_blocked(tmp_path, code)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr


def test_schema_records_import_failures_and_generator_refuses(tmp_path, monkeypatch, caplog):
    import importlib
    import importlib.util
    import logging
    from rook.hub import settings_schema
    real = importlib.import_module

    def broken(name, *a, **k):
        if name == "rook.worker.plugins.info":
            raise ImportError("no such backend")
        return real(name, *a, **k)

    monkeypatch.setattr(settings_schema.importlib, "import_module", broken)
    with caplog.at_level(logging.WARNING, logger="rook.hub.settings_schema"):
        schema = settings_schema.Schema()
    assert schema.import_errors == ["rook.worker.plugins.info: ImportError: no such backend"]
    assert any("rook.worker.plugins.info" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)

    spec = importlib.util.spec_from_file_location(
        "gen_settings_reference_t", ROOT / "tools" / "gen_settings_reference.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    monkeypatch.setattr(sys, "argv", ["gen", "--check"])
    assert gen.main() == 2
