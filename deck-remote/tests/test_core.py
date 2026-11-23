"""The app's core without a screen: the profile store (the firmware's
profile_save JSON), the routes, a key through a profile (the event the hub's
jobs filter on), the socket between the app and the plugin, and the plugin's
caps end to end. python -m unittest discover -s deck-remote/tests (the plugin
test needs the repository's rook package on the path: run from its root)."""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="rookmote-test-")

from rookmote import bridge, router  # noqa: E402
from rookmote.profiles import ROUTE_IR, ROUTE_ROOK, ROUTE_WIFI, ProfileError, Store, routes  # noqa: E402

NEC = {"protocol": "nec", "address": 51178, "command": 7}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(tempfile.mkdtemp()) / "profiles.json")

    def test_save_merge_and_routes(self):
        p, created = self.st.save("LG TVs", {"power": NEC, "play": {"rook": True}}, icon="tv", set_icon=True)
        self.assertTrue(created)
        self.assertEqual(routes(p), ROUTE_IR | ROUTE_ROOK)
        p, created = self.st.save("LG TVs", {"power": None, "bt_c77": {"lan": True}})
        self.assertFalse(created)
        self.assertEqual(sorted(p["keys"]), ["bt_c77", "play"])
        self.assertEqual(routes(p), ROUTE_ROOK | ROUTE_WIFI)
        p, _ = self.st.save("LG TVs", {"ok": {"rook": True}}, merge=False)
        self.assertEqual(list(p["keys"]), ["ok"])
        self.assertEqual(p["icon"], "tv")                 # left out: as it was

    def test_firmware_json_and_checks(self):
        self.st.save("Box", {"settings": {"cap": "media.play", "worker": "cachyrig", "args": {"x": 1}},
                             "select": {"raw": [9000, 4500, 560], "carrier": 38000}})
        p = self.st.find("box")
        self.assertIn("menu", p["keys"])                  # the firmware's aliases: settings = menu, select = ok
        self.assertIn("ok", p["keys"])
        listing = self.st.listing(p)
        self.assertEqual(listing["cap_keys"], ["menu"])
        self.assertEqual(listing["ir_keys"], ["ok"])
        self.assertTrue(listing["ir_unavailable"])
        for bad in ({"rook": False}, {"lan": ""}, {"cap": "nodot"}, {"protocol": "nec", "address": 1}, "x"):
            with self.assertRaises(ProfileError):
                self.st.save("Box", {"ok": bad})
        with self.assertRaises(ProfileError):
            self.st.save("Box", {"BAD-NAME": {"rook": True}})
        with self.assertRaises(ProfileError):
            self.st.save("x" * 21, {})

    def test_rename_select_delete_and_reload(self):
        self.st.save("A", {})
        self.st.select("A")
        self.st.rename("A", "B")
        self.assertEqual(self.st.selected, "B")
        other = Store(self.st.path)                       # the other process sees it
        self.assertEqual(other.selected, "B")
        self.st.delete("B")
        time.sleep(0.01)
        other.reload()
        self.assertIsNone(other.selected)
        self.assertEqual(other.profiles, [])


class RouterTest(unittest.TestCase):
    def test_payload_matches_the_firmware(self):
        st = Store(Path(tempfile.mkdtemp()) / "p.json")
        st.save("Steam Deck", {"ok": {"rook": True}, "menu": {"cap": "media.play", "args": {"a": 1}}, "power": NEC})
        st.select("Steam Deck")
        via, a, p = router.route(st, "ok")
        self.assertEqual(via, "rook")
        pl = router.payload("ok", via, a, p)
        # what the hub's remote-steamdeck-* jobs filter on: profile and button
        self.assertEqual((pl["profile"], pl["button"], pl["action"], pl["via"], pl["local"]),
                         ("Steam Deck", "ok", "press", "rook", True))
        via, a, p = router.route(st, "settings")
        self.assertEqual(router.payload("settings", via, a, p)["call"], {"cap": "media.play", "args": {"a": 1}})
        self.assertEqual(router.route(st, "power")[0], "ir")
        self.assertEqual(router.route(st, "9")[0], "none")
        st.select(None)
        self.assertEqual(router.payload("ok", *router.route(st, "ok"))["button"], "ok")
        self.assertNotIn("profile", router.payload("ok", *router.route(st, "ok")))


class BridgeTest(unittest.TestCase):
    def test_both_ways_and_reconnect(self):
        path = Path(tempfile.mkdtemp()) / "s.sock"
        got_srv, got_app = [], []
        srv = bridge.Server(got_srv.append, path)
        cli = bridge.Client(got_app.append, path, hello={"version": "t"})
        self.assertTrue(bridge.wait_for(lambda: srv.connected and cli.connected))
        self.assertTrue(cli.send({"t": "event", "event": "remote.button", "payload": {"button": "ok"}}))
        self.assertTrue(bridge.wait_for(lambda: any(m.get("t") == "event" for m in got_srv)))
        self.assertTrue(srv.send({"t": "press", "button": "up"}))
        self.assertTrue(bridge.wait_for(lambda: any(m.get("t") == "press" for m in got_app)))
        srv.close()
        self.assertTrue(bridge.wait_for(lambda: not cli.connected))
        srv2 = bridge.Server(got_srv.append, path)        # the worker restarted
        self.assertTrue(bridge.wait_for(lambda: srv2.connected and cli.connected, 5))
        cli.close()
        srv2.close()


@unittest.skipUnless(importlib.util.find_spec("rook"), "the repository's rook package is not on the path")
class PluginTest(unittest.TestCase):
    """The plugin's script, as the worker runs it (start hook, caps), with a pretend ctx."""

    def test_caps_and_events(self):
        os.environ["ROOKMOTE_SOCKET"] = str(Path(tempfile.mkdtemp()) / "p.sock")
        spec = importlib.util.spec_from_file_location(
            "remote_deck", HERE.parent / "plugin" / "remote-deck" / "scripts" / "remote_deck.py")
        rd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rd)
        emitted = []

        class Ctx:
            def emit(self, event, payload):
                emitted.append((event, payload))
        ctx = Ctx()
        rd.start(ctx)
        try:
            out = rd.ir(ctx, "profile_save", name="Steam Deck", keys={"ok": {"rook": True}, "power": NEC}, icon="game")
            self.assertTrue(out["created"])
            self.assertEqual(out["profile"]["rook_keys"], ["ok"])
            rd.ir(ctx, "profile_select", name="Steam Deck")
            # the app closed: a press is the picked profile's action, and the event goes out
            self.assertEqual(rd.press(ctx, "ok")["via"], "rook")
            self.assertEqual(emitted[-1], ("remote.button", {"button": "ok", "action": "press", "index": -1, "held_ms": 0,
                                                             "source": "deck", "target": "Steam Deck", "local": True,
                                                             "profile": "Steam Deck", "via": "rook"}))
            with self.assertRaises(rd.KitError):           # IR: unavailable
                rd.press(ctx, "power")
            with self.assertRaises(rd.KitError):
                rd.ir(ctx, "send")
            # the app open: its events are emitted, presses go to it
            got = []
            cli = bridge.Client(got.append)
            self.assertTrue(bridge.wait_for(lambda: rd._app_open() and cli.connected))
            cli.send({"t": "event", "event": "remote.button", "payload": {"button": "up", "action": "press",
                                                                          "profile": "Steam Deck"}})
            self.assertTrue(bridge.wait_for(lambda: emitted[-1][1].get("button") == "up"))
            cli.send({"t": "event", "event": "not.ours", "payload": {}})
            self.assertTrue(rd.press(ctx, "down")["app"])
            self.assertTrue(bridge.wait_for(lambda: any(m.get("t") == "press" and m["button"] == "down" for m in got)))
            st = rd.state(ctx)
            self.assertTrue(st["app"])
            self.assertEqual(st["routes"], ["ir", "rook"])
            rd.ir(ctx, "profile_rename", name="Steam Deck", to="Deck")
            self.assertEqual(rd.ir(ctx, "profiles")["selected"], "Deck")
            self.assertFalse(any(e == "not.ours" for e, _ in emitted))
            cli.close()
        finally:
            rd.stop(ctx)


if __name__ == "__main__":
    unittest.main()
