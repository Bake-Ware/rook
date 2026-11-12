"""Password login, the human front door (T4, L1)."""

import json

import pytest

from helpers import Clock
from rook_hub import Hub, HubError

PASSWORDS = ["correct horse battery", "new staple 42", "pässwörd ünïcode"]


def journal(hub):
    return [dict(row) for row in hub._db.execute("SELECT * FROM journal ORDER BY id")]


def logins(hub):
    return [(e["user"], e["token"], json.loads(e["result"])) for e in journal(hub)
            if e["kind"] == "auth" and ("login" in e["result"] or "password_changed" in e["result"])]


def failure(call):
    with pytest.raises(HubError) as e:
        call()
    assert e.value.message["next"]
    return e.value.message


def test_password_login(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    try:
        # 1. A user is created with a password. The database holds no copy of
        #    it, only a salted hash; equal passwords hash differently.
        alice = hub.auth.create_user("Alice", "correct horse battery")
        bob = hub.auth.create_user("Bob", "correct horse battery")
        assert "password" not in alice and set(alice) == {"guid", "moniker", "name", "created_at", "revoked_at"}
        stored = {row[0]: row[1] for row in hub._db.execute("SELECT guid, password FROM users")}
        assert stored[alice["guid"]].startswith("scrypt$") and stored[bob["guid"]].startswith("scrypt$")
        assert stored[alice["guid"]] != stored[bob["guid"]]
        assert "correct horse battery" not in "\n".join(hub._db.iterdump())

        # 2. The right name and password answer with that user, journaled
        #    under that user with no password in the entry.
        assert hub.auth.login("Alice", "correct horse battery") == alice
        assert logins(hub) == [(alice["guid"], None, {"login": "ok"})]

        # 3. A wrong password and a name nobody has get the same error, which
        #    says what to try next. Only the first is journaled.
        wrong = failure(lambda: hub.auth.login("Alice", "not it"))
        nobody = failure(lambda: hub.auth.login("Carol", "correct horse battery"))
        assert wrong == nobody and wrong["code"] == "login_failed"
        assert logins(hub)[1:] == [(alice["guid"], None, {"login": "wrong_password"})]

        # 4. The password changes: the old one stops working, the new one
        #    works, and the change is journaled.
        hub.auth.change_password(alice["guid"], "new staple 42", user=alice["guid"])
        assert failure(lambda: hub.auth.login("Alice", "correct horse battery"))["code"] == "login_failed"
        assert hub.auth.login("Alice", "new staple 42")["guid"] == alice["guid"]
        assert (alice["guid"], None, {"password_changed": alice["guid"]}) in logins(hub)

        # 5. A revoked user cannot log in, even with the right password.
        hub.auth.revoke_user(alice["guid"], user=bob["guid"])
        assert failure(lambda: hub.auth.login("Alice", "new staple 42"))["code"] == "user_revoked"
        assert logins(hub)[-1] == (alice["guid"], None, {"login": "user_revoked"})

        # 6. A name already taken cannot be taken again (names are compared
        #    case-insensitively, after Unicode normalisation).
        for name in ("Alice", "alice", "ALICE"):
            assert failure(lambda: hub.auth.create_user(name, "x"))["code"] == "name_taken"
        assert len(hub.auth.list_users()) == 2

        # No password reached the database, the journal or a return value.
        dump = "\n".join(hub._db.iterdump()) + json.dumps(hub.auth.list_users())
        assert not any(p in dump for p in PASSWORDS)
    finally:
        hub.close()


def test_names_are_compared_without_case(tmp_path):
    hub = Hub(tmp_path / "hub")
    try:
        alice = hub.auth.create_user("Alice", "pässwörd ünïcode")
        assert hub.auth.login("alice", "pässwörd ünïcode")["guid"] == alice["guid"]
        assert hub.auth.login("ALICE", "pässwörd ünïcode")["name"] == "Alice"
        assert failure(lambda: hub.auth.login("Alice", "PÄSSWÖRD ÜNÏCODE"))["code"] == "login_failed"
    finally:
        hub.close()


def test_changing_a_password_needs_an_actor_and_a_real_user(tmp_path):
    hub = Hub(tmp_path / "hub")
    try:
        alice = hub.auth.create_user("Alice", "one")
        agent = hub.auth.mint("agent", user=alice["guid"])
        hub.auth.change_password(alice["moniker"], "two", token=agent)   # an agent of hers may change it
        assert hub.auth.login("Alice", "two")["guid"] == alice["guid"]
        assert failure(lambda: hub.auth.change_password("nobody", "three", user=alice["guid"]))["code"] == "unknown_user"
        assert failure(lambda: hub.auth.change_password(alice["guid"], "three"))["code"] == "no_actor"
        assert hub.auth.login("Alice", "two")["guid"] == alice["guid"]
    finally:
        hub.close()
