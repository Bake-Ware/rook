#!/usr/bin/env python3
"""Rook remote worker — runs on remote machines, connects back to Rook.

Bootstrap:
  Windows (PowerShell):
    iex (irm https://rook.bake.systems/worker)
  Linux/Mac:
    curl -sL https://rook.bake.systems/worker | python3 -

Manual:
    python worker.py --name mypc --server wss://rook.bake.systems/ws --token SECRET
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import textwrap


# ── Platform detection ───────────────────────────────────────────────────────

def is_termux() -> bool:
    """Detect if running inside Termux on Android."""
    return os.path.isdir("/data/data/com.termux") or "com.termux" in os.environ.get("PREFIX", "")


def setup_termux() -> None:
    """Install termux-api and configure for Rook."""
    print("[r00k] Termux detected! Setting up Android integration...")

    # Install termux-api package if not present
    if not shutil.which("termux-notification"):
        print("[r00k] Installing termux-api tools...")
        subprocess.run(["pkg", "install", "-y", "termux-api"], capture_output=True)

    # Make sure python and websockets are available
    if not shutil.which("python3") and not shutil.which("python"):
        print("[r00k] Installing python...")
        subprocess.run(["pkg", "install", "-y", "python"], capture_output=True)

    # Acquire wakelock so Android doesn't kill us
    print("[r00k] Acquiring wakelock...")
    subprocess.Popen(["termux-wake-lock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Send a notification
    try:
        subprocess.run([
            "termux-notification",
            "--title", "R☠☠K Worker",
            "--content", "Connected to Rook",
            "--ongoing",
            "--id", "rook-worker",
        ], capture_output=True)
    except Exception:
        pass

    print("[r00k] Termux setup complete")
    print("[r00k] Available Android commands:")
    print("         termux-sms-send, termux-sms-list")
    print("         termux-notification, termux-clipboard-get/set")
    print("         termux-location, termux-camera-photo")
    print("         termux-volume, termux-torch, termux-vibrate")
    print("         termux-battery-status, termux-wifi-connectioninfo")
    print()


# ── Service install ──────────────────────────────────────────────────────────

SYSTEMD_UNIT = """[Unit]
Description=Rook Remote Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} {script} --server {server} --token {token} --name {name}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

def install_service_linux(script_path: str, server: str, token: str, name: str) -> None:
    python = shutil.which("python3") or sys.executable
    install_dir = os.path.expanduser("~/.local/share/rook")
    os.makedirs(install_dir, exist_ok=True)
    dest = os.path.join(install_dir, "worker.py")
    shutil.copy2(script_path, dest)

    unit = SYSTEMD_UNIT.format(
        python=python, script=dest,
        server=server, token=token, name=name,
    )

    # Try user systemd first, fall back to system
    user_unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(user_unit_dir, exist_ok=True)
    unit_path = os.path.join(user_unit_dir, "rook-worker.service")
    with open(unit_path, "w") as f:
        f.write(unit)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "rook-worker"], check=True)
    subprocess.run(["systemctl", "--user", "start", "rook-worker"], check=True)
    # Enable lingering so user services run without login
    subprocess.run(["loginctl", "enable-linger"], capture_output=True)

    # Install "rook" CLI command
    cli_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(cli_dir, exist_ok=True)
    cli_path = os.path.join(cli_dir, "rook")
    with open(cli_path, "w") as f:
        f.write(f"""#!/bin/bash
exec {python} {dest} --server {server} --token {token} --name {name} --no-service "$@"
""")
    os.chmod(cli_path, 0o755)

    print(f"[r00k] Service installed (user systemd) and started")
    print(f"[r00k] CLI installed at {cli_path}")
    print(f"[r00k]   systemctl --user status rook-worker")
    print(f"[r00k]   journalctl --user -u rook-worker -f")


def install_service_windows(script_path: str, server: str, token: str, name: str) -> None:
    python = sys.executable
    install_dir = os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "rook")
    os.makedirs(install_dir, exist_ok=True)
    dest = os.path.join(install_dir, "worker.py")
    shutil.copy2(script_path, dest)

    # Use NSSM if available, otherwise create a scheduled task that runs at startup
    nssm = shutil.which("nssm")
    if nssm:
        subprocess.run([nssm, "install", "RookWorker", python, dest,
                       "--server", server, "--token", token, "--name", name], check=True)
        subprocess.run([nssm, "set", "RookWorker", "AppStdout", os.path.join(install_dir, "stdout.log")], check=True)
        subprocess.run([nssm, "set", "RookWorker", "AppStderr", os.path.join(install_dir, "stderr.log")], check=True)
        subprocess.run([nssm, "start", "RookWorker"], check=True)
        print(f"[r00k] NSSM service installed and started")
    else:
        # Fallback: scheduled task at logon
        cmd = f'"{python}" "{dest}" --server {server} --token {token} --name {name}'
        subprocess.run([
            "schtasks", "/create", "/tn", "RookWorker",
            "/tr", cmd, "/sc", "onlogon", "/rl", "highest", "/f",
        ], check=True)
        # Also start it now
        subprocess.Popen(
            [python, dest, "--server", server, "--token", token, "--name", name],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        print(f"[r00k] Scheduled task created (runs at logon) and started now")
        print(f"[r00k]   schtasks /query /tn RookWorker")

    # Install "rook" CLI command as a batch file in a PATH directory
    cli_dir = os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "rook")
    cli_bat = os.path.join(cli_dir, "rook.bat")
    with open(cli_bat, "w") as f:
        f.write(f'@echo off\n"{python}" "{dest}" --server {server} --token {token} --name {name} --no-service %*\n')

    # Add to PATH if not already there
    current_path = os.environ.get("PATH", "")
    if cli_dir.lower() not in current_path.lower():
        subprocess.run(
            ["setx", "PATH", f"{current_path};{cli_dir}"],
            capture_output=True,
        )
        print(f"[r00k] Added {cli_dir} to PATH (restart terminal to use)")

    print(f"[r00k] CLI installed — type 'rook' anywhere to chat")


def install_service_termux(script_path: str, server: str, token: str, name: str) -> None:
    """Install as a Termux:Boot script that runs on device boot."""
    boot_dir = os.path.expanduser("~/.termux/boot")
    os.makedirs(boot_dir, exist_ok=True)

    # Copy worker script
    install_dir = os.path.expanduser("~/rook")
    os.makedirs(install_dir, exist_ok=True)
    dest = os.path.join(install_dir, "worker.py")
    shutil.copy2(script_path, dest)

    python = shutil.which("python3") or shutil.which("python") or "python3"

    # Create boot script
    boot_script = os.path.join(boot_dir, "rook-worker.sh")
    with open(boot_script, "w") as f:
        f.write(f"""#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
{python} {dest} --server {server} --token {token} --name {name} --no-service &
""")
    os.chmod(boot_script, 0o755)

    # Install "rook" CLI command
    cli_path = os.path.expanduser("~/../usr/bin/rook")
    with open(cli_path, "w") as f:
        f.write(f"""#!/data/data/com.termux/files/usr/bin/bash
exec {python} {dest} --server {server} --token {token} --name {name} --no-service "$@"
""")
    os.chmod(cli_path, 0o755)

    print("[r00k] Termux:Boot script installed")
    print("[r00k]   Requires Termux:Boot app from F-Droid")
    print(f"[r00k]   Script: {boot_script}")
    print("[r00k] CLI installed — type 'rook' anywhere to chat")

    # Start it now in background
    subprocess.Popen(
        [python, dest, "--server", server, "--token", token, "--name", name, "--no-service"],
    )
    print("[r00k] Worker started in background")


def uninstall_linux() -> None:
    """Remove service, CLI, and all rook files on Linux."""
    # Stop and remove user systemd service
    subprocess.run(["systemctl", "--user", "stop", "rook-worker"], capture_output=True)
    subprocess.run(["systemctl", "--user", "disable", "rook-worker"], capture_output=True)
    unit_path = os.path.expanduser("~/.config/systemd/user/rook-worker.service")
    if os.path.exists(unit_path):
        os.remove(unit_path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # Remove files
    install_dir = os.path.expanduser("~/.local/share/rook")
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir)
    cli_path = os.path.expanduser("~/.local/bin/rook")
    if os.path.exists(cli_path):
        os.remove(cli_path)
    venv_dir = os.path.expanduser("~/.rook-venv")
    if os.path.isdir(venv_dir):
        shutil.rmtree(venv_dir)

    print("[r00k] Uninstalled: service, CLI, venv, and all files removed.")


def uninstall_windows() -> None:
    """Remove service, CLI, and all rook files on Windows."""
    nssm = shutil.which("nssm")
    if nssm:
        subprocess.run([nssm, "stop", "RookWorker"], capture_output=True)
        subprocess.run([nssm, "remove", "RookWorker", "confirm"], capture_output=True)
    subprocess.run(["schtasks", "/delete", "/tn", "RookWorker", "/f"], capture_output=True)

    install_dir = os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "rook")
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir)
    venv_dir = os.path.join(os.path.expanduser("~"), ".rook-venv")
    if os.path.isdir(venv_dir):
        shutil.rmtree(venv_dir)

    print("[r00k] Uninstalled: service, CLI, venv, and all files removed.")


def uninstall_termux() -> None:
    """Remove boot script, CLI, and all rook files on Termux."""
    boot_script = os.path.expanduser("~/.termux/boot/rook-worker.sh")
    if os.path.exists(boot_script):
        os.remove(boot_script)
    install_dir = os.path.expanduser("~/rook")
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir)
    cli_path = os.path.expanduser("~/../usr/bin/rook")
    if os.path.exists(cli_path):
        os.remove(cli_path)
    venv_dir = os.path.expanduser("~/.rook-venv")
    if os.path.isdir(venv_dir):
        shutil.rmtree(venv_dir)
    subprocess.run(["termux-wake-unlock"], capture_output=True)

    print("[r00k] Uninstalled: boot script, CLI, venv, and all files removed.")


def uninstall() -> None:
    """Uninstall rook worker from this machine."""
    if is_termux():
        uninstall_termux()
    elif platform.system().lower() == "linux":
        uninstall_linux()
    elif platform.system().lower() == "windows":
        uninstall_windows()
    else:
        print(f"[r00k] Uninstall not supported on {platform.system()}")


def is_installed() -> bool:
    """Check if rook worker is installed as a service."""
    if is_termux():
        return os.path.exists(os.path.expanduser("~/.termux/boot/rook-worker.sh"))
    elif platform.system().lower() == "linux":
        return os.path.exists(os.path.expanduser("~/.config/systemd/user/rook-worker.service"))
    elif platform.system().lower() == "windows":
        r = subprocess.run(["schtasks", "/query", "/tn", "RookWorker"], capture_output=True)
        return r.returncode == 0
    return False


def offer_service_install(script_path: str, server: str, token: str, name: str) -> bool:
    """Ask user if they want to install/uninstall the service. Returns True if installed."""
    installed = is_installed()

    if installed:
        print("\n[r00k] Worker service is currently installed.")
        try:
            answer = input("       Keep installed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return True
        if answer in ("n", "no"):
            uninstall()
            print("[r00k] Done. Run the bootstrap again to reinstall.")
            raise SystemExit(0)
        return True
    else:
        print("\n[r00k] Install as a persistent service?")
        print("       Auto-starts on boot, reconnects if disconnected.")
        print()
        try:
            answer = input("       Install? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer not in ("y", "yes"):
            return False

    plat = platform.system().lower()
    try:
        if is_termux():
            install_service_termux(script_path, server, token, name)
        elif plat == "linux":
            install_service_linux(script_path, server, token, name)
        elif plat == "windows":
            install_service_windows(script_path, server, token, name)
        else:
            print(f"[r00k] Service install not supported on {plat}")
            return False
        return True
    except Exception as e:
        print(f"[r00k] Service install failed: {e}")
        print(f"[r00k] You may need to run as root/admin")
        return False


# ── Command execution ────────────────────────────────────────────────────────

async def run_command(command: str) -> dict:
    """Execute a shell command and return the result."""
    try:
        if platform.system() == "Windows":
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        return {
            "stdout": stdout.decode("utf-8", errors="replace")[:16000],
            "stderr": stderr.decode("utf-8", errors="replace")[:4000],
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        return {"stdout": "", "stderr": "Command timed out (300s)", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


# ── WebSocket connection ─────────────────────────────────────────────────────

async def connect(server: str, name: str, token: str,
                  offer_install: bool = False, script_path: str = "") -> None:
    """Connect to Rook and process commands."""
    try:
        import websockets
    except ImportError:
        print("[r00k] websockets not found, setting up environment...")

        # Try direct pip install first
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "websockets", "-q"],
                stderr=subprocess.DEVNULL,
            )
            import websockets
        except (subprocess.CalledProcessError, ImportError):
            # Externally managed (PEP 668) or no pip — use a venv
            venv_dir = os.path.join(os.path.expanduser("~"), ".rook-venv")
            venv_python = os.path.join(venv_dir, "bin", "python3") if platform.system() != "Windows" \
                else os.path.join(venv_dir, "Scripts", "python.exe")

            if not os.path.exists(venv_python):
                print(f"[r00k] Creating venv at {venv_dir}...")
                subprocess.check_call([sys.executable, "-m", "venv", venv_dir])

            print("[r00k] Installing websockets in venv...")
            subprocess.check_call([venv_python, "-m", "pip", "install", "websockets", "-q"])

            # Re-exec ourselves under the venv python
            print("[r00k] Restarting under venv...")
            argv = sys.argv + (["--no-banner"] if "--no-banner" not in sys.argv else [])
            os.execv(venv_python, [venv_python] + argv)
            # execv replaces the process, so we never reach here

    # Service install prompt — only after venv/deps are resolved
    if offer_install and sys.stdin.isatty() and script_path:
        installed = offer_service_install(script_path, server, token, name)
        if installed:
            print("[r00k] Service installed and running in the background.")
            print("[r00k] Starting chat session...\n")

    hostname = socket.gethostname()
    plat = "android" if is_termux() else platform.system().lower()

    # If interactive and a service is running, use a different name to avoid duplicates
    if sys.stdin.isatty() and is_installed():
        name = f"{name}-cli"

    print(f"[r00k] Connecting to {server} as '{name}' ({plat}/{hostname})...")

    _user_quit = False

    while not _user_quit:
        try:
            async with websockets.connect(server, ping_interval=30, ping_timeout=300) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "name": name,
                    "platform": plat,
                    "hostname": hostname,
                    "token": token,
                }))

                response = await ws.recv()
                data = json.loads(response)
                if data.get("type") == "registered":
                    worker_id = data.get("id", "?")
                    print(f"[r00k] Connected! Worker ID: {worker_id}")
                else:
                    print(f"[r00k] Registration failed: {data}")
                    return

                async def heartbeat():
                    while True:
                        try:
                            await ws.send(json.dumps({"type": "heartbeat"}))
                            await asyncio.sleep(30)
                        except Exception:
                            break

                hb_task = asyncio.create_task(heartbeat())

                # Chat input loop — runs concurrently with command listener
                # Handles multi-line pastes by buffering with a short timeout
                async def input_loop():
                    import select as _select

                    def _read_paste():
                        """Read first line with prompt, then drain any buffered paste lines."""
                        first = input(" ~ ")
                        lines = [first]
                        # Check if more data is waiting (paste)
                        try:
                            while _select.select([sys.stdin], [], [], 0.05)[0]:
                                extra = sys.stdin.readline()
                                if extra:
                                    lines.append(extra.rstrip("\n"))
                                else:
                                    break
                        except (OSError, ValueError):
                            pass  # select not supported (Windows)
                        return "\n".join(lines)

                    loop = asyncio.get_event_loop()
                    while True:
                        try:
                            content = await loop.run_in_executor(None, _read_paste)
                            content = content.strip()
                            if not content:
                                continue
                            if content.lower() in ("/quit", "/exit", "quit", "exit"):
                                print("[r00k] Bye.")
                                nonlocal _user_quit
                                _user_quit = True
                                await ws.close()
                                return
                            # Show line count for pastes
                            line_count = content.count("\n") + 1
                            if line_count > 1:
                                print(f" ♖ ... ({line_count} lines)", end="", flush=True)
                            else:
                                print(" ♖ ...", end="", flush=True)
                            await ws.send(json.dumps({
                                "type": "chat",
                                "content": content,
                            }))
                        except (EOFError, KeyboardInterrupt):
                            return
                        except Exception:
                            return

                # Only start input loop if we have a terminal
                input_task = None
                if sys.stdin.isatty():
                    input_task = asyncio.create_task(input_loop())
                    print("[r00k] Chat active. /quit to exit.\n")

                try:
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            if data.get("type") == "update":
                                new_script = data.get("script", "")
                                req_id = data.get("id", "")
                                if new_script:
                                    print("\r[r00k] Update received, applying...")
                                    script_file = os.path.abspath(sys.argv[0])
                                    with open(script_file, "w", encoding="utf-8") as f:
                                        f.write(new_script)
                                    await ws.send(json.dumps({
                                        "type": "result", "id": req_id,
                                        "stdout": "Updated. Restarting...",
                                        "stderr": "", "returncode": 0,
                                    }))
                                    await ws.close()
                                    # Re-exec ourselves
                                    os.execv(sys.executable, [sys.executable] + sys.argv)
                            elif data.get("type") == "uninstall":
                                print("\r[r00k] Remote uninstall requested...")
                                uninstall()
                                await ws.send(json.dumps({
                                    "type": "result",
                                    "id": data.get("id", ""),
                                    "stdout": "Uninstalled successfully",
                                    "stderr": "",
                                    "returncode": 0,
                                }))
                                await ws.close()
                                raise SystemExit(0)
                            elif data.get("type") == "exec":
                                req_id = data.get("id", "")
                                command = data.get("command", "")
                                print(f"\r[r00k] Executing: {command[:80]}")

                                result = await run_command(command)
                                result["type"] = "result"
                                result["id"] = req_id

                                await ws.send(json.dumps(result))
                                print(f"[r00k] Done (exit={result['returncode']})")
                                if sys.stdin.isatty():
                                    print(" ~ ", end="", flush=True)
                            elif data.get("type") == "tool_status":
                                content = data.get("content", "")
                                print(f"\r {content}          ", end="", flush=True)
                            elif data.get("type") == "chat_response":
                                content = data.get("content", "")
                                print(f"\r ♖ {content}\n")
                                if sys.stdin.isatty():
                                    print(" ~ ", end="", flush=True)
                        except json.JSONDecodeError:
                            pass
                finally:
                    hb_task.cancel()
                    if input_task:
                        input_task.cancel()

        except Exception as e:
            if _user_quit:
                return
            print(f"[r00k] Disconnected: {e}")
            print("[r00k] Reconnecting in 5s...")
            await asyncio.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Rook remote worker")
    parser.add_argument("--name", default=socket.gethostname(), help="Worker name (default: hostname)")
    parser.add_argument("--server", default="wss://rook.bake.systems/ws", help="Rook WebSocket URL")
    parser.add_argument("--token", default="", help="Auth token (PSK)")
    parser.add_argument("--no-service", action="store_true", help="Skip service install prompt")
    parser.add_argument("--no-banner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.no_banner:
        print("""
⠀⠀⠀⠀⠀⠀⢠⣤⣤⡀⠀⠀⢀⣤⣤⣤⣤⡀⠀⠀⢀⣤⣤⡄⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⢸⣿⣿⡇⠀⠀⢸⣿⣿⣿⣿⡇⠀⠀⢸⣿⣿⡇⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⢸⣿⣿⣧⣤⣤⣼⣿⣿⣿⣿⣧⣤⣤⣼⣿⣿⡇⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠀⠀⠀⠀⠀⠀
""")

    script_path = os.path.abspath(__file__)

    # Termux setup
    if is_termux():
        setup_termux()

    # If stdin isn't a TTY (piped), try to reopen /dev/tty for interactive use
    if not sys.stdin.isatty():
        try:
            sys.stdin = open("/dev/tty", "r")
        except (OSError, FileNotFoundError):
            pass  # no TTY available (headless)

    # Always connect first (handles venv setup + execv if needed)
    # Service install prompt happens AFTER venv is resolved
    try:
        asyncio.run(connect(args.server, args.name, args.token,
                            offer_install=not args.no_service,
                            script_path=script_path))
    except KeyboardInterrupt:
        print("\n[r00k] Shutting down.")


if __name__ == "__main__":
    main()
