"""Open until claimed (brief HA3; 0.5's `hub init --no-password`): a hub started with --no-password
makes its first owner with no password, and while nobody has set a password the site lets any
visitor in as that owner. Setting a password ends that for good: a new browser then gets the login,
while the browser that set it stays signed in. Driven through real HTTP on localhost."""

import asyncio
import getpass

from nacl.signing import SigningKey

from helpers import link_fields
from rook_hub import Hub, journal
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


def system_worker(hub, name):
    """A worker joined with a join token the system owner holds, as the installer joins this machine's
    (brief HA4: `python -m rook_hub join-token` before the hub's first start, when no person exists)."""
    signing_key = SigningKey.generate()
    key = signing_key.verify_key.encode().hex()
    hub.knock({"type": "knock", "protocol": 1, "name": name, "key": key, "macs": [], "caps": [],
               **link_fields(signing_key)})
    return hub.approve(key, user=hub.auth.system)["guid"]


def owner_changes(hub):
    return [e for e in journal.read(hub, kind="auth", limit=100) if "owner_changed" in (e.get("result") or {})]


def test_claiming_hands_the_systems_workers_to_the_owner(tmp_path):
    """The steward's note on HA3: when the first person claims the hub, every worker the system owner
    owns becomes theirs, in the same step (on 0.5 the owner owned the installer's machine)."""
    hub = Hub(tmp_path / "hub")
    try:
        owner = hub.auth.open_until_claimed("bake")
        desk = system_worker(hub, "desk")
        assert hub.get_worker(desk)["owner"] == hub.auth.system      # open, not yet claimed: still the system's
        hub.auth.change_password(owner["guid"], "correct horse", user=owner["guid"])      # claimed
        assert hub.get_worker(desk)["owner"] == owner["guid"]
        [change] = owner_changes(hub)
        assert change["result"]["from"] == hub.auth.system and change["result"]["to"] == owner["guid"]
        # Once: a worker the system owns later, and a later password, move nothing.
        shop = system_worker(hub, "shop")
        hub.auth.change_password(owner["guid"], "battery staple", user=owner["guid"])
        assert hub.get_worker(shop)["owner"] == hub.auth.system and len(owner_changes(hub)) == 1
    finally:
        hub.close()


def test_the_setup_pages_first_user_claims_the_systems_workers(tmp_path):
    hub = Hub(tmp_path / "hub")
    try:
        desk, nas = system_worker(hub, "desk"), system_worker(hub, "nas")
        alice = hub.auth.create_user("Alice", "correct horse", first=True)
        assert {hub.get_worker(w)["owner"] for w in (desk, nas)} == {alice["guid"]}
        assert len(owner_changes(hub)) == 2
        later = system_worker(hub, "shop")
        hub.auth.create_user("Bob", "battery staple")                 # a second person claims nothing
        assert hub.get_worker(later)["owner"] == hub.auth.system and len(owner_changes(hub)) == 2
    finally:
        hub.close()
