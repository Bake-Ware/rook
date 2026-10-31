"""The 0.4 -> 0.5 migration (rook.core2.migrate) against a synthetic 0.4 hub
data folder built from master's schemas, plus one built with the 0.4 stores'
own classes while they are still in the tree."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import nacl.secret
import nacl.utils
import pytest
import yaml

from rook.core2.hub.runtime import load_config
from rook.core2.migrate import IMPORTER, MigrateError, NotACopy, convert, migrate, render, vault
from rook.core2.migrate.source import Source

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = REPO / "tests" / "fixtures" / "knowledge_v2.db"
BAND = "ab" * 16
SECRET = "hunter2-very-secret-value"
NOW = 1793000000.0

# -- a 0.4 hub's data folder (schemas as on master) ------------------------------

ACCESS = """
CREATE TABLE members (user_id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('owner','member')),
    label TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, actor TEXT NOT NULL);
CREATE TABLE bands (id TEXT PRIMARY KEY, psk_hash TEXT NOT NULL, name TEXT NOT NULL DEFAULT '');
CREATE TABLE tokens (agent_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, actor TEXT NOT NULL);
CREATE TABLE resources (kind TEXT NOT NULL, ref TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', seen REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'new', ask TEXT NOT NULL DEFAULT '', settled REAL, settled_by TEXT,
    words TEXT NOT NULL DEFAULT '', PRIMARY KEY(kind, ref));
CREATE TABLE chat_links (platform TEXT NOT NULL, platform_user TEXT NOT NULL, user_id TEXT NOT NULL,
    handle TEXT NOT NULL DEFAULT '', created REAL NOT NULL, PRIMARY KEY (platform, platform_user));
CREATE TABLE spaces (platform TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, actor TEXT NOT NULL, PRIMARY KEY (platform, space_id));
CREATE TABLE grants (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
    caps TEXT NOT NULL DEFAULT '["*"]', tier TEXT NOT NULL DEFAULT 'exec', created REAL NOT NULL,
    actor TEXT NOT NULL, words TEXT NOT NULL DEFAULT '', revoked REAL, revoked_by TEXT, revoke_words TEXT);
"""
ACCOUNTS = """CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE, password TEXT, name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '', email_verified INTEGER NOT NULL DEFAULT 0, admin INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, avatar_source TEXT NOT NULL DEFAULT 'google', avatar BLOB,
    avatar_updated REAL NOT NULL DEFAULT 0)"""
VAULT = """CREATE TABLE secrets(name TEXT PRIMARY KEY, value BLOB NOT NULL, description TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, set_by TEXT NOT NULL);
CREATE TABLE access(seq INTEGER PRIMARY KEY, name TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    via TEXT, task TEXT, ts REAL NOT NULL);"""
MEMORY = """CREATE TABLE memories (id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, text TEXT NOT NULL,
    hash TEXT NOT NULL, slot TEXT, state TEXT NOT NULL, confidence REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0, reinforced INTEGER NOT NULL DEFAULT 0, last_used REAL, created REAL NOT NULL,
    updated REAL NOT NULL, source TEXT NOT NULL, author TEXT NOT NULL, room TEXT, ref TEXT,
    supersedes TEXT NOT NULL DEFAULT '[]', superseded_by TEXT, tags TEXT NOT NULL DEFAULT '[]', reason TEXT,
    approved INTEGER NOT NULL DEFAULT 0)"""
JOURNAL = """CREATE TABLE calls (seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, ts REAL, identity TEXT,
    cap TEXT, worker TEXT, thread_id TEXT, ok INTEGER, error TEXT, reply TEXT, agent_id TEXT, key_id TEXT,
    auth TEXT, actor TEXT, principal TEXT, decision TEXT, rule TEXT, policy_rev INTEGER, tier TEXT, why TEXT)"""
TRUST = {
    "format": "rook-trust/1", "version": 7, "updated": NOW, "unlisted": "guarded", "unlisted_set_by": None,
    "entries": [
        {"id": "kaiju", "title": "kaiju", "kind": "host", "level": "sacred", "targets": ["worker:kaiju"],
         "description": "GPU box"},
        {"id": "tvs", "title": "TVs", "kind": "devices", "level": "sacred", "targets": ["worker:*-tv"],
         "description": ""},
        {"id": "nas", "title": "nas", "kind": "host", "level": "guarded", "targets": ["worker:nas", "cap:file.*"],
         "description": "Backups live here."},
        {"id": "ks", "title": "Knowledge service", "kind": "project", "level": "sacred",
         "targets": ["project:knowledge-service"], "description": ""},
        {"id": "lab", "title": "lab", "kind": "host", "level": "ship-it", "targets": ["worker:lab"],
         "description": ""},
    ],
    "exceptions": [{"id": "x1"}], "baseline": [], "ledger": [], "proposals": [], "notices": [], "grants": [],
}


def _db(path: Path, script: str, rows: dict) -> None:
    db = sqlite3.connect(path)
    db.executescript(script)
    for table, items in rows.items():
        for r in items:
            db.execute(f"INSERT INTO {table} ({','.join(r)}) VALUES ({','.join('?' * len(r))})", list(r.values()))
    db.commit()
    db.close()


def make_04(root: Path) -> Path:
    """A copy of a 0.4 hub's data folder, laid out as on the live hub."""
    d = root / "rook-04-copy"
    (d / "plugins" / "memory").mkdir(parents=True)
    _db(d / "enrollment.db", ACCOUNTS, {"users": [
        {"id": "u1", "username": "bake", "name": "Bake", "created": NOW},
        {"id": "u2", "username": "Autumn R", "name": "Autumn", "created": NOW},
        {"id": "u3", "username": None, "name": "Logan", "created": NOW}]})
    member = {"actor": "system", "created": NOW, "updated": NOW}
    grant = {"actor": "u1", "created": NOW}
    _db(d / "access.db", ACCESS, {
        "members": [{"user_id": "u1", "role": "owner", "label": "Bake", **member},
                    {"user_id": "u2", "role": "member", "label": "Autumn", **member},
                    {"user_id": "u3", "role": "member", "label": "Logan", **member}],
        "grants": [
            {"id": "g1", "user_id": "u2", "kind": "worker", "ref": "f" * 32, "caps": '["tv.*"]', "tier": "admin", **grant},
            {"id": "g2", "user_id": "u2", "kind": "knowledge", "ref": "recipes", "caps": '["knowledge.read"]',
             "tier": "read", **grant},
            {"id": "g3", "user_id": "u2", "kind": "space", "ref": "discord:123", "caps": '["*"]', **grant},
            {"id": "g4", "user_id": "u3", "kind": "band", "ref": "band-a", "caps": '["notify.*", "shell.ex?c"]',
             "tier": "write", **grant},
            {"id": "g5", "user_id": "u3", "kind": "any", "ref": "*", "caps": '["*"]', "tier": "admin",
             "revoked": NOW, **grant}],
        "bands": [{"id": "band-a", "psk_hash": "h1"}, {"id": "band-b", "psk_hash": "h2"}],
        "tokens": [{"agent_id": "claudeweb.x", "user_id": "u1", "label": "laptop", "created": NOW, "actor": "u1"}],
        "resources": [{"kind": "worker", "ref": "f" * 32, "label": "living-room-tv", "seen": NOW - 86400},
                      {"kind": "worker", "ref": "e" * 32, "label": "nas", "seen": NOW - 3600}],
        "chat_links": [{"platform": "telegram", "platform_user": "42", "user_id": "u1", "handle": "@bake",
                        "created": NOW}],
        "spaces": [{"platform": "discord", "space_id": "123", "name": "Home", "created": NOW, "actor": "u1"}]})
    (d / "trust.json").write_text(json.dumps(TRUST))
    key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
    (d / "vault.key").write_bytes(key)
    box = nacl.secret.SecretBox(key)
    _db(d / "vault.db", VAULT, {"secrets": [
        {"name": "github-pat", "value": box.encrypt(SECRET.encode()), "description": "GitHub token",
         "created": NOW, "updated": NOW, "set_by": "u1"},
        {"name": "pikvm", "value": box.encrypt(b"kvm-pass-123"), "description": "", "created": NOW,
         "updated": NOW, "set_by": "u1"}]})
    mem = {"hash": "h", "confidence": 0.9, "source": "chat", "author": "u1", "created": NOW, "updated": NOW}
    _db(d / "plugins" / "memory" / "memory.db", MEMORY, {"memories": [
        {"id": "m1", "kind": "fact", "scope": "shared", "text": "The NAS is in the closet.", "state": "active", **mem},
        {"id": "m2", "kind": "profile", "scope": "user:u1", "text": "Likes dark mode.", "state": "active", **mem},
        {"id": "m3", "kind": "procedure", "scope": "shared", "text": "Restart the TV with cec.", "state": "active",
         **mem},
        {"id": "m4", "kind": "procedure", "scope": "shared", "text": "Back up before deploys.", "state": "active",
         "approved": 1, **mem},
        {"id": "m5", "kind": "fact", "scope": "shared", "text": "A forgotten thing.", "state": "forgotten", **mem},
        {"id": "m6", "kind": "fact", "scope": "shared", "text": "Old fact.", "state": "superseded", **mem}]})
    _db(d / "journal.db", JOURNAL, {"calls": [
        {"call_id": "c1", "ts": NOW - 7200, "cap": "shell.exec", "worker": "kaiju", "ok": 1, "why": "check"},
        {"call_id": "c2", "ts": NOW - 60, "cap": "shell.exec", "worker": "kaiju", "ok": 1, "why": "again"},
        {"call_id": "c3", "ts": NOW - 30, "cap": "tv.power", "worker": "living-room-tv", "ok": 1},
        {"call_id": "c4", "ts": NOW - 20, "cap": "task.write", "worker": "rook", "ok": 1},
        {"call_id": "c5", "ts": NOW - 10, "cap": "audit.x", "worker": "a" * 32, "ok": 1},
        {"call_id": "c6", "ts": NOW - 5, "cap": "vault.get", "worker": None, "ok": 1}]})
    shutil.copy(KNOWLEDGE, d / "knowledge.db")
    return d


def make_05(root: Path, port: int | None = None) -> Path:
    """A 0.5 hub config as init writes it (a band id instead of a transport)."""
    port = port or _free_port()
    path = root / "hub" / "hub.yaml"
    path.parent.mkdir()
    path.write_text("# Rook hub, written by init. It holds the band key: keep it private.\n" + yaml.safe_dump(
        {"data": "./data", "band": BAND, "users": {"bake": {"preset": "owner"}},
         "doors": {"http": {"bind": f"127.0.0.1:{port}"}}}, sort_keys=False))
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hashes(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def copy04(tmp_path):
    return make_04(tmp_path)


# -- the dry run ----------------------------------------------------------------------

def test_dry_run_reports_everything_and_writes_nothing(tmp_path, copy04):
    cfg = make_05(tmp_path)
    before, cfg_text = _hashes(copy04), cfg.read_text()
    r = migrate(copy04, cfg)
    assert _hashes(copy04) == before and cfg.read_text() == cfg_text
    assert not (cfg.parent / "data").exists()
    assert r["applied"] is False and r["found"]["memory"] == "plugins/memory/memory.db"
    people = {p["name"]: p for p in r["people"]}
    assert people["bake"]["preset"] == "owner" and people["bake"]["from"] == "u1"
    assert people["autumn-r"]["preset"] == "member-admin"
    assert people["autumn-r"]["grants"] == ["tv.*@living-room-tv", *convert.KNOWLEDGE_READ]
    assert people["logan"]["preset"] == "member-write"                 # the revoked admin grant is gone
    assert people["logan"]["grants"] == ["notify.*", "shell.ex?c"]
    assert r["presets"] == {"member-admin": {"grants": [], "max_risk": "admin"},
                            "member-write": {"grants": [], "max_risk": "write"}}
    assert r["sacred"] == ["kaiju", "living-room-tv"]                  # *-tv matched a known machine
    assert [s["name"] for s in r["secrets"]] == ["github-pat", "pikvm"]
    assert r["memory"] == {"memory": 3, "notepad": 1, "skipped": {"forgotten": 1, "superseded": 1}}
    assert r["journal"] == {"rows": 6, "first": NOW - 7200, "last": NOW - 5}
    workers = {w["name"]: w for w in r["workers"]}
    assert sorted(workers) == ["kaiju", "living-room-tv", "nas"]       # not the hub, not an unnamed id
    assert workers["kaiju"]["calls"] == 2 and workers["kaiju"]["sacred"]
    assert workers["nas"]["enroll"].endswith("enroll http://" + load_config(cfg).doors["http"]["bind"] + " --name nas")
    notes = "\n".join(r["notes"])
    for words in ("g2 was read-only", "knowledge area 'recipes'", "discord:123", "fnmatch wildcards",
                  "band band-a", "API key laptop", "telegram link", "chat space discord:123",
                  "trust entry nas (guarded): worker:nas, cap:file.*", "were guarded in 0.4",
                  "1 exceptions entries", "sacred worker:*-tv matched living-room-tv"):
        assert words in notes, words
    assert "lab" not in r["sacred"] and "trust entry lab" not in notes        # ship-it is 0.5's default
    text = render(r) + json.dumps(r)
    assert SECRET not in text and "kvm-pass-123" not in text
    assert "Dry run" in render(r) and "`github-pat`: GitHub token" in render(r)


def test_secret_values_never_reach_output_or_logs(tmp_path, copy04, capsys, caplog):
    caplog.set_level("DEBUG")
    cfg = make_05(tmp_path)
    for apply in (False, True):
        r = migrate(copy04, cfg, apply=apply)
        assert SECRET not in render(r) + json.dumps(r)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err + caplog.text
    for f in (cfg.parent / "data" / "migrate").iterdir():
        assert SECRET not in f.read_text()
    assert SECRET.encode() not in (cfg.parent / "data" / "vault.db").read_bytes()


# -- apply ------------------------------------------------------------------------------

def test_apply_writes_the_05_hub_and_reruns_add_nothing(tmp_path, copy04):
    cfg = make_05(tmp_path)
    before = _hashes(copy04)
    r = migrate(copy04, cfg, apply=True)
    assert _hashes(copy04) == before
    data = cfg.parent / "data"
    c = load_config(cfg)
    assert c.users["bake"] == {"preset": "owner"}
    assert c.users["autumn-r"]["preset"] == "member-admin" and c.sacred == ["kaiju", "living-room-tv"]
    assert c.presets["member-write"] == {"grants": [], "max_risk": "write"}
    assert cfg.read_text().startswith("# Rook hub, written by init.")
    assert Path(r["config_backup"]).read_text().count("autumn") == 0
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    # the vault: re-sealed under the hub's root key, readable only with it
    assert vault.names(data) == ["github-pat", "pikvm"] and vault.get(data, "github-pat") == SECRET
    assert stat.S_IMODE((data / "vault.db").stat().st_mode) == 0o600
    root = (data / "keys" / "root.key").read_text()
    (data / "keys" / "root.key").write_text(os.urandom(32).hex())
    with pytest.raises(nacl.exceptions.CryptoError):
        vault.get(data, "github-pat")
    (data / "keys" / "root.key").write_text(root)
    # memory: the house agent's
    agents = sqlite3.connect(data / "plugins" / "agents" / "agents.db")
    mem = [r[0] for r in agents.execute("SELECT text FROM memory WHERE agent='house' ORDER BY id")]
    notes = [r[0] for r in agents.execute("SELECT text FROM note WHERE agent='house'")]
    agents.close()
    assert mem == ["The NAS is in the closet.", "About bake: Likes dark mode.", "Back up before deploys."]
    assert notes == ["Restart the TV with cec."]
    # the journal: whole, read-only, outside the 0.5 chains
    arch = data / "archive" / "0.4" / "journal.db"
    assert stat.S_IMODE(arch.stat().st_mode) == 0o444
    assert sqlite3.connect(arch.as_uri() + "?mode=ro", uri=True).execute("SELECT count(*) FROM calls").fetchone()[0] == 6
    assert r["journal"]["archive"]["sha256"] == hashlib.sha256(arch.read_bytes()).hexdigest()
    assert not (data / "journal.db").exists()
    report = (data / "migrate" / "report.md").read_text()
    assert "- [ ] **kaiju** (sacred): calls in the 0.4 journal: 2" in report and "Applied" in report
    assert stat.S_IMODE((data / "migrate" / "report.json").stat().st_mode) == 0o600

    again = migrate(copy04, cfg, apply=True)
    assert again["config_backup"] is None and again["secrets_written"] == []
    assert again["memory"]["written"] == {"memory": 0, "notepad": 0}
    assert load_config(cfg).users == c.users and vault.get(data, "pikvm") == "kvm-pass-123"


def test_existing_05_people_are_kept_and_rename_maps(tmp_path, copy04):
    cfg = make_05(tmp_path)
    raw = yaml.safe_load(cfg.read_text())
    raw["users"]["autumn"] = {"preset": "adult", "grants": ["lights.*"]}
    cfg.write_text(yaml.safe_dump(raw))
    r = migrate(copy04, cfg, rename={"u2": "autumn", "logan": "lo"})
    people = {p["name"]: p for p in r["people"]}
    assert people["autumn"]["preset"] == "adult"
    assert people["autumn"]["grants"] == ["lights.*", "tv.*@living-room-tv", *convert.KNOWLEDGE_READ]
    assert people["lo"]["from"] == "u3"
    assert "autumn: already a user as adult; kept that (0.4 said member-admin)" in r["notes"]
    assert "member-admin" not in r["presets"]


def test_other_05_users_and_comments_are_left_alone(tmp_path, copy04):
    cfg = make_05(tmp_path)
    raw = yaml.safe_load(cfg.read_text())
    raw["users"]["zed"] = {"preset": "adult"}
    cfg.write_text("# top comment\n" + yaml.safe_dump(raw).replace("users:", "# not a header\nusers:"))
    migrate(copy04, cfg, apply=True)
    assert load_config(cfg).users["zed"] == {"preset": "adult"}
    text = cfg.read_text()
    assert text.startswith("# top comment\n") and text.count("#") == 1


def test_a_copy_without_04_knowledge_skips_the_workbench(tmp_path, copy04):
    (copy04 / "knowledge.db").unlink()
    sqlite3.connect(copy04 / "knowledge.db").close()
    r = migrate(copy04, make_05(tmp_path))
    assert r["workbench"] == {"not_imported": "no 0.4 knowledge.db in the copy"}
    assert any("project levels (knowledge-service)" in n for n in r["notes"])


@pytest.mark.skipif(not IMPORTER.exists(), reason="the workbench importer (PR #277) isn't in this checkout")
def test_apply_imports_the_workbench_and_project_trust(tmp_path, copy04):
    import importlib.util
    cfg = make_05(tmp_path)
    dry = migrate(copy04, cfg)
    assert dry["workbench"]["imported"]["task"] == 3 and dry["workbench"]["trust"] == \
        [{"project": "knowledge-service", "level": "sacred"}]
    assert not (cfg.parent / "data").exists()
    r = migrate(copy04, cfg, apply=True)
    assert r["workbench"]["trust"] == [{"project": "knowledge-service", "level": "sacred"}]
    spec = importlib.util.spec_from_file_location("imp", IMPORTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store = mod.open_store(str(cfg.parent / "data" / "records.db"))
    me = mod.Caller(agent="agent:x", band=BAND)
    try:
        project = store.type("project.project").list(me)[0]
        assert store.type("project.trust").list(me)[0]["project"] == project["id"]
        assert len(store.type("task.task").list(me)) == 3
    finally:
        store.close()
    again = migrate(copy04, cfg, apply=True)
    assert again["workbench"]["imported"] == {} and again["workbench"]["trust"] == []


def test_without_the_workbench_importer_it_says_so(tmp_path, copy04, monkeypatch):
    import rook.core2.migrate as m
    monkeypatch.setattr(m, "IMPORTER", tmp_path / "missing.py")
    r = migrate(copy04, make_05(tmp_path))
    assert r["workbench"] == {"not_imported": "no workbench importer in this checkout"}
    assert any("workbench importer" in n for n in r["notes"])


# -- refusals -------------------------------------------------------------------------------

def test_refuses_a_live_looking_folder(tmp_path, copy04, monkeypatch):
    cfg = make_05(tmp_path)
    wal = copy04 / "access.db-wal"
    wal.write_bytes(b"")
    with pytest.raises(NotACopy, match=r"\.backup"):
        migrate(copy04, cfg)
    wal.unlink()
    monkeypatch.setenv("ROOK_DATA_DIR", str(copy04))
    with pytest.raises(NotACopy, match="live hub"):
        migrate(copy04, cfg)
    monkeypatch.setenv("ROOK_DATA_DIR", str(copy04 / "plugins"))
    with pytest.raises(NotACopy, match="live hub"):
        migrate(copy04, cfg)
    monkeypatch.delenv("ROOK_DATA_DIR")
    with pytest.raises(NotACopy, match="not a folder"):
        migrate(copy04 / "access.db", cfg)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_refuses_a_folder_a_process_holds_open(tmp_path, copy04):
    holder = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); print('ok', flush=True); "
                               "time.sleep(60)", str(copy04 / "journal.db")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        with pytest.raises(NotACopy, match=f"process {holder.pid}"):
            Source(copy04)
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_apply_refuses_while_the_05_hub_runs(tmp_path, copy04):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        cfg = make_05(tmp_path, s.getsockname()[1])
        with pytest.raises(MigrateError, match="stop it first"):
            migrate(copy04, cfg, apply=True)
        assert migrate(copy04, cfg)["applied"] is False                 # a dry run is fine
    assert not (cfg.parent / "data").exists()


def test_needs_a_05_config_and_some_04_stores(tmp_path, copy04):
    with pytest.raises(MigrateError, match="init --owner"):
        migrate(copy04, tmp_path / "nope.yaml")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(MigrateError, match="no 0.4 hub stores"):
        migrate(empty, make_05(tmp_path))


def test_a_missing_vault_key_is_reported_not_fatal(tmp_path, copy04):
    (copy04 / "vault.key").unlink()
    r = migrate(copy04, make_05(tmp_path))
    assert r["secrets"] == [] and any("vault.key is not in the copy: 2 secrets" in n for n in r["notes"])
    (copy04 / "vault.key").write_bytes(nacl.utils.random(32))
    r = migrate(copy04, tmp_path / "hub" / "hub.yaml")
    assert r["secrets"] == [] and any("github-pat doesn't decrypt" in n for n in r["notes"])
    (copy04 / "vault.key").write_bytes(b"short")
    r = migrate(copy04, tmp_path / "hub" / "hub.yaml")
    assert any("isn't a 0.4 vault key: 2 secrets" in n for n in r["notes"])
    (copy04 / "trust.json").write_text("{nope")
    with pytest.raises(NotACopy, match="not a trust map"):
        migrate(copy04, tmp_path / "hub" / "hub.yaml")


# -- the command ----------------------------------------------------------------------------

def test_command_line(tmp_path, copy04):
    cfg = make_05(tmp_path)
    run = [sys.executable, "-m", "rook.core2.migrate", "--from", str(copy04), "--to", str(cfg)]
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    p = subprocess.run(run, capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 0, p.stderr
    assert "Dry run" in p.stdout and SECRET not in p.stdout + p.stderr
    p = subprocess.run(run + ["--json", "--apply"], capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["applied"] is True
    p = subprocess.run(run + ["--rename", "u2=Not Valid"], capture_output=True, text=True, timeout=120, cwd=REPO,
                       env=env)
    assert p.returncode == 2 and "--rename" in p.stderr
    (copy04 / "trust.json-wal").write_text("")
    p = subprocess.run(run, capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 2 and "migrate:" in p.stderr


# -- the mapping ------------------------------------------------------------------------------

def test_user_names():
    assert convert.user_name({"id": "x", "username": "Bake"}, set()) == "bake"
    assert convert.user_name({"id": "x", "username": "bake"}, {"bake"}) == "bake-2"
    assert convert.user_name({"id": "x", "username": "", "label": "Mary Jo"}, set()) == "mary-jo"
    assert convert.user_name({"id": "123abc"}, set()) == "u-123abc"


def test_grant_patterns():
    g = {"id": "g", "kind": "hub", "ref": "*", "caps": ["task.*"], "tier": "exec"}
    pats, notes = convert.grant_patterns(g, {}, False)
    assert pats == ["task.*@rook"] and "castling-caps" in notes[0]
    assert convert.grant_patterns({**g, "caps": ["*"]}, {}, False) == (["*@rook"], [])
    assert convert.grant_patterns({**g, "kind": "knowledge", "caps": ["knowledge.write"]}, {}, False)[0] == \
        ["knowledge.*"]
    assert convert.grant_patterns({**g, "kind": "worker", "ref": "kaiju", "caps": ["*"]}, {}, False) == \
        (["*@kaiju"], [])
    assert convert.grant_patterns({**g, "kind": "band", "caps": ["*"]}, {}, False) == (["*"], [])
    assert convert.grant_patterns({**g, "kind": "worker", "ref": "Kaiju", "caps": ["*"]}, {"kaiju": "kaiju"},
                                  False) == (["*@kaiju"], [])          # 0.4 matched names in any case


def test_trust_project_levels_take_the_strictest():
    doc = {"entries": [{"id": "a", "level": "ship-it", "targets": ["project:p"]},
                       {"id": "b", "level": "guarded", "targets": ["project:p", "project:q"]}]}
    assert convert.trust(doc, []) == ([], {"p": "normal", "q": "normal"}, [])


# -- conformance with the 0.4 stores themselves ------------------------------------------------

def test_a_copy_made_by_the_04_stores(tmp_path):
    """While 0.4's code is in the tree: its own classes make the stores."""
    access = pytest.importorskip("rook.hub.access")
    from rook.band_mcp.journal import Journal
    from rook.band_mcp.vault import Vault
    from rook.hub.plugins.memory.store import MemoryStore
    d = tmp_path / "live-copy"
    (d / "plugins" / "memory").mkdir(parents=True)
    a = access.AccessStore(str(d / "access.db"))
    a.set_role("u1", "owner", actor="system", words="first", label="Bake")
    a.set_role("u2", "member", actor="u1", words="welcome", label="Autumn")
    a.note_resources([{"kind": "worker", "ref": "f" * 32, "label": "office-tv"}])
    a.grant("u2", "worker", "f" * 32, caps=["tv.*"], tier="exec", actor="u1", words="the TV")
    a.set_token_user("claudeweb.y", "u1", actor="u1", words="mine", label="laptop")
    v = Vault(str(d / "vault.db"))
    v.set("github-pat", SECRET, "GitHub token", "u1")
    m = MemoryStore(d / "plugins" / "memory" / "memory.db")
    m.insert({"kind": "fact", "scope": "shared", "text": "The NAS is in the closet.", "hash": "h", "state": "active",
              "confidence": 0.9, "source": "chat", "author": "u1"}, "u1")
    j = Journal(str(d / "journal.db"))
    j.record(cap="tv.power", worker="office-tv", identity="u1", args={}, reply={"ok": True}, why="on")
    for closing in (getattr(x, "close", None) for x in (a, m)):
        if closing:
            closing()
    v._db.close()
    if j._db is not None:
        j._db.close()
    cfg = make_05(tmp_path)
    r = migrate(d, cfg, apply=True)
    people = {p["name"]: p for p in r["people"]}
    assert people["bake"]["preset"] == "owner"
    assert people["autumn"] == {"from": "u2", "name": "autumn", "role": "member", "preset": "member-exec",
                                "grants": ["tv.*@office-tv"]}
    assert vault.get(cfg.parent / "data", "github-pat") == SECRET
    assert r["memory"]["memory"] == 1 and r["journal"]["rows"] == 1
    assert [w["name"] for w in r["workers"]] == ["office-tv"]


def test_checklist_names_machines_as_0_5_enrollment_takes_them():
    from rook.core2.enroll.hub import NAME
    rows = convert.checklist([{"name": "Logan's tablet", "calls": 3, "last_seen": None},
                              {"name": "kaiju", "calls": 1, "last_seen": None},
                              {"name": "  ''  ", "calls": 0, "last_seen": None}],
                             ["Logans-tablet"], "http://hub.example:8740")
    assert [r["name05"] for r in rows] == ["Logans-tablet", "kaiju", "worker"]
    assert all(NAME.fullmatch(r["name05"]) for r in rows)
    assert rows[0]["sacred"] and not rows[1]["sacred"]
    assert rows[0]["enroll"].endswith("enroll http://hub.example:8740 --name Logans-tablet")
    assert convert.worker_name("rook") == "worker"                     # the hub's own name is taken
    sacred, _, _ = convert.trust({"entries": [{"id": "t", "level": "sacred", "targets": ["worker:Mom's phone"]}]}, [])
    assert sacred == ["Moms-phone"]                                    # the sacred list uses the 0.5 name
