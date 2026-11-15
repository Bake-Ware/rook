"""What the house agent can point at on a page (site design point 1: "pointing
at controls"): ``page.get {page}`` answers ``targets``, the ``data-target``
names the page's file marks, a part a template fills in read as ``<...>``.
Every page that ships in the tree marks some, named ``<page name>.<thing>``
(Settings rows are ``<plugin>.<key>``)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from test_core2_dashboard import BAND, Client, run  # noqa: E402
from test_core2_worksurface import PLUGINS, REPO, library, start  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.core2.hub.doors.web.caps import targets  # noqa: E402

PART = r"(?:[a-z0-9_-]|<\.\.\.>)+"
SHAPE = re.compile(rf"{PART}(?:\.{PART})+")


def shipped() -> dict[str, Path]:
    """Every page a plugin in the tree ships: page id -> its file."""
    out = {}
    for manifest in sorted((REPO / "rook").rglob("plugin.yaml")):
        m = yaml.safe_load(manifest.read_text())
        for name, spec in (m.get("pages") or {}).items():
            out[f"{m['plugin']}/{name}"] = manifest.parent / spec["file"]
    return out


def test_targets_read_from_a_file():
    html = """<input data-target="a.name"><b data-target='x'></b>
    ${`<tr data-target="a.${esc(j.name)}.run">`} <p data-target="has space"></p>
    h("div", { "data-target": `a.${m.name}.level` }); q.setAttribute("data-target", "a.search");
    <input data-target="a.name">"""
    assert targets(html) == ["a.name", "a.<...>.run", "a.<...>.level", "a.search"]


def test_every_shipped_page_answers_its_targets(tmp_path):
    pages = shipped()

    async def go():
        lib = library(tmp_path, ("agents", "workbench"))
        for folder in ("jobs", "speech"):
            (lib / folder).symlink_to(PLUGINS / folder, target_is_directory=True)
        cfg = Config(data=tmp_path / "data", band=BAND, library=lib, users={"bake": {"preset": "owner"}},
                     doors={"http": {"bind": "127.0.0.1:0"}})
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        tokens = {"bake": (cfg.data / "owner.token").read_text().strip()}
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        try:
            for pid, file in pages.items():
                got = await c.call("page.get", {"page": pid})
                assert got["ok"], (pid, got)
                names = got["result"]["targets"]
                assert names, f"{pid} marks nothing an agent can point at"
                text = file.read_text()
                page = pid.partition("/")[2]
                for name in names:
                    assert SHAPE.fullmatch(name), (pid, name)
                    if pid != "setting/settings":
                        assert name.split(".")[0] == page, (pid, name)
                    if "<...>" not in name:
                        assert f'"{name}"' in text, (pid, name)
        finally:
            await hub.stop()
    run(asyncio.wait_for(go(), 60))


def test_a_made_page_answers_what_it_marks(tmp_path):
    async def go():
        hub, tokens = await start(tmp_path)
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        try:
            html = '<button data-target="mine.go">Go</button><select data-target="mine.pick"></select>'
            assert (await c.call("page.create", {"name": "mine", "title": "Mine", "note": "A test.", "html": html}))["ok"]
            got = await c.call("page.get", {"page": "page/mine"})
            assert got["result"]["targets"] == ["mine.go", "mine.pick"]
        finally:
            await hub.stop()
    run(asyncio.wait_for(go(), 30))
