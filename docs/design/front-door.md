# The house agent as front door (B2)

Phase 3 of the house agent vision (Rook knowledge page `house-agent-vision`,
sections 1, 5 and 11): the house agent is Bake's personal assistant and the
front door of the band. Over voice, the fast front is a thin proxy for it
(docs/design/voice-front-background.md: the Background's `house_agent` tool
calls `home.ask`). Built 2026-10-23 (overnight track B, phase B2).

Code: `rook/hub/plugins/home/front.py` (routing, asks, disputes, digest),
`rook/hub/plugins/home/playbook.py` (the playbook), wired into
`home.ask`, the home agent's chat replies, the huddle chair
(`rook/hub/plugins/huddle/chair.py`), the relayed approvals list
(`rook/hub/relay.py`, kind `house`) and the phone's decision card
(`rook/hub/plugins/attention/service.py`). Dashboard: **Decisions**
(`rook/web/front.js`, through `/huddle/account-api` `front_*` actions).
Setting `home.front_door` (off by default; on for the live hub).

## 1. Request router

Every request that reaches the house agent goes through
`FrontDoor.handle(text, source, asker, principal, ...)`:

| Client | Entry | `source` |
|---|---|---|
| Phone voice (Background `house_agent`) | `home.ask` | `voice` (from the context) |
| MCP clients (`rook_home_ask`), other agents | `home.ask` | `ask` / given |
| Web chat panel (1:1 huddle with the house agent) | chair round, no roles | `web` |
| Chat rooms, Telegram/Discord bridged rooms | home agent's chat reply | `chat` |
| Huddles it chairs | the chair's close turn: `settle`, `ask_person` | `huddle` |

One model turn with the router tools: `do_action`, `dispatch_session`,
`open_huddle`, `continue_huddle`, `ask_person`, `check_trust`,
`playbook_edit`, `overturn`, `answer_ask`, `digest`, `waiting` (and
`knowledge_search` with `home.tools`). Its system prompt carries the
persona, the trust summary, memory, the whole playbook (rule ids to cite),
the roles, and its open asks.

Paths, and what the hub checks:

- **answer**: no action tool succeeded.
- **do** (`do_action`): the worker has the cap; the asker may call it under
  the band's policy (the house agent never does for someone what they could
  not do themselves); the trust map's ruling for the house agent's own
  principal (`home:<name>`, an agent: holds apply to it like any agent) is
  `go`; the action is reversible (not admin/destructive, not a removing cap,
  not `rm`/`dd`/`reboot`/force push...: `front.irreversible`); not a family
  device (`role.protected_workers`); and the playbook did not say ask first.
  Then it runs as the house agent (journaled `home.front.action`), and the
  digest gets a `did` decision citing the entry. Otherwise: an ask.
- **dispatch** (`dispatch_session`): `role.session_start` for
  `home.front_dispatch_role` (lead) through the roles plugin's own path
  (code locations, branch rules, push guard, minted tokens, report-back);
  the session's trust check (`session_trust`) turns a sacred or guarded repo
  into an ask.
- **huddle** (`open_huddle`, `continue_huddle`): `HuddleService.open_episode`
  starts a chaired huddle with the roles and posts the brief as the house
  agent; `Chair.begin` starts the round (a house agent post never starts a
  round through `on_post`; 1:1 huddles refuse `continue_episode`, so the
  front door cannot loop on itself).
- **ask**: anything above.

**Only the back claims actions are done.** The reply's first line is written
by the hub from tool results (`front.compose`): `Done: <summary>.` only for
an action whose call returned ok (not held, not denied, inner ok), `I've got
the team on it: <three-word name>.` after a session or huddle started, `I
need you for one thing: <line> (Yes / No / Explain: <name>)` after an ask,
`Not done: <summary> (<error>).` after a failure. A model reply that starts
with "Done" or claims an action when none ran is corrected ("Nothing has
been done yet. ..."). A read cap is an answer, never "Done".

## 2. Asks (Yes / No / Explain)

`front_asks` in `<home data>/front.db`: `{kind: action|dispute|gap|playbook|
remember, line (<=160 chars, one line), explain (2-3 sentences, cites the
entry), more, cite, action, change, state, ...}` with a three-word name.
They expire after `home.ask_ttl_hours` (72).

Delivered through the relayed approvals list as kind `house`, with `line`,
`explain` and `options` [Yes, No, Explain]: the phone's decision card
(attention) uses the line and explanation as they are; `rook_approvals`
lists and relays them; the dashboard's Decisions page answers them. The
relay rules hold unchanged (only a client the person enabled; their words;
FLOOR and `approvals.relay_dashboard_only` apply to the ask's call facts).
On the dashboard, a Yes to an action needs a sign-in that is not a band
secret (the same rule as approving holds).

**Yes** runs the action as the house agent; a hold its call meets for that
exact call (the trust map's) is approved with the person's decision
("Bake via phone (yes to house ask brave-otter-lamp)"), recorded with the
relay row. Then it **offers to remember** once: a `remember` ask that adds a
"Just do it" playbook rule, or (when a guarded trust entry made it ask) one
that files a trust proposal the person accepts on the Trust page. Sacred
entries are never offered. **No** does nothing.

## 3. Playbook

`<hub state>/plugins/home/playbook.md` (the house agent's home on the hub,
next to its memory): Markdown, one rule per line under fixed headings, each
with an id `[pb-xxxxxx]` and who set it. Sections: `do`, `ask`,
`dispatch`, `huddle`, `answer`, `rule`. History:
`playbook-history.jsonl`.

Direction: adding to `do`/`dispatch`/`rule` (act or decide without asking)
loosens, removing tightens; the reverse for `ask`/`answer`/`huddle`. The
house agent tightens on its own (applied, logged, a `tightened` digest
entry); a loosening becomes a `playbook` ask. A person edits the file
freely on the Decisions page (every change logged with its direction). The
trust map always wins: the playbook can only add asks.

## 4. Attention protection and disputes

`FrontDoor.settle(question, positions, cap+worker | repo+branch | project,
ruling, rule)` (cap `home.settle`, chair tool `settle` in close turns):

- the action is `go` under a named trust entry (ship-it), or the cited
  playbook rule is a `rule`/`do` rule and the map does not say ask: **ruled**,
  the ruling posted in the huddle with the citation, a `ruled` digest entry;
- sacred (`person`), guarded (`ask`), or nothing covers it: a `dispute` ask
  (gaps are queued in the list, never pushed as an interruption beyond the
  card the attention plugin shows for every list item).

## 5. Digest and overturn

`front_decisions`: `did`, `dispatched`, `huddle`, `ruled`, `tightened`, each
with its citation (trust entry + level + the person's words, exception,
playbook rule). `home.digest` returns a `spoken` line ("2 decisions I made
for you: ..."), the items and the open asks; the router has a `digest`
tool, so "what did you decide for me?" works by voice and from any client.

**Overturn** (dashboard button, `home.overturn` for a person or a relaying
client, the router's `overturn` tool on the person's words) only tightens:
a cited trust entry goes one level up (ship-it -> guarded -> sacred, as the
house agent's or the person's change, shown on the Trust page), a cited
loosening playbook rule is removed, and otherwise an "Ask me before: ..."
rule is added. The action is not undone.

## 6. Not built

- Proactive interjection (the back correcting the front mid-conversation)
  stays Track A's voice pipeline.
- Votes (H6, phase 5): guarded actions still ask the person.
- Asks are not yet urgency-classified; the attention plugin posts each list
  item as a normal card.
- Starting a project from a conversation and the borrowed-state ledger came
  with B3: docs/design/onboarding.md (router tools `note`, `offer_project`,
  `project_intake`, `project_setup`, `borrow`, `restore`, `ledger`,
  `record_baseline`; path `onboard`; ask kinds `restore`, `review`).
