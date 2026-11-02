"""The bots switch (castling/switchover/bots.py): the 0.4 Telegram/Discord
bots move to castling at cutover, staged off until then, and back for a
rollback; a person who linked on 0.4 needn't link again."""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from castling.switchover import bots
from rook.core2.hub.runtime import _key, load_config
from rook.core2.hub.vault import Vault
from rook.kit.ctx import Caller
from rook.kit.manifest import load as load_manifest
from rook.kit.records import RecordStore

BAND = "ab" * 16
REPO = Path(__file__).resolve().parents[1]
LINKS = """CREATE TABLE chat_links (platform TEXT NOT NULL, platform_user TEXT NOT NULL, user_id TEXT NOT NULL,
    handle TEXT NOT NULL DEFAULT '', created REAL NOT NULL, PRIMARY KEY (platform, platform_user));"""


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    """A migrated castling hub's folder: config, library, the 0.4 archive and report."""
    cfg = tmp_path / "hub.yaml"
    cfg.write_text("# Rook hub, written by init. It holds the band key: keep it private.\n" + yaml.safe_dump(
        {"data": "./data", "library": "./library", "band": BAND, "users": {"bake": {"preset": "owner"}},
         "doors": {"http": {"bind": f"127.0.0.1:{_port()}"}},
         "settings": {"backup": {"target": "dir:/tmp/x"}}}, sort_keys=False))
    (tmp_path / "library").mkdir()
    data = tmp_path / "data"
    (data / "archive" / "0.4").mkdir(parents=True)
    (data / "migrate").mkdir()
    (data / "migrate" / "report.json").write_text(json.dumps(
        {"people": [{"from": "u1", "name": "bake"}, {"from": "u2", "name": "autumn"}]}))
    db = sqlite3.connect(data / "archive" / "0.4" / "access.db")
    db.executescript(LINKS)
    db.executemany("INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)", [
        ("telegram", "8016896375", "u1", "James", 1.0),
        ("discord", "555", "u2", "autumn_r", 2.0),
        ("discord", "777", "u9", "nobody", 3.0),          # no 0.5 person
        ("slack", "x", "u1", "j", 4.0)])                   # no 0.5 bot
    db.commit()
    db.close()
    return cfg


def _links(cfg: Path, bot: str) -> list[dict]:
    store = RecordStore(str(load_config(cfg).data / "records.db"))
    try:
        m = load_manifest(bots.PLUGINS / bot)
        store.register(m.data, plugin_dir=m.folder)
        return store.type(f"{bot}.link").list(Caller(band=BAND, owner=True, plugin=bot), limit=50)
    finally:
        store.close()


def test_stage_writes_settings_and_carries_links_once(hub: Path):
    out = bots.stage(hub)
    assert out["settings"]["added"] == {"telegram": "plugin.telegram.token", "discord": "plugin.discord.token"}
    cfg = load_config(hub)
    assert cfg.settings["telegram"] == {"token": "plugin.telegram.token"}
    assert cfg.settings["backup"] == {"target": "dir:/tmp/x"}          # the rest is kept
    assert hub.read_text().startswith("# Rook hub, written by init.")
    assert (hub.parent / "hub.yaml.pre-bots").exists()
    assert {(c["platform"], c["person"]) for c in out["links"]["carried"]} == {
        ("telegram", "person:bake"), ("discord", "person:autumn")}
    assert {s["why"] for s in out["links"]["skipped"]} == {"no 0.5 person", "no 0.5 bot"}
    tg = _links(hub, "telegram")
    assert [(r["person"], r["user"], r["chat"], r["name"]) for r in tg] == [
        ("person:bake", "8016896375", "8016896375", "James")]
    dc = _links(hub, "discord")
    assert [(r["person"], r["user"], r["chat"]) for r in dc] == [("person:autumn", "555", None)]

    again = bots.stage(hub)                         # a re-run changes nothing
    assert again["settings"] == {"changed": False}
    assert again["links"]["carried"] == [] and len(again["links"]["kept"]) == 2
    assert len(_links(hub, "telegram")) == 1


def test_stage_keeps_a_token_setting_and_a_newer_link(hub: Path):
    raw = yaml.safe_load(hub.read_text())
    raw["settings"]["telegram"] = {"token": "my-own-bot"}
    hub.write_text(yaml.safe_dump(raw))
    store = RecordStore(str(load_config(hub).data / "records.db"))
    m = load_manifest(bots.PLUGINS / "telegram")
    store.register(m.data, plugin_dir=m.folder)
    store.type("telegram.link").create(Caller(person="person:bake", band=BAND, plugin="telegram"),
                                       user="999", chat="999", name="new")
    store.close()
    out = bots.stage(hub)
    assert out["settings"]["added"] == {"discord": "plugin.discord.token"}
    assert load_config(hub).settings["telegram"] == {"token": "my-own-bot"}
    assert [r["user"] for r in _links(hub, "telegram")] == ["999"]          # linked on 0.5 since: kept


def test_stage_refuses_while_the_hub_runs(hub: Path):
    port = int(load_config(hub).doors["http"]["bind"].rpartition(":")[2])
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        with pytest.raises(bots.SwitchError, match="stop it first"):
            bots.stage(hub)
    assert not (hub.parent / "hub.yaml.pre-bots").exists()


def test_on_and_off_only_touch_the_bots_library_entries(hub: Path):
    lib = hub.parent / "library"
    (lib / "jobs").symlink_to(REPO / "rook" / "plugins" / "jobs")
    assert bots.library(hub, on=True) == {"telegram": "turned on", "discord": "turned on"}
    assert (lib / "telegram").resolve() == (REPO / "rook" / "plugins" / "telegram").resolve()
    assert bots.library(hub, on=True) == {"telegram": "on", "discord": "on"}
    assert bots.library(hub, on=False) == {"telegram": "turned off", "discord": "turned off"}
    assert sorted(p.name for p in lib.iterdir()) == ["jobs"]
    assert bots.library(hub, on=False) == {"telegram": "off", "discord": "off"}
    (lib / "telegram").mkdir()                       # something else there: look first
    with pytest.raises(bots.SwitchError, match="look first"):
        bots.library(hub, on=True)


def test_check_resolves_tokens_without_printing_them(hub: Path, capsys):
    data = load_config(hub).data
    (data / "keys").mkdir(parents=True)
    _key(data / "keys" / "root.key")
    vault = Vault(data).unlock()
    vault.set("plugin.telegram.token", "123:tg-secret-value", by="test")
    seen = []

    def whoami(bot, token):
        seen.append((bot, token))
        if bot == "discord":
            raise bots.SwitchError("discord refused the token: HTTP 401")
        return "rook_bot"

    out = bots.check(hub, whoami=whoami)
    assert seen == [("telegram", "123:tg-secret-value")]         # discord's entry isn't in the vault
    assert out["telegram"] == {"token": "plugin.telegram.token", "in_library": False, "bot": "rook_bot"}
    assert "error" in out["discord"] and out["ok"] is False
    assert {"platform": "telegram", "person": "person:bake", "name": "James"} in out["links_04"]
    assert "tg-secret-value" not in json.dumps(out)


def test_whoami_never_puts_the_token_in_an_error(monkeypatch):
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(bots.urllib.request, "urlopen", boom)
    with pytest.raises(bots.SwitchError) as e:
        bots._whoami("telegram", "123:secret-token")
    assert "secret-token" not in str(e.value)


def test_switch_script_parses():
    subprocess.run(["bash", "-n", str(REPO / "castling" / "switchover" / "bots-switch.sh")], check=True, timeout=10)
