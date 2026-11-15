"""deploy/castling-worker/install.sh: with --binary, one file in place,
joined to a real hub with a one-time code, and a user unit that restarts it.
Without --binary it takes the Python worker's path (a download, a venv): only
the choice is checked here, since that path needs the network. systemctl and
loginctl are stubs that record what the script asks of them."""
from __future__ import annotations

import asyncio
import os
import secrets
import stat
from pathlib import Path

import pytest

pytest.importorskip("telesthete")
from rook.core2.hub.runtime import Config, Hub  # noqa: E402

from test_core2_minimal_worker import door  # noqa: E402
from test_core2_rust_worker import binary  # noqa: E402,F401  (the fixture)

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "castling-worker" / "install.sh"


@pytest.mark.skipif(os.geteuid() == 0, reason="tests the user install")
def test_install_puts_one_file_in_place_joins_and_sets_up_the_unit(tmp_path, binary):  # noqa: F811
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("systemctl", "loginctl"):
        (stubs / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n'
                                  '[ "$2" = is-active ] && echo active\nexit 0\n')
        (stubs / tool).chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PATH": f"{stubs}:{os.environ['PATH']}"}
    root = tmp_path / "home" / ".rook05"

    async def run(*args):
        p = await asyncio.create_subprocess_exec("bash", str(SCRIPT), *args, env=env,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await p.communicate()
        return p.returncode, out.decode()

    async def go():
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": secrets.token_hex(24), "relay": "127.0.0.1:9"}]}))
        await hub.start()
        try:
            token = (hub.data / "owner.token").read_text().strip()
            code = (await door(hub, token, "/enroll/code", {}))["code"]
            url = f"http://127.0.0.1:{hub.doors[0].port}"
            rc, out = await run("--binary", str(binary), "--hub", url, "--code", code, "--name", "box")
            assert rc == 0, out
            assert "Enrolled box" in out and "done: box, the one-file worker, unit rook05-worker (active)" in out, out
            installed = root / "rook-worker"
            assert installed.read_bytes() == binary.read_bytes() and installed.stat().st_mode & stat.S_IXUSR
            assert (root / "anchor.json").is_file() and "anchor: anchor.json" in (root / "worker.yaml").read_text()
            unit = (tmp_path / "home" / ".config/systemd/user/rook05-worker.service").read_text()
            assert f"ExecStart={installed} --config {root / 'worker.yaml'}" in unit and "Restart=always" in unit
            calls = log.read_text().splitlines()
            for want in ("systemctl --user daemon-reload", "systemctl --user enable rook05-worker",
                         "systemctl --user restart rook05-worker"):
                assert want in calls, calls
            assert not any("rook05-worker" in c and "stop" in c for c in calls)

            log.write_text("")
            rc, out = await run("--upgrade", "--binary", str(binary))     # a new file, the same identity
            assert rc == 0 and "enroll" not in out, out
            assert "systemctl --user stop rook05-worker" in log.read_text()
            assert "anchor: anchor.json" in (root / "worker.yaml").read_text()

            # Without --binary: the Python worker's path, which downloads the code first.
            (stubs / "curl").write_text("#!/bin/sh\necho offline >&2\nexit 22\n")
            (stubs / "curl").chmod(0o755)
            other = tmp_path / "py"
            rc, out = await run("--hub", url, "--root", str(other), "--name", "pybox")
            assert rc != 0 and "== code: Bake-Ware/rook@castling" in out, out
            assert not (other / "rook-worker").exists()
        finally:
            await hub.stop()
    asyncio.run(asyncio.wait_for(go(), 120))
