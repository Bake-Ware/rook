"""The tool descriptions agents see in ``tools/list``.

Every connect pays for these, so they are written for the agent: what the tool
does, the arguments that are not self-explanatory, and what to do next. The
docstrings in server.py stay as developer documentation; a tool without an
entry here is advertised with its dedented docstring. Operator tips
(guidance ``tool:<name>``) are appended after these, and per-cap advice rides
``rook_call`` replies once per session (guidance ``cap:<prefix>``).
"""
from __future__ import annotations

import inspect

TOOLS: dict[str, str] = {
    "rook_whoami": "Your identity as this hub records it (agent_id, key_id, kind, identity). Attribution only; never returns the token.",

    "rook_workers": "Workers on the band. Filters: name (substring/comma list), cap_prefix (adds matching caps), online. Default fields: name, description, serves (hosting), build, hb, last_seen_age_secs (+worker_id if a name is shared). fields: any of worker_id, band, caps, plugins, version, app_release, online and the defaults, or \"all\".",

    "rook_caps": "Caps and their holders: \"*\" = every worker, {all_but:[…]}, or names. prefix filters (e.g. \"shell.\"); worker= lists that worker's caps. Cap names are singular (file.read).",

    "rook_call": "Run a cap on one worker. worker= (name or id) is required. Exact args: cap=\"caps.describe\" args={\"prefix\":\"shell.\"}.\nWaits for the call's own timeout (args.timeout, else the cap's default) +5s: raise args.timeout for slow commands; rook_console_open for long jobs. A timed-out call may still be running.\nReply {ok,id,from,result|error}; id is the journal id. _task, _unread_chat, _tips appear only when new; hint=true re-shows the tip. text=true: plain text (shell.exec: stdout; stderr, exit code only if set).",

    "rook_secret": "Vault: list (names only) | get name (logged) | set name value description | delete name | log name?. Prefer {{secret:name}} in rook_call args: filled in on the way, masked in replies. Never paste values into knowledge, chat or handoffs.",

    "rook_journal": "Recorded rook_call replies. call_id=<reply id> returns that call's full output (recover lost or timed-out output). Otherwise lists entries filtered by worker, cap_prefix, since_secs, only_failures.",

    "rook_handoff_save": "Save a handoff (state, not a transcript) so another agent can continue without asking: goal, state, decisions, next_steps, artifacts (files, hosts, URLs). Omit thread_id to start a thread; pass one to update it. Linked to your claimed task, or task=<id>. status=closed + thread_id closes it.",

    "rook_handoff_get": "A thread's current handoff plus history. Verify anything marked SUPERSEDED or STALE before acting on it.",

    "rook_handoff_list": "Recent handoff threads (latest per thread) with goal and freshness.",

    "rook_chat_start": "Start a chat room; invite = identities (list or comma string). Returns the room id, which is also a thread_id.",

    "rook_chat_send": "Post to a room. mention (list/comma): in rooms of 3+ only mentioned participants are expected to reply; a mentioned non-member is invited. The reply lists who is offline (rook_chat_wake makes one answer now).",

    "rook_chat_read": "Messages after since_seq (0 = all), marked read. Pass last_seq back to page.",

    "rook_chat_rooms": "Your chat rooms, newest first, with unread counts.",

    "rook_chat_delete": "Delete a room and its messages (participants only; final).",

    "rook_presence": "Agents seen over the MCP recently (online within ~90s) and live band workers.",

    "rook_chat_wake": "Make an agent on worker answer in room now (via hermes.chat or agent.wake); note is passed along.",

    "rook_console_open": "Run a slow, interactive or worth-keeping command as a console room, searchable after it exits. Returns once started. task (required) is the title: the goal (\"install cuda on worker-a\"), not the command. argv (list, no shell) or cmd (/bin/sh -c); pty=true for prompts/REPLs. Then rook_console_read/_write, and _close with a summary.",

    "rook_console_read": "Console output after since_seq (page with last_seq), or tail=true for the last limit lines. state says live or frozen.",

    "rook_console_write": "Type into a live console's stdin, verbatim (no shell); {{secret:x}} types a vault value unless literal=true.",

    "rook_console_signal": "Signal a live console's process group: TERM, KILL, INT (ctrl-C) or HUP. The room survives.",

    "rook_console_close": "Freeze a console room. The summary (what it did, what worked, what to watch for) is what makes it findable. kill=true stops a running process first.",

    "rook_console_list": "Console rooms, newest first; filter by worker or state (live|closing|frozen).",

    "rook_console_search": "Full-text search of all console sessions; titles and summaries rank highest (search for the task, not the command). Hits carry seq: rook_console_read(room, since_seq=seq-1).",

    "rook_install_claude_code": "Install or update the Rook mod for Claude Code. installed= the output of `claude plugin list --json`; returns status and the commands to run.",

    "rook_config_get": "A worker's config overrides and pending/confirm state.",

    "rook_config_apply": "Commit-confirmed config push: settings {name, announce_interval, log_level, hub, psk, env:{…}}. Waits for the worker to return, then confirms; one that doesn't auto-reverts after confirm_within s. Ask the user first.",
}


def for_tool(name: str, docstring: str | None) -> str:
    """The advertised description: the table entry, else the docstring
    dedented with each paragraph's hard-wrapped lines joined."""
    if name in TOOLS:
        return TOOLS[name]
    paras = inspect.cleandoc(docstring or "").split("\n\n")
    return "\n\n".join(" ".join(line.strip() for line in p.splitlines()) for p in paras)
