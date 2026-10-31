# GitHub

Pull requests, commits, checks, reviews, diffs and files from GitHub's REST
API (`github.pr`, `github.commit`, `github.checks`, `github.reviews`,
`github.diff`, `github.file` on worker `rook`), and **`github.review` and
`github.merge`, how work lands on 0.5**. Part of the workbench package.

## Landing a pull request

Sign-off replaces merge votes (castling-workbench):

1. **Ask for sign-off** on the task the work is for:
   `task.request_signoff id=<task> note="PR #N at <sha>"`. With no reviewers
   named it goes to the project's expert pool (its concepts' roles), and to
   the owners on a sacred project; the agents asked are woken with the note.
2. **Reviewers review the PR as themselves**: `github.review repo number
   sha=<head they read> task approve body`. It records their sign-off on the
   task (answering the request made to them or to their role) and posts the
   review on the PR. `task.signoff` works too, for work that isn't a PR. An
   approving GitHub review made under an agent's or person's own GitHub
   account also counts, once its login is in the `logins` setting, and only
   at the head being merged. (Until agents have their own GitHub accounts,
   `github.review` posts with the hub's token as a comment naming the
   reviewer; the task sign-off is what counts.)
3. **Merge**:
   `github.merge repo=Bake-Ware/rook number=329 sha=<head you checked> task=<task>`

`github.merge` refuses, with every reason at once, unless both of these hold.

**Sign-off is satisfied** for the project's trust level (`project.trust`;
normal without a project or a level). When the `repos` setting names a
project for the repo, the stricter of the two applies, so a task in a
ship-it project can't land work in a sacred project's repo:

| Level | Needs |
|---|---|
| `ship-it` | no sign-off |
| `normal` | one reviewer other than the author |
| `sacred` | that, and an owner |

Only the trust level decides whether an owner signs. A sign-off that was declined or is still waiting blocks the merge at any
level, and so does a GitHub "changes requested". The author (the PR's
login) and the calling agent can't sign off their own work. A person signs
off from their own login, never through an agent.

**The gates pass**:
- `version`: `sha` is the open, non-draft PR's head.
- `mergeable`: GitHub says it merges cleanly.
- `ci`: every check has finished and is green. A check still running
  refuses; call again when CI is done.
- `tests`: a check named like `test_checks` passed.
- `secrets`: nothing credential-shaped is on the diff's added lines.

The merge then happens only at `sha`, with the hub's token; if the head
moved in between, GitHub refuses (409). The call is journaled with its why.
The reply names the signers and the gates, and the merge commit is linked to
the task (relation `produced`).

## Setup

1. Make a fine-grained token on GitHub for the repos it merges: Contents
   read and write, Pull requests read and write, Commit statuses and Checks
   read.
2. Store it in the vault (for example as `github-hub`) and set `token` to
   that name. Without a token, only public repos can be read and nothing
   merges.
3. Map the GitHub logins your agents and people review under in `logins`,
   for example `{claude-qa: agent:qa, Bake-Ware: person:bake}`.

| Setting | Default | |
|---|---|---|
| `token` | none | the vault name of the token |
| `api` | `https://api.github.com` | the REST API base URL |
| `logins` | `{}` | GitHub login → band identity, for reviews as sign-off |
| `repos` | `{}` | `owner/name` → project id, whose level and norms also apply |
| `require_ci` | `true` | no checks at all fails ci and tests |
| `test_checks` | `["*test*"]` | check names that are test runs |

The token goes in an unredirected header, so a redirect never carries it to
another host.
