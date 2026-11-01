"""GitHub's REST API with the stdlib (plugin.yaml says what each cap does).

``merge`` is how work lands on 0.5 (castling-workbench: sign-off replaces
merge votes). It checks, as the caller, the task's sign-off against what its
project asks for (trust level and concept norm; the ``repos`` setting can
name a stricter project for the repo: :func:`signoff_problems`) and that the
head is the SHA the caller named (:func:`head_problem`), and only then
merges at that SHA with the hub's token. CI, tests and the diff are the
reviewers' checks, not machinery here. The call is journaled like any other,
with its args and why; the reply says who signed. The token goes in an unredirected header, so a redirect never
carries it to another host.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from rook.kit.ctx import KitError

TIMEOUT = 20.0
TEXT_MAX = 8 * 1024 * 1024
JSON = "application/vnd.github+json"
LEVELS = ("ship-it", "normal", "sacred")             # loosest first
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")


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

def signoff_problems(level: str, norm: str, rows: list, revs: list, head: str, author: str | None,
                     caller_agent: str | None, owners: list) -> tuple[list, dict]:
    """What keeps a task's sign-off from being satisfied (empty: it is), and
    who signed. ``level``: the project's trust level (whether the person
    signs: sacred); ``norm``: its concepts' sign-off norm (``none`` or
    ``experts``: whether reviewers sign at all, unless the level is
    ship-it). ``rows``: task.signoff records; ``revs``: :func:`reviews` (the
    latest per login counts; an approval only at ``head``). The author (the
    PR's mapped login) and the calling agent don't sign their own work."""
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
    if level != "ship-it" and norm != "none" and not signers:
        problems.append(f"no sign-off yet: a {level} project needs a reviewer other than the author "
                        "(task.request_signoff, then github.review or task.signoff)")
    if level == "sacred" and not set(signers) & set(owners):
        problems.append(f"an owner ({', '.join(owners) or 'the person'}) must sign off too (the project is sacred)")
    return problems, {"level": level, "norm": norm, "signers": signers}


def head_problem(p: dict, sha: str) -> str | None:
    """The one check in machinery: the PR is open and its head is the sha the
    sign-off was for. CI, tests and the diff are the reviewers' checklist
    (castling-workbench: checks and balances at persona level); GitHub
    refuses a merge that conflicts or that branch protection blocks."""
    if p["state"] != "open":
        return f"the pull request is {'merged' if p['merged'] else p['state']}"
    if p["draft"]:
        return "the pull request is a draft"
    if p["head"] != sha:
        return f"the head is {p['head'][:7]}, not {sha[:7]}: the sign-off and reviews must be for the new head"
    return None


def _level(ctx, project) -> tuple[str, str]:
    """(trust level, sign-off norm) of a project: normal and experts without one."""
    if not project:
        return "normal", "experts"
    p = ctx.call("project.get", id=project)
    trust = ctx.call("project.trust.list", where={"project": p["id"]}, limit=1)
    norm = (ctx.call("concept.pool", concepts=p["concepts"]).get("signoff") if p.get("concepts") else None)
    return (trust[0]["level"] if trust else "normal"), norm or "experts"


def _strictest(ctx, task_project, repo) -> tuple[str, str]:
    """The stricter of the task's project and the one the ``repos`` setting
    names for the repo: a task in a ship-it project can't land work in a
    repo that belongs to a sacred one."""
    mapped = next((p for r, p in (ctx.settings["repos"] or {}).items() if r.lower() == repo.lower()), None)
    found = [_level(ctx, p) for p in dict.fromkeys([task_project, mapped]) if p] or [("normal", "experts")]
    return (max((lv for lv, _ in found), key=LEVELS.index),
            "experts" if any(n == "experts" for _, n in found) else "none")


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
    level, norm = _strictest(ctx, t.get("project"), repo)
    if level == "sacred" and ctx.move_in:     # move-in: no owner sign-off wait; reviewers still sign
        level = "normal"
    rows = ctx.call("task.signoff.list", where={"task": t["id"]}, limit=500)
    p = pr(ctx, repo, number)
    logins = {k.lower(): v for k, v in (ctx.settings["logins"] or {}).items()}
    problems, signed = signoff_problems(level, norm, rows, reviews(ctx, repo, number), sha,
                                        logins.get(p["author"].lower()), ctx.caller.agent, ctx.owners())
    head = head_problem(p, sha)
    if head:
        problems.insert(0, head)
    if problems:
        raise KitError(f"not merged ({repo}#{number} for task {t['id']}): " + "; ".join(problems))
    signers = ", ".join(signed["signers"]) or f"no sign-off, {level}"
    r = _req(ctx, "PUT", f"/repos/{repo}/pulls/{int(number)}/merge",
             {"sha": sha, "merge_method": method,
              "commit_title": f"Merge pull request #{int(number)} from {p['head_ref'] or sha[:7]} "
                              f"(task {t.get('name') or t['id']}, signed: {signers})"[:250]}) or {}
    if not r.get("merged"):
        raise KitError(f"GitHub did not merge it: {r.get('message') or 'no reason given'}")
    out = {"merged": True, "sha": r.get("sha") or "", "task": t["id"], "signoff": signed}
    try:
        ctx.call("task.link", id=t["id"], kind="commit", ref=out["sha"], relation="produced",
                 note=f"{repo}#{number} merged at {sha[:7]}")
    except KitError as e:
        out["link"] = f"not linked: {e.message[:200]}"
    return out
