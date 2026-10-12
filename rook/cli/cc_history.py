"""Claude Code session history browser (``rook sessions``).

Discovers every Claude Code conversation on this machine, indexes them, and
provides browsing and search. Transcripts are read with the worker's
``claude-history`` parsers (one reading of the format for the CLI, the
worker caps and the dashboard), so this is a local, offline view of what
``claude-history.pull``/``read`` serve to the band.

Data lives in:
  ~/.claude/history.jsonl                — user prompts with session IDs and project paths
  ~/.claude/projects/<proj>/<sid>.jsonl  — full conversation (user + assistant + tool calls)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..worker import agents
from ..worker.plugins import claude_history as ch


def _projects_dir() -> Path:
    return agents.claude_projects()


def _history_file() -> Path:
    return agents.claude_home() / "history.jsonl"


def _decode_project_dir(dirname: str) -> str:
    """Best-effort path from Claude's project dir name, for a session whose
    transcript records no cwd. Claude replaces every non-alphanumeric
    character with "-", so this cannot be exact: C--Users-you-rook -> C:\\Users\\you\\rook
    """
    return dirname.replace("--", ":\\", 1).replace("-", "\\")


def _iso_ms(value) -> float:
    """An ISO-8601 transcript timestamp as epoch milliseconds (0 if unreadable)."""
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0


def _ts_to_str(ts: float | int) -> str:
    """Convert timestamp (ms or seconds) to readable string."""
    if ts > 1e12:  # milliseconds
        ts = ts / 1000
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "?"


class SessionInfo:
    """Metadata about a single CC session. Timestamps are epoch milliseconds."""

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
        delta = (self.last_ts - self.first_ts) / 1000
        if delta < 60:
            return f"{delta:.0f}s"
        if delta < 3600:
            return f"{delta / 60:.0f}m"
        return f"{delta / 3600:.1f}h"

    def summary(self) -> str:
        return self.first_prompt[:80] if self.first_prompt else "(no prompt)"


def scan_history() -> dict[str, list[SessionInfo]]:
    """Index history.jsonl prompts and every transcript under the projects
    directory. Returns ``{project_path: [SessionInfo, ...]}``, each list
    newest first."""
    sessions: dict[str, SessionInfo] = {}

    # Phase 1: history.jsonl — the prompts typed into each session.
    hist = _history_file()
    if hist.exists():
        for d in ch._read_lines(hist):
            if not isinstance(d, dict) or not d.get("sessionId"):
                continue
            sid = d["sessionId"]
            s = sessions.setdefault(sid, SessionInfo(sid, d.get("project", "")))
            ts = d.get("timestamp") or 0
            if isinstance(ts, (int, float)):
                if not s.first_ts or ts < s.first_ts:
                    s.first_ts = ts
                s.last_ts = max(s.last_ts, ts)
            s.message_count += 1
            display = d.get("display") or ""
            if display:
                s.first_prompt = s.first_prompt or display
                s.prompts.append(display)

    # Phase 2: transcripts (catches sessions history.jsonl never saw).
    root = _projects_dir()
    for jsonl in ch._iter_session_files(root):
        # Top-level transcripts only: <project>/<sid>/subagents/*.jsonl are
        # counted against their parent below.
        if jsonl.parent.parent != root:
            continue
        meta, _activity = ch.scan_session(jsonl)
        if meta.get("error"):
            continue
        sid = meta["session_id"]
        s = sessions.setdefault(sid, SessionInfo(sid, ""))
        s.jsonl_path = jsonl
        s.project = s.project or meta.get("cwd") or _decode_project_dir(jsonl.parent.name)
        if meta.get("message_count"):
            s.message_count = max(s.message_count, meta["message_count"])
        if not s.first_prompt and meta.get("title") not in (None, "(empty)"):
            s.first_prompt = meta["title"]
        first = _iso_ms(meta.get("first_timestamp")) or (meta.get("last_modified") or 0) * 1000
        last = _iso_ms(meta.get("last_timestamp")) or (meta.get("last_modified") or 0) * 1000
        if first and (not s.first_ts or first < s.first_ts):
            s.first_ts = first
        s.last_ts = max(s.last_ts, last)
        subagents = jsonl.parent / sid / "subagents"
        if subagents.is_dir():
            s.subagent_count = len(list(subagents.glob("*.jsonl")))

    by_project: dict[str, list[SessionInfo]] = defaultdict(list)
    for s in sessions.values():
        by_project[s.project].append(s)
    for proj in by_project:
        by_project[proj].sort(key=lambda s: s.last_ts, reverse=True)
    return dict(by_project)


def read_session(session: SessionInfo, max_messages: int = 0) -> list[dict]:
    """The user and assistant messages of a session's transcript, as
    ``{role, content, timestamp}``. Records that only carry tool results and
    the "System context:" preamble some launchers inject are left out."""
    if not session.jsonl_path or not session.jsonl_path.exists():
        return []
    messages = []
    for rec in ch._read_lines(session.jsonl_path):
        if not isinstance(rec, dict):
            continue
        role = ch._record_role(rec)
        if role not in ("user", "assistant") or not isinstance(rec.get("message"), dict):
            continue
        if role == "user" and ch._tool_result_only(rec):
            continue
        content = ch._message_text(rec)
        if len(content.strip()) < 2:
            continue
        if role == "user" and content.startswith("System context:"):
            continue
        messages.append({"role": role, "content": content, "timestamp": rec.get("timestamp", "")})
        if max_messages and len(messages) >= max_messages:
            break
    return messages


def search_sessions(query: str, by_project: dict[str, list[SessionInfo]]) -> list[tuple[SessionInfo, list[str]]]:
    """Sessions whose prompts or messages contain ``query`` (case-insensitive),
    with up to six matching lines each, most matches first."""
    q = query.lower()
    results = []
    for sessions_list in by_project.values():
        for session in sessions_list:
            matches = [p[:120] for p in session.prompts if q in p.lower()]
            for msg in read_session(session):
                if len(matches) > 5:
                    break
                if q not in msg["content"].lower():
                    continue
                for line in msg["content"].split("\n"):
                    if q in line.lower():
                        matches.append(line.strip()[:120])
                        if len(matches) > 5:
                            break
            if matches:
                results.append((session, matches))
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
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
