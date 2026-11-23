"""The Rook remote on the Steam Deck: fullscreen 1280x800, the Deck's buttons
and touchscreen. The structure is the firmware remote's: Home (the picked
device, its routes, the last key in its box) where the buttons are keys, and
the menu's tabs (Devices, Assistant, Games, Settings) that View opens, as
Guide does on the bridge.

Leaving: hold View and Menu together for a second, Settings > Exit, or the
close button at the top right. When the window loses focus it ignores input
(and SDL is told not to deliver controller events in the background).
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "0")   # not ours when not focused
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

from . import VERSION, bridge, draw as D, keys as K, lan, router, voice  # noqa: E402
from .profiles import ICONS, ProfileError, Store, routes  # noqa: E402

W, H = 1280, 800
TABS = ["Home", "Devices", "Assistant", "Games", "Settings"]
TAB_ICONS = ["rook", "tv", "speaker", "game", "generic"]
FRESH_S = 0.6
HOLD_EXIT_S = 1.0
REPEAT_AFTER, REPEAT_EVERY = 0.40, 0.12

# SDL controller button ids -> names (keys.DEFAULT_MAP's).
_CB = {}


def _controller_names():
    if _CB:
        return _CB
    for n in ("a", "b", "x", "y", "back", "guide", "start", "leftstick", "rightstick", "leftshoulder",
              "rightshoulder", "dpad_up", "dpad_down", "dpad_left", "dpad_right"):
        c = getattr(pygame, "CONTROLLER_BUTTON_" + n.upper(), None)
        if c is not None:
            _CB[c] = n.replace("dpad_", "dp")
    return _CB


# Keyboard, for a desk (and the Deck's own keyboard): to the same names.
_KB = {pygame.K_UP: "dpup", pygame.K_DOWN: "dpdown", pygame.K_LEFT: "dpleft", pygame.K_RIGHT: "dpright",
       pygame.K_RETURN: "a", pygame.K_ESCAPE: "b", pygame.K_BACKSPACE: "b", pygame.K_x: "x", pygame.K_y: "y",
       pygame.K_TAB: "back", pygame.K_SPACE: "start", pygame.K_q: "leftshoulder", pygame.K_e: "rightshoulder",
       pygame.K_1: "lefttrigger", pygame.K_3: "righttrigger"}

# The touch keypad: every key the firmware names, laid out as a remote.
# (labels in angle brackets are drawn: draw.glyph)
KEYPAD = [
    [("power", "Power"), ("input", "Input"), ("info", "Info"), ("exit", "Exit")],
    [("home", "Home"), ("menu", "Menu"), ("back", "Back")],
    [("vol_up", "Vol +"), ("mute", "Mute"), ("ch_up", "Ch +")],
    [("vol_down", "Vol -"), ("stop", "<stop>"), ("ch_down", "Ch -")],
    [("prev", "<prev>"), ("rewind", "<rewind>"), ("play", "<play>"), ("pause", "<pause>"), ("forward", "<forward>"),
     ("next", "<next>")],
]
DIGITS = [["1", "2", "3", "4", "5"], ["6", "7", "8", "9", "0"]]
OSK = ["1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm-_.", " "]


class App:
    def __init__(self, screen: pygame.Surface, headless: bool = False):
        self.s = screen
        self.headless = headless
        self.store = Store()
        self.tab = 0
        self.sel = {t: 0 for t in TABS}
        self.modal = None                 # ("options"|"confirm"|"icons"|"osk"|"keymap"|"devs"|"text", data)
        self.last = None                  # {key, via, ok, text, at}
        self.toast = None                 # (text, until)
        self.display = None               # (text, until or 0)
        self.chat = []                    # [(who, text)]
        self.chat_status = ""
        self.chat_scroll = 0
        self.hub_ok = False
        self.focused = True
        self.held = {}                    # button -> (down_at, next_repeat)
        self.taps = []                    # [(rect, fn)] this frame's touch targets
        self.devs = []                    # Find devices' results
        self.quit = False
        self.trigger_down = {"lefttrigger": False, "righttrigger": False}
        self.controllers = []
        self.stick = [0.0, 0.0]
        self.client = None if headless else bridge.Client(self._from_plugin, hello={"version": VERSION})
        self.inbox = []
        self.rec = None                   # voice.Recording while the microphone is open
        self.voice_id = None
        self.thinking_until = 0.0
        self.chat_max = 0                 # how far back the conversation scrolls (set when drawn)
        self.drag_y = None                # a finger scrolling the conversation
        self.game = None                  # a game on screen (games/): it gets the buttons, the screen, the frames
        self.audio = None                 # games/audio.Audio, made with the first game

    # -- the plugin --------------------------------------------------------------------

    def _from_plugin(self, msg: dict) -> None:
        self.inbox.append(msg)            # handled on the UI thread (step)

    def _handle_plugin(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "hub":
            self.hub_ok = bool(msg.get("ok"))
        elif t == "press" and K.valid_key(msg.get("button") or ""):
            self.key(msg["button"], source="band")
        elif t == "chat":
            if msg.get("clear"):
                self.chat = []
            if msg.get("text"):
                if msg.get("who") in ("agent", "note") and self.thinking_until:
                    self.thinking_until = 0.0
                    self.chat_status = ""
                self.chat.append((msg.get("who") or "agent", msg["text"]))
                self.chat_scroll = 0
                self.tab = TABS.index("Assistant")
            if msg.get("status") is not None:
                self.chat_status = msg.get("status") or ""
        elif t == "display":
            secs = int(msg.get("seconds") or 0)
            self.display = (str(msg.get("text") or ""), time.time() + secs if secs else 0) if msg.get("text") else None
        elif t == "profiles":
            self.store.reload(force=True)
        elif t == "_voice":                       # the recording ended (from its thread)
            self.voice_done(msg["summary"])

    def emit(self, event: str, payload: dict) -> bool:
        return bool(self.client and self.client.send({"t": "event", "event": event, "payload": payload}))

    def push_state(self) -> None:
        if not self.client:
            return
        p = self.store.current()
        self.client.send({"t": "state", "state": {
            "screen": "game" if self.game else "remote" if self.tab == 0 else "menu", "tab": TABS[self.tab],
            "profile": p["name"] if p else None,
            "last": {k: self.last[k] for k in ("key", "via")} if self.last else None}})

    # -- keys --------------------------------------------------------------------------

    def key(self, name: str, source: str = "deck") -> None:
        """A key, as the firmware's on_button: through the picked profile, then the event."""
        name = K.canonical(name)
        via, action, p = router.route(self.store, name)
        ok = True
        text = name
        if via == "ir":
            text, ok = f"{name}: no IR on the Deck", False
        elif via == "lan":
            router.lan_send(p, action, name, lambda err: err and self.say(err))
        elif via == "none":
            text = f"nothing for {name}"
        if via in ("cap", "rook", "hub"):
            ok = self.hub_ok
        if via != "ir":
            self.emit("remote.button", router.payload(name, via, action, p))
        self.last = {"key": name, "via": via, "ok": ok, "text": text, "at": time.time()}
        self.push_state()

    def say(self, text: str, secs: float = 3.0) -> None:
        self.toast = (text, time.time() + secs)

    # -- buttons -----------------------------------------------------------------------

    def button(self, b: str) -> None:
        """A Deck button (DEFAULT_MAP's names), wherever the app is."""
        if self.modal:
            return self.modal_button(b)
        if b == "back":                   # View: the menu, or back to the remote
            self.tab = 0 if self.tab else max(1, TABS.index("Devices"))
            self.push_state()
            return
        if self.tab == 0:
            k = K.DEFAULT_MAP.get(b)
            if k:
                self.key(k)
            return
        if b in ("leftshoulder", "rightshoulder"):
            n = len(TABS) - 1
            self.tab = 1 + ((self.tab - 1) + (1 if b == "rightshoulder" else -1)) % n
            return
        getattr(self, "tab_" + TABS[self.tab].lower())(b)

    def _move(self, tab: str, n: int, b: str) -> None:
        if n <= 0:
            return
        if b == "dpup":
            self.sel[tab] = (self.sel[tab] - 1) % n
        elif b == "dpdown":
            self.sel[tab] = (self.sel[tab] + 1) % n

    # Devices: the profiles, then Add device. A picks, Y (the options key) offers Rename, Icon, Delete.
    def tab_devices(self, b: str) -> None:
        ps = self.store.profiles
        n = len(ps) + 1
        self.sel["Devices"] = min(self.sel["Devices"], n - 1)
        self._move("Devices", n, b)
        i = self.sel["Devices"]
        if b == "a":
            if i == len(ps):
                self.modal = ("osk", {"title": "Name the new device", "text": "", "done": self._add_profile,
                                      "row": 1, "col": 0})
            else:
                self.store.select(ps[i]["name"])
                self.tab = 0
                self.say(f"{ps[i]['name']}")
                self.push_state()
        elif b == "y" and i < len(ps):
            self.modal = ("options", {"profile": ps[i]["name"], "items": ["Rename", "Icon", "Delete"], "sel": 0})
        elif b == "b":
            self.tab = 0

    def _add_profile(self, name: str) -> None:
        try:
            p, created = self.store.save(name, {})
            self.store.select(p["name"])
            self.say(("added " if created else "have ") + p["name"])
        except ProfileError as e:
            self.say(str(e))

    def tab_assistant(self, b: str) -> None:
        if b == "dpup":
            self.scroll_chat(60)
        elif b == "dpdown":
            self.scroll_chat(-60)
        elif b == "a":
            self.talk()
        elif b == "b":
            if self.rec:
                self.rec.stop()
            self.tab = 0

    # -- voice: the microphone, as the firmware's Assistant key ------------------------
    def talk(self) -> None:
        """A: start listening; A again (or letting go after holding it) sends."""
        if self.rec:
            if time.monotonic() - self.rec.started > 0.4:
                self.rec.stop()
            return
        if not (self.client and self.client.connected):
            self.say("No worker: the remote-deck plugin isn't running")
            return
        uid = voice.new_id()

        def part(seq, data):
            self.emit("remote.voice.part", voice.part_event(uid, seq, data))
        try:
            self.rec = voice.Recording(part, lambda summary: self.inbox.append({"t": "_voice", "summary": summary}))
        except OSError as e:
            self.say(f"No microphone: {e}")
            return
        self.voice_id = uid
        self.chat_status = "Listening..."
        self.chat_scroll = 0

    def voice_done(self, summary: dict) -> None:
        uid, self.rec, self.voice_id = self.voice_id, None, None
        if summary.get("error") and not summary.get("parts"):
            self.chat_status = ""
            self.say(f"Microphone: {summary['error']}", 6)
        elif summary.get("parts") and uid:
            self.emit("remote.voice", voice.done_event(uid, summary))
            self.chat_status = "Thinking..."
            self.thinking_until = time.monotonic() + 75
        else:
            self.chat_status = ""
            self.say("Heard nothing")

    def scroll_chat(self, dy: float) -> None:
        self.chat_scroll = int(max(0, min(self.chat_max, self.chat_scroll + dy)))

    GAMES = ["Baketris", "2048", "Towers", "Swarm"]

    def tab_games(self, b: str) -> None:
        self._move("Games", len(self.GAMES), b)
        if b == "a":
            self.open_game(self.GAMES[self.sel["Games"]])
        elif b == "b":
            self.tab = 0

    def open_game(self, name: str) -> None:
        if name != "Baketris":
            self.say(f"{name} comes in the next update")
            return
        from .games import audio, baketris
        if self.audio is None:
            self.audio = audio.Audio(enabled=not self.headless)
        self.game = baketris.Baketris((W, H), self.audio, rumble=self._rumble)
        self.audio.warm(self.game.skin, min(self.game.music_sel, 3))
        self.held.clear()
        self.push_state()

    def _rumble(self, low: float, high: float, ms: int) -> None:
        for c in self.controllers:
            try:
                c.rumble(low, high, ms)
            except Exception:
                pass

    def close_game(self) -> None:
        self.game = None
        self.tab = TABS.index("Games")
        self.held.clear()
        self.push_state()

    SETTINGS = ["Find devices", "Button map", "About", "Exit"]

    def tab_settings(self, b: str) -> None:
        self._move("Settings", len(self.SETTINGS), b)
        if b == "a":
            what = self.SETTINGS[self.sel["Settings"]]
            if what == "Exit":
                self.quit = True
            elif what == "Button map":
                self.modal = ("keymap", {})
            elif what == "About":
                self.modal = ("text", {"title": "About", "lines": [
                    f"Rook remote for the Steam Deck {VERSION}",
                    "The firmware remote's structure, the Deck's buttons and touch.",
                    "Left out (the OS does them, or a Deck has none): IR, Wi-Fi setup,",
                    "updates over the air, Bluetooth pairing, sleep, the button learner."]})
            elif what == "Find devices":
                self.modal = ("devs", {"sel": 0, "busy": True, "list": []})
                import threading

                def scan():
                    found = lan.scan()
                    if self.modal and self.modal[0] == "devs":
                        self.modal[1].update({"busy": False, "list": found})
                threading.Thread(target=scan, daemon=True).start()
        elif b == "b":
            self.tab = 0

    def modal_button(self, b: str) -> None:
        kind, d = self.modal
        if kind == "osk":
            rows = OSK
            if b == "dpup":
                d["row"] = (d["row"] - 1) % len(rows)
            elif b == "dpdown":
                d["row"] = (d["row"] + 1) % len(rows)
            elif b in ("dpleft", "dpright"):
                d["col"] = (d["col"] + (1 if b == "dpright" else -1)) % len(rows[d["row"]])
            d["col"] = min(d["col"], len(rows[d["row"]]) - 1)
            if b == "a":
                d["text"] = (d["text"] + rows[d["row"]][d["col"]])[:20]
            elif b == "x":
                d["text"] = d["text"][:-1]
            elif b == "y":
                d["text"] = (d["text"][:1].upper() + d["text"][1:]) if d["text"] else d["text"]
            elif b == "start":
                self.modal = None
                if d["text"].strip():
                    d["done"](d["text"].strip())
            elif b == "b":
                self.modal = None
            return
        if kind == "options":
            if b in ("dpup", "dpdown"):
                d["sel"] = (d["sel"] + (1 if b == "dpdown" else -1)) % len(d["items"])
            elif b == "a":
                what = d["items"][d["sel"]]
                name = d["profile"]
                if what == "Rename":
                    self.modal = ("osk", {"title": f"Rename {name}", "text": name, "row": 1, "col": 0,
                                          "done": lambda t: self._try(lambda: self.store.rename(name, t), f"renamed {t}")})
                elif what == "Icon":
                    self.modal = ("icons", {"profile": name, "sel": 0})
                elif what == "Delete":
                    self.modal = ("confirm", {"profile": name, "sel": 0})
            elif b in ("b", "y"):
                self.modal = None
            return
        if kind == "icons":
            if b in ("dpleft", "dpright", "dpup", "dpdown"):
                step = {"dpleft": -1, "dpright": 1, "dpup": -3, "dpdown": 3}[b]
                d["sel"] = (d["sel"] + step) % len(ICONS)
            elif b == "a":
                name, ic = d["profile"], ICONS[d["sel"]]
                self._try(lambda: self.store.save(name, {}, icon=ic, set_icon=True), f"{name}: {ic}")
                self.modal = None
            elif b == "b":
                self.modal = None
            return
        if kind == "confirm":
            if b in ("dpup", "dpdown", "dpleft", "dpright"):
                d["sel"] ^= 1
            elif b == "a":
                if d["sel"] == 1:
                    self._try(lambda: self.store.delete(d["profile"]), f"deleted {d['profile']}")
                self.modal = None
            elif b == "b":
                self.modal = None
            return
        if kind == "devs":
            n = len(d["list"])
            if n and b in ("dpup", "dpdown"):
                d["sel"] = (d["sel"] + (1 if b == "dpdown" else -1)) % n
            elif b == "a" and n:
                self._adopt(d["list"][d["sel"]])
                self.modal = None
            elif b == "b":
                self.modal = None
            return
        if b in ("b", "a", "back"):
            self.modal = None

    def _try(self, fn, ok_text: str) -> None:
        try:
            fn()
            self.say(ok_text)
        except ProfileError as e:
            self.say(str(e))

    def _adopt(self, dev: dict) -> None:
        """A device found: its profile is picked, or one is made for it (linked), as the firmware's Find devs."""
        for p in self.store.profiles:
            if p.get("link") == dev["link"]:
                self.store.select(p["name"])
                self.say(p["name"])
                return
        name = dev["name"][:20]
        base, n = name, 2
        while self.store.find(name):
            name = f"{base[:17]} {n}"
            n += 1
        self._try(lambda: self.store.save(name, {}, link=dev["link"], set_link=True, icon="tv", set_icon=True),
                  f"added {name}")
        self.store.select(name)
        if dev["driver"] == "webos":
            self.say("Accept the prompt on the TV", 8)
            import threading
            threading.Thread(target=lambda: self._pair(dev["link"]), daemon=True).start()

    def _pair(self, link: str) -> None:
        try:
            lan.pair(link)
            self.say("paired")
        except (lan.LanError, OSError) as e:
            self.say(str(e))

    # -- events ------------------------------------------------------------------------

    def handle(self, e: pygame.event.Event) -> None:
        if e.type == pygame.QUIT:
            self.quit = True
        elif e.type in (pygame.WINDOWFOCUSLOST, getattr(pygame, "WINDOWMINIMIZED", -1)):
            self.focused = False
            self.held.clear()
            if self.game:
                self.game.host_pause()
        elif e.type in (pygame.WINDOWFOCUSGAINED, getattr(pygame, "WINDOWRESTORED", -2)):
            self.focused = True
        elif not self.focused:
            return
        elif self.game:
            self.handle_game(e)
        elif e.type == pygame.CONTROLLERDEVICEADDED:
            self._open_controllers()
        elif e.type == pygame.CONTROLLERBUTTONDOWN:
            n = _controller_names().get(e.button)
            if n and n != "guide":            # Steam's own
                self.down(n)
        elif e.type == pygame.CONTROLLERBUTTONUP:
            n = _controller_names().get(e.button)
            self.held.pop(n, None)
            if n == "a" and self.rec and time.monotonic() - self.rec.started > 0.6:
                self.rec.stop()                   # held while talking: letting go sends
        elif e.type == pygame.CONTROLLERAXISMOTION and e.axis in (pygame.CONTROLLER_AXIS_TRIGGERLEFT,
                                                                  pygame.CONTROLLER_AXIS_TRIGGERRIGHT):
            n = "lefttrigger" if e.axis == pygame.CONTROLLER_AXIS_TRIGGERLEFT else "righttrigger"
            on = e.value > 20000
            if on and not self.trigger_down[n]:
                self.down(n)
            elif not on and self.trigger_down[n]:
                self.held.pop(n, None)
            self.trigger_down[n] = on
        elif e.type == pygame.KEYDOWN and e.key in _KB:
            self.down(_KB[e.key])
        elif e.type == pygame.KEYUP and e.key in _KB:
            self.held.pop(_KB[e.key], None)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            self.tap(e.pos)
        elif e.type == pygame.FINGERDOWN:
            self.tap((int(e.x * W), int(e.y * H)))
            self.drag_y = e.y * H if TABS[self.tab] == "Assistant" else None
        elif e.type == pygame.FINGERMOTION and self.drag_y is not None:
            self.scroll_chat(e.y * H - self.drag_y)
            self.drag_y = e.y * H
        elif e.type == pygame.FINGERUP:
            self.drag_y = None
        elif e.type == pygame.MOUSEWHEEL and TABS[self.tab] == "Assistant":
            self.scroll_chat(e.y * 60)

    def handle_game(self, e: pygame.event.Event) -> None:
        """A game has the controls: its own mapping (the exit chord, View + Start held, still works)."""
        g = self.game
        if e.type == pygame.CONTROLLERDEVICEADDED:
            self._open_controllers()
        elif e.type == pygame.CONTROLLERBUTTONDOWN:
            n = _controller_names().get(e.button)
            if n and n != "guide":
                now = time.monotonic()
                self.held[n] = (now, now)
                g.pad_down(n)
        elif e.type == pygame.CONTROLLERBUTTONUP:
            n = _controller_names().get(e.button)
            self.held.pop(n, None)
            if n:
                g.pad_up(n)
        elif e.type == pygame.CONTROLLERAXISMOTION:
            if e.axis in (pygame.CONTROLLER_AXIS_TRIGGERLEFT, pygame.CONTROLLER_AXIS_TRIGGERRIGHT):
                g.trigger("lefttrigger" if e.axis == pygame.CONTROLLER_AXIS_TRIGGERLEFT else "righttrigger",
                          e.value > 16384)
            elif e.axis in (pygame.CONTROLLER_AXIS_LEFTX, pygame.CONTROLLER_AXIS_LEFTY):
                self.stick[0 if e.axis == pygame.CONTROLLER_AXIS_LEFTX else 1] = max(-1.0, e.value / 32767)
                g.stick_motion(*self.stick)
        elif e.type == pygame.KEYDOWN:
            g.key_down(e.key)
        elif e.type == pygame.KEYUP:
            g.key_up(e.key)
        elif e.type in (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP):
            x, y = e.x * W, e.y * H
            fn = {pygame.FINGERDOWN: g.touch_down, pygame.FINGERMOTION: g.touch_move, pygame.FINGERUP: g.touch_up}[e.type]
            fn(("finger", e.finger_id), x, y)
        elif e.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP) and e.button == 1 and not getattr(e, "touch", False):
            (g.touch_down if e.type == pygame.MOUSEBUTTONDOWN else g.touch_up)("mouse", *e.pos)
        elif e.type == pygame.MOUSEMOTION and e.buttons[0] and not getattr(e, "touch", False):
            g.touch_move("mouse", *e.pos)

    def down(self, b: str) -> None:
        now = time.monotonic()
        self.held[b] = (now, now + REPEAT_AFTER)
        self.button(b)

    def tap(self, pos) -> None:
        for r, fn in reversed(self.taps):
            if r.collidepoint(pos):
                fn()
                return

    def _open_controllers(self) -> None:
        try:
            from pygame._sdl2 import controller
            controller.init()
            self.controllers = [controller.Controller(i) for i in range(controller.get_count())
                                if controller.is_controller(i)]
        except Exception:
            self.controllers = []

    def step(self, dt: float = 1000 / 60) -> None:
        """Each frame: the plugin's messages, held buttons (repeat, and the exit chord), timers; a game's frame."""
        while self.inbox:
            self._handle_plugin(self.inbox.pop(0))
        self.store.reload()
        now = time.monotonic()
        if "back" in self.held and "start" in self.held:
            if now - max(self.held["back"][0], self.held["start"][0]) > HOLD_EXIT_S:
                self.quit = True
        if self.thinking_until and now > self.thinking_until:
            self.thinking_until = 0.0
            self.chat_status = "No answer came"
        if self.game:
            self.game.update(dt)
            if self.game.done:
                self.close_game()
            return
        if self.tab == 0 and not self.modal:
            for b, (t0, nxt) in list(self.held.items()):
                k = K.DEFAULT_MAP.get(b)
                if k in K.REPEATS and now >= nxt:
                    self.held[b] = (t0, now + REPEAT_EVERY)
                    self.key(k)
        elif self.tab and not self.modal:
            for b, (t0, nxt) in list(self.held.items()):
                if b in ("dpup", "dpdown") and now >= nxt:
                    self.held[b] = (t0, now + REPEAT_EVERY)
                    self.button(b)

    # -- drawing -----------------------------------------------------------------------

    def render(self) -> None:
        s = self.s
        self.taps = []
        if self.game:
            self.game.draw(s)
            return
        s.fill(D.BG)
        self.draw_top()
        self.draw_tabs()
        getattr(self, "draw_" + TABS[self.tab].lower())()
        if self.modal:
            getattr(self, "draw_modal_" + self.modal[0])(self.modal[1])
        now = time.time()
        if self.display and (not self.display[1] or now < self.display[1]):
            r = pygame.Rect(140, 200, 1000, 360)
            D.rounded(s, r, D.PANEL2, 18)
            D.rounded(s, r, D.ACCENT, 18, 3)
            y = r.y + 30
            for line in _wrap(self.display[0], D.font(40), r.w - 60)[:7]:
                D.text(s, line, (r.x + 30, y), 40)
                y += 44
            self.taps.append((r, lambda: setattr(self, "display", None)))
        if self.toast and now < self.toast[1]:
            img = D.font(32).render(self.toast[0][:90], True, D.TEXT)
            r = img.get_rect(midbottom=(W // 2, H - 24)).inflate(40, 22)
            D.rounded(s, r, D.PANEL2, 14)
            D.rounded(s, r, D.LINE, 14, 2)
            s.blit(img, img.get_rect(center=r.center))

    def draw_top(self) -> None:
        s = self.s
        pygame.draw.rect(s, D.PANEL, (0, 0, W, 60))
        D.icon(s, "rook", 18, 12, 36, D.ACCENT)
        D.text(s, "Rook remote", (64, 16), 34, D.TEXT, True)
        x = W - 70
        r = pygame.Rect(x, 8, 52, 44)                          # close
        D.rounded(s, r, D.PANEL2, 10)
        pygame.draw.line(s, D.DIM, (r.x + 16, r.y + 12), (r.right - 16, r.bottom - 12), 4)
        pygame.draw.line(s, D.DIM, (r.x + 16, r.bottom - 12), (r.right - 16, r.y + 12), 4)
        self.taps.append((r, lambda: setattr(self, "quit", True)))
        x -= 110
        D.text(s, time.strftime("%H:%M"), (x, 17), 34, D.TEXT)
        bat = _battery()
        if bat is not None:
            x -= 100
            D.text(s, f"{bat}%", (x + 40, 17), 32, D.DIM)
            pygame.draw.rect(s, D.DIM, (x, 22, 32, 18), 2, border_radius=3)
            pygame.draw.rect(s, D.GOOD if bat > 20 else D.BAD, (x + 3, 25, int(26 * bat / 100), 12))
        x -= 190
        D.icon(s, "rook", x, 14, 32, D.ACCENT if self.hub_ok else D.DIM, struck=not self.hub_ok)
        D.text(s, "on the band" if self.hub_ok else "no hub", (x + 40, 18), 30, D.TEXT if self.hub_ok else D.DIM)

    def draw_tabs(self) -> None:
        s = self.s
        pygame.draw.rect(s, D.BG, (0, 60, W, 64))
        tw = W // len(TABS)
        for i, (name, ic) in enumerate(zip(TABS, TAB_ICONS)):
            r = pygame.Rect(i * tw + 6, 66, tw - 12, 52)
            on = i == self.tab
            D.rounded(s, r, D.PANEL2 if on else D.PANEL, 12)
            if on:
                pygame.draw.rect(s, D.ACCENT, (r.x + 16, r.bottom - 5, r.w - 32, 4), border_radius=2)
            D.icon(s, ic, r.x + 18, r.y + 10, 32, D.ACCENT if on else D.DIM)
            D.text(s, name, (r.x + 62, r.y + 14), 32, D.TEXT if on else D.DIM, on)
            self.taps.append((r, lambda i=i: self._go_tab(i)))
        hint = "View: menu" if self.tab == 0 else "L1/R1: tabs · View: remote"
        D.text(s, hint, (W - 16, 128), 22, D.DIM, anchor="topright")

    def _go_tab(self, i: int) -> None:
        self.tab = i
        self.modal = None
        self.push_state()

    # Home: the device card (icon, name, its routes), the last key's box, the button map; the touch keys.
    def draw_home(self) -> None:
        s = self.s
        p = self.store.current()
        card = pygame.Rect(24, 150, 600, 200)
        D.rounded(s, card, D.PANEL, 18)
        if p:
            rt = routes(p)
            D.route_row(s, card.x + 24, card.y + 22, 56, p.get("icon") or "generic", rt)
            D.text(s, p["name"], (card.x + 24, card.y + 104), 72, D.TEXT, True, maxw=card.w - 48)
            if p.get("link"):
                D.text(s, p["link"], (card.x + 26, card.y + 160), 26, D.DIM)
        else:
            D.text(s, "no device picked", (card.x + 24, card.y + 40), 48, D.DIM)
            D.text(s, "keys go to the hub as they are (Devices picks one)", (card.x + 24, card.y + 104), 28, D.DIM)
        self.taps.append((card, lambda: self._go_tab(TABS.index("Devices"))))
        self.draw_keybox(pygame.Rect(24, 370, 600, 110))
        self.draw_legend(pygame.Rect(24, 500, 600, 280))
        self.draw_keypad(pygame.Rect(650, 150, 606, 630))

    def draw_keybox(self, r: pygame.Rect) -> None:
        """The last key, in its bordered box: the route's icon at the left, the key small beside it."""
        s = self.s
        L = self.last
        fresh = L and time.time() - L["at"] < FRESH_S
        D.rounded(s, r, D.ACCENT if fresh else D.PANEL, 16)
        D.rounded(s, r, D.ACCENT if fresh else D.LINE, 16, 2)
        ink = D.BG if fresh else D.TEXT
        if not L:
            D.text(s, "the last key shows here", (r.x + 24, r.centery), 30, D.DIM, anchor="midleft")
            return
        via = L["via"]
        ic = {"ir": "ir", "lan": "wifi", "cap": "rook", "rook": "rook", "hub": "rook"}.get(via)
        x = r.x + 24
        if ic:
            col = ink if fresh else D.ROUTE_COLORS["rook" if ic == "rook" else ic]
            D.icon(s, ic, x, r.centery - 32, 64, col, struck=not L["ok"])
            x += 88
        D.text(s, L["text"], (x, r.centery), 40, ink, anchor="midleft", maxw=r.right - x - 20)

    def draw_legend(self, r: pygame.Rect) -> None:
        s = self.s
        D.rounded(s, r, D.PANEL, 16)
        D.text(s, "Deck buttons", (r.x + 20, r.y + 14), 28, D.DIM)
        items = list(K.DEFAULT_MAP.items())
        col_w = r.w // 2
        for i, (b, k) in enumerate(items):
            x = r.x + 20 + (i // 8) * col_w
            y = r.y + 46 + (i % 8) * 25
            D.text(s, K.BUTTON_LABELS.get(b, b), (x, y), 26, D.ACCENT2)
            D.text(s, k, (x + 120, y), 26, D.TEXT)
        D.text(s, "View: menu · hold View + Menu: exit", (r.x + 20, r.bottom - 30), 24, D.DIM)

    def draw_keypad(self, r: pygame.Rect) -> None:
        s = self.s
        D.rounded(s, r, D.PANEL, 18)
        # the d-pad and ok, top left
        cx, cy, z = r.x + 124, r.y + 112, 62
        for k, dx, dy, label in (("up", 0, -1, "<up>"), ("down", 0, 1, "<down>"), ("left", -1, 0, "<left>"),
                                 ("right", 1, 0, "<right>"), ("ok", 0, 0, "OK")):
            self._keybtn(pygame.Rect(cx + dx * (z + 6) - z // 2, cy + dy * (z + 6) - z // 2, z, z), k, label)
        # rows of named keys, to the right of it and under it
        y = r.y + 18
        x0 = r.x + 262
        for row in KEYPAD[:2]:
            w = (r.right - 16 - x0 - 8 * (len(row) - 1)) // len(row)
            for i, (k, label) in enumerate(row):
                self._keybtn(pygame.Rect(x0 + i * (w + 8), y, w, 56), k, label)
            y += 66
        y = r.y + 228
        for row in KEYPAD[2:] + DIGITS:
            w = (r.w - 32 - 8 * (len(row) - 1)) // len(row)
            for i, item in enumerate(row):
                k, label = item if isinstance(item, tuple) else (item, item)
                self._keybtn(pygame.Rect(r.x + 16 + i * (w + 8), y, w, 66), k, label)
            y += 76

    def _keybtn(self, rect: pygame.Rect, key: str, label: str) -> None:
        s = self.s
        L = self.last
        hot = L and L["key"] == key and time.time() - L["at"] < FRESH_S
        D.rounded(s, rect, D.ACCENT if hot else D.PANEL2, 12)
        ink = D.BG if hot else D.TEXT
        if not (label.startswith("<") and D.glyph(s, label[1:-1], rect.center, min(rect.h, rect.w) * 0.42, ink)):
            D.text(s, label, rect.center, 32 if len(label) < 6 else 28, ink, anchor="center")
        self.taps.append((rect, lambda: self.key(key)))

    def _list(self, tab: str, rows: list, top: int = 150, row_h: int = 84, draw_row=None) -> None:
        s = self.s
        sel = min(self.sel[tab], max(0, len(rows) - 1))
        visible = (H - top - 20) // row_h
        first = max(0, sel - visible + 1)
        for i, row in enumerate(rows[first:first + visible]):
            idx = first + i
            r = pygame.Rect(24, top + i * row_h, W - 48, row_h - 10)
            on = idx == sel
            D.rounded(s, r, D.PANEL2 if on else D.PANEL, 14)
            if on:
                D.rounded(s, r, D.ACCENT, 14, 3)
            draw_row(r, row, on, idx)

            def tapped(idx=idx):
                self.sel[tab] = idx
                getattr(self, "tab_" + tab.lower())("a")
            self.taps.append((r, tapped))

    def draw_devices(self) -> None:
        ps = self.store.profiles
        cur = self.store.selected

        def row(r, p, on, idx):
            s = self.s
            if p is None:
                D.icon(s, "plus", r.x + 20, r.y + 12, 50, D.ACCENT)
                D.text(s, "Add device", (r.x + 90, r.centery), 40, D.TEXT, anchor="midleft")
                return
            D.icon(s, p.get("icon") or "generic", r.x + 20, r.y + 12, 50, D.TEXT)
            D.text(s, p["name"] + ("   ●" if p["name"] == cur else ""), (r.x + 90, r.centery), 40, D.TEXT,
                   anchor="midleft")
            rt = routes(p)
            w = D.route_row(pygame.Surface((1, 1)), 0, 0, 40, None, rt)
            D.route_row(s, r.right - 24 - w - 90, r.y + 17, 40, None, rt)
            ob = pygame.Rect(r.right - 84, r.y + 12, 64, r.h - 24)     # options, for touch
            D.rounded(s, ob, D.PANEL, 10)
            D.glyph(s, "more", ob.center, 30, D.DIM)
            self.taps.append((ob, lambda: (self.sel.__setitem__("Devices", idx), self.tab_devices("y"))))

        self._list("Devices", ps + [None], draw_row=row)
        D.text(self.s, "A: pick · Y: options (rename, icon, delete)", (W - 24, H - 34), 26, D.DIM, anchor="topright")

    def draw_assistant(self) -> None:
        s = self.s
        r = pygame.Rect(24, 150, W - 48, H - 240)
        D.rounded(s, r, D.PANEL, 18)
        clip = s.get_clip()
        s.set_clip(r.inflate(-8, -8))
        y0 = y = r.bottom - 24 + self.chat_scroll
        f = D.font(34)
        for who, text in reversed(self.chat[-60:]):
            lines = _wrap(text, f, int(r.w * 0.62))
            bh = len(lines) * 38 + 24
            bw = max(f.size(l)[0] for l in lines) + 36 if lines else 60
            y -= bh
            mine = who == "you"
            bx = r.right - 24 - bw if mine else r.x + 24
            if who == "note":
                bx = r.centerx - bw // 2
            br = pygame.Rect(bx, y, bw, bh)
            D.rounded(s, br, D.ACCENT if mine else D.PANEL2 if who == "agent" else D.PANEL, 16)
            for i, l in enumerate(lines):
                D.text(s, l, (br.x + 18, br.y + 12 + i * 38), 34, D.BG if mine else D.TEXT)
            y -= 14
        s.set_clip(clip)
        total = y0 - y                                      # the conversation's height
        self.chat_max = max(0, total - (r.h - 40))
        if self.chat_max:                                   # where in it: a thumb on the right
            th = max(40, int(r.h * (r.h - 40) / total))
            ty = r.bottom - 8 - th - int((r.h - 16 - th) * self.chat_scroll / self.chat_max)
            pygame.draw.rect(s, D.LINE, (r.right - 12, ty, 5, th), border_radius=3)
        if not self.chat:
            D.text(s, "Talk to the house agent: A starts, A again sends.", r.center, 34, D.DIM, anchor="center")
        status = self.chat_status or "A: talk · up/down or drag: read back"
        if self.rec:
            status = f"Listening... {self.rec.seconds:.1f} s"
            bar = pygame.Rect(W - 560, H - 66, 280, 26)
            D.rounded(s, bar, D.PANEL2, 8)
            D.rounded(s, (bar.x, bar.y, max(8, int(bar.w * self.rec.level)), bar.h), D.GOOD, 8)
        D.text(s, status, (32, H - 70), 30, D.ACCENT if self.rec else D.DIM)
        btn = pygame.Rect(W - 260, H - 82, 236, 60)
        D.rounded(s, btn, D.BAD if self.rec else D.ACCENT, 14)
        D.text(s, "Send" if self.rec else "Talk", btn.center, 36, D.BG, True, anchor="center")
        self.taps.append((btn, lambda: self.tab_assistant("a")))

    def draw_games(self) -> None:
        def row(r, g, on, idx):
            D.icon(self.s, "game", r.x + 20, r.y + 12, 50, D.ACCENT if idx == 0 else D.TEXT)
            D.text(self.s, g, (r.x + 90, r.centery), 40, D.TEXT, anchor="midleft")
        self._list("Games", self.GAMES, draw_row=row)

    def draw_settings(self) -> None:
        icons = {"Find devices": "wifi", "Button map": "game", "About": "generic", "Exit": "plus"}

        def row(r, what, on, idx):
            D.icon(self.s, icons[what], r.x + 20, r.y + 12, 50, D.TEXT)
            D.text(self.s, what, (r.x + 90, r.centery), 40, D.TEXT, anchor="midleft")
        self._list("Settings", self.SETTINGS, draw_row=row)

    # -- modals ------------------------------------------------------------------------

    def _panel(self, title: str, w=760, h=520) -> pygame.Rect:
        s = self.s
        shade = pygame.Surface((W, H), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 160))
        s.blit(shade, (0, 0))
        r = pygame.Rect((W - w) // 2, (H - h) // 2 + 20, w, h)
        D.rounded(s, r, D.PANEL, 20)
        D.rounded(s, r, D.LINE, 20, 2)
        D.text(s, title, (r.x + 28, r.y + 22), 40, D.TEXT, True, maxw=r.w - 56)
        return r

    def _menu(self, r, items, sel, top=100, on_tap=None):
        for i, it in enumerate(items):
            ir = pygame.Rect(r.x + 24, r.y + top + i * 72, r.w - 48, 62)
            D.rounded(self.s, ir, D.PANEL2 if i == sel else D.PANEL, 12)
            if i == sel:
                D.rounded(self.s, ir, D.ACCENT, 12, 3)
            D.text(self.s, it, (ir.x + 24, ir.centery), 36, D.TEXT, anchor="midleft")
            if on_tap:
                self.taps.append((ir, lambda i=i: on_tap(i)))

    def draw_modal_options(self, d) -> None:
        p = self.store.find(d["profile"])
        r = self._panel(d["profile"], 700, 420)
        if p:                              # the profile's pictures by its name
            D.route_row(self.s, r.right - 28 - D.route_row(pygame.Surface((1, 1)), 0, 0, 40, p.get("icon") or "generic",
                                                           routes(p)), r.y + 22, 40, p.get("icon") or "generic", routes(p))

        def tap(i):
            d["sel"] = i
            self.modal_button("a")
        self._menu(r, d["items"], d["sel"], on_tap=tap)
        self.taps.append((pygame.Rect(0, 0, W, H), lambda: None))   # taps outside do nothing

    def draw_modal_confirm(self, d) -> None:
        r = self._panel(f"Delete {d['profile']}?", 640, 300)

        def tap(i):
            d["sel"] = i
            self.modal_button("a")
        self._menu(r, ["No, keep it", f"Delete {d['profile']}"], d["sel"], on_tap=tap)

    def draw_modal_icons(self, d) -> None:
        r = self._panel(f"Icon for {d['profile']}", 760, 560)
        for i, name in enumerate(ICONS):
            cr = pygame.Rect(r.x + 40 + (i % 3) * 230, r.y + 100 + (i // 3) * 145, 210, 130)
            on = i == d["sel"]
            D.rounded(self.s, cr, D.PANEL2 if on else D.PANEL, 14)
            if on:
                D.rounded(self.s, cr, D.ACCENT, 14, 3)
            D.icon(self.s, name, cr.centerx - 36, cr.y + 10, 72, D.TEXT)
            D.text(self.s, name, (cr.centerx, cr.bottom - 26), 28, D.DIM, anchor="center")

            def tap(i=i):
                d["sel"] = i
                self.modal_button("a")
            self.taps.append((cr, tap))

    def draw_modal_osk(self, d) -> None:
        r = self._panel(d["title"], 1000, 600)
        tr = pygame.Rect(r.x + 28, r.y + 84, r.w - 56, 70)
        D.rounded(self.s, tr, D.BG, 12)
        D.text(self.s, d["text"] + "|", (tr.x + 20, tr.centery), 44, D.TEXT, anchor="midleft")
        for ri, row in enumerate(OSK):
            n = len(row)
            kw = 84 if n > 1 else 500
            x0 = r.centerx - (n * (kw + 8)) // 2
            for ci, ch in enumerate(row):
                kr = pygame.Rect(x0 + ci * (kw + 8), r.y + 176 + ri * 72, kw, 64)
                on = ri == d["row"] and ci == d["col"]
                D.rounded(self.s, kr, D.ACCENT if on else D.PANEL2, 10)
                D.text(self.s, "space" if ch == " " else ch, kr.center, 36, D.BG if on else D.TEXT, anchor="center")

                def tap(ri=ri, ci=ci):
                    d["row"], d["col"] = ri, ci
                    self.modal_button("a")
                self.taps.append((kr, tap))
        D.text(self.s, "A: type · X: delete · Y: capital · Menu: done · B: cancel", (r.centerx, r.bottom - 34), 28,
               D.DIM, anchor="center")
        done = pygame.Rect(r.right - 170, r.y + 84, 142, 70)
        D.rounded(self.s, done, D.ACCENT, 12)
        D.text(self.s, "Done", done.center, 36, D.BG, True, anchor="center")
        self.taps.append((done, lambda: self.modal_button("start")))

    def draw_modal_keymap(self, d) -> None:
        r = self._panel("Button map", 760, 620)
        for i, (b, k) in enumerate(K.DEFAULT_MAP.items()):
            y = r.y + 90 + i * 32
            D.text(self.s, K.BUTTON_LABELS.get(b, b), (r.x + 40, y), 32, D.ACCENT2)
            D.text(self.s, k, (r.x + 300, y), 32, D.TEXT)
        D.text(self.s, "View: the menu · hold View + Menu: exit · touch: every key on Home", (r.x + 40, r.bottom - 44),
               26, D.DIM)
        self.taps.append((pygame.Rect(0, 0, W, H), lambda: setattr(self, "modal", None)))

    def draw_modal_text(self, d) -> None:
        r = self._panel(d["title"], 980, 360)
        for i, line in enumerate(d["lines"]):
            D.text(self.s, line, (r.x + 32, r.y + 96 + i * 44), 32, D.TEXT)
        self.taps.append((pygame.Rect(0, 0, W, H), lambda: setattr(self, "modal", None)))

    def draw_modal_devs(self, d) -> None:
        r = self._panel("Find devices", 900, 600)
        if d["busy"]:
            D.text(self.s, "looking for Rokus and LG TVs…", (r.x + 32, r.y + 100), 34, D.DIM)
        elif not d["list"]:
            D.text(self.s, "none answered on this network", (r.x + 32, r.y + 100), 34, D.DIM)
        else:
            def tap(i):
                d["sel"] = i
                self.modal_button("a")
            self._menu(r, [f"{x['name']}  ({x['link']})" for x in d["list"]], d["sel"], on_tap=tap)


def _wrap(text: str, f: pygame.font.Font, width: int) -> list[str]:
    out = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            t = (line + " " + word).strip()
            if f.size(t)[0] <= width or not line:
                line = t
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def _battery() -> int | None:
    for b in ("BAT1", "BAT0"):
        try:
            with open(f"/sys/class/power_supply/{b}/capacity") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def main(argv=None) -> int:
    pygame.init()
    flags = pygame.FULLSCREEN if "--window" not in (argv or sys.argv) else 0
    screen = pygame.display.set_mode((W, H), flags)
    pygame.display.set_caption("Rook remote")
    pygame.mouse.set_visible("--window" in (argv or sys.argv))
    app = App(screen)
    app._open_controllers()
    clock = pygame.time.Clock()
    dt = 0.0
    while not app.quit:
        for e in pygame.event.get():
            app.handle(e)
        app.step(dt)
        app.render()
        pygame.display.flip()
        dt = clock.tick(60 if app.focused else 10)
    if app.rec:
        app.rec.stop()
    if app.client:
        app.client.close()
    if app.audio:
        app.audio.close()
    pygame.quit()
    return 0
