"""The conference table and votes (docs/design/huddles.md 10; house-agent
vision section 4; overnight phase B4).

**The table.** In a chaired huddle the house agent (the chair) or the person
pins artifacts: a pull request, a commit, a file at a commit, test results,
a document, a screenshot or preview build (a link). Each is pinned to an
exact version: a commit SHA, or the SHA-256 of its content. Every role in
the huddle gets real read access to what is pinned, at the pinned version,
through the hub (``table_read``: the diff, a file at the commit, CI status,
the PR's comments), so a reviewer reads the commit under review, never its
own worktree.

**Votes.** The chair calls a vote on one action of one pinned artifact:
``merge``, ``deploy``, ``qa`` (ready for QA) or ``ship`` (to users). The
hub decides with the trust map whether a vote is needed (a ship-it target
may skip it; anything else votes; a sacred one never runs without the
person). Hard gates run first and are not voted on: the version is still
the head, CI is green, a test run passed, the diff has no secret in it, the
pull request merges cleanly, and (deploys) the rollback dry run passed.
Then the required voters (data: ``huddle.vote_voters``; merge = lead, QA,
security, critic) each vote in a turn of their own, **blind**: a ballot is
committed (its hash is posted) before anyone sees another, and revealed when
all are in. A ballot binds to the version it read; a new commit voids the
vote. "Couldn't verify" is a no, a yes needs evidence and a read of the
pinned version, the critic names a risk, and authors and the proposer never
vote on their own work. Any required voter's no blocks: the chair gets one
resolution round (one re-vote), then the person gets a one-line Yes / No /
Explain ask with each side in two lines (the house agent's asks, so the
phone's decision card and the relayed-approvals list carry it).

**Execution.** A passed vote (all gates, every required voter yes) is
carried out by the hub itself, with a credential no session has (the vault
secret ``huddle.vote_github_secret``): GitHub merges only at the voted SHA.
Deploys run the target's runbook call as the house agent. The trust ramp
(``huddle.vote_auto``) says which kinds go on their own after a pass
(merge, qa); the rest ask the person. Every execution is journaled with
the vote, the versions and the gates, and lands in the house agent's digest
as "do it, then tell".
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

from ....band_mcp.secret_mask import scrub
from ....core import names
from ....core.text import clip

log = logging.getLogger("rook.hub.plugins.huddle.table")

API = "https://api.github.com"
ARTIFACT_KINDS = ("pr", "commit", "file", "doc", "tests", "link")
MOTION_KINDS = ("merge", "deploy", "qa", "ship")
VOTES = ("yes", "no", "cant_verify")
#: Required voters per action (``huddle.vote_voters``). A seat ``a|b`` is
#: filled by the first of those roles that exists, is on and may vote.
VOTERS = {"merge": ["lead", "qa", "security", "critic|skeptic"],
          "qa": ["lead", "qa", "critic|skeptic"],
          "deploy": ["release|devops|sre", "qa", "security", "critic|skeptic"],
          "ship": ["release|devops|sre", "qa", "security", "critic|skeptic", "product"]}
#: Who stands in when a seat's role may not vote (it wrote the work).
ALTERNATES = {"lead": ["architect"]}
#: Kinds a passed vote carries out on its own (the trust ramp); the rest ask.
AUTO = ["merge", "qa"]
#: Seats whose voter must name a specific risk (or say why there is none).
CRITIC = ("critic", "skeptic")
GATES = {"merge": ("version", "mergeable", "ci", "tests", "secrets"),
         "qa": ("version", "ci", "tests", "secrets"),
         "deploy": ("version", "ci", "tests", "secrets", "rollback"),
         "ship": ("version", "ci", "tests", "secrets", "rollback")}
OPEN = ("gating", "voting", "resolving", "executing")
READ_CHARS = 12000
DOC_MAX = 200_000
LINK_MAX = 25 * 1024 * 1024
HTTP_TIMEOUT = 20.0
POLL_S = 20.0
#: Things that look like credentials, besides the vault's own values.
SECRET_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|xox[abposr]-[A-Za-z0-9-]{10,}|"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35})")
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


class TableError(ValueError):
    """Refused; the text says why and what to do instead."""


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def _now() -> float:
    return time.time()


def _line(text: Any, limit: int) -> str:
    return clip(" ".join(str(text or "").split()), limit)


def short(sha: str) -> str:
    return str(sha or "")[:7]


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# -- GitHub, read and (with the hub's credential) merge ---------------------------------

def _http(method: str, url: str, headers: dict, body: dict | None = None,
          raw: bool = False) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            text = r.read(LINK_MAX + 1)
    except urllib.error.HTTPError as e:
        msg = ""
        with contextlib.suppress(Exception):
            msg = json.loads(e.read().decode() or "{}").get("message") or ""
        raise GitHubError(e.code, msg or e.reason) from None
    except urllib.error.URLError as e:
        raise GitHubError(0, str(e.reason)) from None
    if raw:
        return text
    return json.loads(text.decode() or "null")


class GitHub:
    """A small GitHub REST client. ``token()`` gives the hub's credential (or
    None: anonymous reads). Roles never see it: they read through
    :meth:`Table.read`, and only the hub merges."""

    def __init__(self, token: Callable[[], str | None], http: Callable[..., Any] = _http,
                 api: str = API) -> None:
        self._token = token
        self._http = http
        self.api = api.rstrip("/")

    def _headers(self, accept: str = "application/vnd.github+json", auth: bool = True) -> dict:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "rook-hub"}
        tok = self._token() if auth else None
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def get(self, path: str, accept: str = "application/vnd.github+json",
            raw: bool = False) -> Any:
        return self._http("GET", self.api + path, self._headers(accept), None, raw)

    def write(self, method: str, path: str, body: dict) -> Any:
        if not self._token():
            raise GitHubError(0, "no hub GitHub credential (Settings > Huddles > "
                                 "vote_github_secret names the vault secret)")
        return self._http(method, self.api + path, self._headers(), body, False)

    def pr(self, repo: str, number: int) -> dict:
        p = self.get(f"/repos/{repo}/pulls/{int(number)}")
        return {"number": int(p["number"]), "title": p.get("title") or "",
                "body": p.get("body") or "", "state": p.get("state"),
                "merged": bool(p.get("merged")), "draft": bool(p.get("draft")),
                "mergeable": p.get("mergeable"), "mergeable_state": p.get("mergeable_state"),
                "head": p["head"]["sha"], "head_ref": p["head"].get("ref") or "",
                "base": p["base"].get("ref") or "", "base_sha": p["base"].get("sha") or "",
                "author": (p.get("user") or {}).get("login") or "",
                "url": p.get("html_url") or ""}

    def commit(self, repo: str, ref: str) -> dict:
        c = self.get(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
        return {"sha": c["sha"], "message": (c.get("commit") or {}).get("message") or "",
                "author": ((c.get("author") or {}).get("login")
                           or ((c.get("commit") or {}).get("author") or {}).get("name") or ""),
                "parents": [p["sha"] for p in c.get("parents") or []],
                "files": [{"path": f.get("filename"), "status": f.get("status"),
                           "additions": f.get("additions"), "deletions": f.get("deletions")}
                          for f in c.get("files") or []]}

    def diff(self, repo: str, base: str, head: str) -> str:
        raw = self.get(f"/repos/{repo}/compare/{base}...{head}",
                       accept="application/vnd.github.diff", raw=True)
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    def files(self, repo: str, base: str, head: str) -> list[dict]:
        c = self.get(f"/repos/{repo}/compare/{base}...{head}")
        return [{"path": f.get("filename"), "status": f.get("status"),
                 "additions": f.get("additions"), "deletions": f.get("deletions")}
                for f in c.get("files") or []]

    def file(self, repo: str, path: str, sha: str) -> str:
        raw = self.get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}",
                       accept="application/vnd.github.raw", raw=True)
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    def checks(self, repo: str, sha: str) -> dict:
        runs = self.get(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
        status = self.get(f"/repos/{repo}/commits/{sha}/status")
        return {"runs": [{"name": r.get("name"), "status": r.get("status"),
                          "conclusion": r.get("conclusion")}
                         for r in (runs or {}).get("check_runs") or []],
                "statuses": [{"context": s.get("context"), "state": s.get("state")}
                             for s in (status or {}).get("statuses") or []],
                "state": (status or {}).get("state")}

    def comments(self, repo: str, number: int) -> list[dict]:
        out = []
        for c in self.get(f"/repos/{repo}/issues/{int(number)}/comments?per_page=50") or []:
            out.append({"by": (c.get("user") or {}).get("login"), "kind": "comment",
                        "text": c.get("body") or "", "at": c.get("created_at")})
        for r in self.get(f"/repos/{repo}/pulls/{int(number)}/reviews?per_page=50") or []:
            if r.get("body") or r.get("state") not in ("COMMENTED",):
                out.append({"by": (r.get("user") or {}).get("login"), "kind": "review",
                            "state": r.get("state"), "text": r.get("body") or "",
                            "commit": r.get("commit_id"), "at": r.get("submitted_at")})
        for c in self.get(f"/repos/{repo}/pulls/{int(number)}/comments?per_page=50") or []:
            out.append({"by": (c.get("user") or {}).get("login"), "kind": "line",
                        "path": c.get("path"), "text": c.get("body") or "",
                        "commit": c.get("commit_id"), "at": c.get("created_at")})
        return out

    def merge(self, repo: str, number: int, sha: str, method: str, title: str) -> dict:
        return self.write("PUT", f"/repos/{repo}/pulls/{int(number)}/merge",
                          {"sha": sha, "merge_method": method, "commit_title": title})

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.write("POST", f"/repos/{repo}/issues/{int(number)}/comments", {"body": body})

    def label(self, repo: str, number: int, labels: list[str]) -> Any:
        return self.write("POST", f"/repos/{repo}/issues/{int(number)}/labels",
                          {"labels": labels})


def secret_hits(text: str) -> list[str]:
    """Credential-looking strings and vault values on the added lines of a
    diff (or in a document): where, never the value."""
    hits = []
    for i, line in enumerate(str(text or "").splitlines(), 1):
        if line.startswith("+++") or (line.startswith(("-", " ", "@@", "diff ", "index "))):
            continue
        body = line[1:] if line.startswith("+") else line
        m = SECRET_SHAPES.search(body)
        if m:
            hits.append(f"line {i}: looks like a credential ({m.group(0)[:6]}...)")
        elif body and scrub(body) != body:
            hits.append(f"line {i}: contains a value from the band's vault")
    return hits[:20]


def _norm_repo(text: Any) -> str:
    from ...trust import norm_repo
    s = norm_repo(str(text or ""))
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", s):
        raise TableError(f"repo {text!r}: owner/name, e.g. Bake-Ware/rook")
    return s


# -- the table ---------------------------------------------------------------------------

class Table:
    """Artifacts, motions and ballots for :class:`.service.HuddleService`
    (tables in chat.db: migrations/003_table.sql)."""

    def __init__(self, svc: Any, node: Callable[[], Any], settings: Callable[[], dict],
                 http: Callable[..., Any] | None = None) -> None:
        self.svc = svc
        self._node = node
        self._settings = settings
        self.github = GitHub(self._token, http or _http)
        self.fetch: Callable[[str], bytes] = self._fetch_url

    # -- plumbing ----------------------------------------------------------------------
    @property
    def store(self):
        return self.svc.store

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        return self.store._q(sql, args)

    def _x(self, sql: str, args: tuple = ()) -> int:
        return self.store._x(sql, args)

    def setting(self, key: str, default: Any) -> Any:
        v = (self._settings() or {}).get(key)
        return default if v is None or v == "" and not isinstance(default, str) else v

    def _plugin(self, ns: str):
        node = self._node()
        return node.plugin(ns) if node is not None else None

    @property
    def chair(self):
        return self.svc.chair

    def _token(self) -> str | None:
        name = str(self.setting("vote_github_secret", "") or "").strip()
        if not name:
            return None
        vault = getattr(self._node(), "_vault", None)
        if vault is None:
            return None
        try:
            return vault.get(name, "system:huddle-table", via="hub merge credential")
        except KeyError:
            log.warning("table: no vault secret %r (huddle.vote_github_secret)", name)
            return None

    def _fetch_url(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "rook-hub"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = r.read(LINK_MAX + 1)
        if len(data) > LINK_MAX:
            raise TableError(f"{url} is larger than {LINK_MAX // (1024 * 1024)} MB")
        return data

    def _post(self, room: str, text: str, quiet: bool = False) -> dict:
        return self.svc._send(room, self.chair.chair_identity(), clip(text, 7900), [],
                              quiet=quiet)

    def _journal(self, cap: str, args: dict, reply: dict, room: str,
                 audit: dict | None = None) -> None:
        self.chair.journal(cap, args, reply, room, self.chair.chair_identity(), audit)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.chair.tasks.add(task)
        task.add_done_callback(self.chair.tasks.discard)
        return task

    async def _gh(self, fn: Callable, *args) -> Any:
        return await asyncio.to_thread(fn, *args)

    def resume(self) -> None:
        """Votes cut off by a restart end as failed (nothing reruns: a new
        vote starts clean, with fresh gates and fresh ballots)."""
        for r in self._q("SELECT id, room FROM huddle_motions WHERE state IN "
                         f"({','.join('?' * len(OPEN))})", OPEN):
            self._set(r["id"], state="failed", ended=_now(),
                      result={"error": "the hub restarted mid-vote; call it again"})

    # -- rows ------------------------------------------------------------------------------
    @staticmethod
    def _art(r: dict) -> dict:
        r = dict(r)
        r["ref"] = json.loads(r.get("ref") or "{}")
        r.pop("content", None)
        return r

    def artifact(self, ref: str, room: str = "") -> dict:
        ref = str(ref or "").strip()
        rows = self._q("SELECT * FROM huddle_artifacts WHERE id=?", (ref,))
        if not rows:
            with self.store._lock:
                ids = names.find(self.store._db, "artifact", ref)
            rows = self._q("SELECT * FROM huddle_artifacts WHERE id=?", (ids[0],)) \
                if len(ids) == 1 else []
        n = ref[1:] if ref[:1] in ("A", "a") else ref
        if not rows and room and n.isdigit() and 0 < int(n) < 1000:
            # "A3": the third thing pinned in this huddle
            rows = self._q("SELECT * FROM huddle_artifacts WHERE room=? ORDER BY pinned "
                           "LIMIT 1 OFFSET ?", (room, int(n) - 1))
        if not rows or (room and rows[0]["room"] != room):
            raise TableError(f"no artifact {ref!r} on this huddle's table (table lists them)")
        out = self._art(rows[0])
        out["name"] = self.store.name("artifact", out["id"])
        return out

    def _content(self, aid: str) -> str:
        rows = self._q("SELECT content FROM huddle_artifacts WHERE id=?", (aid,))
        return (rows[0]["content"] or "") if rows else ""

    def artifacts(self, room: str, all_: bool = False) -> list[dict]:
        rows = self._q("SELECT * FROM huddle_artifacts WHERE room=? ORDER BY pinned", (room,))
        out = []
        for i, r in enumerate(rows, 1):
            a = self._art(r)
            a["n"] = i
            a["name"] = self.store.name("artifact", a["id"])
            if all_ or a["state"] == "pinned":
                out.append(a)
        return out

    @staticmethod
    def _mo(r: dict) -> dict:
        r = dict(r)
        for k, d in (("target", "{}"), ("seats", "[]"), ("excluded", "[]"), ("decision", "{}"),
                     ("gates", "[]"), ("result", "{}")):
            r[k] = json.loads(r.get(k) or d)
        return r

    def motion(self, ref: str, room: str = "") -> dict:
        ref = str(ref or "").strip()
        rows = self._q("SELECT * FROM huddle_motions WHERE id=?", (ref,))
        if not rows:
            with self.store._lock:
                ids = names.find(self.store._db, "motion", ref)
            rows = self._q("SELECT * FROM huddle_motions WHERE id=?", (ids[0],)) \
                if len(ids) == 1 else []
        if not rows or (room and rows[0]["room"] != room):
            raise TableError(f"no vote {ref!r}")
        m = self._mo(rows[0])
        m["name"] = self.store.name("motion", m["id"])
        return m

    def motions(self, room: str) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM huddle_motions WHERE room=? ORDER BY created", (room,)):
            m = self._mo(r)
            m["name"] = self.store.name("motion", m["id"])
            out.append(m)
        return out

    def _set(self, mid: str, **fields) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
        sets.append("updated=?")
        vals.append(_now())
        self._x(f"UPDATE huddle_motions SET {', '.join(sets)} WHERE id=?", (*vals, mid))

    def ballots(self, mid: str, rnd: int | None = None, reveal: bool = False) -> list[dict]:
        """A motion's ballots. Sealed ones (not yet revealed) show only who
        voted and the commitment, unless ``reveal`` (the hub's own tally)."""
        sql, args = "SELECT * FROM huddle_ballots WHERE motion=?", [mid]
        if rnd is not None:
            sql += " AND round=?"
            args.append(int(rnd))
        out = []
        for r in self._q(sql + " ORDER BY cast_at", tuple(args)):
            b = dict(r)
            b["evidence"] = json.loads(b.get("evidence") or "[]")
            b["reads"] = json.loads(b.get("reads") or "[]")
            if not (reveal or b["revealed"]):
                b = {k: b[k] for k in ("motion", "round", "voter", "seat", "commitment", "cast_at")}
                b["sealed"] = True
            out.append(b)
        return out

    def view(self, room: str) -> dict:
        """The table for a huddle's readers: artifacts, and votes with sealed
        ballots hidden until every required voter has voted."""
        motions = []
        for m in self.motions(room):
            m["ballots"] = self.ballots(m["id"])
            motions.append(m)
        return {"artifacts": self.artifacts(room, all_=True), "motions": motions}

    def summary_text(self, room: str) -> str:
        """The table, for the chair's and the roles' context."""
        arts = self.artifacts(room)
        if not arts:
            return ""
        lines = ["On the table (read with table_read; each is pinned to its version):"]
        for a in arts:
            lines.append(f"- A{a['n']} ({a['id']}) {a['kind']} {a['title']} @ "
                         f"{short(a['version']) if a['kind'] in ('pr', 'commit', 'file') else a['version'][:12]}")
        for m in self.motions(room):
            if m["state"] in OPEN or m["state"] in ("asked", "blocked"):
                lines.append(f"- vote {m['name'] or m['id']}: {m['kind']} of {m['artifact']} @ "
                             f"{short(m['version'])}: {m['state']}")
        return "\n".join(lines)

    # -- pinning ---------------------------------------------------------------------------
    async def pin(self, room: str, by: str, data: dict) -> dict:
        """Pin an artifact at its exact version. ``data``: kind (pr, commit,
        file, doc, tests, link), title?, note?, and per kind: repo + number
        (pr), repo + sha (commit), repo + path + sha|ref (file), text (doc,
        tests; tests may name repo + sha it ran on), url + sha256? (link)."""
        h = self.svc._chaired(room)
        room = h["room"]
        data = dict(data or {})
        kind = str(data.get("kind") or "").strip().lower()
        if not kind and data.get("url") and PR_URL.search(str(data["url"])):
            kind = "pr"
        if kind not in ARTIFACT_KINDS:
            raise TableError(f"kind: one of {', '.join(ARTIFACT_KINDS)}")
        title = _line(data.get("title"), 200)
        note = _line(data.get("note"), 500)
        ref: dict = {}
        content = None
        try:
            if kind == "pr":
                repo, number = data.get("repo"), data.get("number")
                m = PR_URL.search(str(data.get("url") or ""))
                if m:
                    repo, number = m.group(1), m.group(2)
                repo = _norm_repo(repo)
                if not str(number or "").isdigit():
                    raise TableError("number: the pull request's number")
                p = await self._gh(self.github.pr, repo, int(number))
                version = p["head"]
                ref = {"repo": repo, "number": p["number"], "base": p["base"],
                       "base_sha": p["base_sha"], "head_ref": p["head_ref"],
                       "author": p["author"], "url": p["url"]}
                title = title or f"{repo}#{p['number']}: {p['title']}"
            elif kind == "commit":
                repo = _norm_repo(data.get("repo"))
                c = await self._gh(self.github.commit, repo, str(data.get("sha") or "").strip()
                                   or "HEAD")
                version = c["sha"]
                ref = {"repo": repo, "sha": c["sha"], "parent": (c["parents"] or [""])[0],
                       "author": c["author"]}
                title = title or f"{repo}@{short(c['sha'])}: {_line(c['message'], 80)}"
            elif kind == "file":
                repo = _norm_repo(data.get("repo"))
                path = str(data.get("path") or "").strip().lstrip("/")
                if not path:
                    raise TableError("path: the file's path in the repository")
                c = await self._gh(self.github.commit, repo,
                                   str(data.get("sha") or data.get("ref") or "HEAD"))
                text = await self._gh(self.github.file, repo, path, c["sha"])
                version = c["sha"]
                ref = {"repo": repo, "path": path, "sha": c["sha"], "hash": sha256(text)}
                title = title or f"{repo}:{path}@{short(c['sha'])}"
            elif kind in ("doc", "tests"):
                content = str(data.get("text") or "")
                if not content.strip():
                    raise TableError("text: the document (or test output) to pin")
                if len(content) > DOC_MAX:
                    raise TableError(f"text is longer than {DOC_MAX} characters; pin a link")
                version = sha256(content)
                if data.get("repo"):
                    ref["repo"] = _norm_repo(data["repo"])
                if data.get("sha"):
                    ref["sha"] = str(data["sha"])
                title = title or ("Test results" if kind == "tests" else "Document")
            else:
                url = str(data.get("url") or "").strip()
                if not re.match(r"https?://", url):
                    raise TableError("url: an http(s) address (a screenshot, a preview build)")
                want = str(data.get("sha256") or "").strip().lower()
                try:
                    body = await asyncio.to_thread(self.fetch, url)
                    version = sha256(body)
                    ref = {"url": url, "bytes": len(body)}
                except TableError:
                    raise
                except Exception as e:  # noqa: BLE001 - say why
                    if not want:
                        raise TableError(f"could not fetch {url} to hash it ({e}); give sha256")
                    version, ref = want, {"url": url, "unchecked": True}
                if want and want != version:
                    raise TableError(f"{url} does not match sha256 {want[:12]}... (it is "
                                     f"{version[:12]}...)")
                title = title or url
        except GitHubError as e:
            raise TableError(str(e)) from None
        # The same thing pinned again at a new version replaces the old pin,
        # and voids any vote on the old version; at the same version it is
        # the pin already there.
        same = [a for a in self.artifacts(room) if a["kind"] == kind and self._same(a, ref)]
        for old in same:
            if old["version"] == version and (old["ref"] or {}).get("base") == ref.get("base"):
                return {"artifact": old["id"], "name": old["name"], "n": old["n"], "kind": kind,
                        "title": old["title"], "version": version, "ref": old["ref"],
                        "already": True}
        aid = "a_" + uuid.uuid4().hex[:10]
        now = _now()
        self._x("INSERT INTO huddle_artifacts(id, room, kind, title, ref, version, pinned_by, "
                "pinned, state, note, content) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, room, kind, title, json.dumps(ref), version, by, now, "pinned", note,
                 content))
        name = self.store.assign("artifact", aid)
        for old in same:
            self._x("UPDATE huddle_artifacts SET state='superseded', superseded_by=? WHERE id=?",
                    (aid, old["id"]))
            for m in self.motions(room):
                if m["artifact"] == old["id"] and m["state"] in OPEN + ("asked", "blocked"):
                    self._void(m, f"a new version was pinned ({short(version)})")
        n = len(self._q("SELECT id FROM huddle_artifacts WHERE room=?", (room,)))
        self._post(room, f"Pinned A{n} [{kind}] {title} @ "
                         f"{short(version) if kind in ('pr', 'commit', 'file') else version[:12]}"
                   + (f" ({note})" if note else ""), quiet=True)
        self._journal("huddle.pin", {"room": room, "kind": kind, "ref": ref},
                      {"ok": True, "result": {"artifact": aid, "version": version}}, room)
        return {"artifact": aid, "name": name, "n": n, "kind": kind, "title": title,
                "version": version, "ref": ref}

    @staticmethod
    def _same(a: dict, ref: dict) -> bool:
        r = a.get("ref") or {}
        if a["kind"] == "pr":
            return r.get("repo") == ref.get("repo") and r.get("number") == ref.get("number")
        if a["kind"] == "file":
            return r.get("repo") == ref.get("repo") and r.get("path") == ref.get("path")
        if a["kind"] == "link":
            return r.get("url") == ref.get("url")
        return False

    async def current_version(self, a: dict) -> str:
        """Where the artifact is now (a PR's head); "" when it cannot move."""
        if a["kind"] == "pr":
            p = await self._gh(self.github.pr, a["ref"]["repo"], a["ref"]["number"])
            return p["head"]
        return a["version"]

    # -- reading ---------------------------------------------------------------------------
    async def read(self, room: str, ref: str, what: str = "summary", path: str = "",
                   offset: int = 0, reader: str = "") -> dict:
        """What a role (or anyone in the huddle) reads, always at the pinned
        version: summary, diff, files, file (path), checks, comments,
        content. Long text is paged (``offset``)."""
        a = self.artifact(ref, room)
        what = str(what or "summary").strip().lower()
        ref_ = a["ref"]
        out: dict = {"artifact": a["id"], "kind": a["kind"], "title": a["title"],
                     "version": a["version"], "what": what}
        if a["state"] != "pinned":
            out["note"] = f"this pin is {a['state']} (a newer version is pinned)"
        text: str | None = None
        try:
            if a["kind"] in ("pr", "commit", "file"):
                repo = ref_["repo"]
                sha = a["version"]
                base = ref_.get("base_sha") or ref_.get("parent") or ""
                if what == "summary":
                    if a["kind"] == "pr":
                        p = await self._gh(self.github.pr, repo, ref_["number"])
                        files = await self._gh(self.github.files, repo, base, sha)
                        out.update(number=p["number"], pr_title=p["title"], author=p["author"],
                                   base=p["base"], state=p["state"], merged=p["merged"],
                                   body=clip(p["body"], 3000), files=files[:100],
                                   current_version=p["head"])
                        if p["head"] != sha:
                            out["stale"] = (f"the PR has moved on to {short(p['head'])}; this "
                                            f"pin (and any vote on it) is {short(sha)}")
                    elif a["kind"] == "commit":
                        c = await self._gh(self.github.commit, repo, sha)
                        out.update(message=c["message"], author=c["author"], files=c["files"])
                    else:
                        what = "file"
                if what == "diff":
                    if not base:
                        raise TableError("no base to diff against for this artifact")
                    text = await self._gh(self.github.diff, repo, base, sha)
                elif what == "files":
                    out["files"] = await self._gh(self.github.files, repo, base, sha)
                elif what == "file":
                    p = path or ref_.get("path") or ""
                    if not p:
                        raise TableError("path: which file (files lists them)")
                    text = await self._gh(self.github.file, repo, p, sha)
                    out["path"] = p
                elif what == "checks":
                    out["checks"] = await self._gh(self.github.checks, repo, sha)
                elif what == "comments":
                    if a["kind"] != "pr":
                        raise TableError("comments: only a pull request has them")
                    out["comments"] = [{**c, "text": clip(c["text"], 1500)} for c in
                                       await self._gh(self.github.comments, repo, ref_["number"])]
                elif what not in ("summary",):
                    raise TableError("what: summary, diff, files, file, checks or comments")
            elif a["kind"] in ("doc", "tests"):
                text = self._content(a["id"])
                if sha256(text) != a["version"]:
                    raise TableError("the stored document does not match its pinned hash")
            else:
                url = ref_["url"]
                out["url"] = url
                if what in ("content", "summary") and not ref_.get("unchecked"):
                    body = await asyncio.to_thread(self.fetch, url)
                    now = sha256(body)
                    if now != a["version"]:
                        out["stale"] = f"{url} has changed since it was pinned (now {now[:12]})"
                    elif what == "content":
                        text = body.decode("utf-8", "replace")
        except GitHubError as e:
            raise TableError(str(e)) from None
        if text is not None:
            offset = max(0, int(offset or 0))
            out["text"] = text[offset:offset + READ_CHARS]
            out["chars"] = len(text)
            if offset + READ_CHARS < len(text):
                out["next_offset"] = offset + READ_CHARS
        self._journal("huddle.table.read", {"room": room, "artifact": a["id"], "what": what,
                                            "reader": reader or None},
                      {"ok": True, "result": {"version": a["version"]}}, room)
        return out

    # -- votes -----------------------------------------------------------------------------
    def voters_config(self) -> dict:
        raw = self.setting("vote_voters", None)
        cfg = {k: list(v) for k, v in VOTERS.items()}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in MOTION_KINDS and isinstance(v, list):
                    cfg[k] = [str(x) for x in v if str(x).strip()]
        return cfg

    def _deploy_target(self, tid: str) -> dict:
        for t in self.setting("vote_deploy_targets", []) or []:
            if isinstance(t, dict) and str(t.get("id")) == tid:
                return t
        ids = [str(t.get("id")) for t in self.setting("vote_deploy_targets", []) or []
               if isinstance(t, dict)]
        raise TableError(f"no deploy target {tid!r} (huddle.vote_deploy_targets: "
                         f"{', '.join(ids) or 'none set'})")

    def authors(self, a: dict) -> list[dict]:
        """Roles that wrote the work: a role whose coding session made this
        branch or pull request, and whoever pinned it if a role did."""
        out: dict[str, str] = {}
        if str(a.get("pinned_by") or "").startswith("role:"):
            out[a["pinned_by"][5:]] = "pinned it"
        roles = self._plugin("role")
        ref = a.get("ref") or {}
        if roles is not None and a["kind"] == "pr":
            with contextlib.suppress(Exception):
                for s in roles.sessions.list(limit=500):
                    url = str(s.get("pr_url") or "")
                    m = PR_URL.search(url)
                    same_pr = m and _norm_repo(m.group(1)) == ref.get("repo") \
                        and int(m.group(2)) == int(ref.get("number") or 0)
                    same_branch = s.get("branch") and s.get("branch") == ref.get("head_ref")
                    if same_pr or same_branch:
                        out.setdefault(s["role"], f"its session {s['id']} wrote it")
        return [{"role": r, "why": w} for r, w in out.items()]

    def seats(self, kind: str, excluded: set[str]) -> list[dict]:
        roles = self._plugin("role")
        known: dict[str, dict] = {}
        if roles is not None:
            with contextlib.suppress(Exception):
                for r in roles.store.list():
                    doc = roles.store.get(r["id"])
                    if doc is not None and doc.get("enabled", True):
                        known[doc["id"]] = doc
        alts = self.setting("vote_alternates", None)
        alts = alts if isinstance(alts, dict) else ALTERNATES
        out, taken = [], set()
        for seat in self.voters_config().get(kind) or []:
            cands = [c.strip().lower() for c in str(seat).split("|") if c.strip()]
            pick, why = None, ""
            for c in cands:
                if c in known and c not in excluded and c not in taken:
                    pick = c
                    break
            if pick is None:
                for c in cands:
                    for alt in alts.get(c) or []:
                        if alt in known and alt not in excluded and alt not in taken:
                            pick, why = alt, f"stands in for {c}"
                            break
                    if pick:
                        break
            if pick is None:
                gone = [c for c in cands if c in excluded]
                why = (f"no eligible voter ({', '.join(gone)} wrote the work)" if gone else
                       f"no role {' or '.join(cands)} on this hub")
            else:
                taken.add(pick)
            out.append({"seat": seat, "role": pick, **({"note": why} if why else {})})
        return out

    def ruling(self, kind: str, a: dict, target: dict) -> dict:
        """What the trust map says, and what a pass does: ``vote`` (needed or
        may be skipped), ``mode`` auto | ask | person."""
        from ...trust import Ctx, rule_map
        node = self._node()
        from ..trust import node_store
        st = node_store(node)
        ref = a.get("ref") or {}
        ctx = Ctx(cap=f"huddle.vote.{kind}", repo=ref.get("repo") or "",
                  branch=ref.get("base") or "", worker=target.get("worker") or "")
        doc = st.current() if st is not None else {"version": 0, "entries": [],
                                                    "unlisted": "balanced"}
        r = rule_map(doc, ctx, "exec", (), False)
        # The level comes from the entries the action touches; an exception
        # changes rulings on calls, never whether a vote is needed or a
        # sacred target runs without the person.
        level = r.level
        prot = set()
        roles = self._plugin("role")
        if roles is not None:
            with contextlib.suppress(Exception):
                prot = {p.lower() for p in roles.protected()}
        if (target.get("worker") or "").lower() in prot:
            level = "sacred"
        auto = self.setting("vote_auto", AUTO)
        auto = auto if isinstance(auto, list) else AUTO
        if level == "sacred":
            mode, why = "person", "sacred: only the person says go"
        elif kind in auto:
            mode, why = "auto", f"{kind} goes after a passed vote (huddle.vote_auto)"
        else:
            mode, why = "ask", f"{kind} still asks the person after a passed vote (trust ramp)"
        return {"level": level, "vote": "optional" if level == "ship-it" else "required",
                "mode": mode, "why": why, "rule": r.rule, "cite": r.cite,
                "entry": (r.entry or {}).get("id") or "unlisted",
                "words": ((r.entry or {}).get("set_by") or {}).get("words") or "",
                **({"exception": r.exception["id"]} if r.exception else {})}

    async def call(self, room: str, by: str, data: dict) -> dict:
        """The chair (or a person) calls a vote: ``data`` {kind, artifact,
        reason?, target? (deploy/ship: a deploy target id), skip_vote?}."""
        h = self.svc._chaired(room)
        room = h["room"]
        data = dict(data or {})
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in MOTION_KINDS:
            raise TableError(f"kind: one of {', '.join(MOTION_KINDS)}")
        a = self.artifact(str(data.get("artifact") or ""), room)
        if a["state"] != "pinned":
            raise TableError(f"{a['id']} was superseded; vote on the newest pin")
        if kind in ("merge", "qa") and a["kind"] != "pr":
            raise TableError(f"a {kind} vote is on a pinned pull request")
        if kind in ("deploy", "ship") and a["kind"] not in ("pr", "commit"):
            raise TableError(f"a {kind} vote is on a pinned commit or pull request")
        target: dict = {}
        if kind in ("deploy", "ship"):
            t = self._deploy_target(str(data.get("target") or ""))
            target = {"id": t["id"], "title": t.get("title") or t["id"],
                      "worker": (t.get("deploy") or {}).get("worker") or t.get("worker") or ""}
        for m in self.motions(room):
            if m["artifact"] == a["id"] and m["kind"] == kind and \
                    m["state"] in OPEN + ("asked", "blocked"):
                raise TableError(f"vote {m['name'] or m['id']} on this is already {m['state']}")
        decision = self.ruling(kind, a, target)
        skip = bool(data.get("skip_vote"))
        if skip and decision["vote"] != "optional":
            raise TableError(f"a vote is required: {decision['entry']} is {decision['level']} "
                             f"({decision['cite']})")
        authors = self.authors(a)
        proposer_role = by[5:] if by.startswith("role:") else ""
        excluded = {x["role"] for x in authors} | ({proposer_role} if proposer_role else set())
        excl = authors + ([{"role": proposer_role, "why": "called the vote"}]
                          if proposer_role and proposer_role not in {x["role"] for x in authors}
                          else [])
        seats = [] if skip else self.seats(kind, excluded)
        mid = "v_" + uuid.uuid4().hex[:10]
        now = _now()
        self._x("INSERT INTO huddle_motions(id, room, kind, artifact, version, target, proposer, "
                "reason, seats, excluded, decision, gates, state, round, result, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, room, kind, a["id"], a["version"], json.dumps(target), by,
                 _line(data.get("reason"), 500), json.dumps(seats), json.dumps(excl),
                 json.dumps(decision), "[]", "gating", 1, "{}", now, now))
        name = self.store.assign("motion", mid)
        who = ", ".join(f"@{s['role']}" if s["role"] else f"({s['seat']}: none)" for s in seats)
        self._post(room, f"Vote {name}: {kind} {a['title']} @ {short(a['version'])}. "
                         f"Trust: {decision['entry']} ({decision['level']}); after a pass: "
                         f"{decision['mode']}. "
                         + ("No vote (ship-it); gates first." if skip else
                            f"Gates first, then a blind vote: {who}.")
                         + (f" Not voting: {', '.join(x['role'] + ' (' + x['why'] + ')' for x in excl)}."
                            if excl else ""))
        self._journal("huddle.vote", {"room": room, "action": "call", "kind": kind,
                                      "artifact": a["id"], "version": a["version"]},
                      {"ok": True, "result": {"motion": mid, "decision": decision}}, room)
        self._spawn(self._run(mid, skip))
        return {"motion": mid, "name": name, "kind": kind, "artifact": a["id"],
                "version": a["version"], "seats": seats, "excluded": excl,
                "decision": decision, "state": "gating"}

    async def _run(self, mid: str, skip: bool = False) -> None:
        try:
            m = self.motion(mid)
            gates = await self.gates(m)
            self._set(mid, gates=gates)
            failed = [g for g in gates if not g["ok"]]
            if failed:
                self._set(mid, state="gates_failed", ended=_now())
                self._post(m["room"], f"Vote {m['name']}: stopped at the gates (not voted on): "
                           + "; ".join(f"{g['id']}: {g['detail']}" for g in failed))
                return
            self._post(m["room"], f"Vote {m['name']}: gates passed ("
                       + ", ".join(g["id"] for g in gates) + ").", quiet=True)
            if skip:
                self._set(mid, state="passed")
                await self._passed(self.motion(mid))
                return
            await self._vote_round(mid)
        except Exception as e:  # noqa: BLE001 - a vote failing never breaks the huddle
            log.exception("table: vote %s failed", mid)
            with contextlib.suppress(Exception):
                self._set(mid, state="failed", result={"error": str(e)[:300]}, ended=_now())
                self._post(self.motion(mid)["room"], f"(Vote {mid} failed: {str(e)[:300]})")

    # -- gates -------------------------------------------------------------------------------
    async def gates(self, m: dict) -> list[dict]:
        a = self.artifact(m["artifact"])
        out = []
        for gid in GATES[m["kind"]]:
            try:
                ok, detail = await getattr(self, f"_gate_{gid}")(m, a)
            except (GitHubError, TableError) as e:
                ok, detail = False, str(e)[:300]
            out.append({"id": gid, "ok": bool(ok), "detail": detail, "at": _now()})
            if gid == "version" and not ok:
                break
        return out

    async def _gate_version(self, m: dict, a: dict) -> tuple[bool, str]:
        if a["kind"] != "pr":
            return True, f"pinned at {short(a['version'])}"
        p = await self._gh(self.github.pr, a["ref"]["repo"], a["ref"]["number"])
        if p["merged"] and m["kind"] in ("deploy", "ship"):
            return True, "merged"
        if p["state"] != "open":
            return False, f"the pull request is {p['state']}"
        if p["draft"] and m["kind"] == "merge":
            return False, "the pull request is a draft"
        if p["head"] != m["version"]:
            return False, f"the PR moved to {short(p['head'])}; pin it again and call a new vote"
        return True, f"head is {short(m['version'])}"

    async def _gate_mergeable(self, m: dict, a: dict) -> tuple[bool, str]:
        for _ in range(4):
            p = await self._gh(self.github.pr, a["ref"]["repo"], a["ref"]["number"])
            if p["mergeable"] is not None:
                break
            await asyncio.sleep(POLL_S / 4)
        if p["mergeable"] is False:
            return False, f"conflicts with {p['base']} ({p['mergeable_state']}); rebase it"
        if p["mergeable"] is None:
            return False, "GitHub has not worked out whether it merges cleanly yet"
        return True, f"merges cleanly into {p['base']}"

    async def _checks(self, m: dict, a: dict) -> dict:
        """The commit's checks, waiting while they run (``huddle.vote_gate_wait_minutes``)."""
        deadline = time.monotonic() + float(self.setting("vote_gate_wait_minutes", 20)) * 60
        while True:
            c = await self._gh(self.github.checks, a["ref"]["repo"], m["version"])
            running = [r for r in c["runs"] if r["status"] != "completed"] + \
                [s for s in c["statuses"] if s["state"] == "pending"]
            if not running or time.monotonic() >= deadline:
                return c
            await asyncio.sleep(POLL_S)

    async def _gate_ci(self, m: dict, a: dict) -> tuple[bool, str]:
        c = await self._checks(m, a)
        m.setdefault("_checks", c)
        if not c["runs"] and not c["statuses"]:
            if self.setting("vote_require_ci", True):
                return False, f"no CI ran on {short(m['version'])}"
            return True, "no CI on this repository (huddle.vote_require_ci is off)"
        bad = [f"{r['name']}: {r['conclusion'] or r['status']}" for r in c["runs"]
               if r["status"] != "completed" or r["conclusion"] not in ("success", "neutral",
                                                                         "skipped")]
        bad += [f"{s['context']}: {s['state']}" for s in c["statuses"] if s["state"] != "success"]
        if bad:
            return False, "CI is not green: " + "; ".join(bad[:6])
        return True, f"{len(c['runs']) + len(c['statuses'])} checks green"

    async def _gate_tests(self, m: dict, a: dict) -> tuple[bool, str]:
        c = m.get("_checks") or await self._checks(m, a)
        pats = self.setting("vote_test_checks", ["*test*"]) or ["*test*"]
        runs = [r for r in c["runs"] if any(fnmatch.fnmatch(str(r["name"]).lower(), p.lower())
                                            for p in pats)]
        if not runs:
            if not self.setting("vote_require_ci", True) and not c["runs"]:
                return True, "no test runs (huddle.vote_require_ci is off)"
            return False, f"no test run among the checks (huddle.vote_test_checks: {pats})"
        bad = [r["name"] for r in runs if r["conclusion"] != "success"]
        if bad:
            return False, "tests failed: " + ", ".join(bad)
        return True, ", ".join(r["name"] for r in runs) + " passed"

    async def _gate_secrets(self, m: dict, a: dict) -> tuple[bool, str]:
        ref = a["ref"]
        if a["kind"] == "pr":
            text = await self._gh(self.github.diff, ref["repo"], ref["base_sha"], m["version"])
        elif ref.get("parent"):
            text = await self._gh(self.github.diff, ref["repo"], ref["parent"], m["version"])
        else:
            return True, "no parent to diff against"
        hits = secret_hits(text)
        if hits:
            return False, "the diff has secrets in it: " + "; ".join(hits[:5])
        return True, "no secrets in the diff"

    async def _gate_rollback(self, m: dict, a: dict) -> tuple[bool, str]:
        t = self._deploy_target(m["target"].get("id") or "")
        dry = t.get("dry_run") or {}
        if not dry.get("cap"):
            return False, f"deploy target {t['id']} has no rollback dry run (dry_run)"
        out = await self._run_call(dry, m["version"], f"rollback dry run for vote {m['id']}")
        res = out.get("raw") if isinstance(out.get("raw"), dict) else {}
        code = res.get("code", res.get("exit_code", 0))
        if not out["ok"] or code not in (0, None):
            return False, f"the rollback dry run failed: {out.get('error') or 'exit ' + str(code)}"
        return True, f"rollback dry run passed ({dry.get('cap')} on {dry.get('worker')})"

    async def _run_call(self, spec: dict, sha: str, why: str, approved_by: str = "") -> dict:
        """A target's runbook call, as the house agent (the trust map rules on
        it like any call; ``approved_by`` approves its hold for this call)."""
        home = self._plugin("home")
        front = getattr(home, "front", None)
        if front is None:
            raise TableError("deploys run as the house agent: turn it on (Manage > Home agent)")

        def fill(v: Any) -> Any:
            if isinstance(v, str):
                return v.replace("{sha}", sha)
            if isinstance(v, list):
                return [fill(x) for x in v]
            if isinstance(v, dict):
                return {k: fill(x) for k, x in v.items()}
            return v
        w = front.resolve_worker(str(spec.get("worker") or ""), str(spec["cap"]))
        out = await front.run_action({"worker": w["name"], "wid": w["wid"], "cap": spec["cap"],
                                      "args": fill(spec.get("args") or {}), "summary": why},
                                     approved_by=approved_by)
        raw = {}
        with contextlib.suppress(Exception):
            raw = json.loads(out.get("result") or "{}")
        out["raw"] = raw if isinstance(raw, dict) else {}
        return out

    # -- a vote round ------------------------------------------------------------------------
    async def _vote_round(self, mid: str, note: str = "") -> None:
        m = self.motion(mid)
        self._set(mid, state="voting")
        seats = [s for s in m["seats"] if s.get("role")]
        await asyncio.gather(*(self._vote_turn(m, s, note) for s in seats))
        for s in m["seats"]:
            if not s.get("role"):
                self._cast(m, s, f"(none: {s['seat']})", {"vote": "cant_verify", "reasons":
                           s.get("note") or "no eligible voter for this seat"}, [], forced=True)
        await self._tally(mid)

    def _cast(self, m: dict, seat: dict, voter: str, ballot: dict, reads: list[dict],
              forced: bool = False) -> dict:
        """Record one sealed ballot. Returns what the voter is told."""
        vote = str(ballot.get("vote") or "").strip().lower().replace("'", "").replace(" ", "_")
        vote = {"couldnt_verify": "cant_verify", "cannot_verify": "cant_verify",
                "veto": "no", "approve": "yes"}.get(vote, vote)
        if vote not in VOTES:
            raise TableError("vote: yes, no or cant_verify")
        reasons = _line(ballot.get("reasons"), 1500)
        if not reasons:
            raise TableError("reasons: why, in a sentence or two")
        ev = ballot.get("evidence") or []
        ev = [ev] if isinstance(ev, str) else ev
        evidence = [_line(e, 500) for e in ev if str(e).strip()][:10]
        risk = _line(ballot.get("risk"), 600)
        notes = []
        cur = self.motion(m["id"])
        if cur["state"] not in ("voting",) and not forced:
            raise TableError(f"vote {cur['name'] or cur['id']} is {cur['state']}; no ballot taken")
        if not forced:
            read_pinned = [r for r in reads if r.get("version") == m["version"]]
            if vote == "yes" and not evidence:
                vote, _ = "cant_verify", notes.append("a yes needs evidence; counted as "
                                                      "couldn't verify")
            if vote == "yes" and not read_pinned:
                vote, _ = "cant_verify", notes.append(
                    f"no read of the pinned version {short(m['version'])} (table_read) this "
                    "turn; counted as couldn't verify")
            if any(c in str(seat.get("seat") or "") for c in CRITIC) and not risk:
                if vote == "yes":
                    vote = "cant_verify"
                notes.append("the critic names a specific risk (or why there is none)")
        salt = secrets.token_hex(8)
        body = json.dumps({"voter": voter, "vote": vote, "reasons": reasons,
                           "evidence": evidence, "risk": risk, "version": m["version"]},
                          sort_keys=True)
        commitment = sha256(salt + body)[:16]
        rnd = int(cur["round"])
        try:
            self._x("INSERT INTO huddle_ballots(motion, round, voter, seat, vote, reasons, "
                    "evidence, risk, version, reads, note, commitment, salt, cast_at) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m["id"], rnd, voter, seat["seat"], vote, reasons, json.dumps(evidence),
                     risk, m["version"], json.dumps(reads), "; ".join(notes), commitment, salt,
                     _now()))
        except Exception:  # noqa: BLE001 - the primary key: one ballot a round
            raise TableError("you already voted in this round") from None
        if not forced:
            self._post(m["room"], f"@{voter} has voted on {cur['name'] or m['id']} "
                                  f"(sealed {commitment}).", quiet=True)
        return {"ok": True, "sealed": commitment, "counted_as": vote,
                **({"note": "; ".join(notes)} if notes else {}),
                "next": "Your ballot is sealed until every required voter has voted. Answer "
                        "in a line now; do not vote again."}

    async def _vote_turn(self, m: dict, seat: dict, note: str = "") -> None:
        roles = self._plugin("role")
        role_id = seat["role"]
        reads: list[dict] = []
        cast: dict = {}
        a = self.artifact(m["artifact"])
        critic = any(c in str(seat["seat"]) for c in CRITIC)

        async def handler(role_doc: dict, name: str, args: dict) -> str:
            try:
                if name == "table_read":
                    out = await self.read(m["room"], str(args.get("artifact") or a["id"]),
                                          str(args.get("what") or "summary"),
                                          str(args.get("path") or ""),
                                          int(args.get("offset") or 0), f"role:{role_id}")
                    reads.append({"artifact": out["artifact"], "what": out["what"],
                                  "version": out["version"], "at": _now()})
                    return clip(json.dumps(out, default=str), 14000)
                if name == "vote":
                    if cast:
                        return json.dumps({"ok": False, "error": "you already voted"})
                    res = self._cast(m, seat, role_id, args, list(reads))
                    cast.update(res)
                    return json.dumps(res)
            except TableError as e:
                return json.dumps({"ok": False, "error": str(e)})
            return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

        ask = VOTE_ASK.format(
            kind=m["kind"], title=a["title"], version=m["version"], short=short(m["version"]),
            seat=seat["seat"], reason=m.get("reason") or "(none given)",
            what=KIND_WORDS[m["kind"]],
            critic=CRITIC_ASK if critic else "",
            note=f"\nThe chair, after the first vote: {note}" if note else "")
        context = self.summary_text(m["room"])
        try:
            await roles.turn(roles.role(role_id), ask, context, self.chair.chair_identity(),
                             extra_tools=[TABLE_READ, VOTE], extra_handler=handler,
                             guide=VOTE_GUIDE, via=f"vote-{role_id}", room=m["room"])
        except Exception as e:  # noqa: BLE001 - no ballot = couldn't verify
            log.warning("table: %s's vote turn on %s failed: %s", role_id, m["id"], e)
            note = f"the turn failed: {str(e)[:200]}"
        else:
            note = "it did not vote"
        if not cast:
            with contextlib.suppress(TableError):
                self._cast(m, seat, role_id, {"vote": "cant_verify", "reasons": note}, reads,
                           forced=True)

    async def _tally(self, mid: str) -> None:
        m = self.motion(mid)
        if m["state"] != "voting":
            return
        a = self.artifact(m["artifact"])
        try:
            now = await self.current_version(a)
        except (GitHubError, TableError) as e:
            now = ""
            log.warning("table: could not re-read %s: %s", a["id"], e)
        if now and now != m["version"]:
            self._void(m, f"a new commit landed ({short(now)}) while it was voted on")
            return
        rows = self.ballots(mid, m["round"], reveal=True)
        self._x("UPDATE huddle_ballots SET revealed=1 WHERE motion=? AND round=?",
                (mid, m["round"]))
        lines = [f"Votes on {m['name']} ({m['kind']} {a['title']} @ {short(m['version'])}), "
                 f"round {m['round']}, revealed:"]
        for b in rows:
            word = {"yes": "YES", "no": "NO", "cant_verify": "COULDN'T VERIFY (counts as no)"}[
                b["vote"]]
            line = f"- @{b['voter']} ({b['seat']}): {word}. {b['reasons']}"
            if b.get("risk"):
                line += f" Risk: {b['risk']}"
            if b["evidence"]:
                line += " Evidence: " + "; ".join(b["evidence"][:4])
            if b.get("note"):
                line += f" [hub: {b['note']}]"
            lines.append(line + f" (sealed {b['commitment']})")
        no = [b for b in rows if b["vote"] != "yes"]
        if not no and rows:
            lines.append("Unanimous: passed.")
            self._post(m["room"], "\n".join(lines))
            self._set(mid, state="passed")
            await self._passed(self.motion(mid))
            return
        lines.append("Blocked by " + ", ".join(f"@{b['voter']}" for b in no) + ".")
        self._post(m["room"], "\n".join(lines))
        if int(m["round"]) < 2:
            self._set(mid, state="resolving")
            await self._resolve(self.motion(mid), rows)
        else:
            await self._ask_person(self.motion(mid), rows, "the second vote was still blocked")

    def _void(self, m: dict, why: str) -> None:
        self._set(m["id"], state="void", result={**(m.get("result") or {}), "void": why},
                  ended=_now())
        with contextlib.suppress(Exception):
            if m.get("ask"):
                front = getattr(self._plugin("home"), "front", None)
                if front is not None:
                    front.store.close_ask(m["ask"], "cancelled", "system:huddle-table", "",
                                          f"vote void: {why}")
        self._post(m["room"], f"Vote {m.get('name') or m['id']} is void: {why}. Ballots bind to "
                              f"{short(m['version'])}; call a new vote on the new version.")

    # -- the chair's one resolution round ----------------------------------------------------
    async def _resolve(self, m: dict, rows: list[dict]) -> None:
        state: dict = {}
        home = None
        try:
            home = self.chair.home()
            cfg = home.config()
            cfg["tools"] = False

            async def handler(name: str, args: dict) -> str:
                if state:
                    return json.dumps({"ok": False, "error": "you already decided"})
                if name in ("revote", "escalate", "withdraw"):
                    state.update(action=name, note=_line(args.get("note") or args.get("reason"),
                                                         800))
                    return json.dumps({"ok": True})
                return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

            ballots = "\n".join(f"- @{b['voter']} ({b['seat']}): {b['vote']}. {b['reasons']}"
                                + (f" Risk: {b['risk']}" if b.get("risk") else "")
                                for b in rows)
            system = home.system_prompt(cfg, CHAIR_RESOLVE, "")
            user = (f"Vote {m['name']}: {m['kind']} of {self.artifact(m['artifact'])['title']} "
                    f"@ {short(m['version'])}. Reason it was called: {m.get('reason') or '-'}\n"
                    f"The ballots:\n{ballots}\n\nYou get one resolution round. Call exactly one "
                    "tool: revote (only when the dissent rests on something the voters can "
                    "check again now, e.g. a misread; say what to look at), escalate (a real "
                    "disagreement: the person decides), or withdraw (the dissent is right; "
                    "the work needs changes first).")
            await home.tool_turn([{"role": "system", "content": system},
                                  {"role": "user", "content": user}], cfg,
                                 [REVOTE, ESCALATE, WITHDRAW], handler, via="vote-chair",
                                 max_calls=2)
        except Exception as e:  # noqa: BLE001 - when the chair cannot decide, the person does
            log.warning("table: the chair's resolution turn on %s failed: %s", m["id"], e)
        action = state.get("action") or "escalate"
        note = state.get("note") or ""
        self._set(m["id"], result={**m.get("result", {}), "resolution": {"action": action,
                                                                          "note": note}})
        if action == "withdraw":
            self._set(m["id"], state="withdrawn", ended=_now())
            self._post(m["room"], f"Vote {m['name']} withdrawn by the chair: {note or 'the dissent stands'}."
                                  " Fix the work, pin the new version and call a new vote.")
            return
        if action == "revote":
            self._set(m["id"], round=2)
            self._post(m["room"], f"Vote {m['name']}: the chair calls one re-vote (blind again)"
                                  + (f": {note}" if note else "."))
            await self._vote_round(m["id"], note)
            return
        await self._ask_person(self.motion(m["id"]), rows, note or "the chair could not settle it")

    # -- the person -----------------------------------------------------------------------------
    def _sides(self, rows: list[dict]) -> tuple[str, str]:
        def side(bs: list[dict]) -> str:
            if not bs:
                return "nobody"
            parts = [f"{b['voter']}: {_line(b.get('risk') if b['vote'] != 'yes' and b.get('risk') else b['reasons'], 110)}"
                     for b in bs[:3]]
            return clip("; ".join(parts), 260)
        yes = [b for b in rows if b["vote"] == "yes"]
        no = [b for b in rows if b["vote"] != "yes"]
        return side(yes), side(no)

    async def _ask_person(self, m: dict, rows: list[dict], why: str, passed: bool = False) -> None:
        a = self.artifact(m["artifact"])
        what = {"merge": "Merge", "deploy": "Deploy", "qa": "Mark ready for QA",
                "ship": "Ship"}[m["kind"]]
        tgt = a["title"] if m["kind"] in ("merge", "qa") else \
            f"{short(m['version'])} to {m['target'].get('title') or m['target'].get('id')}"
        if passed:
            line = f"{what} {tgt}? The vote passed; {m['decision'].get('why')}."
            explain = (f"Every required voter said yes at {short(m['version'])}: "
                       f"{self._sides(rows)[0]}. Trust: {m['decision'].get('cite')}")
        else:
            no = [b for b in rows if b["vote"] != "yes"]
            line = (f"{what} {tgt}? " + ", ".join(b["voter"] for b in no)
                    + (" said no" if len(no) == 1 else " said no or couldn't verify"))
            yes_side, no_side = self._sides(rows)
            explain = f"For: {yes_side}. Against: {no_side}."
        more = "\n".join(f"{b['voter']} ({b['seat']}): {b['vote']}. {b['reasons']}"
                         + (f" Risk: {b['risk']}" if b.get("risk") else "")
                         + (f" Evidence: {'; '.join(b['evidence'])}" if b['evidence'] else "")
                         for b in rows) + f"\nWhy it reached you: {why}."
        front = getattr(self._plugin("home"), "front", None)
        if front is None:
            q = await self.svc.ask(self.chair.chair_identity(), _line(line, 300),
                                   {"options": ["Yes", "No"]}, room=m["room"])
            self._set(m["id"], state="asked", ask=f"q:{q.get('question') or q.get('seq') or ''}")
            return
        ask = front.make_ask(
            "vote", line, explain, more,
            cite={"trust": {"id": m["decision"].get("entry"), "level": m["decision"].get("level"),
                            "text": m["decision"].get("cite"),
                            **({"words": m["decision"]["words"]} if m["decision"].get("words")
                               else {})},
                  "note": f"vote {m['name']}"},
            action={"motion": m["id"], "cap": "huddle.vote", "summary": f"{what} {tgt}",
                    "tier": "write", "worker": "rook"},
            source="huddle", room=m["room"])
        self._set(m["id"], state="asked", ask=ask["id"])
        self._post(m["room"], f"Vote {m['name']}: asking {front.person_name()}: {ask['line']} "
                              f"(Yes / No / Explain: {ask.get('name')})")

    async def answer(self, mid: str, yes: bool, by: str) -> dict:
        """The person's Yes or No to a vote's ask (front.py carries it here)."""
        m = self.motion(mid)
        if m["state"] != "asked":
            return {"ok": False, "note": f"vote {m['name']} is {m['state']}; nothing to do"}
        if not yes:
            self._set(mid, state="rejected", ended=_now(),
                      result={**m["result"], "rejected_by": by})
            self._post(m["room"], f"Vote {m['name']}: {by} said no. Nothing was done.")
            return {"ok": True, "note": "Nothing was done."}
        return await self.execute(mid, approved_by=by)

    async def _passed(self, m: dict) -> None:
        mode = m["decision"].get("mode")
        if mode == "auto":
            await self.execute(m["id"])
        else:
            await self._ask_person(m, self.ballots(m["id"], m["round"], reveal=True),
                                   m["decision"].get("why") or mode, passed=True)

    # -- execution ------------------------------------------------------------------------------
    async def execute(self, mid: str, approved_by: str = "") -> dict:
        """Carry out a passed vote (or one the person said yes to): the gates
        again, the version again, then the action with the hub's own
        credential. Journaled with the vote, versions and gates; the digest
        gets a "did" line."""
        m = self.motion(mid)
        if m["state"] not in ("passed", "asked"):
            raise TableError(f"vote {m['name']} is {m['state']}")
        if not approved_by and m["decision"].get("mode") != "auto":
            raise TableError("this vote needs the person's yes")
        self._set(mid, state="executing")
        a = self.artifact(m["artifact"])
        gates = await self.gates(m)
        failed = [g for g in gates if not g["ok"]]
        if failed:
            moved = [g for g in failed if g["id"] == "version" and "moved" in g["detail"]]
            if moved:
                self._void(m, moved[0]["detail"])
                return {"ok": False, "note": f"Not done: {moved[0]['detail']}"}
            self._set(mid, state="failed", gates=gates, ended=_now(),
                      result={**m["result"], "error": "gates failed at execution"})
            msg = "; ".join(f"{g['id']}: {g['detail']}" for g in failed)
            self._post(m["room"], f"Vote {m['name']}: not done, a gate failed just before: {msg}")
            return {"ok": False, "note": f"Not done: {msg}"}
        ballots = self.ballots(mid, m["round"], reveal=True)
        voters = ", ".join(b["voter"] for b in ballots if b["vote"] == "yes")
        ref = a["ref"]
        result: dict = {}
        try:
            if m["kind"] == "merge":
                title = (f"Merge pull request #{ref['number']} from {ref.get('head_ref')} "
                         f"(vote {m['name']}: {voters or 'no vote, ship-it'})")
                r = await self._gh(self.github.merge, ref["repo"], ref["number"], m["version"],
                                   str(self.setting("vote_merge_method", "merge")), title)
                if not r.get("merged"):
                    raise TableError(r.get("message") or "GitHub did not merge it")
                result = {"merged": True, "merge_sha": r.get("sha")}
                did = f"Merged {ref['repo']}#{ref['number']} at {short(m['version'])}"
            elif m["kind"] == "qa":
                await self._gh(self.github.label, ref["repo"], ref["number"], ["ready-for-qa"])
                await self._gh(self.github.comment, ref["repo"], ref["number"],
                               f"Ready for QA at {m['version']}: vote {m['name']} passed "
                               f"({voters}).")
                result = {"labelled": "ready-for-qa"}
                did = f"Marked {ref['repo']}#{ref['number']} ready for QA at {short(m['version'])}"
            else:
                t = self._deploy_target(m["target"].get("id") or "")
                spec = t.get("deploy") or {}
                if not spec.get("cap"):
                    raise TableError(f"deploy target {t['id']} has no deploy call")
                out = await self._run_call(spec, m["version"], f"{m['kind']} vote {m['id']}",
                                           approved_by=approved_by or
                                           f"vote {m['name']} ({voters})")
                if not out["ok"]:
                    raise TableError(out.get("error") or "the deploy call failed")
                result = {"deployed": t["id"], "reply": out.get("result")}
                did = (f"{'Shipped' if m['kind'] == 'ship' else 'Deployed'} "
                       f"{short(m['version'])} to {t.get('title') or t['id']}")
        except (GitHubError, TableError) as e:
            err = str(e)[:300]
            if isinstance(e, GitHubError) and e.status == 409:
                self._void(m, f"GitHub refused: the head is no longer {short(m['version'])}")
                return {"ok": False, "note": f"Not done: {err}"}
            self._set(mid, state="failed", gates=gates, ended=_now(),
                      result={**m["result"], "error": err})
            self._post(m["room"], f"Vote {m['name']}: carrying it out failed: {err}")
            self._journal("huddle.vote.execute", {"motion": mid, "kind": m["kind"],
                                                  "version": m["version"]},
                          {"ok": False, "error": err}, m["room"], self._audit(m, gates, ballots,
                                                                               approved_by))
            return {"ok": False, "note": f"Not done: {err}"}
        how = f"after {approved_by}'s yes" if approved_by else \
            (f"after a unanimous vote ({voters})" if voters else "ship-it, no vote")
        self._set(mid, state="executed", gates=gates, ended=_now(),
                  result={**m["result"], **result, "by": approved_by or "the hub",
                          "at": _now()})
        self._post(m["room"], f"Done: {did} ({how}; vote {m['name']}).")
        self._journal("huddle.vote.execute", {"motion": mid, "kind": m["kind"],
                                              "version": m["version"]},
                      {"ok": True, "result": result}, m["room"],
                      self._audit(m, gates, ballots, approved_by))
        front = getattr(self._plugin("home"), "front", None)
        if front is not None:
            with contextlib.suppress(Exception):
                front.decide("did", f"{did} {how}",
                             {"trust": {"id": m["decision"].get("entry"),
                                        "level": m["decision"].get("level"),
                                        "text": m["decision"].get("cite"),
                                        **({"words": m["decision"]["words"]}
                                           if m["decision"].get("words") else {})},
                              "note": f"vote {m['name']}: gates "
                                      + ", ".join(g["id"] for g in gates)},
                             source="huddle", room=m["room"],
                             data={"motion": mid, "version": m["version"]})
        return {"ok": True, "note": f"Done: {did}.", **result}

    @staticmethod
    def _audit(m: dict, gates: list, ballots: list, approved_by: str) -> dict:
        return {"kind": "huddle-vote", "motion": m["id"], "action": m["kind"],
                "artifact": m["artifact"], "version": m["version"],
                "decision": {k: m["decision"].get(k) for k in ("entry", "level", "mode", "rule")},
                "gates": [{k: g[k] for k in ("id", "ok", "detail")} for g in gates],
                "ballots": [{k: b.get(k) for k in ("voter", "seat", "vote", "version",
                                                   "commitment")} for b in ballots],
                "approved_by": approved_by or None}


def _fn(name: str, description: str, props: dict, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       **({"required": required} if required else {})}}}


TABLE_READ = _fn(
    "table_read",
    "Read an artifact pinned on this huddle's table, always at its pinned version (commit SHA or "
    "content hash), through the hub: summary, diff, files, file (path), checks (CI at that "
    "commit), comments (the PR's), content (a pinned document or link). Long text is paged: "
    "pass next_offset as offset.",
    {"artifact": {"type": "string", "description": "its id, or A<n> as listed"},
     "what": {"type": "string", "enum": ["summary", "diff", "files", "file", "checks",
                                         "comments", "content"]},
     "path": {"type": "string"}, "offset": {"type": "integer"}}, ["artifact"])
VOTE = _fn(
    "vote",
    "Cast your sealed ballot (once). yes needs evidence you checked at the pinned version "
    "(what you read with table_read: e.g. 'diff: only docs/ changed', 'checks: pytest 3.12 "
    "success'). cant_verify when you could not check it: it counts as no. no blocks the action.",
    {"vote": {"type": "string", "enum": list(VOTES)},
     "reasons": {"type": "string", "description": "why, in a sentence or two"},
     "evidence": {"type": "array", "items": {"type": "string"}},
     "risk": {"type": "string", "description": "the specific risk you see, or why there is none "
                                               "(required of the critic)"}},
    ["vote", "reasons"])
PIN = _fn(
    "pin",
    "Pin an artifact on this huddle's table at its exact version, so every role can read it: "
    "pr (repo, number), commit (repo, sha), file (repo, path, sha), doc or tests (text), link "
    "(url: a screenshot or preview build; the hub hashes it).",
    {"kind": {"type": "string", "enum": list(ARTIFACT_KINDS)}, "repo": {"type": "string"},
     "number": {"type": "integer"}, "sha": {"type": "string"}, "path": {"type": "string"},
     "url": {"type": "string"}, "text": {"type": "string"}, "title": {"type": "string"},
     "note": {"type": "string"}}, ["kind"])
CALL_VOTE = _fn(
    "call_vote",
    "Call a vote on one action of a pinned artifact: merge (a PR), qa (ready for QA), deploy or "
    "ship (target: a deploy target id). The hub checks the trust map: it says whether a vote is "
    "needed and what a pass does; gates (CI, tests, secrets, ...) run first. The required voters "
    "vote blind; you never vote. skip_vote only on ship-it targets.",
    {"kind": {"type": "string", "enum": list(MOTION_KINDS)},
     "artifact": {"type": "string"}, "reason": {"type": "string"},
     "target": {"type": "string"}, "skip_vote": {"type": "boolean"}}, ["kind", "artifact"])
REVOTE = _fn("revote", "One re-vote, blind again, with a note on what to check.",
             {"note": {"type": "string"}}, ["note"])
ESCALATE = _fn("escalate", "The person decides (a one-line Yes / No / Explain ask).",
               {"note": {"type": "string"}})
WITHDRAW = _fn("withdraw", "Withdraw the vote: the dissent is right; the work needs changes.",
               {"reason": {"type": "string"}}, ["reason"])

KIND_WORDS = {"merge": "merging this pull request into its base branch",
              "qa": "marking this pull request ready for QA",
              "deploy": "deploying this commit to the target",
              "ship": "shipping this commit to users"}
VOTE_ASK = (
    "You are a required voter ({seat} seat) on {what}: {title}, pinned at {version}. Reason it "
    "was called: {reason}.{note}\n"
    "Vote on the pinned version only: read it with table_read (start with summary, then the "
    "diff, the files you need and checks). Your ballot is sealed: nobody sees it, and you see no "
    "one else's, until every voter has voted. Then call vote once: yes (with evidence of what "
    "you checked), no (it should not go ahead: say why), or cant_verify (you could not check "
    "it: it counts as no).{critic} Then answer in one line.")
CRITIC_ASK = (" As the critic, name the most specific risk you see in it (or say why there is "
              "none) in risk.")
VOTE_GUIDE = ("This turn is a vote, not a discussion: no cards, no sessions. Your concerns and "
              "checklist decide your vote; do not vote yes on trust.")
CHAIR_RESOLVE = ("You chair a vote that a required voter blocked. You never vote and never "
                 "overrule a voter; you keep things moving.")
