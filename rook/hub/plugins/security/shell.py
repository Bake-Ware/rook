"""Shell commands in plain words, with the read-only guess (advanced
security; moved from rook/hub/plain.py by the change-management reset).

Read-only is earned, not guessed: a command reads only when it is a known
read-only program, run with flags from that program's allowlist (votes
high-inch-gallery and gifted-organ-marigold, 2026-10-23). Core says every
command is a change; with the plugin on, :func:`shell_summary` words the
Risk line as before.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from ...plain import _argv, _base, short

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
    "lsblk", "lsusb", "lspci", "netstat", "ping", "dig", "nslookup", "host", "jq",
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
    "nvidia-smi": lambda w: all(x in ("dmon", "pmon") or re.fullmatch(r"[\d,]+", x) for x in w),
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
#: Value-less options a subcommand tool may take before its subcommand; any
#: other leading option may take a value that would look like a subcommand
#: (``kubectl --namespace get delete pod x`` deletes).
SUB_GLOBAL = {"systemctl": {"--user", "--system", "--no-pager", "-l", "--full", "-a", "--all",
                            "-q", "--quiet", "--no-legend"}}
#: Subcommand tools that read with no subcommand at all.
READ_BARE = {"systemctl", "hostnamectl", "timedatectl", "loginctl", "hostname"}
#: Flags that make a subcommand tool act elsewhere or run code: never read.
SUB_DENY = ("-H", "--host", "-M", "--machine", "--kubeconfig", "--context", "--server",
            "--exec", "-c", "--config")
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "blame",
            "shortlog", "rev-list", "cat-file", "ls-tree"}
#: Git flags that run a program or write a file.
GIT_DENY = ("--ext-diff", "--textconv", "--output", "-O", "--open-files-in-pager",
            "--upload-pack", "--exec", "--receive-pack", "--run", "--config", "--hook",
            "--filters")
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
    "sqlite_version", "sqlite_source_id", "changes", "total_changes", "last_insert_rowid",
    "randomblob", "zeroblob", "octet_length", "concat", "concat_ws", "unhex", "timediff",
    "likely", "unlikely", "likelihood", "ntile", "first_value", "last_value", "nth_value",
    "percent_rank", "cume_dist", "median", "percentile",
    # SQL words followed by a parenthesis, not functions
    "in", "exists", "values", "as", "over", "filter", "using", "on", "and", "or", "not",
    "from", "join", "where", "select", "with", "case", "when", "then", "else", "is",
    "by", "between", "limit", "offset", "having", "distinct", "all", "union", "except",
    "intersect", "escape",
}
#: PRAGMAs that read with an argument (``pragma name(arg)``); any other
#: ``pragma name(arg)`` or ``pragma name = value`` sets something.
SQL_PRAGMA_READ = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                   "foreign_key_list", "table_list", "database_list", "function_list",
                   "integrity_check", "quick_check", "foreign_key_check"}
#: PRAGMAs that only read when given no argument (``pragma optimize``,
#: ``wal_checkpoint``, ``incremental_vacuum`` and ``shrink_memory`` act).
SQL_PRAGMA_BARE = {"journal_mode", "user_version", "schema_version", "application_id",
                   "page_size", "page_count", "freelist_count", "encoding", "foreign_keys",
                   "table_list", "database_list", "compile_options", "integrity_check",
                   "quick_check", "foreign_key_check", "function_list", "collation_list",
                   "module_list", "pragma_list", "data_version", "cache_size", "synchronous",
                   "auto_vacuum", "busy_timeout", "mmap_size", "temp_store", "locking_mode",
                   "max_page_count", "secure_delete", "recursive_triggers", "query_only",
                   "cell_size_check", "defer_foreign_keys", "ignore_check_constraints",
                   "legacy_alter_table", "reverse_unordered_selects", "trusted_schema"}
SQL_DOT_READ = re.compile(r"^\s*\.(tables|schema|indexes|indices|headers|mode|databases|"
                          r"show|width|nullvalue)\b[^;]*$", re.I)
SQLITE_FLAGS = {"-readonly", "-safe", "-header", "-noheader", "-column", "-json", "-line",
                "-csv", "-list", "-box", "-markdown", "-table", "-batch", "-bail", "-separator",
                "-newline", "-nullvalue"}
GH_API_READ = ("--paginate", "--jq", "--template", "--header", "--include", "--cache",
               "--hostname", "--silent", "--verbose", "--slurp")
GH_API_SHORT = set("qtHi")
SERVICE_VERBS = {"restart": "restart", "stop": "stop", "start": "start", "reload": "reload",
                 "enable": "enable", "disable": "disable", "mask": "mask", "kill": "kill",
                 "daemon-reload": "reload the service definitions"}
_CHANGE = {"kind": "change"}


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


def _plain_words(line: str) -> bool:
    """No word of ``line`` is changed by the shell before the command sees
    it: no ``$`` (variables, ``${..}``, ``$'..'``), no unquoted glob at the
    start of a word (``*`` can expand to a file named ``-delete``), no brace
    expansion. Read from the raw words, quotes kept (vote sticky-foal-vanilla)."""
    lex = shlex.shlex(line, posix=False, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        return False
    for w in raw:
        if "$" in w or ("{" in w and ("," in w or ".." in w)):
            return False
        if _unquoted_glob(w) and not (w[:1] not in "-*?[\"'\\" and w[:1] != ""):
            # a glob is safe only behind a plain literal start (/var/log/*.log):
            # *, ""* or '-'* could expand to a file named -delete
            return False
    return True


def _unquoted_glob(word: str) -> bool:
    """Whether a raw shell word has a glob character outside quotes."""
    quote, i = "", 0
    while i < len(word):
        c = word[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c in "*?[":
            return True
        i += 1
    return False


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
        if not _plain_words(line):
            return None
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
        return name in (SQL_PRAGMA_READ if m.group(2) else SQL_PRAGMA_BARE)
    if not SQL_READ.match(s) or SQL_WRITE.search(s):
        return False
    if safe:
        return True
    return _sql_calls_ok(s)


def _sql_calls_ok(stmt: str) -> bool:
    """Every parenthesis in a statement is grouping or a call of an
    allowlisted function. Only single-quoted literals are allowed, so they
    pair up unambiguously: any double quote, bracket or backtick (a quoted
    identifier, which could hide a quote inside it: vote olive-flipper-birch)
    and any comment (``--``, ``/*``: vote steep-garlic-comic) is never
    read-only. Literals are removed before the parentheses are checked."""
    if re.search(r'["`\[\]]', stmt):
        return False
    text = re.sub(r"'(?:[^']|'')*'", "''", stmt)          # string literals
    if "'" in text.replace("''", "") or "--" in text or "/*" in text:
        return False
    for m in re.finditer(r"\(", text):
        before = text[:m.start()].rstrip()
        if not before:
            continue
        last = before[-1]
        if last in "\"]`'":
            return False
        if last.isalnum() or last == "_":
            name = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before)
            if name is None or name.group(1).lower() not in SQL_FUNCS:
                return False
    return True


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
    words = [a for a in rest if not a.startswith("-") or a == "-"]   # "-" is stdin/stdout
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
        lead = SUB_GLOBAL.get(prog, set())
        i = 0
        while i < len(rest) and rest[i] in lead:
            i += 1
        if i == len(rest):
            return prog in READ_BARE
        return not rest[i].startswith("-") and rest[i] in READ_SUBS[prog]
    if prog == "pacman":
        first = rest[0] if rest else ""
        return (first.startswith("-Q") or first in ("-Ss", "-Si")) and all(
            not a.startswith("-") for a in rest[1:])
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
        if prog in ("grep", "egrep", "fgrep", "rg"):
            return {"kind": "read", "title": "Search files", "want": "search files for text"}
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
    if prog in ("curl", "wget"):
        # Even a GET can change things on the server it reaches (an admin URL),
        # and curl's -w can write a file: never "read-only".
        return {"kind": "change", "title": "Send a web request",
                "want": "send a web request (it can change things on the server, or save a file)"}
    if prog in ("cp", "mv", "tee", "touch", "mkdir", "ln", "chmod", "chown", "install",
                "rsync", "scp", "tar", "unzip", "sed", "awk", "patch"):
        return {"kind": "change", "title": "Change files", "want": f"run {prog}, which can change things"}
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
