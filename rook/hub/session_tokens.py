"""Session tokens the hub mints for coding sessions (docs/design/trust.md 5;
house-agent vision 11; security card c_839e89992532).

A role's coding session must be able to push its own branch and open a pull
request, and must not be able to merge, push ``master``, change branch
protection or read secrets. Bake's own GitHub credential can do all of
those, so a session must never run with it.

The hub holds a **GitHub App** (``role.session_github_app_id``,
``role.session_github_installation`` and the app's private key, a vault
secret ``role.session_github_app_key``) and, for each session, mints an
installation token:

* scoped to **one repository** (the code location's), with ``contents:
  write`` and ``pull_requests: write`` (and ``metadata: read``). It cannot
  touch settings, secrets, workflows or other repositories;
* that **expires** within the hour (GitHub's limit), and is revoked when the
  session ends;
* **logged**: every mint (session, role, repository, permissions, expiry,
  why, and a fingerprint, never the token) is appended to
  ``session-tokens.jsonl`` in the hub data directory.

``contents: write`` alone could still merge a pull request through the API,
so the repository's ruleset on ``master`` restricts updates to repository
admins (the app is not one): the app can push branches and open PRs but
never update ``master``. Merges stay hub actions (later, after votes).

Without an app configured, :func:`mint` raises :class:`NotConfigured` and
the session runs as before, with the hub's push guard (a pre-push hook the
session cannot bypass with its allowlist) and GitHub's branch protection
(``enforce_admins``) refusing any push to ``master``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("rook.hub.session_tokens")

API = "https://api.github.com"
PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
#: Permissions a session token may never carry, whatever a caller asks.
FORBIDDEN = ("administration", "secrets", "workflows", "actions", "environments",
             "organization_administration", "members", "deployments")


class NotConfigured(RuntimeError):
    """No GitHub App on this hub: sessions keep the push guard only."""


class MintError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    app_id: str
    installation_id: str
    private_key: str

    @classmethod
    def from_settings(cls, get: Callable[[str], Any]) -> "AppConfig":
        app = str(get("session_github_app_id") or "").strip()
        inst = str(get("session_github_installation") or "").strip()
        key = str(get("session_github_app_key") or "").strip()
        if not (app and inst and key):
            raise NotConfigured("no GitHub App for sessions (Settings > Roles: session GitHub "
                                "app id, installation and private key)")
        return cls(app, inst, key)


def app_jwt(cfg: AppConfig, now: float | None = None) -> str:
    """The app's own short JWT (RS256, 9 minutes, backdated 60 s for clock skew)."""
    import jwt
    now = int(time.time() if now is None else now)
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": cfg.app_id}, cfg.private_key,
                      algorithm="RS256")


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _post(url: str, headers: dict, body: dict | None, method: str = "POST") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise MintError(f"GitHub said {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MintError(f"GitHub unreachable: {e}") from None


class Minter:
    """Mints and revokes session tokens and keeps the mint log."""

    def __init__(self, log_path: str | None, *, http: Callable[..., dict] = _post,
                 clock: Callable[[], float] = time.time, api: str = API) -> None:
        self.log_path = log_path
        self.http = http
        self.clock = clock
        self.api = api.rstrip("/")

    def mint(self, cfg: AppConfig, *, repo: str, session: str, role: str, why: str,
             permissions: dict | None = None) -> dict:
        """``{token, expires_at, fingerprint, repo, permissions}`` for one
        session on one repository (``owner/name``)."""
        owner, _, name = str(repo or "").partition("/")
        if not owner or not name or "/" in name:
            raise MintError(f"repo {repo!r}: owner/name, like Bake-Ware/rook")
        perms = dict(PERMISSIONS)
        for k, v in (permissions or {}).items():
            if k in FORBIDDEN:
                raise MintError(f"a session token never carries {k}")
            if k in perms and v not in ("read", "write"):
                raise MintError(f"permission {k}: read or write")
            if k not in perms:
                raise MintError(f"a session token carries only {', '.join(PERMISSIONS)}")
            perms[k] = v
        headers = {"Authorization": f"Bearer {app_jwt(cfg, self.clock())}",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "rook-hub"}
        reply = self.http(f"{self.api}/app/installations/{cfg.installation_id}/access_tokens",
                          headers, {"repositories": [name], "permissions": perms})
        token = str(reply.get("token") or "")
        if not token:
            raise MintError("GitHub returned no token")
        got = reply.get("permissions") or perms
        bad = [k for k in got if k in FORBIDDEN]
        if bad:
            # The app was installed with more than sessions need: refuse rather
            # than hand a session a token that can change settings.
            self.revoke(token)
            raise MintError(f"the GitHub App grants {', '.join(bad)}; give it only contents, "
                            "pull requests and metadata")
        out = {"token": token, "expires_at": str(reply.get("expires_at") or ""),
               "fingerprint": fingerprint(token), "repo": f"{owner}/{name}", "permissions": got}
        self._log({"op": "mint", "session": session, "role": role, "repo": out["repo"],
                   "permissions": got, "expires_at": out["expires_at"], "why": why[:300],
                   "fingerprint": out["fingerprint"]})
        return out

    def revoke(self, token: str, session: str = "") -> bool:
        """Revoke a token (the session ended). False when GitHub refused."""
        try:
            self.http(f"{self.api}/installation/token",
                      {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "rook-hub"},
                      None, method="DELETE")
        except MintError as e:
            log.warning("session token %s not revoked: %s", fingerprint(token), e)
            return False
        self._log({"op": "revoke", "session": session, "fingerprint": fingerprint(token)})
        return True

    def _log(self, row: dict) -> None:
        row = {"at": self.clock(), **row}
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            log.exception("session token log unwritable")

    def history(self, limit: int = 50) -> list[dict]:
        if not self.log_path or not os.path.exists(self.log_path):
            return []
        rows = []
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return list(reversed(rows[-limit:]))
