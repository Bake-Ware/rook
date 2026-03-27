"""tmux-like manager for Claude Code sessions.

Spawn, attach, detach, and manage multiple Claude Code CLI sessions
from a single terminal. Sessions persist in the background.

Usage:
    rook tmux                           — interactive session manager
    rook tmux spawn <prompt>            — start a new CC session
    rook tmux spawn -d <dir> <prompt>   — start in a specific directory
    rook tmux list                      — list all managed sessions
    rook tmux attach <id>               — attach to a running session
    rook tmux kill <id>                 — kill a session
    rook tmux send <id> <text>          — send input to a session
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DATA_DIR = Path.home() / ".rook"
DB_PATH = DATA_DIR / "sessions.db"
OUTPUT_DIR = DATA_DIR / "output"

# ANSI
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
WHITE = "\033[97m"
RED = "\033[31m"

# Stderr lines from CC that are just noise
_STDERR_NOISE = ("Warning: no stdin", "ENOENT", "ExperimentalWarning")


def _init_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            short_id TEXT NOT NULL,
            pid INTEGER,
            cwd TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            started_at REAL NOT NULL,
            ended_at REAL,
            output_file TEXT,
            output_lines INTEGER DEFAULT 0,
            last_output TEXT
        )
    """)
    db.commit()
    return db


def _short_id() -> str:
    return uuid.uuid4().hex[:6]


def _ts_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _elapsed(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    return f"{delta / 3600:.1f}h"


def _find_claude_binary() -> str:
    """Find the claude CLI binary."""
    if sys.platform == "win32":
        which = shutil.which("claude") or shutil.which("claude.cmd")
        if which:
            cmd_path = which if which.endswith(".cmd") else which + ".cmd"
            if Path(cmd_path).exists():
                return cmd_path
            return which
        for c in (r"C:\nvm4w\nodejs\claude.cmd",
                  str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
                  r"C:\Program Files\nodejs\claude.cmd"):
            if Path(c).exists():
                return c
        return "claude.cmd"
    return shutil.which("claude") or "claude"


def render_stream_json(raw: str, *, print_it: bool = True) -> str:
    """Extract displayable text from a stream-json line.

    Returns the extracted text (empty string for non-text events).
    Optionally prints it live.
    """
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        if print_it:
            print(raw)
        return raw

    event_type = event.get("type", "")

    if event_type == "content_block_delta":
        text = event.get("delta", {}).get("text", "")
        if text and print_it:
            print(text, end="", flush=True)
        return text

    if event_type == "result":
        text = event.get("result", "")
        if text and print_it:
            print(text)
        return text

    if event_type in ("content_block_stop", "message_stop"):
        if print_it:
            print()
        return "\n"

    # System/init/rate_limit events — skip for display
    return ""


class SessionManager:
    """Manages Claude Code subprocess sessions."""

    def __init__(self):
        self.db = _init_db()
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._output_tasks: dict[str, asyncio.Task] = {}

    async def spawn(self, prompt: str, cwd: str | None = None, print_output: bool = True) -> str:
        """Spawn a new Claude Code session. Returns the session short_id."""
        sid = str(uuid.uuid4())
        short = _short_id()
        work_dir = cwd or os.getcwd()
        output_file = str(OUTPUT_DIR / f"{short}.log")

        self.db.execute(
            "INSERT INTO sessions (id, short_id, cwd, prompt, status, started_at, output_file) VALUES (?,?,?,?,'starting',?,?)",
            (sid, short, work_dir, prompt, time.time(), output_file),
        )
        self.db.commit()

        claude_bin = _find_claude_binary()
        escaped = prompt.replace('"', '\\"')

        try:
            if sys.platform == "win32":
                proc = await asyncio.create_subprocess_shell(
                    f'"{claude_bin}" -p "{escaped}" --output-format stream-json --verbose',
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    claude_bin, "-p", prompt,
                    "--output-format", "stream-json", "--verbose",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                )
        except FileNotFoundError:
            self.db.execute("UPDATE sessions SET status='error', last_output=? WHERE id=?",
                            (f"Claude CLI not found at '{claude_bin}'", sid))
            self.db.commit()
            print(f"{RED}Error: Claude CLI not found.{RESET}")
            return short

        self._procs[sid] = proc
        self.db.execute("UPDATE sessions SET pid=?, status='running' WHERE id=?", (proc.pid, sid))
        self.db.commit()

        if print_output:
            print(f"{GREEN}♖ Session {short}{RESET} spawned in {DIM}{work_dir}{RESET}")
            print(f"  {DIM}PID {proc.pid} — {prompt[:60]}{RESET}\n")

        task = asyncio.create_task(self._read_output(sid, short, proc, output_file, print_output))
        self._output_tasks[sid] = task
        return short

    async def _read_output(self, sid: str, short: str, proc: asyncio.subprocess.Process,
                           output_file: str, print_live: bool) -> None:
        """Read CC stdout, render stream-json, persist to log."""
        line_count = 0
        last_text = ""

        try:
            with open(output_file, "w", encoding="utf-8") as f:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").rstrip()
                    if not raw:
                        continue

                    text = render_stream_json(raw, print_it=print_live)
                    if text:
                        last_text = text.rstrip() or last_text

                    f.write(raw + "\n")
                    f.flush()
                    line_count += 1

            # Drain stderr, suppress known noise
            stderr_data = await proc.stderr.read()
            if stderr_data:
                stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
                meaningful = [l for l in stderr_text.splitlines()
                              if not any(n in l for n in _STDERR_NOISE)]
                if meaningful and print_live:
                    print(f"{RED}{''.join(meaningful[:5])}{RESET}")
                if stderr_text:
                    with open(output_file, "a", encoding="utf-8") as f:
                        f.write(f"\n--- STDERR ---\n{stderr_text}\n")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            last_text = f"Error: {e}"

        try:
            await proc.wait()
        except Exception:
            pass

        returncode = proc.returncode or 0
        status = "completed" if returncode == 0 else f"exited({returncode})"

        self.db.execute(
            "UPDATE sessions SET status=?, ended_at=?, output_lines=?, last_output=? WHERE id=?",
            (status, time.time(), line_count, last_text[-500:] if last_text else "", sid),
        )
        self.db.commit()

        if print_live:
            print(f"\n{GREEN}♖ Session {short}{RESET} {status} {DIM}({line_count} lines){RESET}")

    async def send_input(self, short_id: str, text: str) -> bool:
        """Send text to a running session's stdin."""
        row = self.db.execute("SELECT id FROM sessions WHERE short_id=? AND status='running'", (short_id,)).fetchone()
        if not row:
            return False
        proc = self._procs.get(row["id"])
        if not proc or proc.stdin is None:
            return False
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            await proc.stdin.drain()
            return True
        except Exception:
            return False

    def list_sessions(self, status_filter: str | None = None) -> list[dict]:
        if status_filter:
            rows = self.db.execute("SELECT * FROM sessions WHERE status=? ORDER BY started_at DESC", (status_filter,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_session(self, short_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sessions WHERE short_id=?", (short_id,)).fetchone()
        return dict(row) if row else None

    def read_output(self, short_id: str, tail: int = 50) -> str:
        row = self.db.execute("SELECT output_file FROM sessions WHERE short_id=?", (short_id,)).fetchone()
        if not row or not row["output_file"]:
            return ""
        p = Path(row["output_file"])
        if not p.exists():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if tail and len(lines) > tail:
            lines = lines[-tail:]
        return "\n".join(lines)

    async def kill_session(self, short_id: str) -> bool:
        row = self.db.execute("SELECT id, pid FROM sessions WHERE short_id=?", (short_id,)).fetchone()
        if not row:
            return False
        proc = self._procs.get(row["id"])
        if proc:
            try:
                proc.terminate()
                await asyncio.sleep(1)
                if proc.returncode is None:
                    proc.kill()
            except Exception:
                pass
        if row["pid"]:
            try:
                os.kill(row["pid"], 9)
            except (OSError, ProcessLookupError):
                pass
        self.db.execute("UPDATE sessions SET status='killed', ended_at=? WHERE id=?", (time.time(), row["id"]))
        self.db.commit()
        return True

    def cleanup_dead(self) -> int:
        rows = self.db.execute("SELECT id, pid FROM sessions WHERE status='running'").fetchall()
        cleaned = 0
        for row in rows:
            if row["pid"]:
                try:
                    os.kill(row["pid"], 0)
                except (OSError, ProcessLookupError):
                    self.db.execute("UPDATE sessions SET status='dead', ended_at=? WHERE id=?", (time.time(), row["id"]))
                    cleaned += 1
        if cleaned:
            self.db.commit()
        return cleaned


# ── TUI ──────────────────────────────────────────────────────────────────────

def _print_sessions(mgr: SessionManager):
    sessions = mgr.list_sessions()
    mgr.cleanup_dead()
    if not sessions:
        print(f"  {DIM}No sessions.{RESET}")
        return

    running = [s for s in sessions if s["status"] == "running"]
    done = [s for s in sessions if s["status"] != "running"]

    if running:
        print(f"\n  {BOLD}{GREEN}Running{RESET}")
        for s in running:
            cwd_short = s["cwd"][-35:] if len(s["cwd"]) > 35 else s["cwd"]
            print(f"    {YELLOW}{s['short_id']}{RESET}  {WHITE}{s['prompt'][:50]}{RESET}")
            print(f"          {DIM}pid={s['pid']}  {cwd_short}  {_elapsed(s['started_at'])}{RESET}")

    if done:
        print(f"\n  {BOLD}Completed{RESET}")
        for s in done[:10]:
            color = GREEN if s["status"] == "completed" else RED
            last = (s.get("last_output") or "")[:60]
            print(f"    {DIM}{s['short_id']}{RESET}  {color}{s['status']}{RESET}  {_ts_str(s['started_at'])}  {WHITE}{s['prompt'][:40]}{RESET}")
            if last:
                print(f"          {DIM}{last}{RESET}")


def _render_log(raw_log: str):
    """Render stored stream-json log lines as readable text."""
    for line in raw_log.splitlines():
        render_stream_json(line, print_it=True)


async def _interactive_attach(mgr: SessionManager, short_id: str):
    """Attach to a session and stream output live."""
    session = mgr.get_session(short_id)
    if not session:
        print(f"{RED}Session {short_id} not found{RESET}")
        return

    print(f"{GREEN}♖ Attached to {short_id}{RESET}  {DIM}(Ctrl+C to detach){RESET}")
    print(f"  {DIM}{session['cwd']} — {session['prompt'][:60]}{RESET}\n")

    recent = mgr.read_output(short_id, tail=30)
    if recent:
        print(f"{DIM}--- recent output ---{RESET}")
        _render_log(recent)
        print(f"{DIM}--- live ---{RESET}")

    if session["status"] != "running":
        print(f"\n{DIM}Session already {session['status']}{RESET}")
        return

    output_path = Path(session["output_file"]) if session.get("output_file") else None
    if not output_path or not output_path.exists():
        print(f"{DIM}No output file yet{RESET}")
        return

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    render_stream_json(line.rstrip(), print_it=True)
                else:
                    fresh = mgr.get_session(short_id)
                    if fresh and fresh["status"] != "running":
                        print(f"\n{DIM}Session {fresh['status']}{RESET}")
                        break
                    await asyncio.sleep(0.1)
    except KeyboardInterrupt:
        print(f"\n{DIM}Detached from {short_id}{RESET}")


async def _interactive(mgr: SessionManager):
    """Interactive tmux-like interface."""
    print(f"\n{BOLD}{CYAN}♖ Rook CC Manager{RESET}  {DIM}(tmux for Claude Code){RESET}")
    print(f"  {DIM}spawn <prompt> | list | attach <id> | kill <id> | output <id> | send <id> <text> | quit{RESET}")
    _print_sessions(mgr)

    loop = asyncio.get_event_loop()
    while True:
        try:
            cmd = await loop.run_in_executor(None, lambda: input(f"\n{CYAN}rook♖{RESET} ").strip())
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            continue
        parts = cmd.split(maxsplit=1)
        verb, rest = parts[0].lower(), parts[1] if len(parts) > 1 else ""

        if verb in ("q", "quit", "exit"):
            break
        elif verb in ("ls", "list"):
            _print_sessions(mgr)
        elif verb == "spawn":
            if not rest:
                print(f"  {DIM}Usage: spawn [-d <dir>] <prompt>{RESET}")
                continue
            cwd, prompt = None, rest
            if rest.startswith("-d "):
                dp = rest[3:].split(maxsplit=1)
                if len(dp) == 2:
                    cwd, prompt = dp[0], dp[1]
                else:
                    print(f"  {DIM}Usage: spawn -d <dir> <prompt>{RESET}")
                    continue
            short = await mgr.spawn(prompt, cwd=cwd, print_output=False)
            s = mgr.get_session(short)
            if s and s["status"] != "error":
                print(f"  {GREEN}♖ Spawned {short}{RESET}  {DIM}pid={s.get('pid', '?')}{RESET}")
                print(f"  {DIM}Use 'attach {short}' to watch{RESET}")
        elif verb == "attach":
            if rest:
                await _interactive_attach(mgr, rest.strip())
            else:
                print(f"  {DIM}Usage: attach <id>{RESET}")
        elif verb == "kill":
            if rest:
                print(f"  {RED}Killed{RESET}" if await mgr.kill_session(rest.strip()) else f"  {DIM}Not found{RESET}")
            else:
                print(f"  {DIM}Usage: kill <id>{RESET}")
        elif verb == "output":
            if rest:
                out = mgr.read_output(rest.strip(), tail=50)
                _render_log(out) if out else print(f"  {DIM}No output{RESET}")
            else:
                print(f"  {DIM}Usage: output <id>{RESET}")
        elif verb == "send":
            sp = rest.split(maxsplit=1)
            if len(sp) == 2:
                print(f"  {GREEN}Sent{RESET}" if await mgr.send_input(sp[0], sp[1]) else f"  {RED}Failed{RESET}")
            else:
                print(f"  {DIM}Usage: send <id> <text>{RESET}")
        else:
            print(f"  {DIM}Unknown: {verb}{RESET}")


async def _run_spawn(args):
    mgr = SessionManager()
    short = await mgr.spawn(args.prompt, cwd=args.dir, print_output=True)
    session = mgr.get_session(short)
    if session:
        task = mgr._output_tasks.get(session["id"])
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="tmux for Claude Code sessions")
    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("spawn", help="Spawn a new CC session")
    sp.add_argument("prompt", nargs="+", help="Prompt for Claude Code")
    sp.add_argument("-d", "--dir", help="Working directory")

    sub.add_parser("list", aliases=["ls"], help="List sessions")

    att = sub.add_parser("attach", help="Attach to a session")
    att.add_argument("id", help="Session short ID")

    ki = sub.add_parser("kill", help="Kill a session")
    ki.add_argument("id", help="Session short ID")

    out = sub.add_parser("output", help="Show session output")
    out.add_argument("id", help="Session short ID")
    out.add_argument("-n", "--lines", type=int, default=50)

    snd = sub.add_parser("send", help="Send input to session")
    snd.add_argument("id", help="Session short ID")
    snd.add_argument("text", nargs="+", help="Text to send")

    args = parser.parse_args()
    mgr = SessionManager()

    if args.command == "spawn":
        args.prompt = " ".join(args.prompt)
        asyncio.run(_run_spawn(args))
    elif args.command in ("list", "ls"):
        _print_sessions(mgr)
    elif args.command == "attach":
        asyncio.run(_interactive_attach(mgr, args.id))
    elif args.command == "kill":
        asyncio.run(mgr.kill_session(args.id))
        print(f"Killed {args.id}")
    elif args.command == "output":
        out = mgr.read_output(args.id, tail=args.lines)
        _render_log(out) if out else print("(no output)")
    elif args.command == "send":
        text = " ".join(args.text)
        print("Sent" if asyncio.run(mgr.send_input(args.id, text)) else "Failed")
    else:
        asyncio.run(_interactive(mgr))


if __name__ == "__main__":
    main()
