# GitHub

Pull requests, commits, checks, diffs and files from GitHub's REST API, for
the huddle's gates and table and for anyone the hub lets read them. Caps on
worker `rook`: `github.pr`, `github.commit`, `github.checks`, `github.diff`,
`github.files`, `github.file`.

`github.merge`, `github.label` and `github.comment` use the hub's token for
the plugins in `writers` only (the huddle), calling as themselves. A person
or agent can't call them, so nothing merges around the huddle's gates.
`github.merge` takes the head SHA and GitHub refuses if the head has moved.

## Setup

1. Make a fine-grained token on GitHub for the repos the huddle merges:
   Contents read and write, Pull requests read and write, Commit statuses and
   Checks read.
2. Store it in the vault (for example as `github-hub`) and set the `token`
   setting to that name. Without a token, only public repos can be read.

| Setting | Default | |
|---|---|---|
| `token` | none | the vault name of the token |
| `api` | `https://api.github.com` | the REST API base URL |
| `writers` | `[huddle]` | the plugins that may write |

The token goes in an unredirected header, so a redirect never carries it to
another host.
