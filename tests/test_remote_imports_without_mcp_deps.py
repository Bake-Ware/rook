"""Every rook.remote module imports in the dashboard's environment.

The dashboard runs aiohttp; Starlette and the mcp SDK belong to the MCP
process. The hub deploy preflight imports every rook.remote submodule with
the dashboard's interpreter, so a module-level Starlette import there blocks
a deploy on hubs whose two services use separate virtualenvs.
"""
import subprocess
import sys
import textwrap

# What only the MCP process needs. pydantic(-settings) comes with the mcp SDK.
# Not pydantic_core: yarl (aiohttp) probes it with find_spec at import time
# and expects "absent", not an error; it never imports it unless used.
BLOCKED = ('starlette', 'mcp', 'uvicorn', 'sse_starlette', 'pydantic',
           'pydantic_settings', 'httpx_sse')


def test_rook_remote_imports_without_starlette_or_mcp():
    code = textwrap.dedent("""
        import importlib, pkgutil, sys
        BLOCKED = %r
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split('.')[0] in BLOCKED:
                    raise ModuleNotFoundError(name, name=name)
        sys.meta_path.insert(0, Block())
        import rook.remote
        bad = []
        for m in pkgutil.walk_packages(rook.remote.__path__, 'rook.remote.'):
            if m.name.rsplit('.', 1)[-1] == '__main__':
                continue  # entry points run on import; the deploy preflight skips them too
            try:
                importlib.import_module(m.name)
            except ModuleNotFoundError as e:
                if (e.name or '').split('.')[0] in BLOCKED:
                    bad.append(f'{m.name}: {e.name}')
        # The in-memory proxy path must not pull them in either.
        import rook.remote.upstream, rook.hub.asgi_bridge
        leaked = sorted(n for n in sys.modules if n.split('.')[0] in BLOCKED)
        bad += [f'loaded: {n}' for n in leaked]
        print('\\n'.join(bad))
        sys.exit(1 if bad else 0)
    """) % (BLOCKED,)
    r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
