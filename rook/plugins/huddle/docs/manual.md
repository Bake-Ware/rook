# Huddles

A **huddle** is a group thread where agents and people work something out,
chaired by an agent: the house agent unless the huddle names another. It has
a **thread** (posts, plus status lines for pins, calls and outcomes) and a
**table**: artifacts pinned at exact versions. All of it is caps on worker
`rook`; put `huddle` and `github` in the hub's library (and `changes` for
deploys, `projects` for trust levels).

## Landing a pull request

The same flow as 0.4, with the same cap names:

1. **Pin.** Only a person, or the huddle's chair agent (`agent:house`), pins.
   An agent asks the chair: `huddle.post huddle=<id> text="@house please pin
   Bake-Ware/rook#326 at head <sha>"` (naming the chair sends it the post).
   ```
   huddle.pin huddle=<id> kind=pr repo=Bake-Ware/rook number=326 sha=<head>
   → {artifact, version, title, superseded}
   ```
   A `sha` that isn't the head is refused. Pinning the same PR again after a
   push supersedes the older pin; calls are on the newest.
2. **Call.** The author calls the merge with an adversarial self-review of the
   full diff (at least 80 characters: what you checked, what could break, why
   it is safe). It goes on the record and in the thread.
   ```
   huddle.vote huddle=<id> action=call kind=merge artifact=<artifact>
     reason="..." self_review="..."
   → {motion, state: gating, level, gates}
   ```
3. **Gates**, in the background: `version` (the head is still the pinned SHA),
   `mergeable`, `ci` (every check green; waits up to `gate_wait` while they
   run), `tests` (a check named like `test_checks` passed), `secrets` (nothing
   credential-shaped on the diff's added lines).
4. **Merge** through the `github` plugin with the hub's token, only at the
   pinned SHA: if the head moved, GitHub refuses and the motion is `void`.
   The commit title reads `Merge pull request #N from <branch> (vote <motion>:
   no vote, <level>)`.

Follow it with `huddle.vote action=status motion=<id>`, `huddle.table`, or on
GitHub. There are no ballots on 0.5 (sign-off replaces the merge-votes room),
so the self-review is always required.

## Trust

The huddle's `project` (set at `huddle.start`) gives the level
(`project.trust`; normal without one):

| Level | After the gates pass |
|---|---|
| `ship-it`, `normal` | carried out at once |
| `sacred` | one card to an owner (`huddle.carry` is a hard stop); yes runs the gates again, then carries it out; no ends it `declined` |

## Other calls

- `kind=qa`: gates version, ci, tests, secrets; then the `qa_label` label and
  a comment on the PR.
- `kind=deploy` (a pinned PR or commit): gates version, ci, tests, secrets;
  then `change.submit` **as you**, with `change` holding the change record's
  fields (scripts, tests, rollback, risk, plan). `{version}` in their args
  becomes the pinned SHA, and the huddle's project goes on it. The change
  record tests it and rolls back; in a sacred project its card is the one an
  owner gets.

## The table and the thread

- `huddle.table huddle=<id>`: the pins and calls. `action=read artifact=<id>
  what=summary|diff|files|file|checks|content` reads one at its pinned
  version, through the hub (you need no GitHub access of your own). Long text
  is paged: pass `next_offset` as `offset`.
- Kinds: `pr` (repo, number), `commit` (repo, sha), `file` (repo, path, sha),
  `doc` (text; version is its SHA-256), `link` (url; `sha256` if you have
  one, else `unhashed`; the hub doesn't fetch it).
- `huddle.post`, `huddle.read` (cursor `after`, `next`; `wait` long-polls),
  `huddle.start`, `huddle.close` (its creator or an owner), `huddle.list`,
  `huddle.get`, `huddle.search`, `huddle.motion.list|get|search`,
  `huddle.artifact.list|get`.

A hub restart ends open calls as `failed`: call again.

## Settings

| Setting | Default | |
|---|---|---|
| `chair` | `house` | the agent that chairs new huddles |
| `gate_wait` | `20m` | how long ci/tests wait for running checks |
| `poll` | `20s` | how often they look again |
| `require_ci` | `true` | no checks at all fails ci and tests |
| `test_checks` | `["*test*"]` | check names that are test runs |
| `merge_method` | `merge` | merge, squash or rebase |
| `qa_label` | `ready-for-qa` | |
