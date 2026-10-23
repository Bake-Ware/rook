"""Approval asks in plain words: the one shape every ask reaching the person
takes, wherever it is shown (the dashboard's Approvals page, the phone's
decision card, ``rook_approvals``, huddle cards and questions, job holds,
trust and hub-host holds, the house agent's asks).

Bake (2026-10-23): "those need to be better formatted before sending to me.
A simple title, for what task(project) and like I want to xyz, the risk is
abc." So every ask is::

    Title:      a few plain words              ("Read hub approvals database")
    For:        the task, project, huddle or job it serves ("no task" if none)
    Who:        the agent or person asking, by its readable name
    I want to:  one plain sentence of what will happen
    Why:        the asker's own reason, when it gave one (rook_call why=)
    Risk:       one plain sentence, from the tier, the trust level and
                whether the call only reads, changes or destroys
    Explain:    the technical detail (cap, args, trust entry, hold id and its
                three-word name), shown only on demand

:func:`describe` builds that from one waiting item as
:meth:`rook.hub.relay.Relay.items` lists it (or a hold row, via
:func:`for_hold`). It never resolves a secret: it reads the args the hold
kept, which were redacted when the hold was made.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

TITLE_MAX, QUESTION_MAX, LINE_MAX, EXPLAIN_MAX = 60, 100, 220, 900
#: Words for a tier, when nothing more specific is known.
TIER_RISK = {
    "read": ("Low", "it only reads"),
    "write": ("Medium", "it changes data"),
    "exec": ("Medium", "it runs a command that can change things"),
    "admin": ("High", "it changes who can do what or band-wide settings"),
}

# -- small helpers -----------------------------------------------------------------


def short(text: Any, limit: int) -> str:
    """One line of at most ``limit`` characters, cut at a word with an
    ellipsis (these are read by a person, not parsed)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _sentence(text: str) -> str:
    text = short(text, LINE_MAX)
    return text[:1].upper() + text[1:] if text else text


def _base(path: str) -> str:
    return os.path.basename(str(path).rstrip("/")) or str(path)


# -- who ---------------------------------------------------------------------------

_ROLE = re.compile(r"role:([a-z][a-z0-9_-]{0,31})")


def who_name(who: Any = "", *, principal: Any = "", role: str | None = None,
             job: str | None = None) -> str:
    """The asker by a readable name: ``Claude web``, ``architect (huddle
    role)``, ``the house agent``, ``job nightly-backup``."""
    if job:
        return f"job {job}"
    text = str(who or "").strip()
    pid = str(principal or "").strip()
    m = _ROLE.search(text) or _ROLE.search(pid)
    role = role or (m.group(1) if m else None)
    if role:
        return f"{role} (huddle role)"
    for raw in (text, pid):
        low = raw.lower()
        if low.startswith("the house agent"):
            return raw
        if low in ("agent:home", "home"):
            return "the house agent"
    if text.startswith("human:"):
        return "a person"
    if text.startswith(("agent:", "user:")):
        text = text.split(":", 1)[1]
    if not text or re.fullmatch(r"(token:|device:)?(agent_)?[0-9a-f]{12,}", text):
        return "an agent"
    return text


# -- for ---------------------------------------------------------------------------

def for_text(ctx: dict | None, frm: dict | None = None) -> str:
    """The task, project, huddle, job or console the ask serves; ``no task``."""
    ctx, frm = ctx or {}, frm or {}
    task = str(ctx.get("task_title") or "").strip()
    project = str(ctx.get("project") or "").strip()
    if task:
        return short(f"{task} ({project})" if project and project != task else task, 120)
    if project:
        return short(f"project {project}", 120)
    huddle = str(frm.get("huddle_title") or ctx.get("huddle") or frm.get("huddle") or "").strip()
    huddle = re.sub(r"^huddle:\s*", "", huddle, flags=re.I)
    if huddle:
        return short(f"huddle {huddle}", 120)
    job = str(frm.get("job") or ctx.get("job") or "").strip()
    if job:
        return short(f"job {job}", 120)
    room = str(ctx.get("room_title") or "").strip()
    if room:
        return short(f"console {room}", 120)
    if ctx.get("task"):
        return short(f"task {ctx['task']}", 120)
    if frm.get("house"):
        return HOUSE_FOR.get(frm["house"], "the house agent's front door")
    return "no task"


#: What a house agent's ask is for, by its kind (rook/hub/plugins/home/front.py).
HOUSE_FOR = {
    "action": "something an agent asked the house agent to do",
    "dispute": "settling a disagreement between agents",
    "gap": "a gap in the trust map (nothing covers this yet)",
    "playbook": "the house agent's playbook",
    "remember": "remembering your answer for next time",
    "restore": "putting a borrowed machine back",
    "review": "reviewing a new project's setup",
    "vote": "a huddle vote that did not pass on its own",
}


# -- shell commands ------------------------------------------------------------------
#
# Read-only is earned, not guessed (votes high-inch-gallery and
# gifted-organ-marigold, 2026-10-23): a command reads only when it is a known
# read-only program, run with flags from that program's allowlist, with no
# environment prefix, no ssh, no wrapper options, no redirect into a file and
# no shell the tokenizer does not follow. Everything else is a change, said
# as Medium, so a wrong "Low: looks read-only" cannot lead the person to
# approve a write. Unusual reads are Medium too: that errs the safe way.

#: Programs whose every flag only changes what they print; their other
#: arguments are files, patterns or names to read.
READ_ANY = {
    "cat", "head", "tail", "ls", "wc", "du", "df", "free", "uptime", "ps", "pgrep", "id",
    "whoami", "uname", "which", "printenv", "echo", "printf", "grep", "egrep", "fgrep", "zcat",
    "zgrep", "lsblk", "lsusb", "lspci", "netstat", "ping", "dig", "nslookup", "host", "jq",
    "cut", "diff", "cmp", "md5sum", "sha256sum", "sha1sum", "readlink", "realpath", "basename",
    "dirname", "test", "true", "false", "sleep", "column", "tr", "hexdump", "strings", "lscpu",
    "lsmod", "last", "w", "who", "groups", "getent", "nproc", "vmstat", "iostat", "pwd", "nl",
    "tac", "rev", "seq", "expr", "stat", "cd",
}
#: Programs that read with these flags only: ``{prog: (long flags, short letters)}``.
#: A long flag matches exactly or as ``--flag=value``; values that follow a flag
#: are plain words, which these programs read.
READ_FLAGS = {
    "journalctl": (("--unit", "--lines", "--follow", "--boot", "--priority", "--since",
                    "--until", "--output", "--no-pager", "--reverse", "--dmesg", "--utc",
                    "--grep", "--identifier", "--user", "--system", "--all", "--list-boots",
                    "--disk-usage", "--no-hostname", "--case-sensitive", "--catalog",
                    "--pager-end", "--quiet", "--merge", "--user-unit", "--facility"),
                   set("unfbpSUoreqxkgtaimM")),
    "dmesg": (("--ctime", "--human", "--follow", "--level", "--kernel", "--decode",
               "--color", "--time-format", "--notime", "--reltime", "--facility", "--userspace",
               "--nopager", "--raw", "--show-delta"), set("THwlkxLtefurPd")),
    "find": (("-name", "-iname", "-type", "-maxdepth", "-mindepth", "-mtime", "-mmin", "-atime",
              "-amin", "-ctime", "-cmin", "-size", "-path", "-ipath", "-wholename", "-newer",
              "-print", "-print0", "-ls", "-perm", "-user", "-group", "-empty", "-regex",
              "-iregex", "-not", "-o", "-a", "-or", "-and", "-prune", "-xdev", "-mount",
              "-follow", "-L", "-H", "-P", "-readable", "-writable", "-executable", "-links",
              "-inum", "-samefile", "-nouser", "-nogroup", "-true", "-false"), set()),
    "sort": (("--reverse", "--numeric-sort", "--human-numeric-sort", "--unique", "--key",
              "--field-separator", "--ignore-case", "--version-sort", "--stable",
              "--general-numeric-sort", "--month-sort", "--random-sort", "--zero-terminated",
              "--check"), set("rnhuktfgVsMRzbdci")),
    "uniq": (("--count", "--repeated", "--unique", "--ignore-case", "--skip-fields",
              "--skip-chars", "--check-chars", "--zero-terminated"), set("cduifswz")),
    "ss": (("--tcp", "--udp", "--listening", "--numeric", "--processes", "--all", "--extended",
            "--info", "--summary", "--unix", "--raw", "--ipv4", "--ipv6", "--resolve",
            "--memory", "--options", "--no-header"), set("tulnpaeismxwr46OHZ")),
    "rg": (("--files", "--count", "--line-number", "--no-heading", "--ignore-case",
            "--smart-case", "--fixed-strings", "--word-regexp", "--hidden", "--glob",
            "--type", "--max-count", "--context", "--before-context", "--after-context",
            "--files-with-matches", "--json", "--no-ignore", "--follow", "--max-depth",
            "--color", "--sort", "--multiline", "--invert-match", "--only-matching",
            "--trim", "--stats", "--max-columns", "--type-list"), set("ciFwlLgtmCABnNsvoUeuj")),
    "nvidia-smi": (("--query", "--query-gpu", "--query-compute-apps", "--format", "--id",
                    "--list-gpus", "--loop", "--loop-ms", "--display", "--xml-format"),
                   set("qLilxd")),
    "date": (("--utc", "--universal", "--rfc-email", "--iso-8601", "--rfc-3339", "--date",
              "--reference"), set("uRIdr")),
}
#: For these the words after the flags must also pass (``None``: any words).
READ_WORDS = {
    "uniq": lambda w: len(w) <= 1,                      # a second file is the output
    "nvidia-smi": lambda w: all(x in ("dmon", "pmon") or x.isdigit() or "," in x or "=" in x
                                for x in w),
    "date": lambda w: not any(x[:1].isdigit() for x in w),   # date MMDDhhmm sets the clock
}
#: Subcommand tools: the subcommands that read, with any flags after them.
READ_SUBS = {
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "show", "cat",
                  "list-units", "list-timers", "list-unit-files", "list-sockets",
                  "list-dependencies"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "version", "info"},
    "podman": {"ps", "logs", "inspect", "images", "stats", "version", "info"},
    "kubectl": {"get", "describe", "logs", "top", "version"},
    "pip": {"list", "show", "freeze"}, "pip3": {"list", "show", "freeze"},
    "npm": {"ls", "list", "view"}, "apt": {"list", "show", "search", "policy"},
    "tailscale": {"status", "ip"}, "rook": {"status", "version"},
    "hostnamectl": {"status"}, "timedatectl": {"status", "show", "list-timezones",
                                               "timesync-status"},
    "loginctl": {"list-sessions", "list-users", "list-seats", "session-status",
                 "user-status", "show-session", "show-user", "show-seat"},
}
#: Subcommand tools that read with no subcommand at all.
READ_BARE = {"systemctl", "hostnamectl", "timedatectl", "loginctl", "hostname"}
#: Flags that make a subcommand tool act elsewhere or run code: never read.
SUB_DENY = ("-H", "--host", "-M", "--machine", "--kubeconfig", "--context", "--server",
            "--exec", "-c", "--config")
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "blame",
            "shortlog", "rev-list", "cat-file", "ls-tree"}
#: Git flags that run a program or write a file.
GIT_DENY = ("--ext-diff", "--textconv", "--output", "-O", "--open-files-in-pager",
            "--upload-pack", "--exec", "--receive-pack", "--run", "--config", "--hook")
GIT_LIST = {"branch": {"--list", "-l", "-a", "-r", "-v", "-vv", "--show-current", "--all",
                       "--remotes", "--merged", "--no-merged"},
            "tag": {"--list", "-l", "-n"}}
#: ip objects and the verbs that only show them.
IP_OBJECTS = {"addr", "address", "a", "link", "l", "route", "r", "ro", "neigh", "neighbour",
              "n", "rule", "ru", "maddr", "maddress", "-br", "-brief", "-c", "-color", "-4",
              "-6", "-s", "-stats", "-j", "-json", "-d", "-details", "-p", "-pretty"}
IP_VERBS = {"show", "list", "ls", "lst", "get"}
DESTROY_PROGS = {"rm", "rmdir", "shred", "dd", "mkfs", "wipefs", "fdisk", "parted", "reboot",
                 "shutdown", "poweroff", "halt", "kill", "pkill", "killall", "truncate"}
#: Wrappers that run the command after them, with the options they may take
#: and still say what runs (any other option: a change).
WRAPPERS = {"sudo": {"-n", "--non-interactive"}, "nice": {"-n"}, "timeout": {"-s", "-k"},
            "time": {"-p"}, "stdbuf": {"-oL", "-eL", "-o0", "-e0"}}
#: Shell the tokenizer does not follow: always a change.
OPAQUE = re.compile(r"\$\(|`|<\(|>\(")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPS = {";", "|", "||", "&&", "|&"}
_REDIRS = {">", ">>", ">|", "&>", "&>>", ">&", "<>", "<", "<&"}
_PUNCT = set(";&|<>()")
SQL_WRITE = re.compile(r"\b(insert|update|delete|replace|upsert|create|drop|alter|attach|detach|"
                       r"vacuum|reindex|analyze|begin|commit|rollback|savepoint|release)\b", re.I)
SQL_READ = re.compile(r"^\s*(select|explain|with|values)\b", re.I)
#: Read-only SQL functions (a call to anything else, writefile() or
#: load_extension() included, is a change unless the CLI runs with -safe).
SQL_FUNCS = {
    "count", "sum", "total", "min", "max", "avg", "length", "lower", "upper", "substr",
    "substring", "trim", "ltrim", "rtrim", "coalesce", "ifnull", "nullif", "iif", "abs",
    "round", "typeof", "instr", "printf", "format", "quote", "hex", "date", "time", "datetime",
    "julianday", "unixepoch", "strftime", "group_concat", "string_agg", "json", "json_extract",
    "json_object", "json_array", "json_group_array", "json_group_object", "json_each",
    "json_tree", "json_type", "json_valid", "json_array_length", "cast", "like", "glob",
    "row_number", "rank", "dense_rank", "lag", "lead", "random", "char", "unicode", "sign",
    # SQL words followed by a parenthesis, not functions
    "in", "exists", "values", "as", "over", "filter", "using", "on", "and", "or", "not",
    "from", "join", "where", "select", "with", "case", "when", "then", "else", "is",
}
#: PRAGMAs that read with an argument (``pragma name(arg)``); any other
#: ``pragma name(arg)`` or ``pragma name = value`` sets something.
SQL_PRAGMA_READ = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                   "foreign_key_list", "table_list", "database_list", "function_list",
                   "integrity_check", "quick_check", "foreign_key_check"}
SQL_DOT_READ = re.compile(r"^\s*\.(tables|schema|indexes|indices|headers|mode|databases|"
                          r"show|width|nullvalue)\b[^;]*$", re.I)
SQLITE_FLAGS = {"-readonly", "-safe", "-header", "-noheader", "-column", "-json", "-line",
                "-csv", "-list", "-box", "-markdown", "-table", "-batch", "-bail", "-separator",
                "-newline", "-nullvalue"}
CURL_READ_LONG = ("--silent", "--show-error", "--fail", "--location", "--insecure", "--verbose",
                  "--include", "--head", "--header", "--max-time", "--connect-timeout",
                  "--user-agent", "--user", "--referer", "--compressed", "--retry",
                  "--write-out", "--get", "--url", "--http1.1", "--http2", "--ipv4", "--ipv6",
                  "--proxy", "--cacert", "--resolve", "--fail-with-body", "--no-progress-meter")
CURL_READ_SHORT = set("sSfLkviIHmAueGw46x")
GH_API_READ = ("--paginate", "--jq", "--template", "--header", "--include", "--cache",
               "--hostname", "--silent", "--verbose", "--slurp")
GH_API_SHORT = set("qtHi")
SERVICE_VERBS = {"restart": "restart", "stop": "stop", "start": "start", "reload": "reload",
                 "enable": "enable", "disable": "disable", "mask": "mask", "kill": "kill",
                 "daemon-reload": "reload the service definitions"}
_CHANGE = {"kind": "change"}


def _argv(args: Any) -> list[str]:
    if not isinstance(args, dict):
        return []
    v = args.get("argv")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    for key in ("cmd", "command"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return ["sh", "-c", v.strip()]
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return []


def _flags_ok(rest: list[str], longs: tuple | set, shorts: set) -> bool:
    """Every flag in ``rest`` is allowed: a long flag (or a single-dash word
    flag such as find's ``-name``) exactly or as ``--flag=value``; a short
    group whose letters are all allowed, with an optional glued number
    (``-n20``); a bare number (``head -20``)."""
    for a in rest:
        if not a.startswith("-") or a in ("-", "--") or re.fullmatch(r"-\d+", a):
            continue
        if a.split("=", 1)[0] in longs:
            continue
        if a.startswith("--"):
            return False
        m = re.fullmatch(r"-([A-Za-z]+)\d*", a)
        if m is None or not set(m.group(1)) <= shorts:
            return False
    return True


def _unwrap(argv: list[str]) -> list[str] | None:
    """The command a wrapper runs (``sudo``, ``timeout 60``, ``nice``), or
    ``None`` when the wrapper or a ``NAME=value`` prefix changes how it runs
    (env, an option we don't know): those are changes. ``sh -c "<script>"``
    becomes ``['<script>', script]``."""
    out = list(argv)
    for _ in range(6):
        if not out:
            return out
        if _ASSIGN.match(out[0]):
            return None                            # GIT_EXTERNAL_DIFF=..., LD_PRELOAD=...
        prog = _base(out[0])
        if prog in WRAPPERS:
            out = out[1:]
            ok = WRAPPERS[prog]
            while out and out[0].startswith("-"):
                if out[0] not in ok:
                    return None
                takes = prog in ("nice", "timeout") and out[0] in ("-n", "-s", "-k")
                out = out[2:] if takes else out[1:]
            if prog == "timeout":
                if not out or not re.fullmatch(r"[\d.]+[smhd]?", out[0]):
                    return None
                out = out[1:]
            continue
        if prog in ("sh", "bash", "dash", "zsh") and len(out) == 3 and out[1] in (
                "-c", "-lc", "-ec", "-euc", "-eucx", "-xc"):
            return ["<script>", out[2]]
        return out
    return None


def _tokens(script: str) -> tuple[list[list[str]], list[str]] | None:
    """``(simple commands, files written by redirects)`` of a script, or
    ``None`` when it uses shell the tokenizer does not follow (command or
    process substitution, backticks, background ``&``, subshells, heredocs,
    comments)."""
    if OPAQUE.search(script):
        return None
    segs: list[list[str]] = []
    writes: list[str] = []
    for line in script.split("\n"):
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            toks = list(lex)
        except ValueError:
            return None
        cur: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.startswith("#"):
                return None
            if t in _SEPS:
                if cur:
                    segs.append(cur)
                cur = []
                i += 1
                continue
            if t in _REDIRS:
                if cur and cur[-1].isdigit():
                    cur.pop()                      # 2>&1: the 2 is the stream
                target = toks[i + 1] if i + 1 < len(toks) else ""
                if not target or set(target) & _PUNCT:
                    return None
                if t in ("<", "<&") or (t == ">&" and (target.isdigit() or target == "-")):
                    pass
                elif target not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                    writes.append(target)
                i += 2
                continue
            if set(t) <= _PUNCT:                   # &, (, ), <<, <<<, ...
                return None
            cur.append(t)
            i += 1
        if cur:
            segs.append(cur)
    return segs, writes


def _sql_read(stmt: str, safe: bool) -> bool:
    s = stmt.strip()
    if SQL_DOT_READ.match(s):
        return True
    m = re.match(r"^\s*pragma\s+([\w.]+)\s*(\(\s*[\w.'\"]*\s*\))?\s*$", s, re.I)
    if m:
        name = m.group(1).split(".")[-1].lower()
        return not m.group(2) or name in SQL_PRAGMA_READ
    if not SQL_READ.match(s) or SQL_WRITE.search(s):
        return False
    if safe:
        return True
    calls = {c.lower() for c in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", s)}
    return calls <= SQL_FUNCS


def _sqlite(rest: list[str]) -> dict:
    words, flags = [], []
    it = iter(rest)
    for a in it:
        if a.startswith("-"):
            flags.append(a)
            if a in ("-separator", "-newline", "-nullvalue"):
                next(it, None)
        else:
            words.append(a)
    db = words[0] if words else ""
    stmts = [s for w in words[1:] for s in w.split(";") if s.strip()]
    label = f"{_base(db).rsplit('.', 1)[0]} database" if db else "a database"
    ro = all(f in SQLITE_FLAGS for f in flags) and bool(stmts) and all(
        _sql_read(s, "-safe" in flags) for s in stmts)
    if ro:
        return {"kind": "read", "title": f"Read {label}",
                "want": f"run a read-only query on the {label}"}
    return {"kind": "change", "title": f"Change {label}",
            "want": f"run a query that can change the {label}"}


def _git(rest: list[str]) -> dict:
    if rest and rest[0].startswith("-"):
        # git -c / -C / --git-dir before the subcommand: config (core.fsmonitor,
        # core.pager...) or another repository; not judged.
        return {"kind": "change", "title": "Run git", "want": "run git with global options"}
    sub = rest[0] if rest else ""
    args = rest[1:]
    if any(a == d or a.startswith(d + "=") or (d == "-O" and a.startswith("-O"))
           for a in args for d in GIT_DENY):
        read = False
    elif sub in GIT_READ:
        read = True
    elif sub in GIT_LIST:
        read = all(a in GIT_LIST[sub] for a in args)
    elif sub == "remote":
        read = not args or args == ["-v"] or (args[0] in ("show", "get-url") and all(
            not a.startswith("-") for a in args[1:]))
    elif sub == "config":
        read = bool(args) and args[0] in ("--get", "--get-all", "--list", "-l") and len(args) <= 2
    elif sub == "stash":
        read = bool(args) and args[0] == "list"
    elif sub == "worktree":
        read = args == ["list"]
    else:
        read = False
    if read:
        return {"kind": "read", "title": "Look at a git checkout", "want": f"run git {sub}"}
    if sub == "push":
        return {"kind": "change", "title": "Push code", "want": "push commits to a git remote"}
    if sub in ("reset", "clean", "checkout", "restore", "switch") and _has(
            args, ("--hard", "-f", "--force", "-fd", "-fdx", "-x", ".", "--discard-changes")):
        return {"kind": "destroy", "title": "Discard code changes",
                "want": f"run git {sub}, which can throw away uncommitted work"}
    return {"kind": "change", "title": "Change a git checkout", "want": f"run git {sub}".strip()}


def _has(rest: list[str], marks: tuple) -> bool:
    return any(a == m or a.startswith(m) for a in rest for m in marks)


def _reads(prog: str, rest: list[str]) -> bool:
    """Whether ``prog rest`` only reads, by the allowlists above."""
    words = [a for a in rest if not a.startswith("-")]
    if prog in READ_ANY:
        return True
    if prog in READ_FLAGS:
        longs, shorts = READ_FLAGS[prog]
        if not _flags_ok(rest, longs, shorts):
            return False
        check = READ_WORDS.get(prog)
        return check is None or check(words)
    if prog == "hostname":
        return not rest or all(a in ("-f", "-s", "-i", "-I", "-d", "--fqdn", "--short",
                                     "--ip-address", "--all-ip-addresses") for a in rest)
    if prog == "ip":
        if not words:
            return False
        objs = [a for a in rest if a in IP_OBJECTS]
        rest_words = [a for a in rest if a not in IP_OBJECTS]
        return bool(objs) and (not rest_words or rest_words[0] in IP_VERBS)
    if prog in READ_SUBS:
        if _has(rest, SUB_DENY):
            return False
        sub = words[0] if words else ""
        if not sub:
            return prog in READ_BARE
        return sub in READ_SUBS[prog]
    if prog == "pacman":
        first = rest[0] if rest else ""
        return (first.startswith("-Q") or first in ("-Ss", "-Si")) and all(
            not a.startswith("-") for a in rest[1:])
    if prog == "curl":
        urls = [w for w in words if "://" in w]
        return bool(urls) and all(u.startswith(("http://", "https://")) for u in urls) and \
            _flags_ok(rest, CURL_READ_LONG, CURL_READ_SHORT)
    if prog == "gh":
        sub = rest[0] if rest else ""
        if sub == "api":
            return _flags_ok(rest[1:], GH_API_READ, GH_API_SHORT)
        return sub in ("pr", "run", "issue", "repo", "release") and len(rest) > 1 and rest[1] in (
            "view", "list", "status", "diff", "checks")
    return False


def _shell_segment(argv: list[str], depth: int = 0) -> dict:
    """``{kind: read|change|destroy, title, want}`` for one simple command."""
    argv = _unwrap(argv)
    if argv is None:
        return {"kind": "change", "title": "Run a command",
                "want": "run a command with settings that change how it runs"}
    if not argv:
        return {"kind": "change", "title": "Run a command", "want": "run a command"}
    if argv[0] == "<script>":
        if depth < 2:
            return _script(argv[1], depth + 1)
        return {"kind": "change", "title": "Run a script", "want": "run a shell script"}
    prog = _base(argv[0])
    rest = argv[1:]
    words = [a for a in rest if not a.startswith("-")]
    if prog == "sqlite3":
        return _sqlite(rest)
    if prog == "git":
        return _git(rest)
    if prog == "ssh":
        return {"kind": "change", "title": "Run a command over ssh",
                "want": "run a command on another machine over ssh"}
    reads = _reads(prog, rest)
    if prog in ("systemctl", "service"):
        sub = words[0] if words else ""
        unit = words[1] if len(words) > 1 else ""
        if prog == "service":
            unit, sub = (words[0], words[1]) if len(words) > 1 else (sub, "")
        if reads or (prog == "service" and sub == "status"):
            return {"kind": "read", "title": "Check a service" if unit else "Check services",
                    "want": f"check the status of {unit or 'services'}"}
        verb = SERVICE_VERBS.get(sub, sub)
        return {"kind": "change", "title": f"{verb.capitalize()} {unit or 'a service'}".strip(),
                "want": f"{verb} the service {unit}".strip()}
    if reads:
        target = next((w for w in words if "/" in w or "." in w), "")
        if prog in ("journalctl", "dmesg"):
            return {"kind": "read", "title": "Read logs", "want": "read the system logs"}
        if prog in ("cat", "head", "tail", "zcat", "tac", "nl"):
            return {"kind": "read", "title": f"Read {_base(target)}" if target else "Read a file",
                    "want": f"read {target}" if target else "read a file"}
        if prog in ("ls", "stat", "du", "df", "find"):
            return {"kind": "read", "title": "Look at files",
                    "want": f"list files{' in ' + target if target else ''}"}
        if prog in ("grep", "egrep", "fgrep", "zgrep", "rg"):
            return {"kind": "read", "title": "Search files", "want": "search files for text"}
        if prog == "curl":
            return {"kind": "read", "title": "Fetch a web page", "want": "fetch a URL"}
        if prog in ("gh",):
            return {"kind": "read", "title": "Look at GitHub", "want": "read from GitHub"}
        if prog == "ip":
            return {"kind": "read", "title": "Look at the network", "want": "show network settings"}
        if prog == "nvidia-smi":
            return {"kind": "read", "title": "Check the GPUs", "want": "show GPU status"}
        if prog in READ_SUBS or prog == "pacman":
            return {"kind": "read", "title": f"Look at {prog}", "want": f"run {prog} "
                    f"{words[0] if words else ''}".strip()}
        return {"kind": "read", "title": "Check the machine", "want": f"run {prog}"}
    if prog in DESTROY_PROGS:
        what = {"reboot": "reboot the machine", "shutdown": "shut the machine down",
                "poweroff": "power the machine off", "halt": "halt the machine"}.get(prog)
        if what:
            return {"kind": "destroy", "title": what.capitalize().replace(" the machine", ""),
                    "want": what}
        if prog in ("kill", "pkill", "killall"):
            return {"kind": "destroy", "title": "Stop a process", "want": "stop a running process"}
        return {"kind": "destroy", "title": "Delete files" if prog in ("rm", "rmdir", "shred")
                else "Erase a disk or file", "want": f"run {prog}, which deletes data"}
    if prog == "find" and _has(rest, ("-delete",)):
        return {"kind": "destroy", "title": "Find and delete files", "want": "find and delete files"}
    if prog == "sed" and (_has(rest, ("--in-place",)) or any(
            re.match(r"-[A-Za-z]*i", a) for a in rest if not a.startswith("--"))):
        return {"kind": "change", "title": "Edit a file", "want": "edit a file in place"}
    if prog in ("cp", "mv", "tee", "touch", "mkdir", "ln", "chmod", "chown", "install",
                "rsync", "scp", "tar", "unzip", "wget", "sed", "awk", "patch", "curl"):
        return {"kind": "change", "title": "Change files" if prog != "curl"
                else "Send or save a web request", "want": f"run {prog}, which can change things"}
    if prog in ("apt", "apt-get", "pip", "pip3", "npm", "pacman", "yay", "dnf", "brew"):
        return {"kind": "change", "title": "Install or change software",
                "want": f"run {prog} {words[0] if words else ''}".strip()}
    if prog in ("docker", "podman", "kubectl"):
        sub = words[0] if words else ""
        kind = "destroy" if sub in ("rm", "rmi", "prune", "delete", "down", "kill") else "change"
        return {"kind": kind, "title": f"Change containers ({sub})" if sub else f"Run {prog}",
                "want": f"run {prog} {sub}".strip()}
    if "deploy" in " ".join(argv).lower():
        return {"kind": "change", "title": "Deploy", "want": "run a deploy script"}
    if prog in ("pytest", "rook-pytest") or (prog in ("python", "python3") and "pytest" in rest):
        return {"kind": "change", "title": "Run tests", "want": "run the test suite"}
    if prog in ("python", "python3", "node", "bash", "sh", "make", "cargo", "go", "npx", "uv",
                "ruby", "perl"):
        script = next((w for w in words), "")
        return {"kind": "change", "title": f"Run {_base(script) or prog}",
                "want": f"run {prog} {_base(script)}".strip()}
    return {"kind": "change", "title": f"Run {prog}", "want": f"run {prog}"}


def _script(script: str, depth: int = 0) -> dict:
    parsed = _tokens(script)
    if parsed is None:
        return {"kind": "change", "title": "Run a script",
                "want": "run a shell script (not checked: it uses shell features the summary "
                        "does not follow)", "steps": 1}
    segs_raw, writes = parsed
    segs = [_shell_segment(s, depth) for s in segs_raw] or [
        {"kind": "change", "title": "Run a script", "want": "run a shell script"}]
    for path in writes:
        segs.append({"kind": "change", "title": f"Write {_base(path)}", "want": f"write {path}"})
    kinds = {s["kind"] for s in segs}
    kind = "destroy" if "destroy" in kinds else "change" if "change" in kinds else "read"
    main = next((s for s in segs if s["kind"] == kind), segs[0])
    if len(segs) > 1 and kind == "read":
        main = next((s for s in segs if s["title"] not in ("Check the machine",)), main)
    return {**main, "kind": kind, "steps": len(segs)}


def shell_summary(args: Any) -> dict:
    """``{kind, title, want, command}`` for a shell call: ``kind`` is read
    only when every part of the command is on the read allowlists (said as
    "looks read-only"), destroy when any part deletes, else change."""
    argv = _argv(args)
    if not argv:
        return {"kind": "change", "title": "Run a command", "want": "run a command", "command": ""}
    inner = _unwrap(argv)
    if inner is not None and inner[:1] == ["<script>"]:
        out = _script(inner[1])
        command = inner[1]
    else:
        out = _shell_segment(argv)
        if any(a in _REDIRS for a in argv):
            out = {**out, "kind": "destroy" if out["kind"] == "destroy" else "change"}
        command = " ".join(argv)
    return {"kind": out["kind"], "title": out["title"], "want": out["want"],
            "command": short(command, 300), "steps": out.get("steps", 1)}


# -- what a call does --------------------------------------------------------------

def _data(args: Any) -> dict:
    d = args.get("data") if isinstance(args, dict) else None
    return d if isinstance(d, dict) else {}


def _on(worker: str, hub: bool) -> str:
    if hub or str(worker or "").lower() in ("rook", "hub"):
        return "on the hub"
    return f"on {worker}" if worker else ""


def call_summary(cap: str, args: Any, worker: str = "", *, hub: bool = False,
                 tier: str = "", tags: tuple | list = ()) -> dict:
    """``{title, want, kind}`` for one cap call; ``kind`` is read, change,
    destroy or admin (feeds :func:`risk_text`)."""
    cap = str(cap or "")
    args = args if isinstance(args, dict) else {}
    on = _on(worker, hub)
    where = f" {on}" if on else ""
    act = str(args.get("action") or "")
    data = _data(args)
    destructive = "destructive" in (tags or ())
    if cap in ("shell.exec", "proc.spawn", "work.stream.open") and _argv(args):
        s = shell_summary(args)
        want = s["want"] + where
        return {"title": s["title"], "want": want, "kind": s["kind"], "command": s["command"]}
    if cap in ("proc.write", "work.stream.write", "console.write"):
        # Typed text can be an answer to a password prompt: it never goes into
        # the title or the sentence, only under Explain.
        return {"title": "Type into a console", "want": f"type a line into a console{where}",
                "kind": "change", "command": short(str(args.get("data") or ""), 300)}
    if cap.startswith("file."):
        path = str(args.get("path") or args.get("dest") or "")
        verb = cap.split(".", 1)[1]
        if verb in ("read", "stat", "list", "ls", "exists", "hash"):
            return {"title": f"Read {_base(path) or 'a file'}", "want": f"read {path or 'a file'}{where}",
                    "kind": "read"}
        if verb in ("delete", "remove", "rm", "rmdir"):
            return {"title": f"Delete {_base(path) or 'a file'}",
                    "want": f"delete {path or 'a file'}{where}", "kind": "destroy"}
        return {"title": f"Write {_base(path) or 'a file'}", "want": f"write {path or 'a file'}{where}",
                "kind": "change"}
    if cap == "task.write":
        title = str(data.get("title") or "").strip()
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
        if act == "create":
            return {"title": "Create a task", "want": f'create the task "{short(title, 80)}"'
                    + (f" under {data['parent']}" if data.get("parent") else ""), "kind": "change"}
        ref = args.get("id") or "a task"
        if patch.get("state"):
            return {"title": f"Mark a task {patch['state']}",
                    "want": f"set task {ref} to {patch['state']}", "kind": "change"}
        return {"title": f"{(act or 'update').capitalize()} a task",
                "want": f"{act or 'update'} task {ref}", "kind": "change"}
    if cap == "knowledge.write":
        title = str(data.get("title") or args.get("id") or "").strip()
        verb = {"create": "write a new knowledge page", "update": "edit the knowledge page",
                "retract": "retract the knowledge page", "link": "link evidence to the page"}.get(
            act, f"{act or 'change'} knowledge")
        return {"title": "Write knowledge" if act != "retract" else "Retract knowledge",
                "want": f'{verb}{(" " + chr(34) + short(title, 70) + chr(34)) if title else ""}',
                "kind": "change"}
    if cap in ("home.session_note",):
        role = args.get("role") or "house"
        return {"title": "Leave a note for the house agent" if role == "house"
                else f"Leave a note for {role}",
                "want": f'leave a background note for the {role} session: "{short(args.get("text"), 90)}"',
                "kind": "change"}
    if cap in ("home.session_ask", "home.ask"):
        return {"title": "Ask the house agent", "want":
                f'send the house agent: "{short(args.get("text") or args.get("question"), 90)}"',
                "kind": "change"}
    if cap == "home.session_flow":
        return {"title": f"Steer the house agent ({args.get('tool') or 'flow'})",
                "want": f"run the voice flow tool {args.get('tool') or ''} on the house agent".strip(),
                "kind": "change"}
    if cap == "home.session_compact":
        return {"title": "Compact the house agent's memory",
                "want": f"compact the house agent's {args.get('lane') or 'main'} session",
                "kind": "change"}
    if cap.startswith(("home.session", "role.session")) or cap in ("sessions.send",
                                                                    "work.stream.open"):
        if "start" in cap or cap == "work.stream.open":
            return {"title": "Start a coding session",
                    "want": f"start a coding session{where}"
                    + (f" on branch {args['branch']}" if args.get("branch") else "")
                    + (f" in {args['cwd']}" if args.get("cwd") else ""), "kind": "change"}
        return {"title": "Message a coding session",
                "want": f'send a coding session: "{short(args.get("text") or args.get("instructions"), 90)}"',
                "kind": "change"}
    if cap.startswith("huddle."):
        verb = cap.split(".", 1)[1]
        return {"title": {"vote": "Vote in a huddle", "pin": "Pin to the conference table",
                          "start": "Start a huddle", "post": "Post in a huddle",
                          "card": "Decide a huddle card"}.get(verb, "Change a huddle"),
                "want": f"{verb} in a huddle", "kind": "change"}
    if cap.startswith("trust."):
        change = args.get("change") if isinstance(args.get("change"), dict) else _data(args).get("change")
        op = (change or {}).get("op") if isinstance(change, dict) else None
        if "propose" in cap or act == "propose":
            return {"title": "Propose a trust map change",
                    "want": f"propose a trust map change{(' (' + op + ')') if op else ''} for you to accept",
                    "kind": "change"}
        return {"title": "Change the trust map", "want": f"change the trust map ({act or cap})",
                "kind": "admin"}
    if cap.startswith(("secret.", "vault.")):
        name = args.get("name") or ""
        return {"title": "Change a secret" if "get" not in cap else "Read a secret",
                "want": f"{cap.split('.', 1)[1]} the secret {name}".strip(), "kind": "admin"}
    if cap in ("worker.update", "worker.apply", "ota.push", "update.push"):
        return {"title": f"Update {worker or 'a worker'}",
                "want": f"install a new Rook build{where}", "kind": "admin"}
    if cap in ("worker.deauth", "band.deauth"):
        return {"title": f"Remove {worker or 'a worker'} from the band",
                "want": f"take {worker or 'a worker'} off the band", "kind": "destroy"}
    if cap.startswith(("config.", "settings.", "policy.")):
        key = args.get("key") or args.get("path") or ""
        return {"title": "Change settings", "want": f"change {cap.split('.', 1)[0]}"
                + (f" {key}" if key else "") + where, "kind": "admin"}
    if cap.startswith(("job.", "jobs.")):
        name = args.get("name") or args.get("job") or args.get("id") or ""
        verb = cap.split(".", 1)[1].split(":", 1)[0]
        return {"title": f"{'Run' if verb == 'run' else 'Change'} a job",
                "want": f"{verb} the job {name}".strip(), "kind": "change"}
    if cap in ("notify.post", "notify.send", "voice.speak", "attention.send", "sms.send",
               "messages.send", "telegram.send"):
        return {"title": "Send you a message" if cap.startswith(("notify", "voice", "attention"))
                else "Send a message", "want": f"send a message{where}", "kind": "change"}
    if cap.startswith(("chat.", "msg.")):
        verb = cap.split(".", 1)[1]
        return {"title": "Delete chat" if "delete" in verb else "Post in chat",
                "want": f"{verb} in chat", "kind": "destroy" if "delete" in verb else "change"}
    if cap.startswith(("info.", "caps.", "device.info", "battery.")) and tier in ("", "read"):
        return {"title": f"Check {worker or 'a machine'}",
                "want": f"read {cap.split('.', 1)[1].replace('_', ' ')}{where}", "kind": "read"}
    if "deploy" in cap or act == "deploy":
        return {"title": "Deploy", "want": f"deploy{where}", "kind": "admin"}
    # Unknown cap: say it by its parts.
    verb = cap.split(".")[-1].replace("_", " ")
    noun = cap.split(".")[0]
    kind = "destroy" if destructive else "admin" if tier == "admin" else \
        "read" if tier == "read" else "change"
    return {"title": short(f"{verb.capitalize()} {noun}", TITLE_MAX),
            "want": f"use {cap}{(' (' + act + ')') if act else ''}{where}", "kind": kind}


# -- risk --------------------------------------------------------------------------

_TRUST_ENTRY = re.compile(r'entry "([^"]+)" \((ship-it|guarded|sacred)\)')
_TRUST_UNLISTED = re.compile(r"unlisted \((ship-it|balanced|guarded|sacred)\)")


def trust_of(reason: str) -> dict:
    """``{entry, level}`` from a trust hold's cite ("" when not a trust hold)."""
    reason = str(reason or "")
    m = _TRUST_ENTRY.search(reason)
    if m:
        return {"entry": m.group(1), "level": m.group(2)}
    m = _TRUST_UNLISTED.search(reason)
    if m:
        return {"entry": "", "level": m.group(1)}
    return {}


def risk_text(kind: str, tier: str = "", *, hold_kind: str = "", reason: str = "",
              where: str = "", hub: bool = False) -> str:
    """``Low|Medium|High: <why>`` in one sentence."""
    tier = str(tier or "")
    if kind == "read":
        head = ("Low", "looks read-only" if tier and tier != "read" else "it only reads")
    elif kind == "destroy":
        head = ("High", "it can delete or overwrite things")
    elif kind == "admin" or tier == "admin":
        head = ("High", TIER_RISK["admin"][1])
    elif kind == "question":
        head = ("None by itself", "it is a question; your answer goes back to the asker")
    elif kind == "record":
        head = ("Low", "it only adds or edits a record you can change back")
    else:
        head = TIER_RISK.get(tier, ("Medium", "it changes things"))
        if tier == "read":
            head = ("Medium", "it changes things")
    level, why = head
    extra = ""
    t = trust_of(reason) if hold_kind == "trust" or "trust map" in str(reason) else {}
    place = where or ("the hub" if hub else "")
    if t:
        name = t.get("entry") or place or "this"
        if t["level"] == "sacred":
            extra = f"{name} is sacred, so only you change it"
        elif t["level"] == "guarded":
            extra = f"{name} is guarded, so changes there ask you"
        elif t["level"] == "balanced":
            extra = "admin actions ask you"
    elif hold_kind == "hub-host":
        extra = (f"{place or 'it'} runs on the hub's own machine, where a command could "
                 "rewrite the hub's approvals and settings")
    elif hold_kind == "spread":
        m = re.search(r"(\d+\w*) machine in (\d+) minutes", str(reason))
        extra = (f"it would be the {m.group(1)} machine this agent touched in {m.group(2)} "
                 "minutes" if m else "this agent is reaching many machines quickly")
    elif hold_kind == "default" and level != "High":
        extra = "it is an admin or destructive action, which always asks you"
    if kind == "read" and extra:
        return f"{level}: {why}, but {extra}."
    return f"{level}: {why}" + (f"; {extra}." if extra else ".")


# -- the whole ask -----------------------------------------------------------------

def _args_line(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(args)


def explain_text(item: dict, call: dict | None = None) -> str:
    """The technical detail behind Explain: cap, worker, command or args,
    why it was held (the trust entry), rule, and the hold's id and name."""
    parts = []
    cap, worker = item.get("cap"), item.get("worker")
    if cap:
        parts.append(f"{cap} on {worker or 'the band'}")
        cmd = (call or {}).get("command")
        parts.append(f"command: `{short(cmd, 300)}`" if cmd else
                     (f"args: {short(_args_line(item.get('args')), 300)}" if item.get("args") else ""))
    if item.get("tier"):
        parts.append(f"tier {item['tier']}" + (f", tags {', '.join(item['tags'])}"
                                               if item.get("tags") else ""))
    if item.get("reason"):
        parts.append(f"held: {short(item['reason'], 300)}")
    if item.get("hold_kind") and item.get("hold_kind") not in ("trust",):
        parts.append(f"{item['hold_kind']} hold")
    if item.get("rule"):
        parts.append(f"rule {item['rule']}")
    if item.get("principal"):
        parts.append(f"caller {item['principal']}")
    via = item.get("via") or []
    if via:
        parts.append("for " + ", ".join(str(v) for v in via))
    ctx = item.get("context") or {}
    if ctx.get("task"):
        parts.append(f"task {ctx['task']}")
    if ctx.get("room"):
        parts.append(f"console {ctx['room']}")
    if item.get("card_kind"):
        parts.append(f"{item['card_kind']} card")
    if item.get("room"):
        parts.append(f"huddle room {item['room']}")
    ident = item.get("id")
    if ident:
        parts.append(f"id {ident}" + (f" ({item['name']})" if item.get("name") else ""))
    out = "; ".join(p for p in parts if p)
    return short(out, EXPLAIN_MAX)


def describe(item: dict) -> dict:
    """``{title, for, who, want, why?, risk, explain}`` for one waiting item
    (a :meth:`rook.hub.relay.Relay.items` row, or :func:`hold_item` of a
    hold row)."""
    kind = item.get("kind") or "hold"
    frm = item.get("from") if isinstance(item.get("from"), dict) else {}
    ctx = item.get("context") if isinstance(item.get("context"), dict) else {}
    who = who_name(item.get("who_label") or item.get("who"), principal=item.get("principal"),
                   role=frm.get("role"), job=frm.get("job"))
    why = short(ctx.get("why") or "", 200)
    call = None
    if kind == "ask":
        q = str(item.get("question_text") or "").strip()
        opts = item.get("options") or []
        out = {"title": short(q or "A question for you", QUESTION_MAX),
               "want": _sentence(f'get your answer to: "{short(q, 160)}"'
                                 + (f" ({' / '.join(opts)})" if opts else "")),
               "risk": risk_text("question")}
    elif kind == "house":
        line = str(item.get("line") or "").strip()
        cap = item.get("cap")
        if cap:
            call = call_summary(cap, item.get("args") or {}, str(item.get("worker") or ""),
                                hub=bool(item.get("hub")), tier=item.get("tier") or "",
                                tags=item.get("tags") or ())
        out = {"title": short(line or (call or {}).get("title") or "A decision for you",
                              QUESTION_MAX),
               "want": _sentence(call["want"] if call else f"ask you: {line}"),
               "risk": risk_text(call["kind"], item.get("tier") or "", hub=bool(item.get("hub")))
               if call else risk_text("question")}
        who = who_name("the house agent")
    elif kind == "card" and not item.get("cap"):
        summ = str(item.get("summary") or "").strip()
        if item.get("card_kind") == "ask_bake":
            out = {"title": short(summ.rstrip("."), QUESTION_MAX) or "A question for you",
                   "want": _sentence(f'get your answer to: "{short(summ, 160)}"'),
                   "risk": risk_text("question")}
        else:
            out = {"title": f"Note from {who}", "want": _sentence(f"tell you: {short(summ, 180)}"),
                   "risk": "None: a note runs nothing."}
    else:
        call = call_summary(item.get("cap") or "", item.get("args") or {},
                            str(item.get("worker") or ""), hub=bool(item.get("hub")),
                            tier=item.get("tier") or "", tags=item.get("tags") or ())
        title = call["title"]
        data = _data(item.get("args"))
        if item.get("cap") == "task.write" and item.get("args", {}).get("action", "create") == \
                "create" and data.get("title"):
            title = f'Create task "{short(data["title"], 44)}"'
        elif kind == "card" and item.get("card_kind") == "task.create":
            title = "Create a task"
        rk = call["kind"]
        if rk == "change" and item.get("cap") in ("task.write", "knowledge.write") or \
                item.get("cap") == "home.session_note":
            rk = "record"
        where = str(item.get("worker") or "")
        out = {"title": short(title, TITLE_MAX), "want": _sentence(call["want"]),
               "risk": risk_text(rk, item.get("tier") or "", hold_kind=item.get("hold_kind") or "",
                                 reason=item.get("reason") or "", where=where,
                                 hub=bool(item.get("hub")))}
        if kind == "card" and item.get("summary") and not why:
            why = short(item["summary"], 200)
    out["for"] = for_text(ctx, frm)
    out["who"] = who
    if why:
        out["why"] = why
    out["explain"] = explain_text(item, call) if kind not in ("house", "ask") else short(
        (str(item.get("explain") or "") + ("; " if item.get("explain") else "")
         + f"id {item.get('id')}" + (f" ({item['name']})" if item.get("name") else "")),
        EXPLAIN_MAX)
    return out


def lines(p: dict, *, explain: bool = False) -> list[str]:
    """The ask as text lines (phone card body, voice, plain clients)."""
    rows = [f"For: {p['for']}", f"Who: {p['who']}", f"I want to: {p['want']}"]
    if p.get("why"):
        rows.append(f"Why: {p['why']}")
    rows.append(f"Risk: {p['risk']}")
    if explain:
        rows.append(f"Explain: {p['explain']}")
    return rows


def hold_item(hold: dict) -> dict:
    """A hold row (``approvals.db``) as the item :func:`describe` reads."""
    via = [str(v) for v in hold.get("via") or []]
    job = next((v[4:] for v in via if v.startswith("job:")), None)
    ctx = hold.get("context") if isinstance(hold.get("context"), dict) else {}
    frm = {}
    if job:
        frm["job"] = ctx.get("job") or job
    m = _ROLE.search(" ".join([str(hold.get("principal") or ""),
                               str(hold.get("principal_label") or ""), *via]))
    if m:
        frm["role"] = m.group(1)
    return {"kind": "job" if job else "hold", "id": hold.get("id"), "name": hold.get("name"),
            "who": hold.get("principal_label") or hold.get("principal") or "",
            "principal": hold.get("principal"), "from": frm, "context": ctx,
            "cap": hold.get("cap"), "args": hold.get("args") or {},
            "worker": hold.get("target_name") or hold.get("target_id") or "",
            "tier": hold.get("tier"), "tags": list(hold.get("tags") or []),
            "hub": bool(hold.get("is_hub")), "hold_kind": hold.get("kind"),
            "reason": hold.get("reason") or "", "rule": hold.get("rule"), "via": via}


def for_hold(hold: dict) -> dict:
    return describe(hold_item(hold))


def card_item(card: dict, huddle_title: str = "") -> dict:
    """A huddle action card as the item :func:`describe` reads (the huddle
    page; :meth:`rook.hub.relay.Relay._cards` adds the roster's tier and
    hub-host facts for the phone and ``rook_approvals``)."""
    from ..core import authz as core_authz
    call = card.get("call") or {}
    cap = str(call.get("cap") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    tier = core_authz.effective_tier(cap, None) if cap else "write"
    return {"kind": "card", "id": card.get("id"), "name": card.get("name"),
            "who": card.get("by") or "", "cap": cap, "args": args, "worker": call.get("worker"),
            "tier": tier, "tags": list(core_authz.builtin_tags(cap)) if cap else [],
            "hub": str(call.get("worker") or "").lower() in ("rook", "hub"),
            "card_kind": card.get("kind"), "summary": card.get("summary") or "",
            "room": card.get("room"),
            "context": {"huddle": huddle_title or card.get("room") or ""}}


def safe_title(cap: str, worker: str = "", *, hub: bool = False) -> str:
    """A title from the cap and the machine only, never the arguments: what
    leaves the dashboard and the phone (Telegram, notify, voice, the spoken
    summary) uses this, so nothing an agent typed or passed travels there."""
    cap = str(cap or "")
    on = _on(worker, hub)
    where = f" {on}" if on else ""
    if cap in ("shell.exec", "proc.spawn", "work.stream.open"):
        return f"Run a command{where}"
    if cap in ("proc.write", "work.stream.write", "console.write"):
        return f"Type into a console{where}"
    if cap.startswith("file."):
        verb = cap.split(".", 1)[1]
        what = ("Read" if verb in ("read", "stat", "list", "ls", "exists", "hash") else
                "Delete" if verb in ("delete", "remove", "rm", "rmdir") else "Write")
        return f"{what} a file{where}"
    title = call_summary(cap, {}, worker, hub=hub)["title"]
    return title if " on " in title or not where or worker in title else f"{title}{where}"


def outside(item: dict) -> dict:
    """``{title, for, who, risk}`` with an argument-free title, for channels
    that leave the dashboard and the phone (no why, no explain)."""
    p = describe(item)
    title = p["title"] if item.get("kind") in ("ask", "house") or (
        item.get("kind") == "card" and not item.get("cap")) else safe_title(
        item.get("cap") or "", str(item.get("worker") or ""), hub=bool(item.get("hub")))
    return {"title": title, "for": p["for"], "who": p["who"], "risk": p["risk"]}
