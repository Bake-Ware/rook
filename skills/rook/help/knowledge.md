---
title: The knowledge wiki
pitch: Look up or record durable facts, decisions and procedures, with evidence
tools: [rook_knowledge]
caps: [knowledge.]
refs: [references/coordination.md]
notes:
  - rook_knowledge: Search before starting (data.limit 3-5) and try a second phrasing before concluding nothing exists; get only the page you will use.
  - rook_knowledge.create: Search twice first; if a page on the subject exists, update it (get, then update with revision). One page per subject, named for the subject, not the date. Cite evidence; never a secret value.
  - rook_knowledge.update: Pass the current revision. Correcting a wrong fact? Create a new page with attrs.supersedes=[old] instead, so readers see the history.
---
# The knowledge wiki

**Use it when** you learn something that will still be true next week: how a thing is set up, how to do X, why we chose Y, a gotcha, where something lives. Before starting a job, search it.

## How

- `rook_knowledge(action="search", query=…, data={"limit": 3})`; try a second phrasing before concluding nothing exists. `get` only the page you will use.
- About to work on one machine? `rook_knowledge(action="context", data={"worker": …})` lists open tasks and recent pages tagged with it (`attrs.workers`).
- Write: `create` with `title`, `body`, `parent` (a folder page) and `attrs.knowledge_kind` (`fact`, `decision`, `procedure`, `observation`, `question`, `summary`); link related pages with `[[slug]]`.
- Cite: `rook_knowledge(action="link", id=…, data={"kind": "journal", "ref": …, "relation": "evidence"})` (also `commit`, `console`, `file`, …).

## Hygiene

- **Search before creating**, twice, with different words. Update the existing page instead of creating a sibling; slugs ending `-2`, `-3` mean someone skipped this.
- **One page per subject** (a host, a service, a repo, a procedure, a decision), named for the subject ("GPU box: drivers and services"). Event detail (one deploy, one debugging night) goes in the task outcome, the console summary or a handoff; fold only the lasting lesson into the subject page.
- **Durable vs ephemeral.** Not knowledge: what you are doing now (task), where you stopped (handoff), raw output (journal, console), chit-chat (rooms).
- **Verify honestly.** `attrs.verification="verified"` only for what you checked yourself and linked; a URL alone never verifies. A person's verify resets if you edit the body later.
- **Correct a wrong fact** with a new page carrying `attrs.supersedes=[old]`; extend a right one with `update`. A `disputed` page has `attrs.dispute_reason`: fix that.
- **Never store** a secret value, a band key or a token. Refer to credentials by vault name.
