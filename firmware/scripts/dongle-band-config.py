#!/usr/bin/env python3
"""Fetch one band's settings with a pairing code and prepare a private build.

    python3 firmware/scripts/dongle-band-config.py --server https://rook.example.com --udp-hub hub.example.com:7474

Prompts for the six-character code shown on Tokens. Writes a git-ignored
band_secrets.h; Wi-Fi and admin settings remain in secrets.h. Rebuild/flash
locally afterwards. The resulting firmware contains the permanent PSK.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def render_config(config: dict) -> str:
    hub = config.get("hub", "")
    psk = config.get("psk", "")
    if not isinstance(hub, str) or not isinstance(psk, str) or not psk:
        raise ValueError("server returned incomplete band settings")
    host, sep, port = hub.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535 or not host:
        raise ValueError("hub must be host:port")
    # Firmware speaks UDP. A web/WS endpoint on 443 is not a UDP hub address.
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("dongle hub must be a hostname or IPv4 address")
    return ("// Private band settings. Generated locally; do not publish.\n#pragma once\n"
            "#undef HUB_HOST\n#undef HUB_PORT\n#undef BAND_PSK\n"
            f"#define HUB_HOST {json.dumps(host)}\n#define HUB_PORT {int(port)}\n"
            f"#define BAND_PSK {json.dumps(psk)}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="HTTPS Rook installer origin")
    parser.add_argument("--code", help="current six-character pairing code (otherwise prompted)")
    parser.add_argument("--udp-hub", required=True, help="dongle's reachable UDP hub, e.g. hub.example.com:7474")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "include" / "band_secrets.h")
    args = parser.parse_args()
    url = urlsplit(args.server)
    if (url.scheme != "https" or not url.netloc or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        parser.error("--server must be an HTTPS origin, without a path or credentials")
    code = args.code or getpass.getpass("Pairing code: ")
    if not re.fullmatch(r"[a-z0-9]{6}", code):
        parser.error("pairing code must be six lowercase letters/digits")
    request = Request(args.server.rstrip("/") + "/enroll",
                      data=json.dumps({"code": code}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=20) as response:
        config = json.load(response)
    config["hub"] = args.udp_hub
    content = render_config(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=args.output.parent, prefix=".band-secrets-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.replace(name, args.output)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Wrote {args.output}. Build and flash locally; keep the firmware private.")
    print("Saved NVS settings take precedence. For an already configured dongle, update its band/hub settings too.")


if __name__ == "__main__":
    main()
