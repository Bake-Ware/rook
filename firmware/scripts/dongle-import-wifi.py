#!/usr/bin/env python3
"""dongle-import-wifi — copy the host PC's current Wi-Fi credentials onto the
R00K dongle's saved-network list via the USB serial CLI.

Runs on Linux (NetworkManager / iwd / wpa_supplicant), macOS, and Windows.

Usage:
    python3 dongle-import-wifi.py                 # auto-detect port, priority 2
    python3 dongle-import-wifi.py --port COM4
    python3 dongle-import-wifi.py --priority 1

Requirements:
    pip install pyserial   (only required on Windows; optional elsewhere)

Notes:
    - Linux: NetworkManager secret read needs sudo. If not running as root,
      you'll get a polkit/sudo prompt.
    - macOS: Keychain access prompts you to "Always Allow" when reading the PSK.
    - Windows: must run as the user who joined the SSID (no admin needed for
      `netsh wlan show profile key=clear` on profiles owned by you).
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ---------- serial helpers --------------------------------------------------

def open_port(port: str, baud: int = 115200):
    """Open a serial port. Prefer pyserial; fall back to raw file IO on POSIX."""
    try:
        import serial  # type: ignore
        s = serial.Serial(port, baud, timeout=0.3,
                          rtscts=False, dsrdtr=False)
        time.sleep(0.3)
        return _PySerialWrap(s)
    except ImportError:
        if os.name != "posix":
            sys.exit("pyserial required on this OS: pip install pyserial")
        # Raw-mode the tty and use open() — works on Linux/macOS.
        subprocess.run(["stty", "-F" if sys.platform.startswith("linux") else "-f",
                        port, str(baud), "raw", "-echo", "cs8", "-cstopb",
                        "-parenb"], check=True)
        f = open(port, "r+b", buffering=0)
        return _RawWrap(f)


class _PySerialWrap:
    def __init__(self, s):
        self.s = s

    def write(self, data: bytes):
        self.s.write(data)
        self.s.flush()

    def read_for(self, seconds: float) -> bytes:
        end = time.time() + seconds
        buf = bytearray()
        while time.time() < end:
            n = self.s.in_waiting
            if n:
                buf.extend(self.s.read(n))
            else:
                time.sleep(0.05)
        return bytes(buf)

    def close(self):
        self.s.close()


class _RawWrap:
    def __init__(self, f):
        self.f = f
        os.set_blocking(self.f.fileno(), False)

    def write(self, data: bytes):
        self.f.write(data)

    def read_for(self, seconds: float) -> bytes:
        end = time.time() + seconds
        buf = bytearray()
        while time.time() < end:
            try:
                chunk = self.f.read(4096)
            except BlockingIOError:
                chunk = None
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.05)
        return bytes(buf)

    def close(self):
        self.f.close()


def find_port() -> str | None:
    """Auto-detect a likely R00K dongle port."""
    candidates: list[str] = []
    sysname = platform.system()
    if sysname == "Linux":
        candidates = sorted(str(p) for p in Path("/dev").glob("ttyACM*"))
    elif sysname == "Darwin":
        candidates = sorted(str(p) for p in Path("/dev").glob("cu.usbmodem*"))
    elif sysname == "Windows":
        try:
            from serial.tools import list_ports  # type: ignore
            for p in list_ports.comports():
                vidpid = (p.vid or 0, p.pid or 0)
                if vidpid == (0x1209, 0x0001):
                    return p.device
                candidates.append(p.device)
        except ImportError:
            pass
    # Prefer ports whose udev info reports VID 1209 on Linux.
    if sysname == "Linux" and shutil.which("udevadm"):
        for c in candidates:
            try:
                info = subprocess.check_output(
                    ["udevadm", "info", "-q", "property", "-n", c],
                    text=True, stderr=subprocess.DEVNULL)
                if "1209" in info:
                    return c
            except subprocess.CalledProcessError:
                continue
    return candidates[0] if candidates else None


# ---------- per-OS credential readers --------------------------------------

def _run(cmd: list[str], **kw) -> str:
    try:
        return subprocess.check_output(cmd, text=True,
                                       stderr=subprocess.DEVNULL, **kw)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def linux_creds() -> tuple[str, str] | None:
    # NetworkManager
    if shutil.which("nmcli"):
        active = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
        ssid = next((ln.split(":", 1)[1] for ln in active.splitlines()
                     if ln.startswith("yes:")), "")
        if ssid:
            sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
            cmd = sudo + ["nmcli", "-s", "-g",
                          "802-11-wireless-security.psk",
                          "connection", "show", ssid]
            psk = _run(cmd).strip()
            if not psk and os.geteuid() != 0:
                # Need interactive sudo.
                print(f"reading PSK requires sudo for SSID '{ssid}'", file=sys.stderr)
                try:
                    psk = subprocess.check_output(
                        ["sudo", "nmcli", "-s", "-g",
                         "802-11-wireless-security.psk",
                         "connection", "show", ssid],
                        text=True).strip()
                except subprocess.CalledProcessError:
                    psk = ""
            if psk:
                return ssid, psk
    # iwd
    if shutil.which("iwctl"):
        dev_out = _run(["iwctl", "device", "list"])
        dev = ""
        for ln in dev_out.splitlines():
            parts = ln.split()
            if parts and not parts[0].startswith("-") and parts[0] != "Name":
                dev = parts[0]; break
        if dev:
            show = _run(["iwctl", "station", dev, "show"])
            m = re.search(r"Connected network\s+(.+)", show)
            if m:
                ssid = m.group(1).strip()
                psk_path = Path("/var/lib/iwd") / f"{ssid}.psk"
                if not psk_path.is_file():
                    psk_path = Path("/var/lib/iwd") / f"{ssid.encode().hex()}.psk"
                sudo = [] if os.geteuid() == 0 else ["sudo"]
                content = _run(sudo + ["cat", str(psk_path)])
                m2 = re.search(r"^Passphrase=(.+)$", content, re.MULTILINE)
                if m2:
                    return ssid, m2.group(1).strip()
    # wpa_supplicant.conf fallback
    cfg = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
    if cfg.is_file() and shutil.which("iwgetid"):
        ssid = _run(["iwgetid", "-r"]).strip()
        if ssid:
            sudo = [] if os.geteuid() == 0 else ["sudo"]
            text = _run(sudo + ["cat", str(cfg)])
            # naive scan for matching network block
            for block in re.finditer(r"network=\{([^}]+)\}", text, re.DOTALL):
                body = block.group(1)
                bs = re.search(r'ssid="([^"]+)"', body)
                bp = re.search(r'psk="?([^"\n]+)"?', body)
                if bs and bs.group(1) == ssid and bp:
                    return ssid, bp.group(1).strip()
    return None


def macos_creds() -> tuple[str, str] | None:
    if not shutil.which("networksetup"):
        return None
    out = _run(["networksetup", "-getairportnetwork", "en0"])
    m = re.search(r":\s+(.+)$", out.strip())
    if not m:
        return None
    ssid = m.group(1).strip()
    # security writes the password to stderr after "password: ".
    try:
        p = subprocess.run(["security", "find-generic-password", "-ga", ssid],
                           capture_output=True, text=True)
        combined = (p.stdout or "") + "\n" + (p.stderr or "")
    except FileNotFoundError:
        return None
    m2 = re.search(r'password:\s+"([^"]+)"', combined)
    if not m2:
        return None
    return ssid, m2.group(1)


def windows_creds() -> tuple[str, str] | None:
    if not shutil.which("netsh"):
        return None
    iface = _run(["netsh", "wlan", "show", "interfaces"])
    m = re.search(r"^\s*SSID\s+:\s+(.+)$", iface, re.MULTILINE)
    if not m:
        return None
    ssid = m.group(1).strip()
    profile = _run(["netsh", "wlan", "show", "profile",
                    f"name={ssid}", "key=clear"])
    m2 = re.search(r"Key Content\s+:\s+(.+)", profile)
    if not m2:
        return None
    return ssid, m2.group(1).strip()


def read_host_creds() -> tuple[str, str]:
    sysname = platform.system()
    creds = (linux_creds()    if sysname == "Linux"
             else macos_creds() if sysname == "Darwin"
             else windows_creds() if sysname == "Windows"
             else None)
    if not creds:
        sys.exit("could not read host WiFi credentials — connect to a Wi-Fi "
                 "network first, or run with elevated permissions.")
    return creds


# ---------- main -----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="Serial port (auto-detect if omitted)")
    ap.add_argument("--priority", type=int, default=2,
                    help="Priority to save the new entry at (default 2)")
    ap.add_argument("--ssid", help="Override host SSID (skip detection)")
    ap.add_argument("--psk",  help="Override host PSK (skip detection)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        sys.exit("no R00K dongle port found — plug it in or pass --port.")

    if args.ssid and args.psk:
        ssid, psk = args.ssid, args.psk
    else:
        ssid, psk = read_host_creds()

    print(f"host wifi : ssid='{ssid}'  psk=*** ({len(psk)} chars)")
    print(f"target    : {port}  (priority {args.priority})")

    s = open_port(port)
    try:
        # Wake prompt.
        s.write(b"\r\n"); time.sleep(0.3)
        s.write(b"\r\n"); time.sleep(0.3)
        _ = s.read_for(0.5)

        cmds = [
            f"wifi rm {ssid}\r\n",
            f"wifi add {ssid} {psk} {args.priority}\r\n",
            "wifi reconnect\r\n",
        ]
        for c in cmds:
            s.write(c.encode()); time.sleep(0.4)

        # Read response for ~8s (covers scan + connect).
        out = s.read_for(8.0).decode(errors="replace")
        s.write(b"status\r\n"); time.sleep(0.3)
        out += s.read_for(1.5).decode(errors="replace")
    finally:
        s.close()

    print("--- dongle reply ---")
    print(out.strip())
    print("--------------------")
    if f"connected ssid={ssid}" in out:
        print(f"OK: dongle connected to '{ssid}'.")
    else:
        print("note: dongle did not immediately report 'connected'. The save "
              "took, and the background monitor will retry within a minute.")


if __name__ == "__main__":
    main()
