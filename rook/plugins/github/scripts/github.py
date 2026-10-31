"""GitHub's REST API with the stdlib (plugin.yaml says what each cap does).

``merge`` is how work lands on 0.5 (castling-workbench: sign-off replaces
merge votes). It checks, as the caller, the task's sign-off against what its
project's trust level asks for, or the repo's project's when the ``repos``
setting names a stricter one (:func:`signoff_problems`), runs the gates
against GitHub (:func:`gate_problems`), and only then merges at the head SHA
the caller named, with the hub's token. The call is journaled like any
other, with its args and why; the reply says who signed and which gates
passed. The token goes in an unredirected header, so a redirect never
carries it to another host.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from rook.kit.ctx import KitError

TIMEOUT = 20.0
TEXT_MAX = 8 * 1024 * 1024
JSON = "application/vnd.github+json"
PASSING = ("success", "neutral", "skipped")
LEVELS = ("ship-it", "normal", "sacred")             # loosest first
MERGEABLE_TRIES, MERGEABLE_WAIT = 3, 1.0
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")
# Credential shapes (as 0.4's huddle table).
SECRET_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|xox[abposr]-[A-Za-z0-9-]{10,}|"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35})")


def _repo(repo: str) -> str:
    repo = str(repo or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise KitError(f"bad args: repo {repo!r} is owner/name, e.g. Bake-Ware/rook")
    return repo


def _ref(ref: str, what: str = "ref") -> str:
    ref = str(ref or "").strip()
    if not ref or len(ref) > 255 or ".." in ref or any(c in ref for c in " ~^:?*[\\"):
        raise KitError(f"bad args: {what} {ref!r}")
    return ref


def _req(ctx, method: str, path: str, body: dict | None = None, accept: str = JSON, raw: bool = False):
    url = str(ctx.settings["api"]).rstrip("/") + path
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "rook-hub")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    token = ctx.settings["token"]
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(TEXT_MAX + 1)
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read(65536).decode() or "{}").get("message") or ""
        except (ValueError, OSError, AttributeError):
            pass
        raise KitError(f"GitHub {e.code}: {msg or e.reason}", retry=e.code >= 500) from None
    except (urllib.error.URLError, OSError) as e:
        raise KitError(f"GitHub unreachable: {getattr(e, 'reason', e)}", retry=True) from None
    if raw:
        return data
    try:
        return json.loads(data.decode() or "null")
    except ValueError:
        raise KitError("GitHub answered something that isn't JSON") from None


def _text(data: bytes) -> dict:
    return {"text": data[:TEXT_MAX].decode("utf-8", "replace"), "bytes": min(len(data), TEXT_MAX),
            "truncated": len(data) > TEXT_MAX}


def secret_hits(diff: str) -> list[str]:
    """Credential-looking strings on a diff's added lines: where, never the value."""
    hits = []
    for i, line in enumerate(str(diff or "").splitlines(), 1):
        if line.startswith("+") and not line.startswith("+++"):
            m = SECRET_SHAPES.search(line)
            if m:
                hits.append(f"line {i}: looks like a credential ({m.group(0)[:4]}...)")
    return hits[:20]


# -- reads -------------------------------------------------------------------------

def pr(ctx, repo, number):
    p = _req(ctx, "GET", f"/repos/{_repo(repo)}/pulls/{int(number)}")
    head, base = p.get("head") or {}, p.get("base") or {}
    return {"number": int(p["number"]), "title": p.get("title") or "", "body": p.get("body") or "",
            "state": p.get("state"), "merged": bool(p.get("merged")), "draft": bool(p.get("draft")),
            "mergeable": p.get("mergeable"), "mergeable_state": p.get("mergeable_state"),
            "head": head.get("sha") or "", "head_ref": head.get("ref") or "",
            "base": base.get("ref") or "", "base_sha": base.get("sha") or "",
            "author": (p.get("user") or {}).get("login") or "", "url": p.get("html_url") or "",
            "merge_sha": (p.get("merge_commit_sha") or "") if p.get("merged") else ""}


def commit(ctx, repo, ref):
    c = _req(ctx, "GET", f"/repos/{_repo(repo)}/commits/{urllib.parse.quote(_ref(ref), safe='')}")
    info = c.get("commit") or {}
    return {"sha": c["sha"], "message": info.get("message") or "",
            "author": (c.get("author") or {}).get("login") or (info.get("author") or {}).get("name") or "",
            "parents": [p["sha"] for p in c.get("parents") or []],
            "files": [{"path": f.get("filename"), "status": f.get("status"),
                       "additions": f.get("additions"), "deletions": f.get("deletions")}
                      for f in c.get("files") or []]}


def checks(ctx, repo, sha):
    repo, sha = _repo(repo), _ref(sha, "sha")
    runs = _req(ctx, "GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
    status = _req(ctx, "GET", f"/repos/{repo}/commits/{sha}/status") or {}
    return {"runs": [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
                     for r in runs.get("check_runs") or []],
            "statuses": [{"context": s.get("context"), "state": s.get("state")}
                         for s in status.get("statuses") or []],
            "state": status.get("state")}


def reviews(ctx, repo, number):
    logins = {k.lower(): v for k, v in (ctx.settings["logins"] or {}).items()}
    out = []
    for r in _req(ctx, "GET", f"/repos/{_repo(repo)}/pulls/{int(number)}/reviews?per_page=100") or []:
        login = (r.get("user") or {}).get("login") or ""
        out.append({"login": login, "who": logins.get(login.lower()), "state": r.get("state"),
                    "commit": r.get("commit_id"), "body": (r.get("body") or "")[:2000]})
    return out


def diff(ctx, repo, base, head):
    path = f"/repos/{_repo(repo)}/compare/{_ref(base, 'base')}...{_ref(head, 'head')}"
    return _text(_req(ctx, "GET", path, accept="application/vnd.github.diff", raw=True))


def file(ctx, repo, path, ref):
    path = str(path or "").strip().lstrip("/")
    if not path or ".." in path.split("/"):
        raise KitError(f"bad args: path {path!r}")
    q = urllib.parse.quote(path) + "?ref=" + urllib.parse.quote(_ref(ref), safe="")
    return _text(_req(ctx, "GET", f"/repos/{_repo(repo)}/contents/{q}", accept="application/vnd.github.raw", raw=True))


# -- landing -----------------------------------------------------------------------

def signoff_problems(level: str, rows: list, revs: list, head: str, author: str | None,
                     caller_agent: str | None, owners: list) -> tuple[list, dict]:
    """What keeps a task's sign-off from being satisfied (empty: it is), and
    who signed. ``rows``: task.signoff records; ``revs``: :func:`reviews`
    (the latest review per login counts; an approval counts only at
    ``head``). The author (the PR's mapped login) and the calling agent
    don't sign their own work."""
    own = {author, caller_agent} - {None}
    signers, problems = [], []
    for r in rows:
        who = r.get("by") or r["reviewer"]
        if r["state"] == "declined":
            problems.append(f"{r['reviewer']} declined" + (f": {r['note']}" if r.get("note") else ""))
        elif r["state"] == "requested":
            problems.append(f"{r['reviewer']} has not signed off yet")
        elif who not in own:
            signers.append(who)
    latest = {}
    for r in revs:                      # GitHub lists reviews oldest first
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["login"].lower()] = r
    for r in latest.values():
        if r["state"] == "CHANGES_REQUESTED":
            problems.append(f"{r['login']} requested changes on GitHub")
        elif r["state"] == "APPROVED" and r["commit"] == head and r["who"] and r["who"] not in own:
            signers.append(r["who"])
    signers = list(dict.fromkeys(signers))
    if level != "ship-it" and not signers:
        problems.append(f"no sign-off yet: a {level} project needs a reviewer other than the author "
                        "(task.request_signoff, then task.signoff; or an approving GitHub review at the head)")
    if level == "sacred" and not set(signers) & set(owners):
        problems.append(f"an owner ({', '.join(owners) or 'the person'}) must sign off too (the project is sacred)")
    return problems, {"level": level, "signers": signers}


def gate_problems(ctx, repo: str, p: dict, sha: str) -> tuple[list, list]:
    """The gates, in order: (problems, [{id, ok, detail}])."""
    gates: list = []

    def gate(gid, ok, detail):
        gates.append({"id": gid, "ok": bool(ok), "detail": detail})
    if p["state"] != "open":
        gate("version", False, f"the pull request is {'merged' if p['merged'] else p['state']}")
    elif p["draft"]:
        gate("version", False, "the pull request is a draft")
    elif p["head"] != sha:
        gate("version", False, f"the head is {p['head'][:7]}, not {sha[:7]}: check the new head, then call again")
    else:
        gate("version", True, f"head is {sha[:7]}")
    if gates[0]["ok"]:
        for i in range(MERGEABLE_TRIES):
            if p["mergeable"] is not None or i + 1 == MERGEABLE_TRIES:
                break
            time.sleep(MERGEABLE_WAIT)
            p = pr(ctx, repo, p["number"])
        gate("mergeable", p["mergeable"] is True,
             f"merges cleanly into {p['base']}" if p["mergeable"] else
             f"conflicts with {p['base']} ({p['mergeable_state']}); rebase it" if p["mergeable"] is False else
             "GitHub has not worked out whether it merges cleanly yet; call again")
        c = checks(ctx, repo, sha)
        running = [r["name"] for r in c["runs"] if r["status"] != "completed"] + \
            [s["context"] for s in c["statuses"] if s["state"] == "pending"]
        bad = [f"{r['name']}: {r['conclusion']}" for r in c["runs"]
               if r["status"] == "completed" and r["conclusion"] not in PASSING]
        bad += [f"{s['context']}: {s['state']}" for s in c["statuses"] if s["state"] not in ("success", "pending")]
        none = not c["runs"] and not c["statuses"]
        gate("ci", not (running or bad or (none and ctx.settings["require_ci"])),
             f"still running: {', '.join(running[:6])}; call again when CI is done" if running else
             "CI is not green: " + "; ".join(bad[:6]) if bad else
             f"no CI ran on {sha[:7]}" if none and ctx.settings["require_ci"] else
             f"{len(c['runs']) + len(c['statuses'])} checks green")
        pats = [x.lower() for x in ctx.settings["test_checks"] or ["*test*"]]
        tests = [r for r in c["runs"] if any(fnmatch.fnmatchcase(str(r["name"]).lower(), x) for x in pats)]
        failed = [r["name"] for r in tests if r["conclusion"] != "success"]
        gate("tests", (tests and not failed) or (none and not ctx.settings["require_ci"]),
             "tests did not pass: " + ", ".join(failed) if failed else
             ", ".join(r["name"] for r in tests) + " passed" if tests else
             f"no test run among the checks (test_checks: {pats})")
        d = diff(ctx, repo, p["base_sha"], sha)
        hits = [] if d["truncated"] else secret_hits(d["text"])
        gate("secrets", not d["truncated"] and not hits,
             "the diff is over 8 MB: too big to check for secrets" if d["truncated"] else
             "the diff has secrets in it: " + "; ".join(hits[:5]) if hits else "no secrets in the diff")
    return [f"{g['id']}: {g['detail']}" for g in gates if not g["ok"]], gates


def _level(ctx, project) -> str:
    """The project's trust level (project.trust); normal without one."""
    if not project:
        return "normal"
    p = ctx.call("project.get", id=project)
    trust = ctx.call("project.trust.list", where={"project": p["id"]}, limit=1)
    return trust[0]["level"] if trust else "normal"


def _strictest(ctx, task_project, repo) -> str:
    """The stricter of the task's project and the one the ``repos`` setting
    names for the repo: a task in a ship-it project can't land work in a
    repo that belongs to a sacred one."""
    mapped = next((p for r, p in (ctx.settings["repos"] or {}).items() if r.lower() == repo.lower()), None)
    return max([_level(ctx, p) for p in dict.fromkeys([task_project, mapped]) if p] or ["normal"],
               key=LEVELS.index)


def review(ctx, repo, number, sha, task, approve, body):
    """Sign off (or decline) as the caller, then put the review on the PR.
    The task's sign-off record is what github.merge counts.
    TODO(castling:identity): post the review under the reviewer's own GitHub
    account once agents have one (then it is an APPROVE / REQUEST_CHANGES
    review that counts through the logins setting). Until then the hub's token
    posts it as a comment that names the reviewer."""
    repo = _repo(repo)
    p = pr(ctx, repo, number)
    if p["head"] != sha:
        raise KitError(f"the head is {p['head'][:7]}, not {sha[:7]}: review the new head")
    me = ctx.caller.actor
    if not me:
        raise KitError("a review needs a person or an agent")
    rows = ctx.call("task.signoff.list", where={"task": task}, limit=500)
    mine = [r["reviewer"] for r in rows if r["state"] == "requested"
            and r["reviewer"] in (me, f"role:{me.partition(':')[2]}")]
    note = f"{repo}#{int(number)} at {sha[:7]}: {body}"[:2000]
    out = ctx.call("task.signoff", id=task, approve=bool(approve), note=note,
                   **({"reviewer": mine[0]} if mine else {}))
    verdict = "approves" if approve else "asks for changes"
    try:
        r = _req(ctx, "POST", f"/repos/{repo}/pulls/{int(number)}/reviews",
                 {"commit_id": sha, "event": "COMMENT",
                  "body": f"**{me}** {verdict} (task {task}, sign-off as {mine[0] if mine else me}):\n\n{body}"}) or {}
        posted = r.get("html_url") or True
    except KitError as e:
        posted = f"not posted: {e.message[:200]}"
    return {"task": task, "signoff": out.get("signoff"), "review": posted}


def merge(ctx, repo, number, sha, task, method="merge"):
    repo = _repo(repo)
    if not _SHA.fullmatch(str(sha)):
        raise KitError("bad args: sha is the full 40-character head you checked")
    t = ctx.call("task.get", id=task)
    level = _strictest(ctx, t.get("project"), repo)
    rows = ctx.call("task.signoff.list", where={"task": t["id"]}, limit=500)
    p = pr(ctx, repo, number)
    logins = {k.lower(): v for k, v in (ctx.settings["logins"] or {}).items()}
    problems, signed = signoff_problems(level, rows, reviews(ctx, repo, number), sha,
                                        logins.get(p["author"].lower()), ctx.caller.agent, ctx.owners())
    gate_bad, gates = gate_problems(ctx, repo, p, sha)
    problems += gate_bad
    if problems:
        raise KitError(f"not merged ({repo}#{number} for task {t['id']}): " + "; ".join(problems))
    signers = ", ".join(signed["signers"]) or f"no sign-off, {level}"
    r = _req(ctx, "PUT", f"/repos/{repo}/pulls/{int(number)}/merge",
             {"sha": sha, "merge_method": method,
              "commit_title": f"Merge pull request #{int(number)} from {p['head_ref'] or sha[:7]} "
                              f"(task {t.get('name') or t['id']}, signed: {signers})"[:250]}) or {}
    if not r.get("merged"):
        raise KitError(f"GitHub did not merge it: {r.get('message') or 'no reason given'}")
    out = {"merged": True, "sha": r.get("sha") or "", "task": t["id"], "signoff": signed,
           "gates": [g["id"] for g in gates]}
    try:
        ctx.call("task.link", id=t["id"], kind="commit", ref=out["sha"], relation="produced",
                 note=f"{repo}#{number} merged at {sha[:7]}")
    except KitError as e:
        out["link"] = f"not linked: {e.message[:200]}"
    return out
