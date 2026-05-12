"""Rook KVM Bridge — MCP server for controlling machines via USB HID/serial."""

import asyncio
import base64
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from .bridge import RookBridge

# MCP servers must only use stderr for logging (stdout is JSON-RPC).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rook-kvm] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rook-kvm")

BRIDGE_HOST = os.environ.get("ROOK_BRIDGE_HOST", "192.168.1.138")
BRIDGE_PORT = int(os.environ.get("ROOK_BRIDGE_PORT", "80"))

mcp = FastMCP("rook-kvm")
bridge = RookBridge(host=BRIDGE_HOST, port=BRIDGE_PORT)


@mcp.tool()
async def send_keystrokes(text: str) -> str:
    """Type text on the target machine via USB HID keyboard.

    Each character is sent as a separate keystroke. Supports printable ASCII.
    For special keys or modifier combos, use send_key_combo instead.
    """
    result = await bridge.type_text(text)
    return f"Typed {result['typed']} characters"


@mcp.tool()
async def send_key_combo(modifiers: list[str], key: str) -> str:
    """Send a key combination to the target machine.

    Args:
        modifiers: Modifier keys — "ctrl", "shift", "alt", "gui"/"win"/"super"
        key: Single char or special key: enter, tab, escape, backspace, delete,
             up, down, left, right, home, end, pageup, pagedown, f1-f12, space
    """
    await bridge.key_combo(modifiers, key)
    combo = "+".join(modifiers + [key])
    return f"Sent {combo}"


@mcp.tool()
async def send_consumer_key(key: str) -> str:
    """Send a Consumer Control HID code (TV remote-style keys).

    Works on TVs, monitors, OSes that respect USB HID Consumer Control.
    No mouse — this drives volume, mute, media transport, power.

    Args:
        key: One of — volume_up, volume_down, mute, play_pause, next, prev,
             stop, power, brightness_up, brightness_down, home, back.
    """
    result = await bridge.consumer(key=key)
    return f"Consumer {key} sent (code 0x{result.get('code',0):04X})"


@mcp.tool()
async def read_serial(timeout: int = 5) -> str:
    """Read data from the target's CDC serial port.

    Uses WebSocket stream if connected, falls back to HTTP polling.
    """
    # Try WebSocket first
    if bridge._ws:
        data = await bridge.read_serial_ws(timeout=timeout)
        return data if data else "(no data received)"

    # Fallback to HTTP polling
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    data = ""
    while loop.time() < deadline:
        result = await bridge.read_serial()
        if result.get("length", 0) > 0:
            data += result["data"]
            await bridge.clear_serial()
            await asyncio.sleep(0.5)
            result = await bridge.read_serial()
            if result.get("length", 0) > 0:
                data += result["data"]
                await bridge.clear_serial()
            break
        await asyncio.sleep(0.25)
    return data if data else "(no data received)"


@mcp.tool()
async def write_serial(data: str) -> str:
    """Write data to the target's CDC serial port."""
    if bridge._ws:
        await bridge.write_serial_ws(data)
        return f"Wrote {len(data)} bytes (WebSocket)"
    result = await bridge.write_serial(data)
    return f"Wrote {result['written']} bytes"


@mcp.tool()
async def run_command(cmd: str, timeout: int = 10, serial_device: str = "/dev/ttyACM0") -> str:
    """Run a shell command on the target and capture output via serial.

    Types the command with stdout/stderr redirected to the CDC serial device,
    then reads back the output. Assumes a Linux target with a shell prompt.
    """
    marker = "__ROOK_END__"
    full_cmd = f'{cmd} > {serial_device} 2>&1; echo "{marker}" > {serial_device}\n'

    if bridge._ws:
        bridge._serial_buffer.clear()
        await bridge.type_text(full_cmd)

        output = ""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            chunk = await bridge.read_serial_ws(timeout=0.25)
            output += chunk
            if marker in output:
                output = output[: output.index(marker)].rstrip()
                break
        return output if output else f"(no output after {timeout}s)"

    # HTTP fallback
    await bridge.clear_serial()
    await bridge.type_text(full_cmd)

    output = ""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = await bridge.read_serial()
        if result.get("length", 0) > 0:
            output += result["data"]
            await bridge.clear_serial()
            if marker in output:
                output = output[: output.index(marker)].rstrip()
                break
        await asyncio.sleep(0.25)
    return output if output else f"(no output after {timeout}s)"


@mcp.tool()
async def get_status() -> str:
    """Get the Rook KVM bridge device status, including storage mode and HID state."""
    status = await bridge.status()
    lines = [
        f"Device:  {status.get('device', '?')}",
        f"Version: {status.get('version', '?')}",
        f"WiFi:    {status.get('wifi_mode', '?')} @ {status.get('ip', '?')}",
    ]
    if status.get("ap_ip"):
        lines.append(f"  AP IP: {status['ap_ip']}")
    lines += [
        f"Storage mode: {status.get('storage_mode', '?')}",
        f"HID enabled:  {status.get('hid_enabled', '?')}",
        f"Serial:       {status.get('serial_buffered', 0)} bytes buffered",
        f"Storage:      {status.get('storage', 'unknown')}",
    ]
    if status.get("storage") == "ok":
        lines.append(f"  Total: {status.get('storage_total_mb', '?')}MB")
        lines.append(f"  Used:  {status.get('storage_used_mb', '?')}MB")
    lines.append(f"Uptime:  {status.get('uptime_ms', 0) / 1000:.1f}s")
    return "\n".join(lines)


@mcp.tool()
async def get_device_mode() -> str:
    """Return the current SD-card ownership mode: 'internal' or 'msc'.

    - 'internal': firmware owns the SD card; /telesthete/drop endpoints work.
    - 'msc': host PC owns the SD card via USB Mass Storage; firmware file ops return 503.
    """
    result = await bridge.get_mode()
    return result.get("mode", "?")


@mcp.tool()
async def set_device_mode(mode: str) -> str:
    """Switch storage mode between 'internal' and 'msc'. **The device will reboot.**

    Boot takes ~3-5 seconds; subsequent calls may fail until it's back online.
    Use the physical button on the dongle for the same effect (short press toggles).

    Args:
        mode: 'internal' or 'msc'
    """
    if mode not in ("internal", "msc"):
        return f"error: mode must be 'internal' or 'msc', got {mode!r}"
    result = await bridge.set_mode(mode)
    note = " (response was lost — device is rebooting, this is normal)" if result.get("response_lost") else ""
    return f"Switching to mode={result.get('mode', mode)}; device rebooting{note}."


@mcp.tool()
async def get_hid_enabled() -> str:
    """Return whether HID input (typing/keys) is currently enabled on the dongle."""
    result = await bridge.get_hid_enabled()
    return "enabled" if result.get("enabled") else "disabled"


@mcp.tool()
async def set_hid_enabled(enabled: bool) -> str:
    """Toggle the HID kill-switch. When disabled, /type and /key return 503.

    Useful as a safety lock when running exploratory work on the target —
    prevents runaway keystrokes. Runtime change, no reboot.

    Args:
        enabled: True to allow HID input, False to disable it.
    """
    result = await bridge.set_hid_enabled(enabled)
    return f"HID {'enabled' if result.get('enabled') else 'disabled'}"


@mcp.tool()
async def reboot_to_bootloader() -> str:
    """Reboot the dongle into ROM bootloader mode (USB VID:PID 303A:1001).

    Lets the host flash firmware via esptool/PlatformIO without manually
    holding the BOOT button while plugging in.

    Workflow: call this tool → device enumerates as bootloader → run
    `pio run -t upload` → after flashing, **unplug and replug the dongle
    once** to power-cycle (the RTC flag is sticky across soft resets but
    clears on power loss).
    """
    result = await bridge.reboot_to_bootloader()
    note = " (HTTP response was lost as device rebooted — expected)" if result.get("response_lost") else ""
    return f"Device rebooting into ROM bootloader.{note} After flashing, unplug + replug to boot the new firmware."


@mcp.tool()
async def ota_flash(firmware_path: str = "") -> str:
    """Wirelessly flash a new firmware image. Streams firmware.bin over HTTP
    to the device's /ota route. Device reboots into the new image on success.

    Args:
        firmware_path: Absolute path to firmware.bin. Defaults to the
                       PlatformIO build artifact for the t-dongle-s3 env.
    """
    if not firmware_path:
        firmware_path = "/home/bake/Projects/R00K/firmware/.pio/build/t-dongle-s3/firmware.bin"
    result = await bridge.ota(firmware_path)
    if result.get("response_lost"):
        return f"OTA upload sent ({result['uploaded_bytes']} bytes). Device rebooted before responding (expected). Re-check /status in ~10s."
    return f"OTA complete: {result.get('written',result['uploaded_bytes'])} bytes written. Device rebooting."


@mcp.tool()
async def get_config_page() -> str:
    """Fetch the rendered HTML of the /config admin page. Useful for previewing
    current settings without opening a browser. Auth handled automatically."""
    html = await bridge.get_config()
    return html


@mcp.tool()
async def update_config(
    ap_ssid: str = "", ap_pass: str = "",
    sta_ssid: str = "", sta_pass: str = "",
    admin_user: str = "", admin_pass: str = "",
) -> str:
    """Update one or more persistent device settings. Empty strings are skipped.
    Device reboots after save — any unchanged-network reconnection takes ~10s.

    Args:
        ap_ssid: New SSID for the dongle's own AP (RookBridge by default).
        ap_pass: New AP password (>= 8 chars for WPA2, else open).
        sta_ssid: SSID of the upstream WiFi the dongle joins.
        sta_pass: STA password.
        admin_user: New HTTP basic-auth username for /config and /ota.
        admin_pass: New HTTP basic-auth password.
    """
    fields = {k: v for k, v in {
        "ap_ssid": ap_ssid, "ap_pass": ap_pass,
        "sta_ssid": sta_ssid, "sta_pass": sta_pass,
        "admin_user": admin_user, "admin_pass": admin_pass,
    }.items() if v}
    if not fields:
        return "No fields to update."
    result = await bridge.set_config(**fields)
    note = " (response lost during reboot — expected)" if result.get("response_lost") else ""
    return f"Updated {', '.join(result['updated'])}.{note} Device rebooting."


@mcp.tool()
async def factory_reset() -> str:
    """Wipe persisted device settings (NVS) and reboot with compile-time defaults.

    Warning: Resets admin credentials, AP/STA SSID and passwords to factory
    values. Only use if locked out or migrating between devices.
    """
    result = await bridge.factory_reset()
    note = " (response lost — expected)" if result.get("response_lost") else ""
    return f"Factory reset triggered.{note} Device rebooting with compile-time defaults."


@mcp.tool()
async def take_screenshot(timeout: int = 30) -> str:
    """Capture a screenshot from the target Linux machine.

    Takes a screenshot, compresses to JPEG, sends over serial using the
    ROOK_FILE protocol, saves to TF card, then downloads and returns as base64.

    Requires: spectacle or grim + imagemagick on the target.
    """
    await bridge.clear_serial()

    cmd = (
        '(spectacle -b -f -o /tmp/sc.png 2>/dev/null || grim /tmp/sc.png 2>/dev/null) && '
        'magick /tmp/sc.png -resize 640x480 -quality 25 /tmp/sc.jpg && '
        'echo "<<<ROOK_FILE:screenshot.jpg;base64>>>" > /dev/ttyACM0 && '
        'base64 /tmp/sc.jpg > /dev/ttyACM0 && '
        'echo "<<<ROOK_EOF>>>" > /dev/ttyACM0\n'
    )
    await bridge.type_text(cmd, delay_ms=8)

    # Wait for file_ready notification or poll file list
    if bridge._ws:
        evt = await bridge.wait_for_file("screenshot.jpg", timeout=timeout)
        if not evt:
            return "(screenshot transfer timed out)"
    else:
        deadline = asyncio.get_event_loop().time() + timeout
        found = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                files = await bridge.list_files()
                if any(f.get("name") == "screenshot.jpg" for f in files):
                    found = True
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        if not found:
            return "(screenshot transfer timed out)"

    data = await bridge.download_file("screenshot.jpg")
    await bridge.delete_file("screenshot.jpg")
    return base64.b64encode(data).decode()


@mcp.tool()
async def list_drop_files() -> str:
    """List files on the KVM bridge's TF card (Drop zone)."""
    files = await bridge.list_files()
    if not files:
        return "(no files)"
    lines = []
    for f in files:
        if f.get("isDir"):
            lines.append(f"  DIR  {f['name']}")
        else:
            lines.append(f"  {f['size']:>8}  {f['name']}")
    return "\n".join(lines)


@mcp.tool()
async def download_drop_file(path: str) -> str:
    """Download a file from the KVM bridge TF card and return as base64.

    Args:
        path: Filename on the TF card.
    """
    data = await bridge.download_file(path)
    return base64.b64encode(data).decode()


def main():
    log.info("rook-kvm MCP starting (bridge=%s)", BRIDGE_HOST)
    mcp.run()
    log.info("rook-kvm MCP exited")


if __name__ == "__main__":
    main()
