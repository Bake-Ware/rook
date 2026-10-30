"""cec (Pico-CEC) against a local fake bridge: a TCP server answering one
line per connection, like the firmware.

TODO(castling:worker-host): appliance caps can't go through Host.call yet
(N3), so ``call`` runs the bound action with the instance's appliance values,
as test_port_kvm does.
"""

import copy
import socket
import threading

import pytest

from port_helpers import PLUGINS, go
from rook.core2.worker.loader import load_folder
from rook.kit.actions import ActionError, appliance_values, run_action

PLUGIN = load_folder(PLUGINS / "cec", os_name="linux")


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


def call(pico, cap, args=None, **instance):
    c = PLUGIN.caps[cap]
    args = dict(args or {})
    errors = list(c.validator.iter_errors(args))
    if errors:
        raise ValueError(f"bad args: {errors[0].message}")
    for key, spec in c.spec.get("args", {}).items():
        if key not in args and "default" in spec:
            args[key] = copy.deepcopy(spec["default"])
    settings = {"port": instance.pop("port", pico.sock.getsockname()[1])}
    inst = {"name": "living-room-cec", "host": "127.0.0.1", **instance}
    appliance = appliance_values(inst, settings)
    return go(lambda: run_action(c.action, args, appliance=appliance, worker={"os": "linux"},
                                 timeout=c.timeout, cap=c.spec))


def test_send_frames(pico):
    assert call(pico, "cec.send", {"addr": "0", "opcode": "36"}).startswith("OK 0 36")
    assert call(pico, "cec.send", {"addr": "5", "opcode": "44", "operands": ["41", "0f"]}) == "OK 5 44 41 0f"
    assert pico.seen[0].split() == ["send", "0", "36"] and pico.seen[0].endswith("\n")
    assert pico.seen[1] == "send 5 44 41 0f\n"


def test_status_and_old_name(pico):
    assert call(pico, "cec.status") == "pong 4 1.0.0.0"
    assert "cec.ping" in PLUGIN.caps
    assert pico.seen == ["ping\n"]


def test_firmware_error_is_an_error(pico):
    pico.reply = "ERR bus busy"
    with pytest.raises(ActionError, match="ERR bus busy"):
        call(pico, "cec.send", {"addr": "0", "opcode": "04"})


@pytest.mark.parametrize("args", [
    {"addr": "10", "opcode": "36"},              # not one nibble
    {"addr": "0", "opcode": "36\nsend 0 04"},    # no second command smuggled in
    {"addr": "0", "opcode": "36", "operands": ["1 2"]},
    {"addr": "0", "opcode": "36", "raw": "send 0 36"},   # cec.raw is gone
])
def test_args_are_checked(pico, args):
    with pytest.raises(ValueError, match="bad args"):
        call(pico, "cec.send", args)
    assert pico.seen == []


def test_unreachable_and_silent_bridges_fail_cleanly(pico):
    dead = socket.create_server(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    with pytest.raises(ActionError):
        call(pico, "cec.status", port=port)
    pico.silent = True       # the bridge hangs up without a reply
    with pytest.raises(ActionError, match="failed"):
        call(pico, "cec.status")
    pico.silent, pico.reply = False, "garbage"
    with pytest.raises(ActionError, match="failed: garbage"):
        call(pico, "cec.send", {"addr": "0", "opcode": "36"})


def test_no_instance_settings_leak_into_the_plugin():
    assert not PLUGIN.manifest.get("settings")
    assert PLUGIN.manifest["appliance"]["settings"]["port"]["default"] == 9526
