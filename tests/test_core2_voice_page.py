"""The Voice page (the speech plugin) on a hub with the fake speech engine:
it is listed under Agents and declares only caps that exist; the calls it
makes work through its grants (opened as the site opens it); the voice it
sets is the caller's own; "Hear it" gets playable audio. In a real browser
(Playwright's Chromium): pick a voice, hear it, flip the controls, at desktop
and phone width, and a hub with no speech engine says what that means."""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from speech_fakes import BRAIN, BRAIN_PY, ENGINE, ENGINE_PY, plugin  # noqa: E402
from tests.test_core2_dashboard import Client, run  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "rook/plugins/speech/pages/voice.html"
BAND = "e17904e6ed4c664592f64f6f1d02c682"


def library(tmp_path, engine=True):
    lib = tmp_path / "lib"
    lib.mkdir(parents=True)
    if engine:
        plugin(lib, "speech-engine", ENGINE, "engine.py", ENGINE_PY)
    plugin(lib, "agents-local", BRAIN, "brain.py", BRAIN_PY)
    for name in ("speech", "agents"):
        (lib / name).symlink_to(REPO / "rook/plugins" / name, target_is_directory=True)
    return lib


async def start(tmp_path, engine=True):
    cfg = Config(data=tmp_path / "data", band=BAND, library=library(tmp_path, engine),
                 users={"bake": {"preset": "owner"}, "sam": {"preset": "adult"}},
                 settings={"agent": {"warm": "5s"}}, doors={"http": {"bind": "127.0.0.1:0"}})
    hub = Hub(cfg, transport=Loopback())
    await hub.start()
    c = Client(hub)
    c.owner = (cfg.data / "owner.token").read_text().strip()
    await c.sign_in(c.owner)
    return hub, c


async def direct(c, cap, token=None, **args):
    """A call outside the page, as the owner (or ``token``'s person)."""
    out = (await c.json("POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": "Check the Voice page."},
                        bearer=token or c.owner))[1]
    assert out["ok"], out
    return out["result"]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_voice_page_script_parses(tmp_path):
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S).group(1)
    (tmp_path / "page.js").write_text(script)
    subprocess.run(["node", "--check", str(tmp_path / "page.js")], check=True)


def test_the_voice_page(tmp_path):
    async def go():
        hub, c = await start(tmp_path)
        try:
            sidebar = await c.call("page.list")
            assert sidebar["ok"], sidebar
            page = next(p for s in sidebar["result"]["segments"] for p in s["pages"] if p["page"] == "speech/voice")
            assert page["segment"] == "Agents" and page["title"] == "Voice"
            served = hub.dispatcher.workers[hub.node].caps        # voice.tts: the hub's contract cap
            assert set(page["caps"]) <= set(served), set(page["caps"]) - set(served)
            html = PAGE.read_text()
            assert set(re.findall(r'rook\.call\("([a-z.]+)"', html)) == set(page["caps"])
            assert 'rook.open("setting/settings")' in html

            token = (await c.open("speech/voice"))["token"]

            async def ok(cap, **args):
                out = await c.page(token, cap, args)
                assert out["ok"], (cap, out)
                return out["result"]

            # What the page reads.
            mine = {s["key"]: s for s in (await ok("setting.list", plugin="speech"))["settings"]}
            assert mine["voice"]["where"] == "person" and mine["voice"]["value"] == ""
            assert mine["agent"]["value"] == "house" and mine["talker"]["value"] is True and mine["talker"]["doc"]
            status = await ok("speech.status")
            assert status["engines"] == {"voice.stt": ["speech-engine.stt"], "voice.tts": ["speech-engine.tts"],
                                         "voice.turn": ["speech-engine.turn"],
                                         "voice.speaker": ["speech-engine.speaker"]}
            assert (await ok("speech.voices"))["voices"]["speech-engine"] == {"voices": ["af_heart"],
                                                                             "default": "af_heart"}
            titles = {m["plugin"]: m["title"] for m in (await ok("hub.manifests", section="title"))["manifests"]}
            assert titles["speech"] == "Speech" and "speech-engine" not in titles      # the fake has no title
            hub_row = next(w for w in (await ok("hub.workers"))["workers"] if w.get("hub"))
            assert "speech-engine" in hub_row["plugins"] and hub_row["online"]
            assert [a["name"] for a in await ok("agent.list", limit=500)][:1] == ["house"]

            # Your voice, then hear it: the contract answers a playable file.
            await ok("setting.set", plugin="speech", key="voice", value="af_heart")
            spoken = await ok("voice.tts", text="Hello.", voice="af_heart")
            assert spoken["mime"] == "audio/wav" and base64.b64decode(spoken["data"]) == b"RIFF:af_heart:Hello."
            # The voice is the caller's own: Sam's is untouched.
            mine = {s["key"]: s for s in (await ok("setting.list", plugin="speech"))["settings"]}
            assert mine["voice"]["value"] == "af_heart"
            sams = {s["key"]: s for s in (await direct(c, "setting.list", plugin="speech", person="sam"))["settings"]}
            assert sams["voice"]["value"] == "" and sams["voice"]["person"] == "person:sam"
            sam = hub.add_token(Caller(person="person:sam"))
            got = await direct(c, "setting.list", sam, plugin="speech")
            assert next(s for s in got["settings"] if s["key"] == "voice")["value"] == ""

            # Who answers.
            await ok("setting.set", plugin="speech", key="talker", value=False)
            await ok("setting.set", plugin="speech", key="agent", value="house")
            assert (await ok("speech.status"))["talker"] is False
        finally:
            await hub.stop()
    run(go())


async def _until(page, expr, seconds=15):
    deadline = time.monotonic() + seconds
    while not await page.evaluate(expr):
        if time.monotonic() > deadline:
            raise AssertionError(f"not in time: {expr}")
        await page.wait_for_timeout(100)


def test_the_voice_page_in_a_browser(tmp_path):
    pw = pytest.importorskip("playwright.async_api")

    async def look(c, browser, name, width, height):
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        key, _, value = c.cookie.partition("=")
        await ctx.add_cookies([{"name": key, "value": value, "url": f"http://127.0.0.1:{c.port}"}])
        shell = await ctx.new_page()
        await shell.goto(f"http://127.0.0.1:{c.port}/#/speech/voice")
        await _until(shell, "typeof current !== 'undefined' && current && current.page === 'speech/voice'")
        frame = shell.frame_locator("#stage iframe")
        await frame.locator("#contracts b").first.wait_for(timeout=10000)
        return ctx, shell, frame

    async def go():
        hub, c = await start(tmp_path / "with")
        try:
            async with pw.async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as x:
                    pytest.skip(f"no Chromium: {x}")
                for name, width, height in (("desktop", 1280, 900), ("phone", 390, 844)):
                    ctx, shell, frame = await look(c, browser, name, width, height)
                    text = await frame.locator("#contracts").inner_text()
                    assert "Hearing you" in text and "speech-engine on the hub: reachable now." in text, text
                    assert "cannot" not in text
                    # Pick a voice: saved as your own.
                    await frame.locator("#voice").select_option("af_heart")
                    await frame.locator("#voice-status.ok").wait_for(timeout=10000)
                    assert (await frame.locator("#voice option:checked").inner_text()) == "af_heart (its default)"
                    # Hear it: the audio element gets the returned audio as its source.
                    await frame.locator("#hear").click()
                    audio = frame.locator("#audio")
                    await audio.wait_for(state="visible", timeout=10000)
                    src = await audio.get_attribute("src")
                    assert src.startswith("data:audio/wav;base64,")
                    assert base64.b64decode(src.split(",", 1)[1]).startswith(b"RIFF:af_heart:Hello")
                    # Who answers: quick answers off, then on again; the agent dropdown.
                    await frame.locator("#talker").uncheck()
                    await frame.locator("#answer-status.ok").wait_for(timeout=10000)
                    assert (await direct(c, "speech.status"))["talker"] is False
                    await frame.locator("#talker").check()
                    deadline = time.monotonic() + 10
                    while not (await direct(c, "speech.status"))["talker"]:
                        assert time.monotonic() < deadline, "quick answers didn't turn back on"
                        await shell.wait_for_timeout(100)
                    await frame.locator("#agent").select_option("house")
                    await frame.locator("#answer-status.ok").wait_for(timeout=10000)
                    # Back to the provider's default; More settings opens the Settings page.
                    await frame.locator("#voice").select_option("")
                    await frame.locator("#voice-status.ok").wait_for(timeout=10000)
                    await frame.locator("#more").click()
                    await _until(shell, "current && current.page === 'setting/settings'")
                    await ctx.close()
                await browser.close()
        finally:
            await hub.stop()

        hub, c = await start(tmp_path / "without", engine=False)
        try:
            async with pw.async_playwright() as p:
                browser = await p.chromium.launch()
                for name, width, height in (("desktop", 1280, 900), ("phone", 390, 844)):
                    ctx, shell, frame = await look(c, browser, name, width, height)
                    text = await frame.locator("#contracts").inner_text()
                    assert "Rook cannot hear you yet: no speech engine is on the band." in text, text
                    assert "Rook cannot speak yet" in text and "every pause" in text
                    await frame.locator("#hear").click()
                    await frame.locator("#voice-status.bad").wait_for(timeout=10000)
                    await ctx.close()
                await browser.close()
        finally:
            await hub.stop()
    run(go(), 120)
