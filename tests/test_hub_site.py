"""Hub plugins serving paths on the hub site (rook/hub/site.py), plain-words
reasons for a plugin that is not available, and a retry when the setting it
waits for changes (rook/core/host.py)."""

from __future__ import annotations

import asyncio

import pytest

from rook.core.plugin import Plugin, place, setting
from rook.hub.site import SiteError, SiteMounts


async def _h(request):
    return "h"


def test_mounts_match_prefixes_and_exact_aliases():
    site = SiteMounts()

    async def a(request):
        return "a"

    async def b(request):
        return "b"
    site.mount("plugin:x", "/voice", a)
    site.mount("plugin:y", "/voice/admin", b)
    site.alias("plugin:x", "/api/transcribe", b)
    assert site.match("/voice") is a and site.match("/voice/ws") is a
    assert site.match("/voice/admin/api/keys") is b          # longest prefix wins
    assert site.match("/voicemail") is None                 # a prefix is a path segment
    assert site.match("/api/transcribe") is b and site.match("/api/transcribe/x") is None
    assert {r["path"] for r in site.describe()} == {"/voice/*", "/voice/admin/*",
                                                     "/api/transcribe"}
    assert site.unmount("plugin:x") == 2
    assert site.match("/voice/ws") is None and site.match("/voice/admin") is b


@pytest.mark.parametrize("path", ["/account", "/account/plugins/api", "/mcp", "/band",
                                  "/api/chat/send", "/.well-known/x", "/", "", "voice",
                                  "/a b", "/../etc", "/apk", "/apk.json", "/band-worker.pyz",
                                  "/band-worker.json", "/rook.py", "/worker", "/enroll",
                                  "/hub", "/permissions", "/api/policy", "/health",
                                  "/telesthete-hub"])
def test_dashboard_paths_and_bad_paths_are_refused(path):
    with pytest.raises(SiteError):
        SiteMounts().mount("plugin:x", path, _h)


def test_dot_segments_never_reach_a_mount():
    site = SiteMounts()
    site.mount("plugin:x", "/voice", _h)
    site.alias("plugin:x", "/voices", _h)
    for path in ("/voice/../escape", "/voice/../../x", "/voice/./ws", "/voice/a/..",
                 "/voice/.."):
        assert site.match(path) is None, path
    assert site.match("/voice/.well-hidden") is _h          # a dot in a name is fine
    assert site.match("/voice/ws") is _h


def test_serving_is_said_by_rook_hub_serve(monkeypatch):
    from rook.hub import site as site_mod
    monkeypatch.setattr(site_mod, "_SERVED", False)
    assert site_mod.serving() is False                       # two processes: nothing serves
    site_mod.enable_serving()
    assert site_mod.serving() is True


def test_another_owner_cannot_take_a_path():
    site = SiteMounts()
    site.mount("plugin:x", "/voice", _h)
    site.mount("plugin:x", "/voice/", _h)                    # the same owner again: fine
    with pytest.raises(SiteError, match="already served by plugin:x"):
        site.mount("plugin:y", "/voice", _h)


class Waits(Plugin):
    """A plugin that needs an address before it can load."""
    NAMESPACE = "waits"
    PLACEMENT = place(None)
    SETTINGS = (setting("address", "url", ""),)

    def available(self) -> bool:
        return bool(self.settings.get("address"))

    def unavailable_reason(self) -> str:
        return "Set the address first."


class Silent(Plugin):
    NAMESPACE = "silent"
    PLACEMENT = place(None)

    def available(self) -> bool:
        return False


def _host(stored):
    from rook.core.host import PluginHost
    from rook.core.plugin import Candidate
    host = PluginHost(stored_settings=lambda ns: stored.get(ns, {}))
    host.load([Candidate("waits", "package:test", lambda: Waits),
               Candidate("silent", "package:test", lambda: Silent)])
    return host


@pytest.mark.asyncio
async def test_unavailable_reason_and_retry_when_its_setting_changes():
    stored: dict = {}
    host = _host(stored)
    await host.start()
    assert host.status["waits"] == {"state": "unavailable", "reason": "Set the address first.",
                                    "namespace": "waits"}
    # A plugin without a reason keeps the generic note.
    assert host.status["silent"]["reason"] == "available() returned False"
    assert host.retry_unavailable("waits") == ["waits"]
    await asyncio.sleep(0.05)
    assert host.loaded("waits") is None                      # still nothing set
    stored["waits"] = {"address": "http://192.0.2.10:1234"}
    assert host.retry_unavailable("waits") == ["waits"]
    for _ in range(50):
        if host.loaded("waits") is not None:
            break
        await asyncio.sleep(0.01)
    assert host.loaded("waits") is not None
    assert host.retry_unavailable("waits") == []             # loaded: nothing to retry
    assert host.retry_unavailable("nothing") == []
    await host.stop()


@pytest.mark.asyncio
async def test_an_operator_switched_off_plugin_is_never_retried(tmp_path):
    stored = {"waits": {"address": "http://192.0.2.10:1234"}}
    host = _host(stored)
    await host.start()
    assert host.loaded("waits") is not None
    await host.disable("waits")
    assert host.retry_unavailable("waits") == []
    await host.stop()
