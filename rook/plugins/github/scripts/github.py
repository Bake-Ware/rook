"""GitHub's REST API with the stdlib (plugin.yaml says what each cap does).

Reads go to anyone the hub lets call them; ``merge``, ``label`` and
``comment`` only to the plugins in the ``writers`` setting, calling as
themselves (``system:plugin.<name>``, no person): the huddle merges after its
gates with the hub's token, so an agent with broad grants can't use that
token to merge around them. The token goes in an unredirected header: a
redirect never carries it to another host.
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


def _req(ctx, method: str, path: str, body: dict | None = None, accept: str = JSON,
         raw: bool = False):
    url = str(ctx.settings["api"]).rstrip("/") + path
    req = urllib.request.Request(url, method=method,
                                 data=None if body is None else json.dumps(body).encode())
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
    cut = len(data) > TEXT_MAX
    return {"text": data[:TEXT_MAX].decode("utf-8", "replace"), "bytes": min(len(data), TEXT_MAX),
            "truncated": cut}


def _writer(ctx) -> None:
    allowed = {f"system:plugin.{w}" for w in ctx.settings["writers"] or []}
    if ctx.caller.person or ctx.caller.agent not in allowed:
        raise KitError("denied: only the huddle writes to GitHub with the hub's token "
                       "(pin the pull request in a huddle and call a merge vote)")


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


def _compare(repo, base, head) -> str:
    return f"/repos/{_repo(repo)}/compare/{_ref(base, 'base')}...{_ref(head, 'head')}"


def diff(ctx, repo, base, head):
    return _text(_req(ctx, "GET", _compare(repo, base, head), accept="application/vnd.github.diff", raw=True))


def files(ctx, repo, base, head):
    c = _req(ctx, "GET", _compare(repo, base, head)) or {}
    return [{"path": f.get("filename"), "status": f.get("status"),
             "additions": f.get("additions"), "deletions": f.get("deletions")} for f in c.get("files") or []]


def file(ctx, repo, path, ref):
    path = str(path or "").strip().lstrip("/")
    if not path or ".." in path.split("/"):
        raise KitError(f"bad args: path {path!r}")
    q = urllib.parse.quote(path) + "?ref=" + urllib.parse.quote(_ref(ref), safe="")
    return _text(_req(ctx, "GET", f"/repos/{_repo(repo)}/contents/{q}",
                      accept="application/vnd.github.raw", raw=True))


# -- writes (writers only) ---------------------------------------------------------

def merge(ctx, repo, number, sha, title, method="merge"):
    _writer(ctx)
    if not _SHA.fullmatch(str(sha)):
        raise KitError("bad args: sha is the full 40-character head the merge is for")
    r = _req(ctx, "PUT", f"/repos/{_repo(repo)}/pulls/{int(number)}/merge",
             {"sha": sha, "merge_method": method, "commit_title": str(title)[:250]}) or {}
    if not r.get("merged"):
        raise KitError(f"GitHub did not merge it: {r.get('message') or 'no reason given'}")
    return {"merged": True, "sha": r.get("sha") or ""}


def label(ctx, repo, number, labels):
    _writer(ctx)
    _req(ctx, "POST", f"/repos/{_repo(repo)}/issues/{int(number)}/labels", {"labels": [str(x) for x in labels]})
    return {"labels": [str(x) for x in labels]}


def comment(ctx, repo, number, body):
    _writer(ctx)
    r = _req(ctx, "POST", f"/repos/{_repo(repo)}/issues/{int(number)}/comments", {"body": str(body)[:60000]}) or {}
    return {"id": r.get("id"), "url": r.get("html_url") or ""}
