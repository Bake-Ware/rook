"""Change requests (rook/hub/plugins/changes): a standard change is checked,
run, tested and kept or rolled back by the hub; an emergency's standard fix
runs at once with one FYI; anything else reaches the person as one card,
after one huddle round for normal changes. Commands run for real in a
scratch HOME behind a fake band client."""
from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from rook.hub.plugins.changes import Changes
from rook.hub.plugins.changes.model import ChangeError
from tests.test_huddle_chair import hub  # noqa: F401
from tests.test_trust import seeded


class Client:
    def __init__(self, home):
        self.home = home
        self.workers = {"w-cachy": {"name": "cachyrig", "caps": ["shell.exec"]},
                        "w-kaiju": {"name": "kaiju", "caps": ["shell.exec"]}}
        self.calls = []
        self.authz = None

    async def call(self, cap, args, target, timeout, identity, principal):
        assert cap == "shell.exec" and principal.kind == "system"
        self.calls.append((self.workers[target]["name"], args["cmd"], identity))
        p = await asyncio.create_subprocess_exec(
            "bash", "-c", args["cmd"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "HOME": str(self.home)})
        out, err = await p.communicate()
        return {"ok": p.returncode == 0, "result": {"code": p.returncode,
                                                    "stdout": out.decode(), "stderr": err.decode()}}


class Attention:
    def __init__(self):
        self.lines, self.sent = [], []
        self.service = SimpleNamespace(send=self._send)

    def log(self, source, outcome, what, why="", how="", ref=""):
        self.lines.append((outcome, what, how))

    async def _send(self, text, title="", urgency="normal", channel="digest", source=""):
        self.sent.append((urgency, title, text))
        return {"ok": True}


class Front:
    def __init__(self):
        self.asks = []

    def make_ask(self, kind, line, explain, more="", **kw):
        self.asks.append({"kind": kind, "line": line, **kw})
        return {"id": f"a{len(self.asks)}", "name": f"ask-{len(self.asks)}"}


@pytest.fixture
def ch(tmp_path):
    client = Client(tmp_path)
    att, front = Attention(), Front()
    plugins = {"trust": SimpleNamespace(store=seeded()), "attention": att,
               "home": SimpleNamespace(front=front)}
    node = SimpleNamespace(client=client, worker_id="hub", journal=None,
                           plugin=lambda ns: plugins.get(ns))
    p = Changes()
    p.__dict__["_data_root"] = str(tmp_path / "data")
    p.bind_host(node)
    p.client, p.att, p.front, p.home = client, att, front, tmp_path
    p._runner().poll_s = 0.05
    return p


def cr(**over):
    base = {"kind": "standard", "category": "config", "what": "Turn the music player volume up",
            "why": "Bake asked for it to be louder", "machines": ["cachyrig"],
            "plan": "Write the new volume, check it, keep the old one to restore.",
            "scripts": [{"worker": "cachyrig", "run": "echo 7 > $HOME/volume"}],
            "tests": [{"worker": "cachyrig", "run": "cat $HOME/volume", "expect": "7"}],
            "rollback": {"worker": "cachyrig", "run": "cp $HOME/volume.bak $HOME/volume",
                         "dry_run": "test -f $HOME/volume.bak", "point": "test -s $HOME/volume.bak"}}
    base.update(over)
    return base


async def _done(ch, ident):
    for _ in range(200):
        row = ch.store.get(ident)
        if row["state"] not in ("checking", "running"):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"{ident} still {row['state']}: {row['phase']} {row['log']}")


@pytest.mark.asyncio
async def test_a_standard_change_checks_runs_tests_and_is_kept(ch):
    (ch.home / "volume.bak").write_text("3\n")
    out = await ch.submit(cr())
    row = await _done(ch, out["id"])
    assert row["state"] == "kept", row["log"]
    assert [e["phase"] for e in row["log"]] == ["point", "dry_run", "pre", "scripts", "post"]
    assert (ch.home / "volume").read_text() == "7\n"
    assert ch.att.lines[-1][0] == "kept" and not ch.front.asks     # no person, no vote
    assert all(i == f"change:{out['id']}" for *_, i in ch.client.calls)


@pytest.mark.asyncio
async def test_a_failing_test_rolls_back_by_itself(ch):
    (ch.home / "volume.bak").write_text("3\n")
    out = await ch.submit(cr(tests=[{"worker": "cachyrig", "run": "cat $HOME/volume",
                                     "expect": "11"}]))
    row = await _done(ch, out["id"])
    assert row["state"] == "rolled_back" and (ch.home / "volume").read_text() == "3\n"
    assert ch.att.lines[-1][0] == "rolled_back" and not ch.att.sent   # the digest tells


@pytest.mark.asyncio
async def test_no_rollback_point_means_nothing_runs(ch):
    out = await ch.submit(cr())                                       # no volume.bak
    row = await _done(ch, out["id"])
    assert row["state"] == "refused" and not (ch.home / "volume").exists()


@pytest.mark.asyncio
async def test_a_standard_change_needs_every_field(ch):
    with pytest.raises(ChangeError, match="rollback.dry_run"):
        await ch.submit(cr(rollback={"worker": "cachyrig", "run": "true"}))
    with pytest.raises(ChangeError, match="machines"):
        await ch.submit(cr(machines=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("over, why", [
    ({"category": "decommission"}, "not a standard kind"),
    ({"machines": ["cachyrig", "kaiju"],
      "scripts": [{"worker": "kaiju", "run": "systemctl restart voice"}]}, "kaiju is sacred"),
    ({"scripts": [{"worker": "cachyrig", "run": "dropdb music"}]}, "drops a database"),
    ({"scripts": [{"worker": "cachyrig", "run": "ssh kaiju sudo reboot"}]}, "reaches kaiju"),
    ({"scripts": [{"worker": "cachyrig", "run": "sudo nano /var/lib/rook-band-mcp/trust.json"}]},
     "hub's own secrets")])
async def test_what_is_not_standard_goes_to_the_person_as_one_card(ch, over, why):
    out = await ch.submit(cr(**over))
    assert out["state"] == "asked" and out["kind"] == "normal"
    assert any(why in c for c in out["checks"])
    assert [a["kind"] for a in ch.front.asks] == ["change"]
    assert not ch.client.calls                                       # nothing ran
    (ch.home / "volume.bak").write_text("3\n")
    if over.get("category"):
        res = await ch.decided(out["id"], True, "Bake")
        assert res["ok"]
        assert (await _done(ch, out["id"]))["state"] == "kept"


@pytest.mark.asyncio
async def test_a_machine_named_by_its_worker_id_is_still_sacred(ch):
    out = await ch.submit(cr(machines=["w-kaiju"],
                             scripts=[{"worker": "w-kaiju", "run": "echo 7 > $HOME/volume"}],
                             tests=[{"worker": "w-kaiju", "run": "true"}],
                             rollback={"worker": "w-kaiju", "run": "true", "dry_run": "true",
                                       "point": "true"}))
    assert out["state"] == "asked" and "kaiju is sacred" in " ".join(out["checks"])
    assert out["machines"] == ["kaiju"] and not ch.client.calls
    with pytest.raises(ChangeError, match="not on the band"):
        await ch.submit(cr(machines=["nowhere"], scripts=[{"worker": "nowhere", "run": "true"}],
                           tests=[], rollback={}))


@pytest.mark.asyncio
async def test_only_the_huddles_round_reduces_a_change(ch):
    out = await ch.submit(cr(category="decommission"))           # straight to the card
    (ch.home / "volume.bak").write_text("3\n")
    std = await ch.submit(cr())
    with pytest.raises(ChangeError, match="only the huddle"):
        ch.reduce(out["id"], [std["id"]])


@pytest.mark.asyncio
async def test_a_no_drops_it(ch):
    out = await ch.submit(cr(category="decommission"))
    await ch.decided(out["id"], False, "Bake")
    assert ch.store.get(out["id"])["state"] == "declined" and not ch.client.calls


@pytest.mark.asyncio
async def test_an_emergency_standard_fix_runs_now_and_tells_the_person_once(ch):
    out = await ch.submit({"kind": "emergency", "fix": "restart", "what": "Music player is down",
                           "machines": ["cachyrig"],
                           "scripts": [{"worker": "cachyrig", "run": "echo up > $HOME/svc"}]})
    row = await _done(ch, out["id"])
    await asyncio.sleep(0.05)
    assert row["state"] == "kept" and (ch.home / "svc").read_text() == "up\n"
    assert [(u, t) for u, t, _ in ch.att.sent] == [("normal", "Emergency fix")]
    ch.update(out["id"], {"why": "the player crashed after the update", "plan": "restart it",
                          "scripts": [{"worker": "cachyrig", "run": "echo other"}],
                          "machines": ["cachyrig", "kaiju"],
                          "rollback": {"worker": "kaiju", "run": "rm -rf /home/bake"}})
    rec = ch.store.get(out["id"])["record"]
    assert rec["why"].startswith("the player crashed") and "echo up" in rec["scripts"][0]["run"]
    assert rec["rollback"] == {} and rec["machines"] == ["cachyrig"]   # what ran stays
    with pytest.raises(ChangeError, match="no rollback"):
        await ch.revert(out["id"])


@pytest.mark.asyncio
async def test_an_emergency_with_another_fix_is_one_urgent_card(ch):
    out = await ch.submit({"kind": "emergency", "fix": "rebuild", "what": "Music player is down",
                           "machines": ["cachyrig"],
                           "scripts": [{"worker": "cachyrig", "run": "echo x"}]})
    assert out["state"] == "asked" and ch.front.asks[0]["action"] == {"change": out["id"]}
    assert ch.att.sent[0][0] == "urgent" and not ch.client.calls


@pytest.mark.asyncio
async def test_a_detached_step_outlives_a_hub_restart(ch):
    (ch.home / "volume.bak").write_text("3\n")
    out = await ch.submit(cr(scripts=[{"worker": "cachyrig", "detach": True,
                                       "run": "sleep 0.3; echo 7 > $HOME/volume"}]))
    for _ in range(100):                                            # the deploy is under way
        if ch.store.get(out["id"])["phase"] == "scripts":
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    await ch.runner.stop()                                          # the hub restarts
    ch.runner = None
    ch._runner().poll_s = 0.05
    assert ch._runner().resume() == [out["id"]]
    row = await _done(ch, out["id"])
    assert row["state"] == "kept", row["log"]
    started = [c for c in ch.client.calls if "setsid" in c[1]]
    assert len(started) == 1                                        # never started twice


@pytest.mark.asyncio
async def test_revert_that_rolls_a_kept_change_back(ch):
    (ch.home / "volume.bak").write_text("3\n")
    out = await ch.submit(cr())
    await _done(ch, out["id"])
    row = await ch.revert(out["id"])
    assert row["state"] == "reverted" and (ch.home / "volume").read_text() == "3\n"


def test_only_the_person_makes_a_kind_standard(ch):
    from rook.hub.authz import current_principal
    from rook.hub.policy import Principal
    tok = current_principal.set(Principal("token:claude", "token", "agent"))
    try:
        with pytest.raises(PermissionError, match="only the person"):
            ch.catalog("add", "decommission", "I think this is fine")
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(Principal("human:u1", "human", "owner", label="bake"))
    try:
        ch.catalog("add", "decommission", "that kind is fine")
    finally:
        current_principal.reset(tok)
    assert "decommission" in ch.catalog()["catalog"]


@pytest.mark.asyncio
async def test_the_digest_lists_what_ran_once_a_morning(ch, monkeypatch):
    (ch.home / "volume.bak").write_text("3\n")
    a = await ch.submit(cr())
    b = await ch.submit(cr(tests=[{"worker": "cachyrig", "run": "false"}]))
    await _done(ch, a["id"])
    await _done(ch, b["id"])
    text = ch.digest()["text"]
    assert "1 kept" in text and "1 rolled back" in text and a["id"] in text and "revert" in text
    import time

    import rook.hub.plugins.changes as C
    monkeypatch.setattr(C, "DIGEST_HOUR", 0)
    sent = len(ch.att.sent)
    await ch._digest_due(time.time())
    await ch._digest_due(time.time() + 1)
    titles = [t for _, t, _ in ch.att.sent[sent:]]
    assert titles.count("Changes") == 1                       # once a morning
    assert "Change review, last 7 days" in ch.digest(weekly=True)["text"]


@pytest.mark.asyncio
async def test_on_a_real_hub_a_normal_change_reaches_the_house_agents_card(hub, monkeypatch):
    h = hub
    h.node.settings.set("home.front_door", True, actor="human:op")
    h.band.workers["w-cachy"] = {"name": "cachyrig", "caps": ["shell.exec"], "last_seen": 0}
    changes = h.node.plugin("change")
    monkeypatch.setattr(changes, "_plugin", lambda ns: None if ns == "huddle"
                        else h.node.plugin(ns))           # no huddle round: straight to the card
    out = await h.node.dispatch("change.submit", {"data": cr(category="decommission")},
                                "token:claude")
    assert out["ok"] and out["result"]["state"] == "asked", out
    front = h.node.plugin("home").front
    ask, = [a for a in front.store.asks("open") if a["kind"] == "change"]
    assert ask["action"] == {"change": out["result"]["id"]}
    res = await front.answer(ask["id"], "no", by="Bake", quote="no, not now")
    assert "Dropped" in res["note"]
    assert changes.store.get(out["result"]["id"])["state"] == "declined"
