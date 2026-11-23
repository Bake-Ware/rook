"""Every screen of the app at 1280x800, headless (SDL's dummy video driver),
as PNGs: python tests/render_screens.py <out dir>. Uses a temporary data
folder with sample profiles (one copied from the bridge with IR keys)."""

import os
import sys
import tempfile
import time

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="rookmote-render-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pygame  # noqa: E402

from rookmote import app as A  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out, exist_ok=True)
pygame.init()
screen = pygame.display.set_mode((A.W, A.H))
app = A.App(screen, headless=True)
st = app.store
nec = lambda c: {"protocol": "nec", "address": 51178, "command": c}  # noqa: E731
st.save("LG TVs", {"power": nec(8), "vol_up": nec(2), "vol_down": nec(3), "play": {"rook": True}}, icon="tv", set_icon=True)
st.save("Steam Deck", {k: {"rook": True} for k in ("up", "down", "left", "right", "ok", "back", "home", "play")},
        icon="game", set_icon=True)
st.save("Living room Roku", {}, link="roku@192.168.1.20", set_link=True, icon="streaming", set_icon=True)
st.save("Soundbar", {"vol_up": {"cap": "media.volume", "args": {"step": 2}}, "mute": {"lan": True}},
        link="webos@192.168.1.31", set_link=True, icon="speaker", set_icon=True)
st.select("Steam Deck")
app.hub_ok = True


def shot(name):
    app.render()
    pygame.image.save(screen, os.path.join(out, name + ".png"))
    print(name)


app.key("ok")
app.last["at"] = time.time() - 5
shot("01-home-rook")
app.store.select("LG TVs")
app.key("vol_up")
shot("02-home-ir-unavailable-fresh")
app.hub_ok = False
app.key("play")
app.last["at"] = time.time() - 5
shot("03-home-no-hub")
app.hub_ok = True
app.store.select("Living room Roku")
app.last = {"key": "down", "via": "lan", "ok": True, "text": "down", "at": time.time() - 5}
shot("04-home-wifi")
app.key("info")
app.store.select(None)
app.last = {"key": "1", "via": "none", "ok": True, "text": "nothing for 1", "at": time.time() - 5}
shot("05-home-no-profile")
app.tab = 1
app.sel["Devices"] = 1
shot("06-devices")
app.modal = ("options", {"profile": "LG TVs", "items": ["Rename", "Icon", "Delete"], "sel": 1})
shot("07-device-options")
app.modal = ("icons", {"profile": "LG TVs", "sel": 4})
shot("08-icon-picker")
app.modal = ("confirm", {"profile": "LG TVs", "sel": 1})
shot("09-confirm-delete")
app.modal = ("osk", {"title": "Name the new device", "text": "Bedroom T", "row": 2, "col": 3, "done": lambda t: None})
shot("10-keyboard")
app.modal = None
app.tab = 2
app.chat = [("you", "turn the living room lights down"), ("agent", "Done: the living room lights are at 30 percent."),
            ("note", "voice: 3.1 s"), ("you", "and play some jazz on the Roku"),
            ("agent", "Playing a jazz station on the living room Roku. Anything else?")]
shot("11-assistant")
app.tab = 3
shot("12-games")
app.tab = 4
shot("13-settings")
app.modal = ("keymap", {})
shot("14-button-map")
app.modal = ("devs", {"sel": 0, "busy": False, "list": [
    {"name": "Living room Roku", "link": "roku@192.168.1.20", "driver": "roku"},
    {"name": "LG TV 192.168.1.31", "link": "webos@192.168.1.31", "driver": "webos"}]})
shot("15-find-devices")
app.modal = None
app.tab = 0
app.display = ("The hub says: dinner is ready. Pause the game?", 0)
shot("16-display-text")
pygame.quit()
