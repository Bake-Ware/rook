"""Coding sessions a role starts and follows (docs/design/huddles.md 4.5, 5.5).

A role with ``routine`` (or ``act``) autonomy starts a Claude Code session
without a card: ``start_session(location, branch, instructions)``. The hub
keeps the rules, not the role:

* **Where.** ``location`` is one of ``role.code_locations`` (its worker, its
  main checkout and where worktrees go, parsed by :func:`locations`); the
  session runs in a git worktree of that checkout under the worktrees folder,
  never in the main checkout.
* **Which branch.** A new or existing branch that is not the base branch
  (``master``/``main``) or ``HEAD``; the worktree must be on it.
* **What it may do.** Claude Code runs headless (``claude -p``, stream-json)
  with none of the host's own settings, plugins or MCP servers, in
  ``dontAsk`` mode with an allowlist (:func:`session_settings`): edits inside
  the worktree, reading the role's workspace, git for its own branch (push
  only that branch), ``gh pr create``/``view``/``list``/``diff``/``checks``
  and the test runner. Merging, pushing anything else, ``gh api`` and the
  rest are denied, so the session can open a pull request but never merge
  one.

The hub follows each session itself (:class:`Follow`, no watcher job): its
``ROOK-STATUS:`` lines, its result, tool calls it was refused, its exit and
long silences. A status line is posted quietly in the huddle the role started
it from; ``DONE``, ``BLOCKED``, ``QUESTION`` and anything unexpected (it ended
without a status, failed, was refused tools, went quiet, its terminal
vanished) are posted there and wake the role, through the chair.
"""
from __future__ import annotations

import base64
import json
import posixpath
import re
import time
import uuid
import zlib
from typing import Any

#: A branch a role may work on: letters, digits and ._/- (no "..", no leading
#: "-" or "/"), not the base branch.
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")
PROTECTED = ("master", "main", "head", "trunk", "production", "release")
PATH = re.compile(r"(?<![\w.~-])(~?/[^\s,;:()'\"`]+)")
WORKER = re.compile(r"\bworker\s+([A-Za-z0-9][A-Za-z0-9._-]{0,63})", re.I)
BASE = re.compile(r"\b(origin/[A-Za-z0-9._/-]+)")
INSTRUCTIONS_MAX = 20000
STATUS = re.compile(r"^\s*ROOK-STATUS:\s*([A-Z]+)\b[:\s-]*(.*)$", re.M)
EXIT = re.compile(r"^ROOK-EXIT:\s*(-?\d+)\s*$", re.M)
PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
#: Status words a session may print, and what each does.
FINAL = {"DONE": "done", "BLOCKED": "blocked", "QUESTION": "blocked"}
WAKES = ("DONE", "BLOCKED", "QUESTION", "UNEXPECTED")

#: The push guard every session runs with (pre-push hook, through
#: ``core.hooksPath`` in the environment): a session pushes only its own
#: branch. The allowlist already refuses ``git -c`` and ``--no-verify``, and
#: GitHub's branch protection refuses ``master`` for every credential.
GUARD = r"""guard="${XDG_RUNTIME_DIR:-$HOME/.cache}/rook-session-guard/$sid"
mkdir -p "$guard" || { say "BLOCKED could not make the push guard"; printf 'ROOK-EXIT: 3\n'; exit 3; }
cat > "$guard/pre-push" <<'ROOKHOOK'
#!/bin/sh
# Rook session push guard: this session pushes only its own branch.
want="refs/heads/$ROOK_SESSION_BRANCH"
while read -r lref lsha rref rsha; do
  if [ "$rref" != "$want" ]; then
    echo "rook: refused: this session may push only $want, not $rref" >&2
    exit 1
  fi
done
exit 0
ROOKHOOK
chmod 755 "$guard/pre-push"
export ROOK_SESSION_BRANCH="$branch" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath \
  GIT_CONFIG_VALUE_0="$guard"
"""

LAUNCH = r"""set -u
repo=$1 wt=$2 branch=$3 base=$4 sid=$5 resume=$6 model=$7
say() { printf 'ROOK-STATUS: %s\n' "$*"; }
export PATH="$HOME/.local/bin:$PATH"
claude=$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")
[ -x "$claude" ] || { say "BLOCKED Claude Code is not installed on this host"; printf 'ROOK-EXIT: 3\n'; exit 3; }
if [ ! -d "$wt" ]; then
  git -C "$repo" fetch -q origin || say "WORKING could not fetch origin; using the local refs"
  if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$repo" worktree add -q "$wt" "$branch"
  elif git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$repo" worktree add -q --track -b "$branch" "$wt" "origin/$branch"
  else
    git -C "$repo" worktree add -q -b "$branch" "$wt" "$base"
  fi || { say "BLOCKED could not make a worktree for $branch at $wt"; printf 'ROOK-EXIT: 3\n'; exit 3; }
fi
cd "$wt" || { say "BLOCKED no worktree at $wt"; printf 'ROOK-EXIT: 3\n'; exit 3; }
cur=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$cur" = "$branch" ] || { say "BLOCKED $wt is on $cur, not $branch"; printf 'ROOK-EXIT: 3\n'; exit 3; }
__GUARD__
if [ -n "$resume" ]; then set -- --resume "$resume"; else set -- --session-id "$sid"; fi
[ -n "$model" ] && set -- "$@" --model "$model"
set -- "$claude" -p --output-format stream-json --verbose \
  --setting-sources "" --strict-mcp-config --settings "$ROOK_SESSION_SETTINGS" \
  --permission-mode dontAsk "$@"
if [ -n "${ROOK_SESSION_GH_TOKEN:-}" ]; then
  # Isolated: the hub minted a token for this repository only. The session
  # runs without the host's own GitHub credentials (masked), with that token.
  command -v bwrap >/dev/null 2>&1 || { say "BLOCKED bwrap is needed to isolate the session"; printf 'ROOK-EXIT: 3\n'; exit 3; }
  mask=""
  for d in "$HOME/.config/gh" "$HOME/.ssh" "$HOME/.config/rook" "$HOME/.rook" "$HOME/.local/share/rook"; do
    [ -d "$d" ] && mask="$mask --tmpfs $d"
  done
  for f in "$HOME/.git-credentials" "$HOME/.netrc"; do
    [ -e "$f" ] && mask="$mask --ro-bind /dev/null $f"
  done
  export GH_TOKEN="$ROOK_SESSION_GH_TOKEN"
  unset GITHUB_TOKEN GH_ENTERPRISE_TOKEN ROOK_SESSION_GH_TOKEN
  export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1= \
    GIT_CONFIG_KEY_2=credential.helper \
    GIT_CONFIG_VALUE_2='!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
  # shellcheck disable=SC2086
  set -- bwrap --dev-bind / / --proc /proc --unshare-pid --die-with-parent $mask -- "$@"
fi
printf '%s' "$ROOK_SESSION_PROMPT" | "$@"
code=$?
printf 'ROOK-EXIT: %s\n' "$code"
exit $code
"""
LAUNCH = LAUNCH.replace("__GUARD__", GUARD.rstrip("\n"))


class SessionError(ValueError):
    pass


# -- where code lives ---------------------------------------------------------------------

def locations(lines: list[str]) -> list[dict]:
    """``role.code_locations`` lines as ``{name, worker, repo, worktrees,
    base, line}``. A line names the repository first (``rook (...)``), the
    worker (``worker cachyrig``), the main checkout (the first absolute
    path), where worktrees go (a path after the word "worktree", else
    ``<repo>/.claude/worktrees``) and the base (``origin/master``). Lines
    without a worker or a path are skipped (a person reads them; a session
    cannot start there)."""
    out = []
    for line in lines or []:
        line = str(line).strip()
        w = WORKER.search(line)
        paths = [p.rstrip("/.") for p in PATH.findall(line) if len(p) > 1]
        if not w or not paths:
            continue
        repo = paths[0]
        trees = ""
        low = line.lower()
        at = low.find("worktree")
        if at >= 0:
            later = [p for p in PATH.findall(line[at:]) if len(p) > 1]
            trees = later[0].rstrip("/.") if later else ""
        b = BASE.search(line)
        name = re.split(r"[\s(:,]", line, maxsplit=1)[0].strip() or posixpath.basename(repo)
        out.append({"name": name, "worker": w.group(1), "repo": repo,
                    "worktrees": trees or posixpath.join(repo, ".claude", "worktrees"),
                    "base": b.group(1) if b else "origin/master", "line": line})
    return out


def pick_location(locs: list[dict], ref: str) -> dict:
    """The code location ``ref`` names (its name, its folder, or blank when
    there is only one)."""
    if not locs:
        raise SessionError("no code locations are set on this hub (Settings, "
                           "role.code_locations), so a session has nowhere to start; say so")
    ref = str(ref or "").strip().rstrip("/")
    if not ref:
        if len(locs) == 1:
            return locs[0]
        raise SessionError("location: one of " + ", ".join(l["name"] for l in locs))
    for loc in locs:
        if ref.lower() == loc["name"].lower() or ref == loc["repo"] \
                or ref.startswith(loc["repo"] + "/"):
            return loc
    raise SessionError(f"{ref!r} is not a code location; use one of "
                       + ", ".join(f"{l['name']} ({l['repo']} on {l['worker']})" for l in locs))


REPO = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?(?=[\s,;:()'\"`]|$)", re.I)


def repo_of(loc: dict) -> str:
    """The GitHub ``owner/name`` a code location's line names, or ""."""
    m = REPO.search(str((loc or {}).get("line") or ""))
    return m.group(1) if m else ""


def check_branch(branch: str, base: str = "origin/master") -> str:
    b = str(branch or "").strip()
    if b.startswith("refs/heads/"):
        b = b[len("refs/heads/"):]
    if not BRANCH.match(b) or ".." in b or b.endswith((".lock", "/", ".")) or "//" in b \
            or "@{" in b:
        raise SessionError("branch: a git branch name (letters, digits, . _ / -), like "
                           "design-h6")
    base_name = base.split("/", 1)[-1].lower()
    if b.lower() in PROTECTED or b.lower() == base_name:
        raise SessionError(f"sessions never work on {b}: name a new branch (it becomes a pull "
                           f"request into {base_name})")
    return b


def worktree_path(loc: dict, branch: str) -> str:
    return posixpath.join(loc["worktrees"], branch.replace("/", "-"))


# -- what the session may do --------------------------------------------------------------

GIT_OK = ("status", "diff", "log", "show", "add", "commit", "fetch", "rev-parse", "branch",
          "restore", "rm", "mv", "ls-files", "grep", "stash", "rebase", "merge-base",
          "blame", "switch -c", "checkout -b", "clean")
GH_OK = ("pr create", "pr view", "pr list", "pr diff", "pr checks", "pr comment", "pr edit",
         "issue view", "issue list")
#: Shell commands besides git and gh: only ones that cannot write a file
#: (the Read, Glob and Grep tools do the rest).
READ_CMDS = ("ls", "pwd", "git --no-pager log")
TEST_CMDS = ("rook-pytest",)
DENY = ("Bash(gh pr merge:*)", "Bash(gh api:*)", "Bash(gh repo:*)", "Bash(gh release:*)",
        "Bash(gh workflow:*)", "Bash(gh secret:*)", "Bash(git push --force:*)",
        "Bash(git push -f:*)", "Bash(git reset --hard:*)", "Bash(sudo:*)", "Bash(ssh:*)",
        "Bash(scp:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(rook:*)", "WebFetch", "WebSearch")


def session_settings(worktree: str, branch: str, workspace: str = "") -> dict:
    """The ``--settings`` of a role's session: an allowlist, nothing else.
    ``//`` is an absolute path in Claude Code's permission rules."""
    allow = [f"Edit(/{worktree}/**)", f"Write(/{worktree}/**)", f"Read(/{worktree}/**)",
             "Glob", "Grep", "TodoWrite"]
    if workspace:
        allow.append(f"Read(/{workspace}/**)")
    allow += [f"Bash(git {c}:*)" for c in GIT_OK]
    # The forms sessions reach for first (proving run, 2026-10-21): a cd into
    # the worktree before a command, and git -C <worktree>. Each part of a
    # compound command is checked on its own, so cd itself must be allowed.
    allow += [f"Bash(cd {worktree}:*)"]
    allow += [f"Bash(git -C {worktree} {c}:*)" for c in GIT_OK]
    for how in ("git push origin {b}", "git push -u origin {b}", "git push --set-upstream origin {b}",
                "git push origin HEAD:{b}", "git push -u origin HEAD:{b}"):
        allow.append(f"Bash({how.format(b=branch)})")
    allow += [f"Bash(gh {c}:*)" for c in GH_OK]
    allow += [f"Bash({c}:*)" for c in READ_CMDS + TEST_CMDS]
    return {"permissions": {"allow": allow, "deny": list(DENY), "defaultMode": "dontAsk"},
            "includeCoAuthoredBy": True, "autoMemoryEnabled": False,
            "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}}


def session_prompt(role: dict, loc: dict, branch: str, worktree: str, instructions: str,
                   workspace: str = "", huddle: str = "") -> str:
    base = loc["base"].split("/", 1)[-1]
    lines = [
        f"You are a Claude Code session started by the Rook role {role.get('name') or role['id']} "
        f"(@{role['id']})" + (f" from the huddle {huddle}" if huddle else "") + ".",
        f"You work in the git worktree {worktree}, on branch {branch} of {loc['name']}. Stay on "
        f"that branch and inside that folder.",
        "Rules (the host enforces most of them; do not try to get around a refusal):",
        f"- Commit on {branch}, push only {branch} (git push -u origin {branch}), and open a pull "
        f"request into {base} with gh pr create. Never merge a pull request, never push to {base}, "
        "never deploy, never touch secrets.",
        "- Report as you go, each on a line of its own in your reply text:",
        "  ROOK-STATUS: WORKING <what you are doing now> (a few times at most)",
        "  ROOK-STATUS: DONE <one-line summary, with the pull request URL>",
        "  ROOK-STATUS: BLOCKED <why, and what you need> (when you cannot finish)",
        "  End with exactly one DONE or BLOCKED line.",
        "- Read and search files with the Read, Glob and Grep tools, and change them with Edit and "
        "Write; the shell is only for git, gh and the test runner (rook-pytest). Other shell "
        "commands are refused. Your working folder is already the worktree: run git and gh "
        "commands bare, one per call (git add ..., then git commit ...), without cd or git -C. "
        "Update the pull request's description with gh pr edit <n> --body-file <file>; delete a "
        "scratch file you made with git clean -f <file> (git rm for a tracked one).",
        "- When a command you need is refused, say BLOCKED with the command instead of working "
        "around it.",
    ]
    if workspace:
        lines.append(f"- The role's own working files (drafts, notes) are in {workspace}; read "
                     "them when the instructions name them. Do not write there.")
    lines += ["", "The instructions from the role:", instructions.strip()]
    return "\n".join(lines)


def launch_args(loc: dict, branch: str, worktree: str, sid: str, prompt: str, settings: dict,
                title: str, resume: str = "", model: str = "") -> dict:
    """``work.stream.open`` arguments that run the session in a terminal on the
    location's worker: a shell argv (no shell parsing of any value), the
    prompt and settings in the environment."""
    argv = ["/bin/sh", "-c", LAUNCH, "rook-session", loc["repo"], worktree, branch, loc["base"],
            sid, resume or "", model or ""]
    return {"harness": "shell", "argv": argv, "cwd": loc["repo"], "title": title[:150],
            "env": {"ROOK_SESSION_PROMPT": prompt,
                    "ROOK_SESSION_SETTINGS": json.dumps(settings, separators=(",", ":")),
                    "ROOK_SESSION": sid}}


# -- following a session ----------------------------------------------------------------

def decode(enc: str, data: str) -> bytes:
    if not data:
        return b""
    if enc == "t":
        return data.encode("utf-8")
    if enc == "b":
        return base64.b64decode(data)
    if enc == "z":
        return zlib.decompressobj().decompress(base64.b64decode(data), 8 * 1024 * 1024)
    raise ValueError(f"unknown terminal encoding {enc!r}")


class Follow:
    """What a session's terminal output says, folded line by line: Claude
    Code's stream-json events, plus the launcher's own ``ROOK-STATUS`` and
    ``ROOK-EXIT`` lines. :meth:`feed` returns the events worth reporting:
    ``(word, text)`` with word WORKING, DONE, BLOCKED, QUESTION or
    UNEXPECTED."""

    def __init__(self) -> None:
        self.buf = b""
        self.native_id = ""
        self.final = ""                 # DONE / BLOCKED / QUESTION seen
        self.final_text = ""
        self.result: dict | None = None
        self.exit: int | None = None
        self.pr_urls: list[str] = []
        self.denied: list[str] = []
        self.tools = 0
        self.texts: list[str] = []      # the newest assistant texts
        self.raw: list[str] = []        # the newest non-JSON lines (launcher, errors)
        self.cost = None

    def _pr(self, text: str) -> None:
        for u in PR.findall(text or ""):
            if u not in self.pr_urls:
                self.pr_urls.append(u)

    def _status(self, text: str) -> list[tuple[str, str]]:
        out = []
        for word, rest in STATUS.findall(text or ""):
            word = word.upper()
            rest = rest.strip()
            if word in FINAL:
                self.final, self.final_text = word, rest
            if word in FINAL or word == "WORKING":
                out.append((word, rest))
        return out

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        self.buf += chunk.replace(b"\r\n", b"\n")
        out: list[tuple[str, str]] = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if line:
                out += self.line(line)
        return out

    def line(self, line: str) -> list[tuple[str, str]]:
        try:
            ev = json.loads(line) if line.startswith("{") else None
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            self.raw = (self.raw + [line[:300]])[-12:]
            m = EXIT.match(line)
            if m:
                self.exit = int(m.group(1))
                return []
            self._pr(line)
            return self._status(line)
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.native_id = str(ev.get("session_id") or self.native_id)
            return []
        if kind == "assistant":
            out = []
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    text = str(block["text"])
                    self.texts = (self.texts + [text[-2000:]])[-6:]
                    self._pr(text)
                    out += self._status(text)
                elif block.get("type") == "tool_use":
                    self.tools += 1
            return out
        if kind == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)[:2000]
                    self._pr(text)
            return []
        if kind == "result":
            self.result = ev
            self.cost = ev.get("total_cost_usd")
            text = str(ev.get("result") or "")
            self._pr(text)
            for d in ev.get("permission_denials") or []:
                if isinstance(d, dict):
                    inp = d.get("tool_input") or {}
                    what = inp.get("command") or inp.get("file_path") or inp.get("url") or ""
                    self.denied.append(f"{d.get('tool_name')}: {str(what)[:160]}")
            return []
        return []

    # -- what happened, at the end -----------------------------------------------------
    def report(self, exit_code: Any = None) -> tuple[str, str]:
        """The one report a finished run owes: ``(word, text)``. Its DONE,
        BLOCKED or QUESTION line when it said one, with anything unexpected
        added (an error, a failed exit, refused tool calls); UNEXPECTED when
        it ended without saying either."""
        code = self.exit if self.exit is not None else exit_code
        problems = []
        if self.result is not None and self.result.get("is_error"):
            problems.append("Claude Code ended with an error: "
                            + str(self.result.get("result") or self.result.get("subtype"))[:300])
        if code not in (None, 0):
            problems.append(f"it exited with code {code}")
        if self.result is None and not self.final:
            tail = " | ".join(self.raw[-4:])
            problems.append("it ended without a result" + (f" ({tail})" if tail else ""))
        if self.denied:
            problems.append(f"{len(self.denied)} tool call(s) were refused: "
                            + "; ".join(self.denied[:5]))
        if self.final:
            text = (self.final_text or "(no summary)").rstrip(" .")
            if problems:
                text += ". Unexpected: " + "; ".join(problems)
            return self.final, text
        last = self.texts[-1] if self.texts else str((self.result or {}).get("result") or "")
        problems.append("it never said DONE or BLOCKED; its last words: "
                        + (last.strip()[-600:] or "(none)"))
        return "UNEXPECTED", "; ".join(problems)


def new_id() -> str:
    return "s_" + uuid.uuid4().hex[:12]


def new_native_id() -> str:
    return str(uuid.uuid4())


class SessionStore:
    """``role_sessions`` in the roles plugin's database."""

    COLS = ("id", "role", "room", "worker", "terminal", "native_id", "repo", "branch",
            "worktree", "title", "instructions", "state", "status", "pr_url", "cursor",
            "started", "updated", "ended", "last_output", "runs", "data")

    def __init__(self, db, lock) -> None:
        self.db = db
        self._lock = lock

    def add(self, rec: dict) -> dict:
        now = time.time()
        row = {c: rec.get(c) for c in self.COLS}
        row.update(state=rec.get("state") or "starting", cursor=int(rec.get("cursor") or 0),
                   started=now, updated=now, runs=int(rec.get("runs") or 1),
                   data=json.dumps(rec.get("data") or {}))
        with self._lock, self.db:
            self.db.execute(f"INSERT INTO role_sessions({','.join(self.COLS)}) VALUES "
                            f"({','.join('?' * len(self.COLS))})", [row[c] for c in self.COLS])
        return self.get(row["id"])

    def set(self, sid: str, **fields) -> None:
        if not fields:
            return
        if "data" in fields and not isinstance(fields["data"], str):
            fields["data"] = json.dumps(fields["data"], default=str)
        fields["updated"] = time.time()
        bad = set(fields) - set(self.COLS)
        if bad:
            raise KeyError(f"unknown session fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self.db:
            self.db.execute(f"UPDATE role_sessions SET {sets} WHERE id=?", [*fields.values(), sid])

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except ValueError:
            d["data"] = {}
        return d

    def get(self, sid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM role_sessions WHERE id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def list(self, role: str = "", live: bool = False, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM role_sessions", []
        where = []
        if role:
            where.append("role=?")
            args.append(role)
        if live:
            where.append("state IN ('starting','running')")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY started DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [self._row(r) for r in self.db.execute(q, args)]

    def by_branch(self, role: str, worker: str, branch: str) -> dict | None:
        r = self.db.execute("SELECT * FROM role_sessions WHERE role=? AND worker=? AND branch=? "
                            "ORDER BY started DESC LIMIT 1", (role, worker, branch)).fetchone()
        return self._row(r) if r else None

    def live_on(self, worker: str, worktree: str) -> dict | None:
        r = self.db.execute("SELECT * FROM role_sessions WHERE worker=? AND worktree=? AND "
                            "state IN ('starting','running') LIMIT 1", (worker, worktree)).fetchone()
        return self._row(r) if r else None
