"""Every rook.remote module imports in the dashboard's environment.

The dashboard runs aiohttp; Starlette and the mcp SDK belong to the MCP
process. The hub deploy preflight imports every rook.remote submodule with
the dashboard's interpreter, so a module-level Starlette import there blocks
a deploy on hubs whose two services use separate virtualenvs.
"""
import subprocess
import sys
import textwrap


def test_rook_remote_imports_without_starlette_or_mcp():
    code = textwrap.dedent("""
        import importlib, pkgutil, sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split('.')[0] in ('starlette', 'mcp', 'uvicorn', 'sse_starlette'):
                    raise ModuleNotFoundError(name)
        sys.meta_path.insert(0, Block())
        import rook.remote
        bad = []
        for m in pkgutil.walk_packages(rook.remote.__path__, 'rook.remote.'):
            if m.name.rsplit('.', 1)[-1] == '__main__':
                continue  # entry points run on import; the deploy preflight skips them too
            try:
                importlib.import_module(m.name)
            except ModuleNotFoundError as e:
                if e.name.split('.')[0] in ('starlette', 'mcp', 'uvicorn', 'sse_starlette'):
                    bad.append(f'{m.name}: {e.name}')
        print('\\n'.join(bad))
        sys.exit(1 if bad else 0)
    """)
    r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
