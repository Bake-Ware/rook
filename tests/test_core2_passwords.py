"""Sign-in passwords (rook/core2/hub/passwords.py) and the hub CLI's init and
passwd. castling-switchover-decisions: "a normal username and password login,
like 0.4 is"; nothing may lock Bake out: passwd is the way back in."""
from __future__ import annotations

import io
import json
import stat
import sys
import types

import pytest

pytest.importorskip("nacl")

from rook.core2.hub import passwords  # noqa: E402
from rook.core2.hub.__main__ import main as hub_main  # noqa: E402
from rook.core2.hub.runtime import load_config  # noqa: E402


def test_only_a_hash_is_kept_and_checked(tmp_path):
    passwords.set_password(tmp_path, "bake", "hunter2 ü", by="passwd")
    raw = (tmp_path / "passwords.json").read_text()
    assert "hunter2" not in raw and json.loads(raw)["bake"]["hash"].startswith("scrypt$")
    assert stat.S_IMODE((tmp_path / "passwords.json").stat().st_mode) == 0o600
    assert passwords.check(tmp_path, "bake", "hunter2 ü")
    for name, pw in (("bake", "hunter2"), ("bake", ""), ("bake", None), ("kim", "hunter2 ü"), (None, "x")):
        assert not passwords.check(tmp_path, name, pw)
    assert passwords.set_by(tmp_path, "bake") == "passwd" and passwords.set_by(tmp_path, "kim") is None
    with pytest.raises(ValueError):
        passwords.set_password(tmp_path, "bake", "", by="passwd")
    (tmp_path / "passwords.json").write_text("not json")
    assert not passwords.check(tmp_path, "bake", "hunter2 ü")      # a damaged file signs nobody in


def test_sign_in_needs_a_configured_person(tmp_path):
    hub = types.SimpleNamespace(data=tmp_path, identities=types.SimpleNamespace(
        user=lambda p: object() if p == "person:bake" else None))
    passwords.set_password(tmp_path, "bake", "pw", by="passwd")
    passwords.set_password(tmp_path, "gone", "pw", by="passwd")     # in the file, not in the config
    assert passwords.sign_in(hub, " Bake ", "pw") == "person:bake"
    assert passwords.sign_in(hub, "gone", "pw") is None
    assert passwords.sign_in(hub, "bake", "nope") is None and passwords.sign_in(hub, None, "pw") is None


def _init(tmp_path):
    path = tmp_path / "hub" / "hub.yaml"
    return path, hub_main(["--config", str(path), "init", "--owner", "bake", "--relay", "127.0.0.1:7474"])


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_init_on_a_terminal_asks_for_the_password(tmp_path, monkeypatch, capsys):
    typed = iter(["one", "two", "chosen", "chosen"])         # a mismatch, then the same twice
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(typed))
    path, rc = _init(tmp_path)
    assert rc == 0
    cfg = load_config(path)
    assert passwords.check(cfg.data, "bake", "chosen") and passwords.set_by(cfg.data, "bake") == "init"
    assert not (cfg.data / "owner.password").exists()
    out = capsys.readouterr()
    assert "chosen" not in out.out and "don't match" in out.err and "Sign in at http://127.0.0.1:8740 as bake" in out.out
    assert cfg.agents == {"claude-web": {"grants": ["*"]}}         # the connector's agent


def test_passwd_sets_and_resets_from_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    path, rc = _init(tmp_path)                                       # no terminal: one is made
    data = load_config(path).data
    made = (data / "owner.password").read_text().strip()
    assert rc == 0 and passwords.check(data, "bake", made)
    monkeypatch.setattr(sys, "stdin", io.StringIO("new one\n"))
    assert hub_main(["--config", str(path), "passwd", "bake"]) == 0
    assert passwords.check(data, "bake", "new one") and passwords.set_by(data, "bake") == "passwd"
    assert not passwords.check(data, "bake", made)
    monkeypatch.setattr(sys, "stdin", io.StringIO("x\n"))
    assert hub_main(["--config", str(path), "passwd", "eve"]) == 2           # not a user
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert hub_main(["--config", str(path), "passwd", "bake"]) == 2          # empty
    assert passwords.check(data, "bake", "new one")
    assert "no user 'eve'" in capsys.readouterr().err
