"""Claude Code session history browser.

Discovers all CC conversations across every project directory on this machine,
indexes them, and provides browsing/search capabilities.

Data lives in:
  ~/.claude/history.jsonl          — user prompts with session IDs and project paths
  ~/.claude/projects/<proj>/<sid>.jsonl — full conversation (user + assistant + tool calls)
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CLAUDE_DIR = Path.home() / ".claude"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def _decode_project_dir(dirname: str) -> str:
    """Convert Claude's project dir name back to a real path.

    Claude encodes paths like C:\\Users\\bake\\rook -> C--Users-bake-rook
    """
    # Handle lowercase drive letter variants
    parts = dirname.split("-")
    if not parts:
        return dirname

    # Reconstruct: first part is drive letter, rest are path segments
    # C--Users-bake-rook -> C:\Users\bake\rook
    # But dashes in actual folder names get collapsed, so this is best-effort
    result = dirname.replace("--", ":\\", 1).replace("-", "\\")
    return result


def _ts_to_str(ts: float | int) -> str:
    """Convert timestamp (ms or seconds) to readable string."""
    if ts > 1e12:  # milliseconds
        ts = ts / 1000
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "?"


def _extract_text(content: Any) -> str:
    """Extract text from a CC message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    parts.append(f"[result: {str(block.get('content', ''))[:60]}]")
        return " ".join(parts)
    return str(content)[:200] if content else ""


class SessionInfo:
    """Metadata about a single CC session."""

    def __init__(self, session_id: str, project: str):
        self.session_id = session_id
        self.project = project
        self.first_ts: float = 0
        self.last_ts: float = 0
        self.message_count: int = 0
        self.first_prompt: str = ""
        self.prompts: list[str] = []
        self.jsonl_path: Path | None = None
        self.subagent_count: int = 0

    @property
    def duration_str(self) -> str:
        if not self.first_ts or not self.last_ts:
            return "?"
        delta = (self.last_ts - self.first_ts) / 1000 if self.first_ts > 1e12 else self.last_ts - self.first_ts
        if delta < 60:
            return f"{delta:.0f}s"
        if delta < 3600:
            return f"{delta / 60:.0f}m"
        return f"{delta / 3600:.1f}h"

    def summary(self) -> str:
        return self.first_prompt[:80] if self.first_prompt else "(no prompt)"


def scan_history() -> dict[str, list[SessionInfo]]:
    """Scan history.jsonl and project dirs to build session index.

    Returns: {project_path: [SessionInfo, ...]} sorted by last activity.
    """
    sessions: dict[str, SessionInfo] = {}

    # Phase 1: Read history.jsonl for user prompts and session metadata
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    sid = d.get("sessionId", "")
                    proj = d.get("project", "")
                    ts = d.get("timestamp", 0)
                    display = d.get("display", "")

                    if not sid:
                        continue

                    if sid not in sessions:
                        sessions[sid] = SessionInfo(sid, proj)

                    s = sessions[sid]
                    if not s.first_ts or ts < s.first_ts:
                        s.first_ts = ts
                    if ts > s.last_ts:
                        s.last_ts = ts
                    s.message_count += 1
                    if display and not s.first_prompt:
                        s.first_prompt = display
                    if display:
                        s.prompts.append(display)
                except (json.JSONDecodeError, KeyError):
                    continue

    # Phase 2: Scan project dirs for session JSONLs (catches sessions not in history)
    if PROJECTS_DIR.exists():
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue

            project_path = _decode_project_dir(proj_dir.name)

            for jsonl_file in proj_dir.glob("*.jsonl"):
                sid = jsonl_file.stem
                if sid not in sessions:
                    sessions[sid] = SessionInfo(sid, project_path)

                s = sessions[sid]
                s.jsonl_path = jsonl_file
                if not s.project:
                    s.project = project_path

                # Check for subagents
                subagent_dir = proj_dir / sid / "subagents"
                if subagent_dir.exists():
                    s.subagent_count = len(list(subagent_dir.glob("*.jsonl")))

                # Get timestamps from file if not from history
                if not s.first_ts:
                    try:
                        stat = jsonl_file.stat()
                        s.first_ts = stat.st_ctime * 1000
                        s.last_ts = stat.st_mtime * 1000
                    except OSError:
                        pass

    # Group by project
    by_project: dict[str, list[SessionInfo]] = defaultdict(list)
    for s in sessions.values():
        by_project[s.project].append(s)

    # Sort each project's sessions by last activity (newest first)
    for proj in by_project:
        by_project[proj].sort(key=lambda s: s.last_ts, reverse=True)

    return dict(by_project)


def read_session(session: SessionInfo, max_messages: int = 0) -> list[dict]:
    """Read full conversation from a session's JSONL file.

    Returns list of {role, content, timestamp, type} dicts.
    """
    if not session.jsonl_path or not session.jsonl_path.exists():
        return []

    messages = []
    with open(session.jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                msg_type = d.get("type", "")

                # Skip non-message entries
                if msg_type in ("queue-operation", "last-prompt"):
                    continue

                msg = d.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue

                content = _extract_text(msg.get("content", ""))
                if not content or len(content.strip()) < 2:
                    continue

                # Skip system prompts injected as user messages
                if role == "user" and content.startswith("System context:"):
                    continue

                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": d.get("timestamp", ""),
                })

                if max_messages and len(messages) >= max_messages:
                    break

            except (json.JSONDecodeError, KeyError):
                continue

    return messages


def search_sessions(query: str, by_project: dict[str, list[SessionInfo]]) -> list[tuple[SessionInfo, list[str]]]:
    """Search across all sessions for a query string.

    Returns [(session, [matching_lines]), ...] sorted by relevance.
    """
    query_lower = query.lower()
    results = []

    for project, sessions_list in by_project.items():
        for session in sessions_list:
            matches = []

            # Search prompts first (fast)
            for p in session.prompts:
                if query_lower in p.lower():
                    matches.append(p[:120])

            # Search full conversation if we have the file
            if session.jsonl_path and session.jsonl_path.exists():
                try:
                    with open(session.jsonl_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if query_lower in line.lower():
                                try:
                                    d = json.loads(line)
                                    msg = d.get("message", {})
                                    if isinstance(msg, dict):
                                        content = _extract_text(msg.get("content", ""))
                                        if content and query_lower in content.lower():
                                            # Extract the matching line
                                            for text_line in content.split("\n"):
                                                if query_lower in text_line.lower():
                                                    matches.append(text_line.strip()[:120])
                                                    if len(matches) > 5:
                                                        break
                                except json.JSONDecodeError:
                                    pass
                            if len(matches) > 5:
                                break
                except OSError:
                    pass

            if matches:
                results.append((session, matches))

    # Sort by number of matches (most relevant first)
    results.sort(key=lambda x: len(x[1]), reverse=True)
    return results


# ── TUI ──────────────────────────────────────────────────────────────────────

# ANSI color helpers
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
RED = "\033[31m"


def _print_project_list(by_project: dict[str, list[SessionInfo]]) -> list[str]:
    """Print project list and return sorted project keys."""
    # Sort projects by most recent activity
    sorted_projects = sorted(
        by_project.items(),
        key=lambda x: max(s.last_ts for s in x[1]) if x[1] else 0,
        reverse=True,
    )

    total_sessions = sum(len(v) for v in by_project.values())
    print(f"\n{BOLD}{CYAN}♖ Claude Code Sessions{RESET}  {DIM}({total_sessions} sessions across {len(by_project)} projects){RESET}\n")

    project_keys = []
    for i, (project, sessions_list) in enumerate(sorted_projects):
        project_keys.append(project)
        total_msgs = sum(s.message_count for s in sessions_list)
        latest = _ts_to_str(max(s.last_ts for s in sessions_list))
        subagents = sum(s.subagent_count for s in sessions_list)

        proj_display = project if len(project) < 50 else "..." + project[-47:]
        sub_str = f" {DIM}(+{subagents} subagents){RESET}" if subagents else ""

        print(f"  {YELLOW}{i + 1:>2}{RESET}  {WHITE}{proj_display}{RESET}")
        print(f"      {DIM}{len(sessions_list)} sessions, {total_msgs} msgs, last: {latest}{sub_str}{RESET}")

    return project_keys


def _print_sessions(project: str, sessions_list: list[SessionInfo]) -> None:
    """Print session list for a project."""
    print(f"\n{BOLD}{CYAN}♖ {project}{RESET}\n")

    for i, s in enumerate(sessions_list):
        ts_str = _ts_to_str(s.last_ts)
        sub_str = f" {MAGENTA}+{s.subagent_count} agents{RESET}" if s.subagent_count else ""
        has_file = f"{GREEN}●{RESET}" if s.jsonl_path else f"{DIM}○{RESET}"

        print(f"  {YELLOW}{i + 1:>2}{RESET} {has_file} {ts_str}  {DIM}({s.message_count} msgs, {s.duration_str}){RESET}{sub_str}")
        print(f"      {WHITE}{s.summary()}{RESET}")


def _print_conversation(session: SessionInfo) -> None:
    """Print a full conversation."""
    messages = read_session(session)
    if not messages:
        print(f"  {DIM}(no readable messages){RESET}")
        return

    print(f"\n{BOLD}{CYAN}♖ Session {session.session_id[:8]}{RESET}  {DIM}{session.project}{RESET}")
    print(f"  {DIM}{_ts_to_str(session.first_ts)} — {session.message_count} messages{RESET}\n")

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            print(f"  {GREEN}you>{RESET} {content[:500]}")
            if len(content) > 500:
                print(f"       {DIM}... ({len(content)} chars total){RESET}")
        else:
            # Truncate long assistant messages
            lines = content.split("\n")
            if len(lines) > 20:
                for line in lines[:18]:
                    print(f"  {CYAN}cc>{RESET}  {line}")
                print(f"       {DIM}... ({len(lines)} lines total){RESET}")
            else:
                for line in lines:
                    print(f"  {CYAN}cc>{RESET}  {line}")
        print()


def _print_search_results(results: list[tuple[SessionInfo, list[str]]]) -> None:
    """Print search results."""
    if not results:
        print(f"  {DIM}No matches found.{RESET}")
        return

    print(f"\n  {BOLD}{len(results)} sessions matched{RESET}\n")
    for i, (session, matches) in enumerate(results[:20]):
        ts_str = _ts_to_str(session.last_ts)
        proj_short = session.project[-40:] if len(session.project) > 40 else session.project
        print(f"  {YELLOW}{i + 1:>2}{RESET} {WHITE}{proj_short}{RESET} {DIM}{ts_str}{RESET}")
        for m in matches[:3]:
            print(f"      {DIM}→{RESET} {m}")
        print()


def interactive(args=None):
    """Run the interactive session browser."""
    print(f"\n{DIM}Scanning Claude Code sessions...{RESET}")
    by_project = scan_history()

    if not by_project:
        print("No sessions found.")
        return

    project_keys = _print_project_list(by_project)

    print(f"\n{DIM}Enter number to browse project, 's <query>' to search, 'q' to quit{RESET}")

    while True:
        try:
            cmd = input(f"\n{CYAN}rook>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd or cmd.lower() in ("q", "quit", "exit"):
            break

        if cmd.lower() == "ls":
            project_keys = _print_project_list(by_project)
            continue

        if cmd.lower().startswith("s "):
            query = cmd[2:].strip()
            if query:
                print(f"{DIM}Searching...{RESET}")
                results = search_sessions(query, by_project)
                _print_search_results(results)
            continue

        # Project selection
        try:
            idx = int(cmd) - 1
            if 0 <= idx < len(project_keys):
                project = project_keys[idx]
                sessions_list = by_project[project]
                _print_sessions(project, sessions_list)

                print(f"\n{DIM}Enter number to read session, 'b' to go back{RESET}")
                while True:
                    try:
                        sub = input(f"  {CYAN}session>{RESET} ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if not sub or sub.lower() in ("b", "back"):
                        break
                    try:
                        si = int(sub) - 1
                        if 0 <= si < len(sessions_list):
                            _print_conversation(sessions_list[si])
                        else:
                            print(f"  {RED}Invalid number{RESET}")
                    except ValueError:
                        if sub.lower() in ("q", "quit"):
                            return
                        print(f"  {DIM}Enter a session number or 'b' to go back{RESET}")
            else:
                print(f"  {RED}Invalid number{RESET}")
        except ValueError:
            print(f"  {DIM}Enter a number, 's <query>', or 'q'{RESET}")


def main():
    """Entry point for `rook sessions` command."""
    import argparse
    parser = argparse.ArgumentParser(description="Browse Claude Code session history")
    parser.add_argument("query", nargs="?", help="Search query (optional)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--project", "-p", help="Filter by project path substring")
    args = parser.parse_args()

    if args.json:
        by_project = scan_history()
        if args.project:
            by_project = {k: v for k, v in by_project.items() if args.project.lower() in k.lower()}

        output = {}
        for proj, sessions_list in by_project.items():
            output[proj] = [
                {
                    "session_id": s.session_id,
                    "first_ts": s.first_ts,
                    "last_ts": s.last_ts,
                    "message_count": s.message_count,
                    "first_prompt": s.first_prompt,
                    "subagent_count": s.subagent_count,
                    "has_conversation": s.jsonl_path is not None,
                }
                for s in sessions_list
            ]
        print(json.dumps(output, indent=2, default=str))
        return

    if args.query:
        by_project = scan_history()
        results = search_sessions(args.query, by_project)
        _print_search_results(results)
        return

    interactive()


if __name__ == "__main__":
    main()
