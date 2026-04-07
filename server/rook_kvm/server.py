"""Rook KVM Bridge — MCP server for controlling machines via USB HID/serial."""

import asyncio
import base64
import os

from mcp.server.fastmcp import FastMCP
from .bridge import RookBridge

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
    """Get the Rook KVM bridge device status."""
    status = await bridge.status()
    lines = [
        f"Device:  {status.get('device', '?')}",
        f"Version: {status.get('version', '?')}",
        f"WiFi:    {status.get('wifi_mode', '?')} @ {status.get('ip', '?')}",
        f"Serial:  {status.get('serial_buffered', 0)} bytes buffered",
        f"Storage: {status.get('storage', 'unknown')}",
    ]
    if status.get("storage") == "ok":
        lines.append(f"  Total: {status.get('storage_total_mb', '?')}MB")
        lines.append(f"  Used:  {status.get('storage_used_mb', '?')}MB")
    lines.append(f"Uptime:  {status.get('uptime_ms', 0) / 1000:.1f}s")
    return "\n".join(lines)


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
    mcp.run()
