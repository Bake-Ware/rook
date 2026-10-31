"""The hub's vault (rook.core2.hub.vault): sealing, unlock, recovery,
references, journal redaction, the CLI and the migration's format."""
from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("nacl")

from rook.core2.enroll.hub import recovery_key  # noqa: E402
from rook.core2.hub.runtime import _key  # noqa: E402
from rook.core2.hub.vault import (Locked, Vault, VaultError, canonical, journal_args, resolver,  # noqa: E402
                                  secret_values, substitute)

REPO = Path(__file__).resolve().parents[1]
OWNERS = {"person:bake"}


def hub_data(tmp_path: Path, name: str = "data") -> Path:
    data = tmp_path / name
    (data / "keys").mkdir(parents=True)
    _key(data / "keys" / "root.key")
    return data


def who(person=None, agent=None):
    return SimpleNamespace(person=person, agent=agent)


def test_values_are_sealed_and_never_listed(tmp_path):
    data = hub_data(tmp_path)
    v = Vault(data).unlock()
    assert v.list() == [] and stat.S_IMODE((data / "vault.db").stat().st_mode) == 0o600
    out = v.set("gh-token", "ghp_s3cretvalue", by="person:bake", description="GitHub")
    assert out["replaced"] is False and out["description"] == "GitHub"
    assert v.get("gh-token") == "ghp_s3cretvalue"
    assert b"ghp_s3cretvalue" not in (data / "vault.db").read_bytes()
    row = v.list()[0]
    assert set(row) == {"name", "description", "created", "updated", "set_by"} and row["set_by"] == "person:bake"
    again = v.set("gh-token", "ghp_new", by="person:ann")       # a new value keeps the description
    assert again["replaced"] and v.get("gh-token") == "ghp_new" and v.list()[0]["description"] == "GitHub"
    assert v.list()[0]["created"] == row["created"]
    assert v.delete("gh-token") and not v.delete("gh-token") and v.names() == []
    with pytest.raises(KeyError):
        v.get("gh-token")
    for bad in ("", "-x", "a b", "x" * 65, "band-psk-" + "0" * 32):
        with pytest.raises(VaultError):
            v.set(bad, "v", by="t")
    with pytest.raises(VaultError):
        v.set("ok", "", by="t")


def test_a_locked_vault_refuses_and_recovery_reopens_it(tmp_path):
    old = hub_data(tmp_path, "old")
    Vault(old).unlock().set("pikvm", "kvm-pass", by="person:bake")
    key = recovery_key(old)                       # made with the vault
    new = hub_data(tmp_path, "new")               # a new hub: another root key
    (new / "vault.db").write_bytes((old / "vault.db").read_bytes())
    v = Vault(new)
    with pytest.raises(Locked, match="recover-vault"):
        v.unlock()
    assert v.locked and v.names() == ["pikvm"]    # names still list
    for fn in (lambda: v.get("pikvm"), lambda: v.set("x", "y", by="t")):
        with pytest.raises(Locked):
            fn()
    with pytest.raises(VaultError, match="isn't a recovery key"):
        v.recover("not-a-key")
    with pytest.raises(VaultError, match="doesn't open"):
        v.recover(recovery_key(hub_data(tmp_path, "other")))
    v.recover(key.lower().replace("-", " "))      # however it was written down
    assert v.get("pikvm") == "kvm-pass"
    assert recovery_key(new) == key == canonical(key)    # the owner keeps one key
    assert Vault(new).unlock().get("pikvm") == "kvm-pass"   # and the next start opens it by itself
    assert Vault(old).unlock().get("pikvm") == "kvm-pass"   # the old copy is untouched


def test_wraps_are_rows_not_keys_on_disk(tmp_path):
    data = hub_data(tmp_path)
    Vault(data).unlock().set("a", "b", by="t")
    db = sqlite3.connect(data / "vault.db")
    kinds = sorted(r[0] for r in db.execute("SELECT kind FROM wraps"))
    db.close()
    assert kinds == ["recovery", "root"] and not list(data.glob("vault.key*"))
    # A vault with secrets but no wraps can't be guessed open.
    db = sqlite3.connect(data / "vault.db")
    with db:
        db.execute("DELETE FROM wraps")
    db.close()
    with pytest.raises(Locked, match="no keys"):
        Vault(data).unlock()



def test_references_resolve_for_owners_only(tmp_path):
    v = Vault(hub_data(tmp_path)).unlock()
    v.set("gh", "ghp_x", by="t")
    resolve = resolver(v, lambda p: p in OWNERS)
    args = {"argv": ["curl", "-H", "Authorization: Bearer {{secret:gh}}"], "n": 3, "k": {"t": "{{ secret: gh }}"}}
    out = resolve(args, who("person:bake"))
    assert out == {"argv": ["curl", "-H", "Authorization: Bearer ghp_x"], "n": 3, "k": {"t": "ghp_x"}}
    assert args["k"]["t"] == "{{ secret: gh }}"                                   # a copy
    assert resolve(args, who("person:bake", "agent:claude"))["k"]["t"] == "ghp_x"  # their agents too
    assert resolve({"t": "no refs {{here}}"}, who("person:ann")) == {"t": "no refs {{here}}"}
    with pytest.raises(ValueError, match="only an owner"):
        resolve(args, who("person:ann"))
    with pytest.raises(ValueError, match="only an owner"):
        resolve(args, who(None, "agent:x"))
    with pytest.raises(ValueError, match="nope is not in the vault"):
        resolve({"t": "{{secret:nope}}"}, who("person:bake"))
    with pytest.raises(ValueError, match="core.trust.set"):
        resolve({"t": "x{{secret:band-psk-%s}}" % ("0" * 32)}, who("person:bake"))
    locked = Vault(tmp_path / "data")
    with pytest.raises(ValueError, match="locked"):
        resolver(locked, lambda p: True)({"t": "{{secret:gh}}"}, who("person:bake"))
    assert substitute(["{{secret:a}}{{secret:b}}"], {"a": "1", "b": "2"}.get) == ["12"]


def test_secret_typed_args_are_redacted_for_the_journal():
    specs = {"name": {"type": "str"}, "value": {"type": "secret"}, "pin": {"secret": True}}
    assert journal_args({"name": "gh", "value": "ghp_x", "pin": "{{secret:pin}}"}, specs) == \
        {"name": "gh", "value": "[redacted]", "pin": "{{secret:pin}}"}
    assert journal_args({"value": "a {{secret:x}}"}, specs) == {"value": "[redacted]"}   # not only a reference
    assert journal_args({"name": "gh"}, specs) == {"name": "gh"}
    assert journal_args({"x": 1}, None) == {"x": 1} and journal_args(None, specs) is None
    assert sorted(secret_values({"value": "ghp_x", "pin": 1234}, specs)) == ["1234", "ghp_x"]


def test_cli_sets_lists_deletes_and_recovers(tmp_path):
    def cli(cfg, *args, input=""):
        return subprocess.run([sys.executable, "-m", "rook.core2.hub", "--config", str(cfg), *args],
                              input=input, capture_output=True, text=True, timeout=60, cwd=REPO,
                              env={**os.environ, "PYTHONPATH": str(REPO)})

    def config(name):
        cfg = tmp_path / name / "hub.yaml"
        cfg.parent.mkdir()
        cfg.write_text("data: ./data\nband: %s\nusers: { bake: { preset: owner } }\n" % ("e1" * 16))
        return cfg

    a, b = config("a"), config("b")
    r = cli(a, "secret", "set", "gh", "--description", "GitHub", input="ghp_cli\n")
    assert r.returncode == 0 and "ghp_cli" not in r.stdout + r.stderr, r.stderr
    assert cli(a, "secret", "list").stdout == "gh\tGitHub\n"
    assert Vault(a.parent / "data").unlock().get("gh") == "ghp_cli"
    assert cli(a, "secret", "set").returncode == 2
    key = cli(a, "recovery-key").stdout.strip()

    (b.parent / "data").mkdir()
    (b.parent / "data" / "vault.db").write_bytes((a.parent / "data" / "vault.db").read_bytes())
    r = cli(b, "secret", "set", "x", input="y")
    assert r.returncode == 2 and "recover-vault" in r.stderr
    r = cli(b, "recover-vault", input="wrong")
    assert r.returncode == 2 and "isn't a recovery key" in r.stderr
    r = cli(b, "recover-vault", input=key + "\n")
    assert r.returncode == 0 and "1 secrets" in r.stdout and key not in r.stdout, r.stderr
    assert Vault(b.parent / "data").unlock().get("gh") == "ghp_cli"
    assert cli(b, "secret", "delete", "gh").returncode == 0 and cli(b, "secret", "list").stdout == ""
    assert cli(b, "secret", "delete", "gh").returncode == 2


def test_the_recovery_key_owners_see_always_opens_it(tmp_path):
    data = hub_data(tmp_path)
    Vault(data).unlock().set("a", "b", by="t")
    (data / "keys" / "recovery.key").unlink()     # lost, then made again on the next start
    Vault(data).unlock()
    shown = recovery_key(data)
    other = hub_data(tmp_path, "other")
    (other / "vault.db").write_bytes((data / "vault.db").read_bytes())
    v = Vault(other)
    v.recover(shown)
    assert v.get("a") == "b"


def test_two_openers_of_a_new_vault_share_one_key(tmp_path):
    import threading
    data = hub_data(tmp_path)
    vaults, errors = [Vault(data) for _ in range(8)], []

    def go(v):
        try:
            v.unlock()
        except Exception as e:      # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go, args=(v,)) for v in vaults]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors
    vaults[0].set("k", "v", by="t")
    assert all(v.get("k") == "v" for v in vaults)
