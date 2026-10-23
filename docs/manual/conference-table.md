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

In a huddle with roles ([[manual-huddles]]), the house agent in the chair
can put things **on the table** for the roles to look at, and call a
**vote** when work is ready to merge, ready for QA, to deploy or to ship.
When every required role votes yes, the hub does it and tells you
afterwards. You only hear about a vote when the roles disagree, when the
trust map says the target is yours to decide ([[manual-trust]]), or when a
deploy still asks you.

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
"put the test output on the table". From an AI client: `rook_huddle`
`action=pin` and `action=table`.

## Votes

The chair decides when a vote is needed, and the hub checks that against
the trust map:

- **ship-it** targets may skip the vote (the checks below still run);
- **guarded** and unlisted targets always vote;
- **sacred** targets vote, but nothing happens until you say yes.

### Checks first

Some things are facts, not opinions, so they are checked before anyone
votes and are never voted on: the pull request is still at the pinned
commit, it merges cleanly, CI is green, a test run passed, the diff has no
password or token in it, and, for deploys, the rollback dry run passed. If
one fails, the vote stops there and the huddle says why.

### Who votes

| Action | Required voters |
|---|---|
| merge | lead, QA, security, critic (or skeptic) |
| ready for QA | lead, QA, critic |
| deploy | release (or devops / SRE), QA, security, critic |
| ship to users | the deploy voters plus the product owner |

Change these in **Settings > Huddles > Conference table and votes**.

- Whoever wrote the work never votes on it. If the lead's coding session
  wrote the pull request, the architect stands in. With no stand-in, the
  empty seat counts as "couldn't verify".
- Votes are **blind**. Each role votes in its own turn and its ballot is
  sealed: the huddle only shows "@qa has voted (sealed 3f2a91c0)". All the
  ballots are shown together once everyone has voted.
- A ballot counts only for the commit it was cast on. If a new commit lands
  during the vote, the vote is void, and the chair pins the new version and
  calls a new vote.
- "Couldn't verify" counts as no. A yes needs evidence, and the voter must
  have read the pinned version. The critic must name a specific risk, or
  say why there is none.

### When a role says no

Any required voter can block. The chair gets one try at sorting it out: a
second blind vote (when someone misread something), or withdrawing the vote
(the work needs changes). If it is still blocked, you get one line, for
example:

> Merge bake-ware/rook#180: Docs: a line? skeptic said no
> (Yes / No / Explain)

**Explain** gives each side in a couple of lines. It arrives like the house
agent's other questions: on the phone's decision card, on **More >
Decisions**, and in `rook_approvals`. Yes does it; No drops it.

## The hub does it

A vote that passes every check with every required voter saying yes is
carried out by the hub itself:

- **Merge:** the hub merges through GitHub at exactly the commit that was
  voted on, using a credential that sessions and roles never see (the vault
  secret named in **Hub GitHub credential**).
- **Ready for QA:** the hub labels the pull request `ready-for-qa` and
  comments.
- **Deploy, ship:** the hub runs the target's deploy command as the house
  agent. Targets are set in **Deploy targets**: a rollback dry run (the
  check) and the deploy command.

Each one is recorded with the vote, the commit and the checks, and appears
in the house agent's list of what it decided for you ("Merged
bake-ware/rook#180 at 1a2b3c4 after a unanimous vote (lead, qa, security,
skeptic)"). Overturn works as usual: it tightens the trust entry, it does
not undo the merge.

**Which actions go on their own** (setting **Carry out after a passed
vote**): merges and ready-for-QA. Deploys and ships still ask you after the
vote passes, until you add them to that list. Sacred targets always ask
you. A trust exception never removes a vote.

## Settings

Settings > Huddles > Conference table and votes: on/off, required voters,
stand-ins, which actions go on their own, the hub's GitHub credential, the
merge method, whether CI is required, which checks count as tests, how long
to wait for CI, and deploy targets.
