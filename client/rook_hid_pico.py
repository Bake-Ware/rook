#!/usr/bin/env python3
"""Rook HID-KVM source client — Pico UART-bridge transport.

Captures this machine's keyboard + pointer (an Elecom HUGE trackball) via evdev,
translates events into USB HID reports, and writes them — as framed bytes — to
the *bridge* Pico's USB serial port. That Pico relays the frames over its UART
to its soldered partner Pico, which is plugged into the target laptop and
replays them as a real USB keyboard/mouse. No network, no encryption: it's a
wire. The target needs no software and is unaffected by its corp VPN.

Toggle forwarding with a DOUBLE-TAP of RIGHT CTRL:
  - ON  -> grabs the input devices (EVIOCGRAB) so this PC stops seeing them.
  - OFF -> releases the grab; input is local again.
On each toggle an "all released" report is sent so nothing sticks down.

Wire frame (matches firmware-pico src/main.cpp):
    0xAB | type | len | payload[len] | crc8(type, len, payload)
    type 1 = keyboard (8 bytes), 2 = mouse (7 bytes), 4 = consumer (2 bytes)

Deps:  pip install evdev pyserial
Run :  sudo python3 rook_hid_pico.py            # auto-detects the bridge Pico
       sudo python3 rook_hid_pico.py --port /dev/ttyACM0
"""

import argparse
import selectors
import signal
import struct
import sys
import time

import evdev
from evdev import ecodes as e
import serial
from serial.tools import list_ports

REPORT_ID_KEYBOARD = 1
REPORT_ID_MOUSE = 2

DOUBLE_TAP_WINDOW_S = 0.4  # max gap between the two Right-Ctrl taps

FRAME_SYNC = 0xAB

# ---- framing (must match firmware crc8 / parser) --------------------------

def crc8(data: bytes) -> int:
    """CRC-8, poly 0x07, init 0 — over (type, len, payload)."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else ((crc << 1) & 0xFF)
    return crc


def frame(report_id: int, body: bytes) -> bytes:
    head = bytes([report_id, len(body)]) + body
    return bytes([FRAME_SYNC]) + head + bytes([crc8(head)])

# ---- evdev -> USB HID translation -----------------------------------------

MODIFIERS = {
    e.KEY_LEFTCTRL: 0x01, e.KEY_LEFTSHIFT: 0x02,
    e.KEY_LEFTALT: 0x04, e.KEY_LEFTMETA: 0x08,
    e.KEY_RIGHTCTRL: 0x10, e.KEY_RIGHTSHIFT: 0x20,
    e.KEY_RIGHTALT: 0x40, e.KEY_RIGHTMETA: 0x80,
}

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
    """Writes framed HID reports to the bridge Pico's serial port."""

    def __init__(self, port):
        self.ser = serial.Serial(port, baudrate=115200, timeout=0,
                                 write_timeout=0.2)

    def send(self, report_id, body: bytes):
        try:
            self.ser.write(frame(report_id, body))
        except serial.SerialException as ex:
            print(f"serial write failed: {ex}", file=sys.stderr)


class Forwarder:
    def __init__(self, sender):
        self.s = sender
        self.mods = 0
        self.keys = []  # ordered list of held HID usages (max 6 in report)
        self.btn_mask = 0
        self.dx = self.dy = self.wheel = self.pan = 0
        self.mouse_dirty = False

    # ---- keyboard ----
    def kbd_event(self, code, value):
        """value: 1=down, 0=up, 2=autorepeat (ignored — target repeats)."""
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


def autodetect_port():
    """Find the bridge Pico's CDC port. Prefer our product/manufacturer string,
    fall back to any Raspberry Pi RP2040 (VID 0x2E8A), else first ttyACM."""
    acm = [p for p in list_ports.comports() if "ACM" in p.device]
    for p in acm:
        prod = (p.product or "") + (p.manufacturer or "")
        if "Rook" in prod:
            return p.device
    for p in acm:
        if (p.vid == 0x2E8A):
            return p.device
    return acm[0].device if acm else None


def find_devices(kbd_paths, mouse_paths):
    kbds, mice = [], []
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        # Never capture our own bridge Pico's phantom HID interfaces — that's the
        # idle loopback side and grabbing it is pointless (and risks a feedback
        # loop if the gadget's injected events ever surfaced locally).
        if "Rook" in d.name:
            d.close()
            continue
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
    ap = argparse.ArgumentParser(description="Rook HID-KVM Pico-bridge client")
    ap.add_argument("--port", help="bridge Pico serial port (auto-detect if omitted)")
    ap.add_argument("--kbd", action="append", help="explicit keyboard device path")
    ap.add_argument("--mouse", action="append", help="explicit pointer device path")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("no bridge Pico serial port found (looked for /dev/ttyACM*)",
              file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    sender = Sender(port)
    fwd = Forwarder(sender)

    kbds, mice = find_devices(args.kbd, args.mouse)
    if not kbds and not mice:
        print("no input devices found (need root or 'input' group)", file=sys.stderr)
        sys.exit(1)
    devices = kbds + mice
    mouse_fds = {d.fd for d in mice}

    print(f"bridge Pico   : {port}")
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

    HEARTBEAT_S = 0.025
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
