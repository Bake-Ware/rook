"""Open until claimed (brief HA3; 0.5's `hub init --no-password`): a hub started with --no-password
makes its first owner with no password, and while nobody has set a password the site lets any
visitor in as that owner. Setting a password ends that for good: a new browser then gets the login,
while the browser that set it stays signed in. Driven through real HTTP on localhost."""

import asyncio
import getpass

from rook_hub import Hub
from rook_hub.auth import UNCLAIMED
from rook_hub.errors import HubError
from test_first_run import Started
from test_site import ask, browser, login


def test_open_until_claimed(tmp_path):
    asyncio.run(open_until_claimed(tmp_path))


async def open_until_claimed(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    try:
        owner = hub.auth.open_until_claimed("bake")
        assert owner["name"] == "bake" and (hub.state_dir / UNCLAIMED).exists()
        assert hub.auth.open_until_claimed("someone-else") is None          # only a hub with no user is opened
        assert [u["name"] for u in hub.auth.list_users()] == ["bake"]
        try:
            hub.auth.login("bake", "")                                     # no password: none signs in by name
            raise AssertionError("signed in with no password")
        except HubError as e:
            assert e.message["code"] == "login_failed"

        async with browser() as first, browser() as second, browser() as later:
            # 1. Any visitor is the owner, with a session of their own, and needs no login.
            for client in (first, second):
                status, reply = await ask(client, "GET", base + "/api/session")
                assert status == 200 and reply["user"]["name"] == "bake", reply
                assert reply["password"] is False and reply["unclaimed"] is True
                assert (await ask(client, "GET", base + "/api/workers"))[0] == 200    # the session works
            assert await ask(first, "GET", base + "/api/setup") == (200, {"needed": False, "vault_key_file": None})

            # 2. Setting a password claims it: the browser that set it stays in, a new one gets the login.
            assert await ask(first, "POST", base + "/api/password", {"password": "correct horse"}) == \
                (200, {"changed": True})
            assert not (hub.state_dir / UNCLAIMED).exists()
            status, reply = await ask(first, "GET", base + "/api/session")
            assert status == 200 and reply["password"] is True and reply["unclaimed"] is False
            status, reply = await ask(later, "GET", base + "/api/session")
            assert status == 401 and reply["code"] == "no_session"
            assert (await login(later, base, "bake", "correct horse"))[0] == 200

            # 3. For good: a later password change, or a new start, never opens it again.
            hub.auth.change_password(owner["guid"], "battery staple", user=owner["guid"])
            assert hub.auth.unclaimed_owner() is None
    finally:
        await hub.stop_serving()
        hub.close()


def test_a_hub_with_a_password_is_never_open(tmp_path):
    hub = Hub(tmp_path / "hub")
    try:
        hub.auth.create_user("Alice", "correct horse", first=True)
        assert hub.auth.open_until_claimed("bake") is None
        assert not (hub.state_dir / UNCLAIMED).exists() and hub.auth.unclaimed_owner() is None
    finally:
        hub.close()


def test_no_password_start(tmp_path):
    asyncio.run(no_password_start(tmp_path))


async def no_password_start(tmp_path):
    data, key = tmp_path / "data", tmp_path / "keys" / "vault.key"
    # The start command, as the installer runs it: the owner defaults to this computer's login.
    async with Started(data, key, extra=["--no-password"]) as hub, browser() as client:
        assert f"  It needs no sign-in until you set a password; you'll sign in as {getpass.getuser()}." in hub.lines
        status, reply = await ask(client, "GET", hub.base + "/api/session")
        assert status == 200 and reply["user"]["name"] == getpass.getuser() and reply["unclaimed"] is True
    # Started again with the same flag: the hub has a user now, so it is left as it is (still open:
    # nobody set a password); --owner names someone else and makes nobody.
    async with Started(data, key, extra=["--no-password", "--owner", "someone"]) as hub, browser() as client:
        assert "  --no-password: this hub has a user already, so it is left as it is." in hub.lines
        status, reply = await ask(client, "GET", hub.base + "/api/session")
        assert status == 200 and reply["user"]["name"] == getpass.getuser()
