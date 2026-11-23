"""Voice on the Assistant tab (rookmote/voice.py and the app): the ATVV frames
the Deck makes decode with the hub's own remote-voice decoder, a recording
leaves as remote.voice.part events then remote.voice (codec adpcm16k), the
replies land in the conversation, and the plugin names its worker.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_voice"""

import importlib.util
import io
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from array import array
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from rookmote import voice as V  # noqa: E402

HUB_VOICE = HERE.parents[1] / "rook" / "plugins" / "remote-voice" / "scripts" / "voice.py"


def hub_decoder():
    spec = importlib.util.spec_from_file_location("hub_remote_voice", HUB_VOICE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def speechish(seconds=1.0):
    n = int(V.RATE * seconds)
    return [int(11000 * math.sin(2 * math.pi * 220 * i / V.RATE) * (0.5 + 0.5 * math.sin(i / 900))
                + 4000 * math.sin(2 * math.pi * 1900 * i / V.RATE)) for i in range(n)]


def record(pcm):
    parts, done = [], []
    r = V.Recording(lambda seq, d: parts.append((seq, d)), done.append, source=io.BytesIO(array("h", pcm).tobytes()))
    r.thread.join(5)
    return parts, done[0]


@unittest.skipUnless(HUB_VOICE.is_file(), "the hub's remote-voice plugin is not in this checkout")
class Frames(unittest.TestCase):
    def test_hub_decodes_what_the_deck_sends(self):
        pcm = speechish(1.5)
        parts, done = record(pcm)
        raw = b"".join(d for _, d in parts)
        dec, info = hub_decoder().decode(raw)
        self.assertEqual(info["skipped"], 0)
        self.assertEqual(info["frames"], math.ceil(len(pcm) / V.SAMPLES))
        n = len(pcm)
        err = sum((a - b) ** 2 for a, b in zip(dec[:n], pcm)) / n
        sig = sum(a * a for a in pcm) / n
        self.assertGreater(10 * math.log10(sig / err), 25)

    def test_loud_and_clipped_input_stays_in_step(self):
        pcm = [32767 if (i // 40) % 2 else -32768 for i in range(V.SAMPLES * 20)]
        parts, _ = record(pcm)
        dec, info = hub_decoder().decode(b"".join(d for _, d in parts))
        self.assertEqual((info["frames"], info["skipped"]), (20, 0))


class Recording(unittest.TestCase):
    def test_parts_and_summary(self):
        pcm = speechish(2.0)
        parts, done = record(pcm)
        self.assertEqual([s for s, _ in parts], list(range(len(parts))))
        self.assertTrue(all(len(d) % V.FRAME == 0 and len(d) <= V.FRAME * V.FRAMES_PER_PART for _, d in parts))
        self.assertEqual(done["parts"], len(parts))
        self.assertEqual(done["bytes"], sum(len(d) for _, d in parts))
        self.assertAlmostEqual(done["seconds"], 2.0, places=2)
        ev = V.done_event("abc", done)
        self.assertEqual((ev["codec"], ev["parts"]), ("adpcm16k", len(parts)))
        self.assertNotIn("worker", ev)                   # the plugin adds it

    def test_stops_at_thirty_seconds(self):
        parts, done = record([0] * int(V.RATE * 31))
        self.assertLessEqual(done["seconds"], 30.0)

    def test_frame_sequence_counts_on(self):
        e = V.Encoder()
        a, b = e.frame([0] * V.SAMPLES), e.frame([100] * V.SAMPLES)
        self.assertEqual((a[:2], b[:2]), (b"\0\0", b"\0\1"))

    @unittest.skipUnless(shutil.which("pw-record"), "no pw-record")
    def test_command(self):
        cmd = V.mic_command("x")
        self.assertEqual(cmd[0], "pw-record")
        self.assertIn("--raw", cmd)
        self.assertEqual(cmd[cmd.index("--rate") + 1], "16000")
        self.assertEqual(cmd[cmd.index("--target") + 1], "x")


class FakeClient:
    connected = True

    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)
        return True


class InTheApp(unittest.TestCase):
    def setUp(self):
        os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
        import pygame
        pygame.init()
        self.pg = pygame
        from rookmote import app as A
        self.A = A
        self.app = A.App(pygame.display.set_mode((1280, 800)), headless=True)
        self.app.client = FakeClient()
        self.app.tab = A.TABS.index("Assistant")
        self.real = V.Recording
        pcm = array("h", speechish(1.0)).tobytes()
        A.voice.Recording = lambda on_part, on_done: self.real(on_part, on_done, source=io.BytesIO(pcm))

    def tearDown(self):
        self.A.voice.Recording = self.real

    def test_talk_send_and_reply(self):
        app = self.app
        app.button("a")
        self.assertIsNotNone(app.rec)
        app.rec.thread.join(5)
        app.step(16)
        app.render()
        ev = [m for m in app.client.sent if m.get("t") == "event"]
        names = [m["event"] for m in ev]
        self.assertEqual(names[-1], "remote.voice")
        self.assertTrue(all(n == "remote.voice.part" for n in names[:-1]))
        uid = ev[-1]["payload"]["id"]
        self.assertTrue(all(m["payload"]["id"] == uid for m in ev))
        self.assertEqual(ev[-1]["payload"]["parts"], len(names) - 1)
        self.assertEqual(app.chat_status, "Thinking...")
        app._handle_plugin({"t": "chat", "who": "you", "text": "what time is it"})
        self.assertEqual(app.chat_status, "Thinking...")
        app._handle_plugin({"t": "chat", "who": "agent", "text": "It is late. " * 300})
        self.assertEqual(app.chat_status, "")
        app.render()
        self.assertGreater(app.chat_max, 0)              # a long answer: it scrolls
        app.button("dpup")
        self.assertEqual(app.chat_scroll, 60)
        for _ in range(100):
            app.button("dpup")
        self.assertEqual(app.chat_scroll, app.chat_max)
        app.render()

    def test_no_worker(self):
        self.app.client.connected = False
        self.app.button("a")
        self.assertIsNone(self.app.rec)
        self.assertIn("plugin", self.app.toast[0])

    def test_silence_is_heard_nothing(self):
        self.A.voice.Recording = lambda on_part, on_done: self.real(on_part, on_done, source=io.BytesIO(b""))
        self.app.button("a")
        self.app.rec.thread.join(5)
        self.app.step(16)
        self.assertEqual(self.app.toast[0], "Heard nothing")
        self.assertFalse([m for m in self.app.client.sent if m.get("t") == "event"])


class PluginNamesItsWorker(unittest.TestCase):
    def test_worker_name_from_config(self):
        try:
            import rook.kit.ctx  # noqa: F401
        except ImportError:
            self.skipTest("needs the rook venv (rook.kit)")
        sys.path.insert(0, str(HERE.parent / "plugin" / "remote-deck" / "scripts"))
        import remote_deck as RD
        cfg = Path(tempfile.mkdtemp()) / "worker.yaml"
        cfg.write_text("hub: x\nname: steamdeck  # the Deck\n")
        old = sys.argv
        sys.argv = ["worker", "--config", str(cfg)]
        RD._name = None
        try:
            self.assertEqual(RD.worker_name(), "steamdeck")
        finally:
            sys.argv = old
        got = []

        class Ctx:
            def emit(self, name, payload):
                got.append((name, payload))
        RD._ctx = Ctx()
        RD._on_msg({"t": "event", "event": "remote.voice", "payload": {"id": "1", "parts": 2}})
        RD._on_msg({"t": "event", "event": "remote.voice", "payload": {"id": "2", "parts": 2, "worker": "w"}})
        RD._ctx = None
        self.assertEqual([p["worker"] for _, p in got], ["steamdeck", "w"])


if __name__ == "__main__":
    unittest.main()
