"""cec (Pico-CEC) on the hub, through a delegate that serves net.open and the
stream caps, against a local fake bridge: a TCP server answering one line per
connection, like the firmware."""

import socket
import threading

import pytest

from port_helpers import PLUGINS, on_worker
from rook.kit.ctx import KitError
from rook.kit.manifest import load


class FakePico:
    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.seen: list[str] = []
        self.reply = None          # None: answer like the firmware
        self.silent = False
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    line += chunk
                text = line.decode()
                self.seen.append(text)
                if self.silent:
                    threading.Event().wait(3)
                    continue
                if self.reply is not None:
                    answer = self.reply
                elif text.startswith("ping"):
                    answer = "pong 4 1.0.0.0"
                elif text.startswith("send "):
                    answer = "OK " + text[5:].strip()
                else:
                    answer = "ERR unknown command"
                conn.sendall(answer.encode() + b"\n")

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def pico():
    p = FakePico()
    yield p
    p.close()


def call(pico, cap, args=None, **settings):
    inst = {"name": "living-room-cec", "host": "127.0.0.1",
            "settings": {"port": pico.sock.getsockname()[1], **settings}}
    return on_worker("cec", cap, args, appliance=inst)[0]


def test_send_frames(pico):
    assert call(pico, "cec.send", {"addr": "0", "opcode": "36"}).startswith("OK 0 36")
    assert call(pico, "cec.send", {"addr": "5", "opcode": "44", "operands": ["41", "0f"]}) == "OK 5 44 41 0f"
    assert pico.seen[0].split() == ["send", "0", "36"] and pico.seen[0].endswith("\n")
    assert pico.seen[1] == "send 5 44 41 0f\n"


def test_status(pico):
    assert call(pico, "cec.status") == "pong 4 1.0.0.0"
    assert "cec.ping" not in load(PLUGINS / "cec").caps                 # no 0.4 names
    assert pico.seen == ["ping\n"]


def test_firmware_error_is_an_error(pico):
    pico.reply = "ERR bus busy"
    with pytest.raises(KitError, match="ERR bus busy"):
        call(pico, "cec.send", {"addr": "0", "opcode": "04"})


@pytest.mark.parametrize("args", [
    {"addr": "10", "opcode": "36"},              # not one nibble
    {"addr": "0", "opcode": "36\nsend 0 04"},    # no second command smuggled in
    {"addr": "0", "opcode": "36", "operands": ["1 2"]},
    {"addr": "0", "opcode": "36", "raw": "send 0 36"},   # cec.raw is gone
])
def test_args_are_checked(pico, args):
    with pytest.raises(KitError, match="bad args"):
        call(pico, "cec.send", args)
    assert pico.seen == []


def test_unreachable_and_silent_bridges_fail_cleanly(pico):
    dead = socket.create_server(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    with pytest.raises(KitError):
        call(pico, "cec.status", port=port)
    pico.silent = True       # the bridge hangs up without a reply
    with pytest.raises(KitError, match="failed"):
        call(pico, "cec.status")
    pico.silent, pico.reply = False, "garbage"
    with pytest.raises(KitError, match="failed: garbage"):
        call(pico, "cec.send", {"addr": "0", "opcode": "36"})


def test_the_port_setting_defaults_to_the_firmwares():
    m = load(PLUGINS / "cec").data
    assert not m.get("settings") and m["appliance"]["settings"]["port"]["default"] == 9526
    assert m["runs_on"] == "hub" and {c["on"] for c in m["caps"].values()} == {"worker"}
