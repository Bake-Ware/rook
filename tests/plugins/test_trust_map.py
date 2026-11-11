"""The 0.4 Trust map on 0.5 (Bake, Nov 9: "Let's bring over the trust map and
knowledge pages too"), in the agents package ("it matters most to the
agents"): trust.map reads machines, projects, agents, hard stops and security
in one call; core's hub.sacred marks a machine sacred in hub.yaml and at
once; the page is the 0.4 one (level cards, Everything else, history)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from rook.core2.hub.dispatch import Worker
from rook.core2.hub.runtime import Known
from tests.test_core2_dashboard import WHY, Client, run, site  # noqa: F401 - the fixture

REPO = Path(__file__).resolve().parents[2]
TRUST = REPO / "rook" / "plugins" / "trust"
PAGE = TRUST / "pages" / "trust.html"


def test_the_trust_page_is_the_04_one():
    page = PAGE.read_text()
    for part in ("Trust map ", "Everything else", "What agents may do", "Hard stops", "History",
                 "How the trust map works", "What agents are told:", "class: \"seg\"", "mg-box", "mg-tag",
                 '"hub.sacred"', '"trust.level.update"', '"trust.level.create"', '"agent.update"',
                 '"setting.set"', "var(--overlay)"):
        assert part in page, part
    assert "prompt(" not in page and "confirm(" not in page and "alert(" not in page   # no modals in the sandbox
    assert ":root" not in page                       # the base style's tokens, not its own
    manifest = yaml.safe_load((TRUST / "plugin.yaml").read_text())
    assert (manifest["package"], manifest["segment"]) == ("agents", "Agents")
    assert {"trust.map", "hub.sacred", "trust.level.*", "agent.update"} <= set(manifest["pages"]["trust"]["caps"])


def _library(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    os.symlink(REPO / "rook" / "plugins" / "projects", lib / "project")
    os.symlink(TRUST, lib / "trust")


def test_trust_map_and_a_sacred_machine(site, tmp_path):
    _library(tmp_path)

    async def go():
        hub, tokens = await site()
        try:
            conf = tmp_path / "hub.yaml"
            conf.write_text("# Rook hub\n# keep me\ndata: data\nband: x\nsacred: []\n")
            os.chmod(conf, 0o640)
            hub.cfg.file = conf
            k = Known("w" * 32, "kaiju", hub.band, description="AI server", online=True, last_seen=hub.clock())
            hub.known[k.id] = k
            hub.dispatcher.workers[k.id] = Worker(k.id, "kaiju", hub.band, {}, {"grants": ["*"]})

            c = Client(hub)
            await c.sign_in(tokens["bake"])
            async def api(cap, **args):
                return (await c.json("POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY},
                                     bearer=tokens["bake"]))[1]
            made = await api("project.create", title="Rook", slug="rook")
            assert made.get("ok"), made
            pid = made["result"]["id"]
            assert (await api("project.create", title="Toys", slug="toys"))["ok"]
            assert (await api("trust.level.create", project=pid, level="sacred"))["ok"]

            t = (await c.open("trust/trust"))["token"]
            m = (await c.page(t, "trust.map"))["result"]
            assert m["security"] == "on" and m["everything_else"] == "normal"
            assert {"destructive", "sends_as", "reveals a secret", "sacred machine"} <= set(m["hard_stops"])
            assert [(x["name"], x["level"]) for x in m["machines"]] == [("kaiju", "normal")]
            levels = {p["title"]: (p["level"], p["set"]) for p in m["projects"]}
            assert levels == {"Rook": ("sacred", True), "Toys": ("normal", False)}
            assert {a["name"]: a["source"] for a in m["agents"]} == {"claude": "hub.yaml", "narrow": "hub.yaml"}
            assert m["missing"] == ["agents"] and m["waiting"] == 0
            assert "Sacred machines: none" in m["summary"] and "sacred projects: Rook" in m["summary"]
            assert [h["cap"] for h in m["history"]] == ["trust.level.create"]

            out = await c.page(t, "hub.sacred", {"worker": "kaiju", "sacred": True}, why="Make kaiju sacred. (“GPU box”)")
            assert out["ok"] and out["result"]["sacred"] and out["result"]["saved"] == str(conf)
            assert hub.cfg.sacred == ["kaiju"] and hub.dispatcher.workers[k.id].sacred
            text = conf.read_text()
            assert text.startswith("# Rook hub\n# keep me\n") and yaml.safe_load(text)["sacred"] == ["kaiju"]
            assert oct(conf.stat().st_mode & 0o777) == "0o640"
            m = (await c.page(t, "trust.map"))["result"]
            assert m["machines"][0]["level"] == "sacred" and "Sacred machines: kaiju" in m["summary"]
            assert m["history"][0]["cap"] == "hub.sacred" and "GPU box" in m["history"][0]["why"]

            # sacred means a hard stop for anything that isn't a read there
            from rook.core2.hub.dispatch import _hard_stops
            assert _hard_stops({}, hub.dispatcher.workers[k.id], "write") == ["sacred machine"]
            assert _hard_stops({}, hub.dispatcher.workers[k.id], "read") == []

            assert (await c.page(t, "hub.sacred", {"worker": "kaiju", "sacred": False}))["ok"]
            assert hub.cfg.sacred == [] and not hub.dispatcher.workers[k.id].sacred
            assert yaml.safe_load(conf.read_text())["sacred"] == []
        finally:
            await hub.stop()
    run(go())

