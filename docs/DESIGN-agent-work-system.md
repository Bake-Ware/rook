# Agent work system: knowledge, tasks, links and hygiene

Status: proposal for Bake's review (2026-09-23). Nothing here is built yet.

## Purpose

Work now spans several providers (Claude Code, claude.ai, Codex, Hermes), and
each keeps its own context. Global understanding has to live outside all of
them. Rook's knowledge and task records are that shared layer:

- **Agents** write and maintain it, tracking their own work across the team.
- **The user** observes and directs: "what's on deck for X?", "pick up Y".
  The user rarely edits records directly.
- **Tasks** are a durable record of work that was done, is being done, or is to
  be done. Each records its outcome and **who did what** as an audit trail.
- **Knowledge** replaces the older memory stores as the central memory layer.

Non-goals: nothing here authorizes, blocks or gates band calls. The validation
rules below apply only to saving records, never to executing work.

## 1. Identity (from the token, never from arguments)

Every actor is derived from the authenticated request:

| Field | Source | Example |
|---|---|---|
| `key` | API token label (`static` for the shared token) | `claude code` |
| `key_id` / `agent_id` | token store (stable across rotation) | `a41d…` / `agent_3f9c…` |
| `client` | MCP `initialize` → `clientInfo.name`, normalized | `claude-code`, `claude-ai`, `codex`, `hermes`, `unknown` |
| `host` | `X-Rook-Host` header; **`web`** if absent or unreadable | `cachyrig`, `web` |

- The **actor id** is `key_id` for named keys. For the shared static token it's
  derived from `(static, client, host)`, so different agents sharing the
  static key are still told apart.
- Records store the full structured actor. The display string stays
  `agent:<key>_<host>` for chat and worker audit logs, which are keyed on it.
- No write can claim to be another actor. Background jobs use a fixed
  `system:<job>` actor that agents can never pass in.
- To verify: that `clientInfo.name` is readable from the session in the prod
  SDK (1.27.1), and what each real client actually sends.

## 2. Records and a loose wiki

The record kinds stay the same:

- **concept:** why.
- **project:** what outcome.
- **task:** a unit of work.
- **knowledge:** what we learned.

Every record also becomes a wiki page:

- **`slug`:** unique per band, human-readable (`band-audit-attribution`).
- **Body:** markdown. `[[slug]]` links any record to any other.
- **Backlinks:** computed on read ("what links here").
- **Page view:** the site shows a record as a page with body, backlinks,
  linked artifacts, history and state. One view serves the human and the agent.
- **Fetch by slug or id:** `rook_knowledge(action="get", id="band-audit-attribution")`.

Knowledge hygiene:

- **Fact status:** `unverified` (default) → `verified` or `disputed`.
- **Verification needs evidence:** a fact can only be marked `verified`
  with at least one **traceable evidence link** (§3): a journal call, commit,
  file on a worker, handoff, console room or another record. A URL alone
  doesn't count.
- **Corrections supersede, never overwrite.** The old page stays readable with
  a "superseded by" banner.

## 3. Links: the audit trail

A new table ties any record to anything it relates to:

```
links(id, band, record, kind, ref, relation, note, actor, ts)
```

| kind | ref example |
|---|---|
| `journal` | journal call id |
| `console` | console room id |
| `handoff` | handoff thread/version id |
| `chat` | room id (+ seq) |
| `file` | `worker:path` |
| `commit` | `repo@sha` |
| `agent` | actor id |
| `record` | another record id/slug |
| `url` | URL |

Relations: `produced`, `evidence`, `touched`, `discussed_in`, `blocked_by`,
`duplicates`, `supersedes`.

- **Manual links:** `rook_task(action="link", id=…, data={kind, ref, relation, note})`,
  and the same on the other record tools.
- **Automatic links** do most of the work:
  - While an agent (actor id) holds a **claim** on a task (§4), the hub links
    that actor's `rook_call` journal entries, console rooms and handoffs to the
    task as `touched`. The audit trail builds itself; it doesn't depend on
    agents remembering.
  - An actor has at most one active claim per session, so there's no guessing.
  - Automatic links are marked `auto`. Agents can relabel them
    (e.g. `touched` → `evidence`).
- Links are append-only. "Removing" one adds a retraction, so the trail stays intact.

## 4. Tasks: claims, states, outcomes

Task states: `todo` → `in_progress` → `done`, plus `blocked`, `paused`,
`cancelled`, `archived`.

- **Claim:** `rook_task(action="claim", id=…)` records the claimant (actor),
  when the claim started, and optionally the agent's provider session id and
  worker (§5).
  - Claims **record** who's on it. They never prevent another agent from
    working.
  - A second claim on a claimed task succeeds, and both claimants are shown.
- **Activity:** the time of the claimant's latest auto-linked activity. The
  deck shows it as "last active 12m ago".
- **Saving rules** (validation on the record, not on execution):
  - `done` needs an **outcome** summary and at least one `evidence` link.
  - Leaving `in_progress` for anything other than `done` (`paused`,
    `blocked`, or dropping the claim) needs a **handoff**: either a new
    `rook_handoff_save` linked to the task, or one passed inline.
  - `blocked` needs a `blocked_by` link or a reason.
- **Deck:** `rook_task(action="deck", project=…)` is the one call behind
  "what's on deck?". It returns, per project:
  - in progress: claimant, last active, latest handoff;
  - blocked: with reasons;
  - todo: in priority order;
  - recently done: with outcomes.

## 5. Hygiene trigger (a deterministic nudge for a nondeterministic process)

Most real work happens on workers, where the hub can see activity stop.

- **A task is dirty** when all of these hold:
  - it is `in_progress` with a claim;
  - the claimant has been idle longer than **N minutes** (default 30,
    editable);
  - and either there is activity since the last handoff, or there's
    unlinked or unrecorded outcome evidence.
- **The nudge,** once per idle period per task, tries these in order:
  1. The claim recorded a provider session and worker that support
     `<client>-history.send` → send the hygiene prompt **into that same
     session**, so the agent that did the work, with its context, writes it up.
  2. The worker has `agent.wake` (or `hermes.chat`) → start a fresh agent with
     a brief built from the task, its links and the latest handoff, and ask it
     to write the handoff and knowledge from the journal trail.
  3. Otherwise → mark the task `needs hygiene` on the deck and post a chat
     notice. The next agent that touches it sees the flag.
- **The prompt** is a new editable Agent instructions slot (`hygiene`). It asks
  the agent to:
  - save a handoff;
  - record outcomes and evidence links;
  - capture durable facts as knowledge pages;
  - update the task state.
- **Safety:** the trigger never changes task state itself. Each nudge is
  recorded as an event and link on the task. It's rate-limited per task and
  per worker.

## 6. Memory migration into knowledge

Sources:

- `memory.*` caps (`memory.search/get/entities`, one worker);
- Sojourn Hermes memory (`hermes.memory.read`, `hermes.sessions.*`);
- the legacy vault that Codex's import used (read directly, not trusted
  through its records).

The migration is a one-off job run by a subagent, after §2–§3 exist:

- Read each source and dedupe.
- Write knowledge pages as `unverified`, each with a `url`/`record`-style
  **source link** to its original id, and the importing actor recorded.
- Afterwards, `memory.*` either writes through to knowledge or is retired
  (decision for later).

## 7. Order of work

Each step ships separately, with tests and the usual rollback:

1. **Identity:** key + client + host, structured actor on writes, the
   derived actor id for the shared token.
2. **Links:** the table, manual `link`, and auto-linking from claims.
3. **Tasks:** claims, the new states, the saving rules, `deck`.
4. **Wiki:** slugs, `[[links]]`, backlinks, the page view on the site.
5. **Hygiene trigger** with the editable prompt.
6. **Memory import** by a subagent.

Before step 1, the existing `knowledge.db` rows (all written by Codex on
2026-09-22, mostly under invented `system:*` actors) are deleted. A copy stays
in `/var/backups/rook/audit-20260922-f279a47/knowledge.db`.

## Open questions

1. Are two hosts on one named key the same agent (one actor id) or two? The
   proposal says one (the key is the agent), with host kept as a separate field.
2. Idle timeout default (30 min?), and whether it's set per project.
3. Should the deck be scoped per band, or cover all bands by default?
4. Should `memory.*` write through to knowledge, or be retired, after import?
