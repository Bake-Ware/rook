"""screenshot.capture_region takes negative x/y: on a multi-monitor desktop a
screen left of or above the primary one has negative virtual coordinates."""

from __future__ import annotations

import pytest

from rook.core import pluginkit as kit
from rook.worker.plugins import screenshot


@pytest.fixture
def seen(monkeypatch):
    got = []

    async def dispatch(quality, region):
        got.append(region)
        return {"ok": True}
    monkeypatch.setattr(screenshot, "_dispatch", dispatch)
    return got


@pytest.mark.asyncio
async def test_negative_offsets_reach_the_backend(seen):
    p = screenshot.ScreenshotPlugin()
    await p._capture_region(x=-1920, y=-200, w=800, h=600)
    assert seen == [(-1920, -200, 800, 600)]


@pytest.mark.asyncio
async def test_size_must_still_be_positive(seen):
    p = screenshot.ScreenshotPlugin()
    for w, h in ((0, 10), (10, -1)):
        with pytest.raises(kit.CapError):
            await p._capture_region(x=0, y=0, w=w, h=h)
    assert seen == []


@pytest.mark.asyncio
async def test_imagemagick_gets_a_signed_crop_offset(monkeypatch):
    calls = []

    async def run(cmd, timeout=None):
        calls.append(cmd)
        return 1, "", "no"
    monkeypatch.setattr(screenshot, "has_display", lambda: True)
    monkeypatch.setattr(screenshot, "which", lambda name: name == "import")
    monkeypatch.setattr(screenshot, "run", run)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(kit.CapError):
        await screenshot._linux_capture(85, (-1920, 40, 800, 600))
    assert "800x600-1920+40" in calls[0]
