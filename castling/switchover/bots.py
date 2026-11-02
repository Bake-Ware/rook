"""Move the band's Telegram and Discord bots from the 0.4 hub to castling (0.5)
at cutover, and back for a rollback. Repo tooling for the switchover, like
:mod:`castling.switchover.migrate`: delete it after cutover.

A bot token can be polled by one hub at a time (two Telegram ``getUpdates``
pollers fight; two Discord gateway sessions both answer), so the bots are
staged on castling **off**: their plugins are kept out of the hub's library
until the switch. ``bots-switch.sh`` beside this file runs the whole move.
Run this module on the castling hub's machine **as the hub's user** (the
stores and the library are its files):

    python -m castling.switchover.bots check  --config /var/lib/rook05/hub.yaml
    python -m castling.switchover.bots stage  --config ...   # hub stopped: settings + links
    python -m castling.switchover.bots on     --config ...   # plugins into the library
    python -m castling.switchover.bots off    --config ...   # plugins out of the library
    python -m castling.switchover.bots status --config ...   # the running hub: are the bots connected

- ``check`` (read-only, the hub may run): each bot's token resolves from the
  hub's vault and the platform accepts it (Telegram ``getMe``, Discord
  ``users/@me``: neither polls, so 0.4 keeps its bots), the people whose 0.4
  links carry over, and whether the plugins are in the library.
- ``stage`` (the hub stopped; a re-run changes nothing): ``settings.<bot>.token``
  in hub.yaml names the vault entry the 0.4 hub used (``plugin.<bot>.token``,
  moved by the migration), the old file kept beside it; and each 0.4 chat
  link (the archive's ``access.db``) becomes the bot plugin's ``link``
  record, so a person who linked on 0.4 needn't link again (their messages
  reach the house agent: rook/plugins/agents ``inbound``).
- ``on`` / ``off``: the plugins' library entries (symlinks to the code), so
  the next hub start loads them or not. Nothing else changes: links and
  settings stay for a later ``on``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

from rook.core2.hub.runtime import _write_private, home_band, load_config
from rook.kit.ctx import Caller
from rook.kit.manifest import load as load_manifest
from rook.kit.records import RecordStore

from .migrate import MigrateError, _check_stopped

PLUGINS = Path(__file__).resolve().parents[2] / "rook" / "plugins"
# The vault entries the 0.4 hub's bots read (0.4 ``plugin.<name>.token``),
# moved into castling's vault by the migration.
BOTS = {"telegram": "plugin.telegram.token", "discord": "plugin.discord.token"}
# Who the bot is, without polling: neither call takes updates away from 0.4.
WHOAMI = {"telegram": ("https://api.telegram.org/bot{token}/getMe", None),
          "discord": ("https://discord.com/api/v10/users/@me", "Bot {token}")}
UA = "DiscordBot (https://github.com/Bake-Ware/rook, 0.5)"     # Cloudflare refuses urllib's own


class SwitchError(ValueError):
    """The step can't run; the text says what to do."""


def _cfg(config: str | Path):
    path = Path(config).resolve()
    try:
        return path, load_config(path)
    except (OSError, ValueError) as e:
        raise SwitchError(f"{path}: {e}") from None


# -- stage --------------------------------------------------------------------------

def stage(config: str | Path) -> dict:
    path, cfg = _cfg(config)
    try:
        _check_stopped(cfg)
    except MigrateError as e:
        raise SwitchError(str(e)) from None
    return {"settings": _settings(path), "links": links(cfg)}


def _settings(path: Path) -> dict:
    """``settings.<bot>.token`` = the vault name, unless one is set already."""
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    settings = dict(raw.get("settings") or {})
    added = {}
    for bot, name in BOTS.items():
        mine = dict(settings.get(bot) or {})
        if not mine.get("token"):
            mine["token"] = name
            settings[bot] = mine
            added[bot] = name
    if not added:
        return {"changed": False}
    backup = path.with_name(path.name + ".pre-bots")
    if not backup.exists():
        _write_private(backup, text)
    head = "".join(line for line in text.splitlines(keepends=True) if line.startswith("#"))
    _write_private(path, head + yaml.safe_dump({**raw, "settings": settings}, sort_keys=False))
    try:
        load_config(path)
    except ValueError as e:
        _write_private(path, text)
        raise SwitchError(f"the new config doesn't load ({e}); {path} is as it was") from None
    return {"changed": True, "added": added, "backup": str(backup)}


def _people(data: Path) -> dict:
    """0.4 user id -> 0.5 person, from the migration's report."""
    try:
        report = json.loads((data / "migrate" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SwitchError(f"{data / 'migrate' / 'report.json'}: {e}: run the migration first") from None
    return {p["from"]: f"person:{p['name']}" for p in report.get("people") or [] if p.get("from") and p.get("name")}


def _old_links(data: Path) -> list[dict]:
    db_path = data / "archive" / "0.4" / "access.db"
    if not db_path.exists():
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_links'").fetchone():
            return []
        return [dict(zip(("platform", "user", "user_id", "handle"), r)) for r in db.execute(
            "SELECT platform, platform_user, user_id, handle FROM chat_links ORDER BY created")]
    finally:
        db.close()


def links(cfg) -> dict:
    """The 0.4 chat links -> the bot plugins' ``link`` records, written as the
    plugin writes one on ``/link CODE`` (as that person, for the plugin).
    One per person and per account: an existing one is left as it is."""
    old = _old_links(cfg.data)
    out = {"carried": [], "kept": [], "skipped": []}
    if not old:
        return out
    people, band = _people(cfg.data), home_band(cfg)
    store = RecordStore(str(cfg.data / "records.db"))
    try:
        for bot in BOTS:
            m = load_manifest(PLUGINS / bot)
            store.register(m.data, plugin_dir=m.folder)
        for row in old:
            bot, person = row["platform"], people.get(row["user_id"])
            if bot not in BOTS or not person:
                out["skipped"].append({"platform": bot, "handle": row["handle"],
                                       "why": "no 0.5 bot" if bot not in BOTS else "no 0.5 person"})
                continue
            rtype = store.type(f"{bot}.link")
            plugin = Caller(band=band, owner=True, plugin=bot)
            rows = rtype.list(plugin, limit=500)
            if any(r["person"] == person or r["user"] == row["user"] for r in rows):
                out["kept"].append({"platform": bot, "person": person})
                continue
            # A Telegram private chat's id is its user's id; Discord makes
            # the direct-message channel on the first send.
            chat = row["user"] if bot == "telegram" else None
            rtype.create(Caller(person=person, band=band, plugin=bot, why="carry the 0.4 chat link"),
                         user=row["user"], chat=chat, name=row["handle"] or None)
            out["carried"].append({"platform": bot, "person": person, "name": row["handle"]})
    finally:
        store.close()
    return out


# -- on / off -----------------------------------------------------------------------

def library(config: str | Path, on: bool) -> dict:
    """The bots' library entries: symlinks to this checkout's plugin folders."""
    _, cfg = _cfg(config)
    lib = cfg.library
    if lib is None or not lib.is_dir():
        raise SwitchError(f"the hub has no library folder ({lib})")
    out = {}
    for bot in BOTS:
        entry = lib / bot
        if on:
            if entry.is_symlink() and entry.resolve() == (PLUGINS / bot).resolve():
                out[bot] = "on"
                continue
            if entry.exists() or entry.is_symlink():
                raise SwitchError(f"{entry} is there and isn't this checkout's {bot} plugin: look first")
            entry.symlink_to(PLUGINS / bot)
            out[bot] = "turned on"
        elif entry.is_symlink():
            entry.unlink()
            out[bot] = "turned off"
        elif entry.exists():
            raise SwitchError(f"{entry} isn't a symlink: look first")
        else:
            out[bot] = "off"
    return out


# -- check --------------------------------------------------------------------------

def _whoami(bot: str, token: str, timeout: float = 15) -> str:
    """The bot's name; raises SwitchError (never with the token in it)."""
    url, auth = WHOAMI[bot]
    req = urllib.request.Request(url.format(token=token), headers={
        "User-Agent": UA, **({"Authorization": auth.format(token=token)} if auth else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SwitchError(f"{bot} refused the token: HTTP {e.code}") from None
    except (OSError, ValueError) as e:         # no str(e): a URL error may hold the token
        raise SwitchError(f"{bot} not reachable: {type(e).__name__}") from None
    me = body.get("result") if bot == "telegram" else body
    return str((me or {}).get("username") or "?")


def check(config: str | Path, whoami: Callable[[str, str], str] = _whoami) -> dict:
    path, cfg = _cfg(config)
    from rook.core2.hub.vault import Vault, VaultError
    vault = Vault(cfg.data)
    out: dict = {"ok": True}
    try:
        vault.unlock()
    except VaultError as e:
        vault, out["ok"], out["vault"] = None, False, f"locked: {e}"
    settings = cfg.settings or {}
    lib = cfg.library
    for bot, default in BOTS.items():
        name = (settings.get(bot) or {}).get("token") or default
        one = {"token": name, "in_library": bool(lib and (lib / bot).exists())}
        try:
            if vault is None:
                raise SwitchError("vault locked")
            one["bot"] = whoami(bot, vault.get(name))
        except KeyError:
            one["error"], out["ok"] = f"no {name} in the vault", False
        except (SwitchError, VaultError) as e:
            one["error"], out["ok"] = str(e), False
        out[bot] = one
    try:
        people = _people(cfg.data)
        out["links_04"] = [{"platform": r["platform"], "person": people.get(r["user_id"]), "name": r["handle"]}
                           for r in _old_links(cfg.data)]
    except SwitchError as e:
        out["links_04"] = str(e)
    return out


def status(config: str | Path, token_file: str | Path | None = None, timeout: float = 30) -> dict:
    """Each bot's ``<bot>.status`` from the running hub, through its http door,
    as the owner (the token init wrote beside the config)."""
    path, cfg = _cfg(config)
    bind = str(((cfg.doors or {}).get("http") or {}).get("bind") or "127.0.0.1:8740")
    host, _, port = bind.rpartition(":")
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "[::]") else host.strip("[]")
    token_path = Path(token_file) if token_file else path.parent / "secrets" / "owner.token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SwitchError(f"{token_path}: {e.strerror}") from None
    out: dict = {"ok": True}
    for bot in BOTS:
        body = json.dumps({"worker": "rook", "cap": f"{bot}.status",
                           "why": "Bots switch: is the bot connected on castling"}).encode()
        req = urllib.request.Request(f"http://{host}:{port}/call", data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                reply = json.loads(r.read().decode())
        except (OSError, ValueError) as e:
            reply = {"ok": False, "error": type(e).__name__}
        result = reply.get("result") if reply.get("ok") else None
        out[bot] = result if isinstance(result, dict) else {"error": reply.get("error") or "no reply"}
        out["ok"] = out["ok"] and bool(out[bot].get("connected"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m castling.switchover.bots", description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["check", "stage", "on", "off", "status"])
    ap.add_argument("--config", default="/var/lib/rook05/hub.yaml")
    a = ap.parse_args(argv)
    try:
        if a.step == "check":
            out = check(a.config)
        elif a.step == "stage":
            out = stage(a.config)
        elif a.step == "status":
            out = status(a.config)
        else:
            out = library(a.config, a.step == "on")
    except SwitchError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
