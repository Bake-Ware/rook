"""Baketris's screen (games/baketris.py): the Android build's controller mapping on
the Deck's buttons, stick and triggers; DAS/ARR/soft drop; the menus, settings
and their file; touch; the listener's effects; and the app opening and closing it.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_baketris_screen"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pygame  # noqa: E402

pygame.init()
pygame.display.set_mode((1280, 800))

from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import rules as G  # noqa: E402


def make(**kw):
    d = Path(tempfile.mkdtemp())
    return B.Baketris(prefs_path=d / "baketris.json", seed=5, **kw)


def playing(bk, mode=G.MARATHON):
    bk.start_game(mode)
    while bk.state != B.S_PLAY:
        bk.update(16.667)
    return bk


class Controls(unittest.TestCase):
    def test_buttons_are_the_android_mapping(self):
        bk = playing(make())
        g = bk.game
        calls = []
        g.shift = lambda d: calls.append(("shift", d)) or True
        g.rotate = lambda d: calls.append(("rotate", d)) or True
        g.hard_drop = lambda: calls.append(("hard",))
        g.do_hold = lambda: calls.append(("hold",))
        for b in ("dpleft", "dpright", "a", "b", "y", "x", "leftshoulder", "rightshoulder", "dpup"):
            bk.pad_down(b)
            bk.pad_up(b)
            bk.last_drop_t = 0
        self.assertEqual(calls, [("shift", -1), ("shift", 1), ("rotate", 1), ("rotate", -1), ("rotate", 2),
                                 ("hold",), ("hold",), ("hold",), ("hard",)])
        bk.pad_down("dpdown")
        self.assertTrue(bk.soft_held)
        bk.pad_up("dpdown")
        self.assertFalse(bk.soft_held)
        bk.pad_down("start")
        self.assertEqual(bk.state, B.S_PAUSE)

    def test_view_pauses_too(self):
        bk = playing(make())
        bk.pad_down("back")
        self.assertEqual(bk.state, B.S_PAUSE)

    def test_triggers(self):
        bk = playing(make())
        calls = []
        bk.game.hard_drop = lambda: calls.append("hard")
        bk.game.do_hold = lambda: calls.append("hold")
        bk.trigger("lefttrigger", True)
        bk.trigger("lefttrigger", True)          # held: once
        bk.trigger("lefttrigger", False)
        bk.trigger("righttrigger", True)
        self.assertEqual(calls, ["hold", "hard"])

    def test_stick(self):
        bk = playing(make())
        calls = []
        bk.game.shift = lambda d: calls.append(d) or True
        bk.game.hard_drop = lambda: calls.append("hard")
        bk.stick_motion(-0.5, 0)                 # under 0.55: nothing
        bk.stick_motion(-0.6, 0)
        self.assertEqual(calls, [-1])
        self.assertTrue(bk.left_held)
        bk.stick_motion(-0.4, 0)                 # hysteresis: still held above 0.35
        self.assertTrue(bk.left_held)
        bk.stick_motion(-0.2, 0)
        self.assertFalse(bk.left_held)
        bk.stick_motion(0, 0.7)
        self.assertTrue(bk.soft_held)
        bk.stick_motion(0, 0)
        self.assertFalse(bk.soft_held)
        bk.stick_motion(0, -0.8)
        self.assertEqual(calls[-1], "hard")

    def test_stick_in_menus_navigates(self):
        bk = make()
        bk.stick_motion(0, 0.9)
        self.assertEqual(bk.sel, 1)
        bk.stick_motion(0, 0)
        bk.stick_motion(0, -0.9)
        self.assertEqual(bk.sel, 0)

    def test_das_then_arr(self):
        bk = playing(make())
        bk.das, bk.arr_idx = 150, 3                # ARR 25 ms
        moves = []
        bk.game.shift = lambda d: moves.append(d) or True
        bk.press(B.A_RIGHT)
        self.assertEqual(moves, [1])               # the tap
        bk.tick_das(149)
        self.assertEqual(moves, [1])
        bk.tick_das(1)                             # charged: one more
        self.assertEqual(moves, [1, 1])
        bk.tick_das(100)                           # then one each 25 ms
        self.assertEqual(len(moves), 6)
        bk.release(B.A_RIGHT)
        bk.tick_das(500)
        self.assertEqual(len(moves), 6)

    def test_arr_zero_is_instant(self):
        bk = playing(make())
        bk.arr_idx = 0
        bk.press(B.A_LEFT)
        bk.tick_das(bk.das)
        self.assertFalse(bk.game.fits(bk.game.cur, bk.game.rot, bk.game.px - 1, bk.game.py))

    def test_hard_drop_debounce(self):
        bk = playing(make())
        n = []
        bk.game.hard_drop = lambda: n.append(1)
        bk.press(B.A_HARD)
        bk.press(B.A_HARD)
        self.assertEqual(len(n), 1)

    def test_last_direction_wins(self):
        bk = playing(make())
        bk.press(B.A_LEFT)
        bk.press(B.A_RIGHT)
        self.assertEqual(bk.last_dir, 1)
        bk.release(B.A_RIGHT)
        self.assertEqual(bk.last_dir, -1)


class Menus(unittest.TestCase):
    def test_main_menu(self):
        bk = make()
        self.assertEqual(bk.menu_count(), 6)            # 4 modes (BAKETRIS too), settings, controls
        bk.pad_down("dpdown")
        bk.pad_down("a")
        self.assertEqual((bk.state, bk.mode), (B.S_READY, G.SPRINT))

    def test_controls_screen_and_back(self):
        bk = make()
        bk.sel = 5
        bk.pad_down("a")
        self.assertEqual(bk.state, B.S_CONTROLS)
        bk.draw(pygame.display.get_surface())
        bk.pad_down("b")
        self.assertEqual((bk.state, bk.sel), (B.S_MENU, 5))

    def test_b_on_the_menu_leaves(self):
        bk = make()
        bk.pad_down("b")
        self.assertTrue(bk.done)

    def test_settings_change_and_persist(self):
        bk = make()
        bk.sel = 4
        bk.pad_down("a")
        self.assertEqual(bk.state, B.S_SETTINGS)
        for row, d in ((2, 1), (4, -1), (9, 1), (10, 1), (11, -1), (8, 1), (7, 0), (3, 1)):
            bk.sel = row
            bk.pad_down("dpright" if d >= 0 else "dpleft")
        bk.pad_down("b")
        self.assertEqual(bk.state, B.S_MENU)
        p = json.loads(bk.prefs_path.read_text())
        self.assertEqual((p["skin"], p["shake"], p["das"], p["arr"], p["sdf"], p["drag"], p["ghost"], p["song"]),
                         (1, 1, 160, 5, 1, 11, False, 1))
        bk2 = B.Baketris(prefs_path=bk.prefs_path)
        self.assertEqual((bk2.skin, bk2.das, bk2.ghost), (1, 160, False))

    def test_every_setting_row_draws(self):
        bk = make()
        bk.state = B.S_SETTINGS
        s = pygame.display.get_surface()
        for skin in range(7):
            bk.skin = skin
            bk.apply_skin()
            bk.draw(s)
        self.assertEqual(len(bk.menu_rects), len(B.SETTINGS_ROWS))

    def test_pause_menu(self):
        bk = playing(make())
        bk.pad_down("start")
        bk.pad_down("dpdown")
        bk.pad_down("a")                           # restart
        self.assertEqual(bk.state, B.S_READY)
        bk = playing(bk)
        bk.pad_down("start")
        bk.pad_down("b")                           # b: resume
        self.assertEqual(bk.state, B.S_READY)

    def test_focus_loss_pauses(self):
        bk = playing(make())
        bk.host_pause()
        self.assertEqual(bk.state, B.S_PAUSE)

    def test_game_over_and_best(self):
        bk = playing(make())
        bk.game.score = 1234
        bk.on_end(False)
        self.assertEqual(bk.state, B.S_OVER)
        self.assertTrue(bk.new_best)
        self.assertEqual(json.loads(bk.prefs_path.read_text())["best0"], 1234)
        for _ in range(40):
            bk.update(16.667)
            bk.draw(pygame.display.get_surface())
        self.assertEqual(len(bk.menu_rects), 2)


class Effects(unittest.TestCase):
    def test_quad_kaboom(self):
        bk = playing(make())
        bk.key_down(pygame.K_F12)
        self.assertEqual(bk.action[0], "BAKETRIS!")
        self.assertGreater(len(bk.parts), 300)                # (fewer than Android's: the Deck's CPU)
        self.assertEqual(len(bk.waves), 3)
        self.assertGreater(bk.punch, 0)
        self.assertEqual(bk.hitstop_t, 210)
        s = pygame.display.get_surface()
        for _ in range(120):
            bk.update(16.667)
            bk.draw(s)
        self.assertEqual(bk.punch, 0)

    def test_shake_off(self):
        bk = playing(make())
        bk.shake_lvl = 0
        bk.on_hard_drop(G.T, 0, 3, 20, 0)
        self.assertEqual(bk.trauma, 0)
        self.assertEqual(bk.kick_y, 0)

    def test_tspin_text(self):
        bk = playing(make())
        bk.on_lock(2, [0, 1, 0, 0], 2, True, 2, False, True)
        self.assertEqual(bk.action, ["T-SPIN", "DOUBLE", "BACK-TO-BACK", "COMBO 2"])

    def test_rumble(self):
        got = []
        bk = playing(make(rumble=lambda lo, hi, ms: got.append(ms)))
        bk.on_hold()
        bk.haptics = False
        bk.on_hold()
        self.assertEqual(len(got), 1)


class Touch(unittest.TestCase):
    def test_tap_rotates_and_hud_pauses(self):
        bk = playing(make())
        r = []
        bk.game.rotate = lambda d: r.append(d) or True
        bk.touch_down(1, 640, 400)
        bk.touch_up(1, 641, 401)
        self.assertEqual(r, [1])
        bk.touch_down(2, 640, 20)
        self.assertEqual(bk.state, B.S_PAUSE)

    def test_drag_moves_and_swipe_drops(self):
        bk = playing(make())
        x0 = bk.game.px
        step = bk.cell * 10 / bk.drag_spd
        bk.touch_down(1, 600, 400)
        bk.touch_move(1, 600 + step * 2, 402)
        self.assertEqual(bk.game.px, x0 + 2)
        bk.touch_up(1, 600 + step * 2, 402)
        drops = []
        bk.game.hard_drop = lambda: drops.append(1)
        bk.touch_down(3, 600, 300)
        bk.touch_move(3, 602, 300 + 50 * B.DENSITY)
        self.assertEqual(drops, [1])

    def test_menu_tap(self):
        bk = make()
        bk.draw(pygame.display.get_surface())
        r = bk.menu_rects[2]
        bk.touch_down("m", *r.center)
        bk.touch_up("m", *r.center)
        self.assertEqual((bk.state, bk.mode), (B.S_READY, G.ULTRA))


class InTheApp(unittest.TestCase):
    def test_open_play_and_close(self):
        os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
        from rookmote.app import TABS, App
        app = App(pygame.display.get_surface(), headless=True)
        app.tab = TABS.index("Games")
        app.button("a")
        self.assertIsNotNone(app.game)
        ev = pygame.event.Event
        app.handle(ev(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_A))
        self.assertEqual(app.game.state, B.S_READY)
        for _ in range(20):
            app.step(100)
        app.render()
        self.assertEqual(app.game.state, B.S_PLAY)
        app.handle(ev(pygame.CONTROLLERAXISMOTION, axis=pygame.CONTROLLER_AXIS_LEFTX, value=-30000))
        self.assertTrue(app.game.left_held)
        app.handle(ev(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_START))
        self.assertEqual(app.game.state, B.S_PAUSE)
        app.game.state = B.S_MENU
        app.handle(ev(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_B))
        app.step(16)
        self.assertIsNone(app.game)
        self.assertEqual(TABS[app.tab], "Games")


if __name__ == "__main__":
    unittest.main()
