"""Smoke-test the live dongle via the same bridge code the MCP tools use.

Runs every documented endpoint that doesn't require a target-side helper
(screenshots and run_command need a CDC listener on the target — out of
scope for this validation).

Usage: python validate_dongle.py [--host 192.168.1.138]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from rook_kvm.bridge import RookBridge


PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


async def run(host: str) -> int:
    bridge = RookBridge(host=host)
    failed = 0

    print(f"--- Validating dongle at {host} ---")

    try:
        status = await bridge.status()
        print(f"{PASS} status: device={status['device']} v{status['version']} "
              f"ip={status['ip']} uptime={status['uptime_ms']/1000:.1f}s "
              f"storage={status.get('storage_used_mb',0)}/{status.get('storage_total_mb',0)}MB")
    except Exception as e:
        print(f"{FAIL} status: {e}")
        failed += 1
        return failed  # can't continue if status fails

    try:
        await bridge.clear_serial()
        print(f"{PASS} clear_serial")
    except Exception as e:
        print(f"{FAIL} clear_serial: {e}")
        failed += 1

    try:
        result = await bridge.type_text("", delay_ms=10)
        print(f"{PASS} type_text (empty echo): {result}")
    except Exception as e:
        print(f"{FAIL} type_text empty: {e}")
        failed += 1

    try:
        files = await bridge.list_files()
        print(f"{PASS} list_files: {len(files)} files on TF card")
        for f in files[:5]:
            print(f"    {f.get('size','?'):>8}  {f.get('name','?')}")
    except Exception as e:
        print(f"{FAIL} list_files: {e}")
        failed += 1

    if files:
        target = files[0]["name"]
        try:
            data = await bridge.download_file(target)
            print(f"{PASS} download_file({target}): {len(data)} bytes")
        except Exception as e:
            print(f"{FAIL} download_file({target}): {e}")
            failed += 1
    else:
        print(f"{SKIP} download_file: no files to download")

    try:
        result = await bridge.read_serial()
        print(f"{PASS} read_serial (HTTP): {result['length']} bytes buffered")
    except Exception as e:
        print(f"{FAIL} read_serial: {e}")
        failed += 1

    try:
        await bridge.connect_stream()
        await asyncio.sleep(0.5)
        if bridge._ws:
            print(f"{PASS} connect_stream: WebSocket established")
        else:
            print(f"{FAIL} connect_stream: ws still None")
            failed += 1
    except Exception as e:
        print(f"{FAIL} connect_stream: {e}")
        failed += 1

    print(f"{SKIP} type_text (real input): not run automatically — would type "
          "on the host attached to the dongle. Use the MCP tool interactively.")
    print(f"{SKIP} key_combo: same reason as above.")
    print(f"{SKIP} run_command: requires target-side CDC listener on /dev/ttyACM0.")
    print(f"{SKIP} take_screenshot: requires Linux target-side helper (grim/magick).")

    try:
        await bridge.close()
    except Exception:
        pass

    print(f"--- {failed} failure(s) ---")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ROOK_BRIDGE_HOST", "192.168.1.138"))
    args = ap.parse_args()
    return asyncio.run(run(args.host))


if __name__ == "__main__":
    sys.exit(main())
