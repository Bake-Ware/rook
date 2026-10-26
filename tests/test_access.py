"""Access v2: hub roles, fine-grained grants and their enforcement
(rook/hub/access.py, Authorizer._grants, the users.* caps, knowledge areas)."""
import hashlib
import sqlite3

import pytest

from rook.hub import access as A
from rook.hub.authz import Authorizer, build_authorizer, current_principal, principal_for_token, \
    principal_for_user, target_from_entry
from rook.hub.node import HubNode
from rook.hub.policy import PolicyStore


def member(uid="u_kid"):
    return principal_for_user({"id": uid, "name": uid}, False)


def owner(uid="u_bake"):
    return principal_for_user({"id": uid, "name": uid}, True)


def store(tmp_path):
    return A.AccessStore.for_dir(str(tmp_path))


def accounts_db(path):
    """A minimal enrollment.db with the account tables seed() reads."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, password TEXT, name TEXT NOT NULL,
                            email TEXT NOT NULL DEFAULT '', admin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE memberships (band_id TEXT, user_id TEXT, role TEXT);
        INSERT INTO users VALUES ('u_op','operator','x','Operator','',1);
        INSERT INTO users VALUES ('u_bake','bake','x','Bake','b@example.com',0);
        INSERT INTO users VALUES ('u_kid','kid','x','Kid','',0);
        INSERT INTO users VALUES ('u_new','new','x','New','',0);
        INSERT INTO memberships VALUES ('band1','u_bake','owner');
        INSERT INTO memberships VALUES ('band1','u_kid','member');
    """)
    con.commit()
    con.close()


class Accounts:
    def __init__(self, path):
        self.path = path

    def db(self):
        from rook.core.db import transaction
        return transaction(self.path)


# -- the store -------------------------------------------------------------------

def test_seed_carries_access_over_once_and_new_people_get_nothing(tmp_path):
    accounts_db(tmp_path / "enrollment.db")
    s = store(tmp_path)
    assert s.seed(Accounts(str(tmp_path / "enrollment.db"))) is True
    assert s.seed(Accounts(str(tmp_path / "enrollment.db"))) is False
    assert s.role("u_op") == "owner" and s.role("u_bake") == "owner"
    assert s.role("u_kid") == "member" and s.role("u_new") is None
    [g] = s.grants("u_kid")
    assert (g["kind"], g["ref"], g["caps"], g["tier"]) == ("band", "band1", ["*"], "exec")
    assert s.grants("u_new") == [] and s.grants("u_bake") == []
    assert {h["actor"] for h in s.history()} == {"system:access-migration"}


def test_roles_keep_the_last_owner_and_every_change_needs_words(tmp_path):
    s = store(tmp_path)
    with pytest.raises(A.AccessError, match="words"):
        s.set_role("u_a", "owner", actor="human:u_a", words="")
    s.set_role("u_a", "owner", actor="human:u_a", words="I set up the hub")
    with pytest.raises(A.AccessError, match="last owner"):
        s.set_role("u_a", "member", actor="human:u_a", words="step down")
    s.set_role("u_b", "owner", actor="human:u_a", words="Bake: make my wife an owner too")
    assert s.set_role("u_a", "member", actor="human:u_b", words="ok")["was"] == "owner"
    with pytest.raises(A.AccessError, match="role"):
        s.set_role("u_a", "admin", actor="x", words="y")
    h = s.history("u_b")[0]
    assert h["action"] == "role" and h["words"] == "Bake: make my wife an owner too"


def test_grant_validation_and_revoke_history(tmp_path):
    s = store(tmp_path)
    for kw, msg in [({"kind": "galaxy"}, "kind"), ({"kind": "worker"}, "ref"),
                    ({"kind": "any", "tier": "root"}, "tier"),
                    ({"kind": "knowledge", "ref": "a b"}, "folder"),
                    ({"kind": "any", "caps": ["shell exec"]}, "caps")]:
        with pytest.raises(A.AccessError, match=msg):
            s.grant("u_kid", actor="human:u_bake", words="w", **kw)
    g = s.grant("u_kid", "worker", "kids-pc", caps="shell.*, file.read", tier="x",
                actor="human:u_bake", words="his desktop is his")
    assert g["caps"] == ["file.read", "shell.*"] and g["tier"] == "exec"
    assert s.role("u_kid") == "member"            # a grant makes someone a member
    with pytest.raises(A.AccessError, match="words"):
        s.revoke(g["id"], actor="human:u_bake", words=" ")
    r = s.revoke(g["id"], actor="human:u_bake", words="grounded")
    assert r["revoke_words"] == "grounded" and s.grants("u_kid") == []
    assert len(s.grants("u_kid", active=False)) == 1
    assert [h["action"] for h in s.history("u_kid")] == ["revoke", "grant", "role"]


def test_evaluate_matches_kind_caps_and_tier(tmp_path):
    s = store(tmp_path)
    nas = target_from_entry("w_nas", {"name": "NAS"})
    pc = target_from_entry("w_pc", {"name": "kids-pc"})
    hub = target_from_entry("w_hub", {"name": "rook"}, local=True)
    assert s.evaluate("u_kid", "shell.exec", "exec", pc)[0] is None          # nothing yet
    assert s.evaluate("u_kid", "users.me", "read", hub)[0]["id"] == "self"   # always about yourself
    s.grant("u_kid", "worker", "KIDS-PC", caps=["shell.*"], actor="o", words="w")
    assert s.evaluate("u_kid", "shell.exec", "exec", pc)[0] is not None
    assert s.evaluate("u_kid", "file.read", "read", pc)[0] is None             # not in caps
    assert s.evaluate("u_kid", "shell.exec", "exec", nas)[0] is None         # other machine
    s.grant("u_kid", "worker", "w_nas", tier="read", actor="o", words="w")
    assert s.evaluate("u_kid", "file.read", "read", nas)[0] is not None
    g, why = s.evaluate("u_kid", "file.write", "write", nas)
    assert g is None and "above what your grant allows" in why
    assert s.evaluate("u_kid", "knowledge.read", "read", hub)[0] is None     # hub needs its own
    s.grant("u_kid", "hub", caps=["chat.*"], tier="write", actor="o", words="w")
    assert s.evaluate("u_kid", "chat.send", "write", hub)[0] is not None
    assert s.evaluate("u_kid", "chat.send", "write", pc)[0] is None          # hub grant: hub only


def test_band_grants_follow_the_band_key(tmp_path):
    s = store(tmp_path)
    s.grant("u_kid", "band", "band1", tier="read", actor="o", words="w")
    t = target_from_entry("w_tv", {"name": "tv"})
    assert s.evaluate("u_kid", "file.read", "read", t, {"h1"})[0] is None     # band unknown yet
    s.sync_bands([{"id": "band1", "psk_hash": "h1", "name": "home"}])
    assert s.evaluate("u_kid", "file.read", "read", t, {"h1"})[0] is not None
    assert s.evaluate("u_kid", "file.read", "read", t, {"h2"})[0] is None     # seen on another band
    assert s.evaluate("u_kid", "file.read", "read", target_from_entry(None, None), {"h1"})[0] is None
    s.sync_bands([{"id": "band1", "psk_hash": "h9", "name": "home"}])       # key changed
    assert s.evaluate("u_kid", "file.read", "read", t, {"h9"})[0] is not None


def test_knowledge_areas(tmp_path):
    s = store(tmp_path)
    assert s.knowledge_areas("u_kid") == set()
    s.grant("u_kid", "knowledge", "homework", tier="write", actor="o", words="w")
    s.grant("u_kid", "knowledge", "recipes", tier="read", actor="o", words="w")
    assert s.knowledge_areas("u_kid") == {"homework", "recipes"}
    assert s.knowledge_areas("u_kid", write=True) == {"homework"}
    s.grant("u_kid", "hub", caps=["knowledge.*"], tier="read", actor="o", words="w")
    assert s.knowledge_areas("u_kid") is None                 # all of it
    assert s.knowledge_areas("u_kid", write=True) == {"homework"}


# -- enforcement in the authorizer -----------------------------------------------

class Band:
    def __init__(self, psk, workers):
        self.psk_hash = hashlib.sha256(psk.encode()).hexdigest()
        self.workers = workers


class Multi:
    def __init__(self, *clients):
        self._clients = list(clients)


def authorizer(tmp_path):
    az = build_authorizer(str(tmp_path), signer=False)
    az.signer = None
    return az


def test_members_reach_only_what_grants_name_even_in_audit_mode(tmp_path):
    az = authorizer(tmp_path)
    assert az.store.current().mode == "audit"
    entry = {"name": "kids-pc"}
    d = az.check("shell.exec", "w_pc", entry, principal=member())
    assert d.denied and d.rule == "access:no-grant" and "no access yet" in d.reason
    az.access.grant("u_kid", "worker", "kids-pc", actor="o", words="his desktop")
    d = az.check("shell.exec", "w_pc", entry, principal=member())
    assert not d.denied and any(t.startswith("grant:g_") for t in d.tags)
    assert az.check("shell.exec", "w_nas", {"name": "nas"}, principal=member()).denied
    # Owners, the shared dashboard password, tokens and hub code are not limited by grants.
    assert not az.check("shell.exec", "w_nas", {"name": "nas"}, principal=owner()).denied
    assert not az.check("file.read", "w_nas", {"name": "nas"},
                        principal=principal_for_user(None, True)).denied
    assert az.check("file.read", "w_nas", {"name": "nas"}, principal=member("u_new")).denied
    att = type("Att", (), {"verified": True, "kind": "agent", "agent_id": "a1", "key_id": None,
                           "label": "claude", "identity": "claude"})()
    assert not az.check("file.read", "w_nas", {"name": "nas"}, principal=principal_for_token(att)).denied


def test_band_grant_through_the_band_client(tmp_path):
    az = authorizer(tmp_path)
    az.attach_client(Multi(Band("psk-a", {"w_tv": {"name": "tv"}}), Band("psk-b", {})))
    az.access.sync_bands([{"id": "band1", "psk_hash": hashlib.sha256(b"psk-a").hexdigest()}])
    az.access.grant("u_kid", "band", "band1", tier="read", actor="o", words="the living room")
    assert az.bands_of("w_tv") == {hashlib.sha256(b"psk-a").hexdigest()}
    assert not az.check("file.read", "w_tv", {"name": "tv"}, principal=member()).denied
    assert az.check("file.write", "w_tv", {"name": "tv"}, principal=member()).denied
    assert az.check("file.read", "w_other", {"name": "x"}, principal=member()).denied


def test_access_errors_deny_members_and_never_owners(tmp_path):
    az = authorizer(tmp_path)

    class Broken:
        def evaluate(self, *a, **k):
            raise sqlite3.OperationalError("disk")
    az.access = Broken()
    d = az.check("file.read", "w", {"name": "w"}, principal=member())
    assert d.decision == "error_deny"
    assert not az.check("file.read", "w", {"name": "w"}, principal=owner()).denied
    # A store that would not open: members reach nothing.
    az2 = Authorizer(PolicyStore(str(tmp_path / "policy.json")), access=__import__(
        "rook.hub.authz", fromlist=["_NoAccess"])._NoAccess())
    assert az2.check("file.read", "w", {"name": "w"}, principal=member()).denied


def test_no_data_dir_means_no_grant_limits(tmp_path):
    az = build_authorizer(None, signer=False)
    assert az.access is None
    assert not az.check("file.read", "w", {"name": "w"}, principal=member()).denied


# -- the users.* caps ------------------------------------------------------------

class Client:
    def __init__(self, authz):
        self.authz = authz
        self.workers = {"w_pc": {"name": "kids-pc"}}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    accounts_db(tmp_path / "enrollment.db")
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    monkeypatch.setenv("ROOK_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    az = authorizer(tmp_path)
    az.access.seed(Accounts(str(tmp_path / "enrollment.db")))
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   client=Client(az)), az


async def as_(principal, coro_fn):
    tok = current_principal.set(principal)
    try:
        return await coro_fn()
    finally:
        current_principal.reset(tok)


@pytest.mark.asyncio
async def test_users_caps_owner_only_writes_and_self_service(hub):
    n, az = hub
    assert {"users.me", "users.explain", "users.list", "users.grant", "users.revoke",
            "users.role", "users.history"} <= set(n.caps())
    tiers = n.host.tiers()
    assert tiers["users.grant"] == "a" and tiers["users.me"] == "r"

    me = await as_(member(), lambda: n.invoke("users.me", {}, "human:u_kid"))
    assert me["role"] == "member" and "band band1" in me["plain"]
    new = await as_(member("u_new"), lambda: n.invoke("users.me", {}, "human:u_new"))
    assert new["role"] == "new" and new["grants"] == [] and "nothing" in new["plain"]

    with pytest.raises(PermissionError, match="hub owners"):
        await as_(member(), lambda: n.invoke("users.grant", {
            "user": "u_kid", "kind": "any", "words": "I want everything"}, "human:u_kid"))
    with pytest.raises(PermissionError):
        await as_(member(), lambda: n.invoke("users.list", {}, "human:u_kid"))

    g = await as_(owner(), lambda: n.invoke("users.grant", {
        "user": "u_new", "kind": "worker", "ref": "kids-pc", "caps": ["file.*"], "tier": "read",
        "words": "Bake: let the new kid read files on the kids pc"}, "human:u_bake"))
    assert g["grant"]["actor"].startswith("human:u_bake")
    ex = await as_(member("u_new"), lambda: n.invoke(
        "users.explain", {"cap": "file.read", "worker": "kids-pc"}, "human:u_new"))
    assert ex["allowed"] is True and ex["grant"]["id"] == g["grant"]["id"]
    ex = await as_(member("u_new"), lambda: n.invoke(
        "users.explain", {"cap": "shell.exec", "worker": "kids-pc"}, "human:u_new"))
    assert ex["allowed"] is False
    with pytest.raises(PermissionError):
        await as_(member("u_new"), lambda: n.invoke(
            "users.explain", {"cap": "file.read", "user": "u_kid"}, "human:u_new"))

    listed = await as_(owner(), lambda: n.invoke("users.list", {}, "human:u_bake"))
    by = {u["user"]: u for u in listed["users"]}
    assert by["u_op"]["role"] == "owner" and by["u_op"]["operator"] is True
    assert by["u_bake"]["name"] == "Bake" and by["u_new"]["role"] == "member"
    assert listed["owners"] == 2

    await as_(owner(), lambda: n.invoke("users.revoke", {"grant": g["grant"]["id"],
                                                         "words": "done"}, "human:u_bake"))
    hist = await as_(owner(), lambda: n.invoke("users.history", {"user": "u_new"}, "human:u_bake"))
    assert [h["action"] for h in hist["history"]][:2] == ["revoke", "grant"]
    r = await as_(owner(), lambda: n.invoke("users.role", {
        "user": "u_kid", "role": "owner", "words": "Bake: he's grown up"}, "human:u_bake"))
    assert r == {"user": "u_kid", "role": "owner", "was": "member"}


@pytest.mark.asyncio
async def test_members_see_and_write_only_their_knowledge_areas(hub):
    n, az = hub

    async def kw(principal, action, **data):
        return await as_(principal, lambda: n.invoke("knowledge.write", {
            "action": action, "request_id": f"r-{action}-{data.get('title', '')}{len(str(data))}",
            "data": data}, principal.id))

    async def kr(principal, action, **args):
        return await as_(principal, lambda: n.invoke("knowledge.read", {"action": action, **args},
                                                     principal.id))

    hw = await kw(owner(), "create", title="Homework", slug="homework")
    priv = await kw(owner(), "create", title="Money", slug="money")
    page = await kw(owner(), "create", title="Fractions homework", parent=hw["id"])
    secret = await kw(owner(), "create", title="Fractions of the budget", parent=priv["id"])

    with pytest.raises(PermissionError, match="no knowledge area"):
        await kr(member(), "search", query="fractions")
    az.access.grant("u_kid", "knowledge", "homework", tier="write", actor="o", words="school")

    found = await kr(member(), "search", query="fractions")
    ids = {r["id"] for r in found["results"]}
    assert page["id"] in ids and secret["id"] not in ids
    listed = await kr(member(), "list")
    assert {r["id"] for r in listed["records"]} <= {hw["id"], page["id"]}
    assert (await kr(member(), "get", id=page["id"]))["title"] == "Fractions homework"
    with pytest.raises(LookupError):
        await kr(member(), "get", id=secret["id"])
    with pytest.raises(PermissionError, match="knowledge-area access"):
        await kr(member(), "deck")

    mine = await kw(member(), "create", title="Long division", parent=hw["id"])
    assert mine["id"]
    with pytest.raises(PermissionError, match="folder in your knowledge areas"):
        await kw(member(), "create", title="Loose page")
    with pytest.raises(PermissionError, match="within your knowledge areas"):
        await as_(member(), lambda: n.invoke("knowledge.write", {
            "action": "update", "id": mine["id"], "request_id": "mv1",
            "data": {"revision": mine["revision"], "patch": {"parent": priv["id"]}}}, "human:u_kid"))
    with pytest.raises(LookupError):
        await as_(member(), lambda: n.invoke("knowledge.write", {
            "action": "update", "id": secret["id"], "request_id": "up2",
            "data": {"revision": secret["revision"], "patch": {"title": "x"}}}, "human:u_kid"))
    # Owners are not limited by areas.
    assert (await kr(owner(), "get", id=secret["id"]))["id"] == secret["id"]


# -- the dashboard: who is an owner ------------------------------------------------

def test_dashboard_owners_come_from_the_access_store(tmp_path):
    from types import SimpleNamespace
    from rook.remote.bootstrap import CombinedServer
    accounts_db(tmp_path / "enrollment.db")
    az = authorizer(tmp_path)
    users = {"u_op": {"id": "u_op", "name": "Operator", "admin": 1},
             "u_bake": {"id": "u_bake", "name": "Bake", "admin": 0},
             "u_kid": {"id": "u_kid", "name": "Kid", "admin": 0}}
    current = {}
    accounts = SimpleNamespace(current=lambda req: current.get("user"), handles=lambda p: False,
                               store=SimpleNamespace(bands=lambda uid: [{"role": "owner"}]))
    srv = SimpleNamespace(_auth_exempt=lambda p: False, _accounts=accounts, _authz=None)
    srv._access_store = lambda: CombinedServer._access_store(srv)
    req = SimpleNamespace(path="/api/call")

    def who(uid):
        current["user"] = users[uid]
        return CombinedServer._principal_for(srv, req)

    # Before the access store is up: the old rule (a band owner is an owner).
    assert who("u_kid").role == "owner"
    srv._authz = az
    accounts.store = Accounts(str(tmp_path / "enrollment.db"))
    CombinedServer._seed_access(srv)
    assert who("u_op").role == "owner"       # operator accounts always
    assert who("u_bake").role == "owner"     # band owner, carried over
    assert who("u_kid").role == "member"
    az.access.set_role("u_kid", "owner", actor="human:u_bake", words="Bake: he's an owner now")
    assert who("u_kid").role == "owner"


def test_a_broken_store_never_breaks_dashboard_sign_in(tmp_path):
    from types import SimpleNamespace
    from rook.hub.authz import _NoAccess
    from rook.remote.bootstrap import CombinedServer
    srv = SimpleNamespace(_authz=SimpleNamespace(access=_NoAccess()))
    assert CombinedServer._access_store(srv) is None
