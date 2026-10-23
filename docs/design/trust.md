# The trust map (intent list) and session isolation

Status: built 2026-10-23 (overnight track B, phase B1). Vision:
knowledge `house-agent-vision` sections 2, 3, 10 and 11. User manual:
`docs/manual/trust.md`.

## 1. What it is

One human-readable, versioned record in the hub that says, for each project,
host, repository and resource, how much agents may do on their own:

| Level | Meaning |
|---|---|
| sacred | Changed only on the person's direct instruction. Agents may read, propose and prepare. |
| guarded | Agents vote, act, then tell. Until votes exist (H6 / B5): an ask-first hold. |
| ship-it | Agents just go. |

The hub enforces it (`rook/hub/trust.py`, consulted by the approval layer in
`rook/hub/approvals.py`), not prompts.

## 2. The record

`trust.json` in the hub data directory, beside `policy.json` and
`approvals.db`; both hub processes read it (mtime cache, 2 s), writes take a
file lock and an atomic rename. `trust-history.jsonl` beside it is the
append-only history: version, time, op, the actor (`person`, `house`,
`system`, `agent` for proposals), the person's words, before and after.

```json
{"format": "rook-trust/1", "version": 7, "unlisted": "balanced",
 "entries": [{"id": "kaiju", "title": "kaiju", "kind": "host", "level": "sacred",
              "targets": ["worker:kaiju"], "description": "...",
              "set_by": {"who": "Bake", "kind": "person", "when": "...", "words": "kaiju changes are sacred, only ever under my direct direction"},
              "confirmed": true, "rev": 1}],
 "exceptions": [{"id": "overnight-20261023", "applies_to": ["*"], "excludes": ["family-devices"],
                 "targets": [], "kinds": ["change", "admin"], "tier": "go",
                 "expires": "2026-10-23T12:00:00-05:00", "expires_at": 1792774800, "set_by": {...}}],
 "ledger": [{"id": "...", "resource": "kaiju", "what": "Strata on GPU 1 stopped", "why": "...",
             "restore": "systemctl start strata@1", "until": "...", "state": "open", "authorized_by": {...}}],
 "proposals": [{"id": "tp_...", "state": "pending", "change": {"op": "...", "data": {...}}, "by": "...", "why": "..."}],
 "notices": [{"version": 8, "op": "set_entry", "id": "nas", "who": "rook", "words": "..."}]}
```

Selectors: `worker:<name glob>` (or worker id), `cap:<cap glob>` (any
worker), `repo:<owner>/<name>[#<branch glob>]`, `project:<slug>`.

## 3. Rulings

`tier = MATRIX[level][kind]`:

| kind \ level | ship-it | balanced (unlisted default) | guarded | sacred |
|---|---|---|---|---|
| read (read-tier cap) | go | go | go | go |
| prepare (hub record caps: task/knowledge/chat/huddle writes) | go | go | go | go |
| change (write/exec) | go | go | ask | person |
| admin (admin tier or tagged destructive) | go | ask | ask | person |

- The level is the **strictest** entry matching the call; no entry: the
  map's `unlisted` level. Adding an entry can therefore only tighten (except
  a ship-it entry on an unlisted target, which is the person's alone).
- An active **exception** (not ended, not expired) lowers the tier to its
  `tier` (default go) when it applies to a matched entry (or `*`), its
  `targets` (if any) match, its `kinds` include the action's, and no matched
  entry is in its `excludes`.
- Every ruling has `rule` = `trust:<entry>@v<version>[+<exception>]` and
  `cite`, one line with the entry, level, who set it, when, and their words.

**Enforcement** (`Approvals.apply`, for holdable principals only: agents,
roles, integrations, unverified; never people at the dashboard, never hub
code): `ask` or `person` from an entry (or a tightened `unlisted`) becomes a
hold of kind `trust` whose reason is the citation; the agent's held reply
says the same. Approving it on the Approvals page (or a relayed approval in
the person's words) runs that one call: the person's direct instruction.
The trust map holds even when `approvals.enabled` is off (the live hub has
had blanket holds off since Bake turned them off on 2026-10-14): that switch
turns off the built-in admin/destructive, operator-rule and spread holds, not
the map the person asked for by name.
`go` adds no hold; the band's other holds (built-in admin/destructive,
operator rules, hub-host, spread) still apply. A ship-it entry never lowers
a hold the band already had, and unlisted/balanced is exactly the hub's
behaviour before the map. Every decision that consulted the map carries the
citation in `Decision.trust_rule` / `trust`, in `explain()` and `journal()`,
and in the journal's `rule` column.

Roles: `cap_tier` appends the citation to an action card's why; a routine
`start_session` in a code location whose repository or worker is guarded or
sacred becomes a `session.start` card. Role prompts and the house agent's
prompt include the summary (`TrustStore.summary`).

## 4. Who changes it

| Who | May |
|---|---|
| Person, on the dashboard (Manage > Trust, `POST /api/trust`, owner/admin, not a weak credential, same origin) | anything; words required |
| House agent (its own in-process identity; `trust.tighten`) | raise a level (same or more targets), add a guarded/sacred entry, make unlisted stricter, end an exception. Logged as a notice the person acknowledges |
| Any agent (`trust.propose`, `rook_trust action=propose`) | propose a change with a reason; checked when proposed; the person accepts or rejects it |
| `system` (the hub machine's CLI, `python -m rook.hub.trust seed`) | seed, recording whose words and who passed them on (`via`); such entries are `confirmed: false` until the person confirms them |

`trust.*` is in the relay FLOOR: nothing relays a change.

## 5. Session isolation (security card c_839e89992532, H6 question 5)

Role sessions ran under Bake's admin GitHub credential, master branch
protection had `enforce_admins` off, and only the session's own allowlist
stopped a push to master. Now:

1. **GitHub refuses master for everyone.** `enforce_admins` is on for
   `master` on Bake-Ware/rook (2026-10-23): pushes must go through a pull
   request, admins included. Pull requests need 0 approvals and no required
   checks, so `gh pr merge` still works. Probed live: a direct push of an
   empty commit to master was refused (GH006).
2. **The push guard** (`sessions.GUARD`, every session): the launcher writes
   a pre-push hook outside the worktree and points `core.hooksPath` at it
   through `GIT_CONFIG_*` in the session's environment. Any ref other than
   `refs/heads/<branch>` is refused. The allowlist already refuses `git -c`,
   `--no-verify` and extra push arguments.
3. **Minted tokens** (`rook/hub/session_tokens.py`), active once a GitHub App
   is configured (Settings > Roles: `session_github_app_id`,
   `session_github_installation`, vault secret `session_github_app_key`):
   each session gets an installation token for its one repository with
   `contents: write`, `pull_requests: write`, `metadata: read`; anything
   else (administration, secrets, workflows, ...) is refused, and an app
   that grants more is refused and the token revoked. Every mint is logged
   (`plugins/roles/session-tokens.jsonl`: session, role, repo, permissions,
   expiry, why, fingerprint; never the token). The session then runs under
   `bwrap` with the host's `~/.config/gh`, `~/.ssh`, `~/.git-credentials`,
   `~/.netrc` and Rook state masked, `--unshare-pid`, and git/gh using only
   the minted token. A configured app that fails to mint stops the session;
   it never falls back to the host's credentials.
4. **The ruleset (Bake, in the morning):** `contents: write` could still merge
   a pull request through the API, so a repository ruleset on `master`
   restricting updates to repository admins (bypass: Repository admin) keeps
   the app from merging. See the overnight page for the exact clicks.

Limits: installation tokens last at most an hour; a session that runs
longer loses push until it is resumed (a resume mints a new token).
Revocation at session end is not wired yet (tokens expire on their own).

## 6. Not built yet

Drift checks (stale or often-overridden entries), learning from repeated
approvals (proposals exist; nothing generates them automatically yet),
onboarding presets (locked down / balanced / YOLO), the borrowed-state
flows (B3), votes for guarded (B5), and `project:` selectors ruling on task
writes (they match only when a caller passes the project: `trust.check`).
