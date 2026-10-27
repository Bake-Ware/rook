---
id: conference-table
title: The conference table and votes
order: 13
features:
  - settings/huddle
since: 0.2.0
deprecated: false
preview: true
---
# The conference table and votes

In a huddle with roles ([[manual-huddles]]), work can be put **on the
table** for everyone to read at the same version, and the hub merges,
labels or deploys it once its checks pass. You hear about it afterwards,
in the house agent's list of what it decided for you. Only shipping to
users and anything on a sacred target ([[manual-trust]]) ask you first.

## The table

The chair pins an **artifact** at an exact version:

| Kind | What | Pinned to |
|---|---|---|
| pr | a pull request | its head commit |
| commit | a commit | its SHA |
| file | a file in a repository | the commit it was read at |
| doc, tests | a document or test output, pasted in | a hash of the text |
| link | a screenshot or preview build | a hash of what the hub downloaded |

Every role in the huddle can read what is pinned through the hub: the
summary, the diff, the list of files, one file, the CI checks, and the pull
request's comments. They always read the pinned version, never their own
copy of the code, so everybody reviews the same thing. If the pull request
moves on after it was pinned, a read says so.

The huddle page shows a **Table** line under the title: open it to see what
is pinned (A1, A2, ...) and the votes on it.

Ask for it in the huddle: "pin PR 180 and get the team to look at it", or
"put the test output on the table". An agent can pin its own pull request.
From an AI client: `rook_huddle` `action=pin` and `action=table`.

Pinning a pull request also says where its CI stands (green, still
running, or failing), so nobody calls a vote on a red or unfinished run.
A vote can name the pinned pull request by A1, by its link, or by `#180`.

## Merging without a vote

Any agent can call a merge (or "ready for QA") of any pinned pull request.
Rook doesn't check who wrote it. The call has to include a hard-nosed
review of the whole diff: what was checked, what could break and why it is
safe (at least 80 characters; Rook checks only the length). The review is
posted in the huddle for the record.

### Checks first

Some things are facts, not opinions, so the hub checks them: the pull
request is still at the pinned commit, it merges cleanly, CI is green, a
test run passed, and the diff has no password or token in it. For deploys,
the rollback dry run must pass too. If one fails, nothing happens and the
huddle says why.

### The hub does it

When the checks pass, the hub does the work itself:

- **Merge:** the hub merges through GitHub at exactly the pinned commit,
  using a credential that sessions and roles never see (the vault secret
  named in **Hub GitHub credential**).
- **Ready for QA:** the hub labels the pull request `ready-for-qa` and
  comments.
- **Deploy:** the hub runs the target's deploy command. Targets are set in
  **Deploy targets**: a rollback dry run (the check) and the deploy command.
- **Ship to users** still asks you. Anything on a sacred target asks you.

Each one is recorded with the commit and the checks, and appears in the
house agent's list of what it decided for you ("Merged
bake-ware/rook#180 at 1a2b3c4").

## Votes (advanced security)

With the advanced security plugin on ([[manual-security]]), the chair calls
a blind **vote** of the roles before a merge, QA, deploy or ship. Only a
person or the chair calls one, after the same checks.

| Action | Required voters |
|---|---|
| merge | lead, QA, security, critic (or skeptic) |
| ready for QA | lead, QA, critic |
| deploy | release (or devops / SRE), QA, security, critic |
| ship to users | the deploy voters plus the product owner |

- Ship-it targets may skip the vote; guarded and unlisted targets always
  vote; sacred targets vote, and then wait for you.
- Whoever wrote the work never votes on it; the architect stands in for the
  lead. An empty seat counts as "couldn't verify", which counts as no.
- Ballots are sealed until everyone has voted, and count only for the
  commit they were cast on. A new commit voids the vote.
- Any required voter can block. The chair gets one more try (a second vote,
  or withdrawing it). If it is still blocked, you get one line with
  **Yes**, **No** and **Explain**, like the house agent's other questions.
- **Carry out after a passed vote** says which actions then go on their
  own: merges and ready-for-QA to start with.

## Settings

Settings > Huddles > Conference table and votes: on/off, the hub's GitHub
credential, the merge method, whether CI is required, which checks count as
tests, how long to wait for CI, and deploy targets. Required voters,
stand-ins and which actions go on their own after a vote matter only with
advanced security on.
