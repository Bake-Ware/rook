"""First run (Bake 13 Nov; T4, X2, X4, P1): the hub's start command run as a real process,
the vault key file it makes outside the data directory, and the setup page that makes the
first user, driven through real HTTP on localhost, as the browser drives it."""

import asyncio
import os
import pathlib
import stat
import sys
import threading

import pytest

from rook_hub import Hub
from rook_hub.errors import HubError
from test_site import ask, browser

ROOT = pathlib.Path(__file__).parent.parent


def start_command(data, key):
    """The command line and environment a person uses from a clone (README.md)."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("protocol/python", "hub")))
    return [sys.executable, "-m", "rook_hub", "--data", str(data), "--vault-key", str(key), "--port", "0"], env


class Started:
    """`python -m rook_hub` as a person runs it from a clone, on a free port."""

    def __init__(self, data, key, umask=-1):
        self.args, self.env = start_command(data, key)
        self.umask = umask          # -1 keeps the test's own

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(*self.args, cwd=ROOT, env=self.env, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE, umask=self.umask)
        self.lines = []
        while not self.lines or not self.lines[-1].startswith("  vault:"):
            line = await asyncio.wait_for(self.process.stdout.readline(), 20)
            assert line, (await self.process.stderr.read()).decode()
            self.lines.append(line.decode().rstrip("\n"))
        self.base = self.lines[0].removeprefix("Rook hub at ").rstrip("/")
        return self

    async def __aexit__(self, *exc):
        self.process.terminate()
        assert await asyncio.wait_for(self.process.wait(), 20) == 0    # stops cleanly on SIGTERM


def test_first_run(tmp_path):
    asyncio.run(first_run(tmp_path))


async def first_run(tmp_path):
    data, key = tmp_path / "data", tmp_path / "keys" / "vault.key"

    # 1. A fresh data directory: the key file is made, owner-only, outside it; the hub needs setup.
    async with Started(data, key) as hub, browser() as client:
        assert key.exists() and stat.S_IMODE(key.stat().st_mode) == 0o600
        assert not key.resolve().is_relative_to(data.resolve()) and len(bytes.fromhex(key.read_text())) == 32
        made = key.read_text()
        assert "  vault:     open" in hub.lines and any("no user yet" in line for line in hub.lines)
        assert await ask(client, "GET", hub.base + "/api/setup") == (200, {"needed": True, "vault_key_file": str(key)})
        status, reply = await ask(client, "GET", hub.base + "/api/session")    # the shell shows the login page's place
        assert status == 401 and reply["code"] == "no_session"
        async with client.post(hub.base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) as response:
            assert response.status == 401 and (await response.json())["error"]["data"]["code"] == "token_missing"
        async with client.get(hub.base + "/") as response:      # the site is served (P1)
            assert response.status == 200 and "shell.js" in await response.text()

        # 2. Setup makes the first user and signs them in (T4).
        status, reply = await ask(client, "POST", hub.base + "/api/setup", {"name": "Alice", "password": "correct horse"})
        assert status == 200 and reply["user"]["name"] == "Alice"
        status, reply = await ask(client, "GET", hub.base + "/api/session")
        assert status == 200 and reply["user"]["name"] == "Alice"
        # The setup page is over for good: it says so, and a second setup is refused.
        assert await ask(client, "GET", hub.base + "/api/setup") == (200, {"needed": False, "vault_key_file": None})
        async with browser() as other:
            status, reply = await ask(other, "POST", hub.base + "/api/setup", {"name": "Mallory", "password": "x"})
            assert status == 403 and reply["code"] == "setup_done" and reply["next"]
            assert (await ask(other, "GET", hub.base + "/api/session"))[0] == 401

        # 3. A secret made now (X2) ...
        status, reply = await ask(client, "POST", hub.base + "/api/secrets",
                                  {"scope": "hub", "name": "deploy-key", "value": "s3cret-value-123"})
        assert status == 200, reply
        secret = reply["created"]["moniker"]

    # ... resolves after a restart, with the key read from the same file; login works (T4).
    async with Started(data, key) as hub, browser() as client:
        assert "  vault:     open" in hub.lines and key.read_text() == made
        assert not any("no user yet" in line for line in hub.lines)
        assert (await ask(client, "GET", hub.base + "/api/setup"))[1]["needed"] is False
        status, _ = await ask(client, "POST", hub.base + "/api/login", {"name": "Alice", "password": "correct horse"})
        assert status == 200
        assert await ask(client, "POST", f"{hub.base}/api/secrets/{secret}/reveal") == (200, {"value": "s3cret-value-123"})
    reopened = Hub(data, vault_key_file=key)
    try:
        assert reopened.vault.substitute({"key": "secret:deploy-key"}, "hub") == {"key": "s3cret-value-123"}
    finally:
        reopened.close()

    # 4. With the key file gone the hub starts with the vault locked, and makes no new key.
    key.unlink()
    async with Started(data, key) as hub, browser() as client:
        assert any(line.startswith("  vault:     locked") for line in hub.lines)
        assert not key.exists()
        await ask(client, "POST", hub.base + "/api/login", {"name": "Alice", "password": "correct horse"})
        status, reply = await ask(client, "GET", hub.base + "/api/secrets")
        assert status == 400 and reply["code"] == "vault_locked"
    assert not key.exists()

    # 5. A wrong key in the file: the vault says so, and the file is left as it was.
    key.write_text(os.urandom(32).hex() + "\n")
    wrong = key.read_text()
    async with Started(data, key) as hub:
        assert any(line.startswith("  vault:     locked: the key file does not hold") for line in hub.lines)
    assert key.read_text() == wrong

    # 6. A key path inside the data directory is refused with a plain message; nothing is made there.
    inside = data / "vault.key"
    args, env = start_command(data, inside)
    process = await asyncio.create_subprocess_exec(*args, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode != 0 and not out
    assert f"The vault key file {inside} is inside the data directory" in err.decode() and "Traceback" not in err.decode()
    assert not inside.exists()


def test_key_file_inside_data_directory(tmp_path):
    """Refused before anything is made, also through a symlink or as the data directory itself."""
    (tmp_path / "link").symlink_to(tmp_path / "data", target_is_directory=True)
    for path in (tmp_path / "data" / "vault.key", tmp_path / "link" / "keys" / "vault.key", tmp_path / "data"):
        with pytest.raises(HubError) as refused:
            Hub(tmp_path / "data", vault_key_file=path)
        assert refused.value.message["code"] == "vault_key_inside_data"
    assert not (tmp_path / "data").exists()


def test_key_file_that_holds_no_key(tmp_path):
    """A file that is not a key is refused with a plain message, and never overwritten."""
    key = tmp_path / "vault.key"
    key.write_text("my notes\n")
    with pytest.raises(HubError) as refused:
        Hub(tmp_path / "data", vault_key_file=key)
    assert refused.value.message["code"] == "vault_key_file_bad"
    assert key.read_text() == "my notes\n"


def test_two_setups_at_once_make_one_user(tmp_path):
    asyncio.run(two_setups(tmp_path))


async def two_setups(tmp_path):
    hub = Hub(tmp_path / "hub", vault_key_file=tmp_path / "vault.key")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    try:
        async with browser() as one, browser() as two:
            replies = await asyncio.gather(ask(one, "POST", base + "/api/setup", {"name": "Alice", "password": "a"}),
                                           ask(two, "POST", base + "/api/setup", {"name": "Bob", "password": "b"}))
        assert sorted(status for status, _ in replies) == [200, 403]
        assert len(hub.auth.list_users()) == 1
    finally:
        await hub.stop_serving()
        hub.close()

    # The same race in threads, straight at the hub: one user is made, the other is refused.
    hub = Hub(tmp_path / "threads", vault_key_file=tmp_path / "threads.key")
    outcomes, start = [], threading.Barrier(8)

    def setup(n):
        start.wait()
        try:
            outcomes.append(hub.auth.create_user(f"user{n}", "pw", first=True)["name"])
        except HubError as e:
            outcomes.append(e.message["code"])
    threads = [threading.Thread(target=setup, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("setup_done") == 7 and len(hub.auth.list_users()) == 1
    hub.close()


def test_no_setup_before_bad_requests(tmp_path):
    """A setup refused for a missing name or password makes no user: setup is still offered."""
    asyncio.run(no_user_yet(tmp_path))


async def no_user_yet(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    try:
        async with browser() as client:
            for body in ({"name": "Carol"}, {"password": "c"}, {"name": " ", "password": "c"}):
                status, reply = await ask(client, "POST", base + "/api/setup", body)
                assert status == 400 and reply["code"] == "bad_message", body
            # A hub started without a key file has none to point at.
            assert await ask(client, "GET", base + "/api/setup") == (200, {"needed": True, "vault_key_file": None})
    finally:
        await hub.stop_serving()
        hub.close()
