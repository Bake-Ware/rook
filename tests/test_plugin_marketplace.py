"""Marketplace preview: the catalog format, the bundled example and the
install stub (docs/design/plugin-marketplace.md)."""

from __future__ import annotations

import json

import pytest

from tests.plugins_fakes import band_env, make_node
from rook.hub import plugin_catalog


# -- marketplace ------------------------------------------------------------------------

def test_catalog_format_and_example():
    cat = plugin_catalog.example_catalog()
    assert cat["example"] and len(cat["plugins"]) == 3 and cat["errors"] == []
    assert all("EXAMPLE" in p["description"] for p in cat["plugins"])
    assert all("example." in p["homepage"] for p in cat["plugins"])
    good = json.loads(plugin_catalog.EXAMPLE_PATH.read_text())["plugins"][1]
    bad = [dict(good, id="Not An Id"),
           dict(good, id="com.example.nosha", package={"kind": "wheel", "url": "https://example.com/x.whl"}),
           dict(good, id="com.example.http", homepage="http://example.com"),
           dict(good, id="com.example.ns", caps=[{"cap": "other.x", "risk": "read"}]),
           dict(good, id="com.example.pin", package={"kind": "pip", "requirement": "unpinned"}),
           dict(good, id="com.example.future", min_core_api="9.0"),
           "junk"]
    parsed = plugin_catalog.parse_catalog({"format": plugin_catalog.FORMAT, "plugins": bad})
    by_id = {p["id"]: p for p in parsed["plugins"]}
    assert "com.example.future" in by_id and not by_id["com.example.future"]["compatible"]
    assert any("sha256" in e for e in parsed["errors"])
    assert any("https" in e for e in parsed["errors"])
    assert any("outside namespace" in e for e in parsed["errors"])
    assert any("pin a version" in e for e in parsed["errors"])
    assert any("not an object" in e for e in parsed["errors"])
    with pytest.raises(ValueError):
        plugin_catalog.parse_catalog({"format": "something-else"})
    with pytest.raises(ValueError):
        plugin_catalog.fetch("http://example.com/catalog.json")
    cache = plugin_catalog.CatalogCache(fetcher=lambda url: (_ for _ in ()).throw(OSError("down")))
    got = cache.load(["https://example.com/c.json"])
    assert got[0]["plugins"] == [] and "down" in got[0]["errors"][0]
    assert cache.load([])[0]["example"]


@pytest.mark.asyncio
async def test_marketplace_caps_list_and_refuse_to_install(tmp_path, monkeypatch):
    band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path)
    await node.start()
    listed = await node.dispatch("plugin.marketplace.list", {"fields": "*"})
    assert listed["ok"] and {e["id"] for e in listed["result"]} >= {"com.example.weather"}
    r = await node.dispatch("plugin.marketplace.install", {"id": "com.example.weather"})
    assert not r["ok"] and "not implemented" in r["error"] and "rook.plugins" in r["error"]
    await node.stop()
