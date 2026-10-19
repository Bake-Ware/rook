#!/usr/bin/env python3
"""Rook HID-KVM source client.

Captures this machine's keyboard + pointer (an Elecom HUGE trackball) via evdev,
translates events into USB HID reports, and streams them — encrypted — over UDP
to a Rook dongle plugged into the target machine. The dongle replays them as a
real USB keyboard/mouse, so the target needs no software and is unaffected by
its corp VPN (the dongle has its own radio; this is pure hardware HID).

Toggle forwarding with a DOUBLE-TAP of RIGHT CTRL:
  - ON  -> grabs the input devices (EVIOCGRAB) so this PC stops seeing them;
           every event now drives the target instead.
  - OFF -> releases the grab; input is local again.
On each toggle an "all released" report is sent so nothing sticks down.

Wire frame (matches firmware recv_hid_stream):
    b'RH' || nonce[24] || secretbox(tag[16] || ciphertext)
    plaintext = seq[8 BE] || report_id[1] || report_body[N]
Key = HKDF-SHA256(PSK, salt="telesthete-v1", info="hid-stream")[:32]
(distinct from the band key so its random nonces can't collide with the band).

Deps:  pip install evdev pynacl
Run :  sudo python3 rook_hid_kvm.py --host 192.168.1.138 --psk "<band_psk>"
       (root, or membership in the 'input' group, is needed to read /dev/input)
"""

import argparse
import hashlib
import hmac
import os
import selectors
import signal
import socket
import struct
import sys
import time

import evdev
from evdev import ecodes as e
from nacl.secret import SecretBox

REPORT_ID_KEYBOARD = 1
REPORT_ID_MOUSE = 2

DOUBLE_TAP_WINDOW_S = 0.4  # max gap between the two Right-Ctrl taps

# ---- key derivation (must match firmware derive_hid_stream_key) ------------

def derive_hid_key(psk: str) -> bytes:
    salt = b"telesthete-v1"
    info = b"hid-stream"
    prk = hmac.new(salt, psk.encode(), hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:32]

# ---- evdev -> USB HID translation -----------------------------------------

# Modifier evdev codes -> HID modifier byte bit.
MODIFIERS = {
    e.KEY_LEFTCTRL: 0x01, e.KEY_LEFTSHIFT: 0x02,
    e.KEY_LEFTALT: 0x04, e.KEY_LEFTMETA: 0x08,
    e.KEY_RIGHTCTRL: 0x10, e.KEY_RIGHTSHIFT: 0x20,
    e.KEY_RIGHTALT: 0x40, e.KEY_RIGHTMETA: 0x80,
}

# evdev KEY_* -> HID Usage (Usage Page 0x07). Modifiers handled separately.
KEYMAP = {
    e.KEY_A: 0x04, e.KEY_B: 0x05, e.KEY_C: 0x06, e.KEY_D: 0x07,
    e.KEY_E: 0x08, e.KEY_F: 0x09, e.KEY_G: 0x0A, e.KEY_H: 0x0B,
    e.KEY_I: 0x0C, e.KEY_J: 0x0D, e.KEY_K: 0x0E, e.KEY_L: 0x0F,
    e.KEY_M: 0x10, e.KEY_N: 0x11, e.KEY_O: 0x12, e.KEY_P: 0x13,
    e.KEY_Q: 0x14, e.KEY_R: 0x15, e.KEY_S: 0x16, e.KEY_T: 0x17,
    e.KEY_U: 0x18, e.KEY_V: 0x19, e.KEY_W: 0x1A, e.KEY_X: 0x1B,
    e.KEY_Y: 0x1C, e.KEY_Z: 0x1D,
    e.KEY_1: 0x1E, e.KEY_2: 0x1F, e.KEY_3: 0x20, e.KEY_4: 0x21,
    e.KEY_5: 0x22, e.KEY_6: 0x23, e.KEY_7: 0x24, e.KEY_8: 0x25,
    e.KEY_9: 0x26, e.KEY_0: 0x27,
    e.KEY_ENTER: 0x28, e.KEY_ESC: 0x29, e.KEY_BACKSPACE: 0x2A,
    e.KEY_TAB: 0x2B, e.KEY_SPACE: 0x2C,
    e.KEY_MINUS: 0x2D, e.KEY_EQUAL: 0x2E, e.KEY_LEFTBRACE: 0x2F,
    e.KEY_RIGHTBRACE: 0x30, e.KEY_BACKSLASH: 0x31, e.KEY_SEMICOLON: 0x33,
    e.KEY_APOSTROPHE: 0x34, e.KEY_GRAVE: 0x35, e.KEY_COMMA: 0x36,
    e.KEY_DOT: 0x37, e.KEY_SLASH: 0x38, e.KEY_CAPSLOCK: 0x39,
    e.KEY_F1: 0x3A, e.KEY_F2: 0x3B, e.KEY_F3: 0x3C, e.KEY_F4: 0x3D,
    e.KEY_F5: 0x3E, e.KEY_F6: 0x3F, e.KEY_F7: 0x40, e.KEY_F8: 0x41,
    e.KEY_F9: 0x42, e.KEY_F10: 0x43, e.KEY_F11: 0x44, e.KEY_F12: 0x45,
    e.KEY_SYSRQ: 0x46, e.KEY_SCROLLLOCK: 0x47, e.KEY_PAUSE: 0x48,
    e.KEY_INSERT: 0x49, e.KEY_HOME: 0x4A, e.KEY_PAGEUP: 0x4B,
    e.KEY_DELETE: 0x4C, e.KEY_END: 0x4D, e.KEY_PAGEDOWN: 0x4E,
    e.KEY_RIGHT: 0x4F, e.KEY_LEFT: 0x50, e.KEY_DOWN: 0x51, e.KEY_UP: 0x52,
    e.KEY_NUMLOCK: 0x53, e.KEY_KPSLASH: 0x54, e.KEY_KPASTERISK: 0x55,
    e.KEY_KPMINUS: 0x56, e.KEY_KPPLUS: 0x57, e.KEY_KPENTER: 0x58,
    e.KEY_KP1: 0x59, e.KEY_KP2: 0x5A, e.KEY_KP3: 0x5B, e.KEY_KP4: 0x5C,
    e.KEY_KP5: 0x5D, e.KEY_KP6: 0x5E, e.KEY_KP7: 0x5F, e.KEY_KP8: 0x60,
    e.KEY_KP9: 0x61, e.KEY_KP0: 0x62, e.KEY_KPDOT: 0x63,
    e.KEY_COMPOSE: 0x65,
}

# Elecom HUGE has 8 buttons; map each to a HID button bit (0..7).
BUTTONMAP = {
    e.BTN_LEFT: 0, e.BTN_RIGHT: 1, e.BTN_MIDDLE: 2,
    e.BTN_SIDE: 3, e.BTN_EXTRA: 4, e.BTN_FORWARD: 5,
    e.BTN_BACK: 6, e.BTN_TASK: 7,
}


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Sender:
    """Encrypts + sends HID report frames to the dongle."""

    def __init__(self, host, port, key):
        self.addr = (host, port)
        self.box = SecretBox(key)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Seed from epoch-ms so the sequence keeps climbing across client
        # restarts — otherwise a fresh start at a low seq is rejected by the
        # firmware's replay guard until it catches up to the last value seen.
        self.seq = int(time.time() * 1000)

    def send(self, report_id, body: bytes):
        self.seq += 1
        pt = struct.pack(">QB", self.seq, report_id) + body
        nonce = os.urandom(24)
        ct = self.box.encrypt(pt, nonce).ciphertext  # tag(16) || ciphertext
        self.sock.sendto(b"RH" + nonce + ct, self.addr)


class Forwarder:
    def __init__(self, sender):
        self.s = sender
        # keyboard state
        self.mods = 0
        self.keys = []  # ordered list of held HID usages (max 6 in report)
        # mouse state
        self.btn_mask = 0
        self.dx = self.dy = self.wheel = self.pan = 0
        self.mouse_dirty = False

    # ---- keyboard ----
    def kbd_event(self, code, value):
        """value: 1=down, 0=up, 2=autorepeat (ignored — host repeats)."""
        if value == 2:
            return
        if code in MODIFIERS:
            bit = MODIFIERS[code]
            if value:
                self.mods |= bit
            else:
                self.mods &= ~bit
            self.send_kbd()
            return
        usage = KEYMAP.get(code)
        if usage is None:
            return
        if value:
            if usage not in self.keys:
                self.keys.append(usage)
        else:
            if usage in self.keys:
                self.keys.remove(usage)
        self.send_kbd()

    def send_kbd(self):
        keys = self.keys[:6]
        report = bytes([self.mods, 0] + keys + [0] * (6 - len(keys)))
        self.s.send(REPORT_ID_KEYBOARD, report)

    # ---- mouse ----
    def mouse_btn(self, code, value):
        bit = BUTTONMAP.get(code)
        if bit is None:
            return
        if value:
            self.btn_mask |= (1 << bit)
        else:
            self.btn_mask &= ~(1 << bit)
        self.mouse_dirty = True

    def mouse_rel(self, code, value):
        if code == e.REL_X:
            self.dx += value
        elif code == e.REL_Y:
            self.dy += value
        elif code == e.REL_WHEEL:
            self.wheel += value
        elif code == e.REL_HWHEEL:
            self.pan += value
        else:
            return
        self.mouse_dirty = True

    def mouse_flush(self):
        if not self.mouse_dirty:
            return
        body = struct.pack(
            "<Bhhbb",
            self.btn_mask,
            clamp(self.dx, -32767, 32767),
            clamp(self.dy, -32767, 32767),
            clamp(self.wheel, -127, 127),
            clamp(self.pan, -127, 127),
        )
        self.s.send(REPORT_ID_MOUSE, body)
        self.dx = self.dy = self.wheel = self.pan = 0
        self.mouse_dirty = False

    # ---- toggle: release everything so nothing sticks down ----
    def release_all(self):
        self.mods = 0
        self.keys = []
        self.btn_mask = 0
        self.dx = self.dy = self.wheel = self.pan = 0
        self.s.send(REPORT_ID_KEYBOARD, bytes(8))
        self.s.send(REPORT_ID_MOUSE, struct.pack("<Bhhbb", 0, 0, 0, 0, 0))


def find_devices(kbd_paths, mouse_paths):
    """Auto-detect keyboards and pointers unless explicit paths are given."""
    kbds, mice = [], []
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        caps = d.capabilities()
        keys = set(caps.get(e.EV_KEY, []))
        has_rel = e.EV_REL in caps
        if has_rel and (e.BTN_LEFT in keys):
            mice.append(d)
        elif e.KEY_A in keys and not has_rel:
            kbds.append(d)
        else:
            d.close()
    if kbd_paths:
        kbds = [evdev.InputDevice(p) for p in kbd_paths]
    if mouse_paths:
        mice = [evdev.InputDevice(p) for p in mouse_paths]
    return kbds, mice


def main():
    ap = argparse.ArgumentParser(description="Rook HID-KVM source client")
    ap.add_argument("--host", required=True, help="dongle IP on the LAN")
    ap.add_argument("--port", type=int, default=7475, help="HID stream UDP port")
    ap.add_argument("--psk", default=os.environ.get("ROOK_BAND_PSK"),
                    help="band PSK (or set ROOK_BAND_PSK)")
    ap.add_argument("--kbd", action="append", help="explicit keyboard device path")
    ap.add_argument("--mouse", action="append", help="explicit pointer device path")
    args = ap.parse_args()
    if not args.psk:
        ap.error("a PSK is required (--psk or ROOK_BAND_PSK)")

    # SIGTERM -> SystemExit so the `finally` cleanup (ungrab + release) still
    # runs; otherwise a kill could leave input devices grabbed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    key = derive_hid_key(args.psk)
    sender = Sender(args.host, args.port, key)
    fwd = Forwarder(sender)

    kbds, mice = find_devices(args.kbd, args.mouse)
    if not kbds and not mice:
        print("no input devices found (need root or 'input' group)", file=sys.stderr)
        sys.exit(1)
    devices = kbds + mice
    mouse_fds = {d.fd for d in mice}

    print(f"target dongle : {args.host}:{args.port}")
    for d in kbds:
        print(f"  keyboard    : {d.path}  {d.name}")
    for d in mice:
        print(f"  pointer     : {d.path}  {d.name}")
    print("double-tap RIGHT CTRL to toggle forwarding. Ctrl-C to quit.")

    sel = selectors.DefaultSelector()
    for d in devices:
        sel.register(d.fd, selectors.EVENT_READ, d)

    forwarding = False
    grabbed = False
    last_rctrl_down = 0.0

    def set_grab(on):
        nonlocal grabbed
        if on and not grabbed:
            for d in devices:
                try:
                    d.grab()
                except OSError:
                    pass
            grabbed = True
        elif not on and grabbed:
            for d in devices:
                try:
                    d.ungrab()
                except OSError:
                    pass
            grabbed = False

    HEARTBEAT_S = 0.025  # keyboard state heartbeat (self-heals a lost down/up)
    last_kbd_hb = 0.0
    try:
        while True:
            ready = sel.select(timeout=HEARTBEAT_S)
            for sk, _ in ready:
                dev = sk.data
                is_mouse = dev.fd in mouse_fds
                try:
                    events = list(dev.read())
                except BlockingIOError:
                    continue
                for ev in events:
                    # toggle detection (works whether forwarding or not)
                    if ev.type == e.EV_KEY and ev.code == e.KEY_RIGHTCTRL and ev.value == 1:
                        now = time.monotonic()
                        if now - last_rctrl_down <= DOUBLE_TAP_WINDOW_S:
                            forwarding = not forwarding
                            set_grab(forwarding)
                            fwd.release_all()
                            print(f"[forwarding {'ON' if forwarding else 'OFF'}]")
                            last_rctrl_down = 0.0
                            continue
                        last_rctrl_down = now

                    if not forwarding:
                        continue

                    if ev.type == e.EV_KEY:
                        if is_mouse and ev.code in BUTTONMAP:
                            fwd.mouse_btn(ev.code, ev.value)
                        else:
                            fwd.kbd_event(ev.code, ev.value)
                    elif ev.type == e.EV_REL:
                        fwd.mouse_rel(ev.code, ev.value)
                    elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                        if is_mouse:
                            fwd.mouse_flush()
    except KeyboardInterrupt:
        pass
    finally:
        set_grab(False)
        fwd.release_all()
        print("\nstopped, devices released.")


if __name__ == "__main__":
    main()
