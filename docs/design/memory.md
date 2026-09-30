# Agent memory: the `memory` hub plugin and `embed.text`

Status: wave 3 of the beta refactor. Implemented in `rook/hub/plugins/memory/`
(hub plugin, off by default) and `rook/worker/plugins/embed.py` (worker
plugin that serves embeddings). Tests: `tests/test_memory_plugin.py`.

Related: [plugins.md](plugins.md) (the plugin contract, section 15 for
knowledge), [permissions.md](permissions.md) (tiers, Appendix A.2),
[settings.md](settings.md), `docs/web/worklog.md` (the `rook.transcript/1`
export), `docs/DESIGN-band-services.md` section 5 (the worker vault this
replaces).

## 1. What it is and is not

Agents keep **memory**: short, attributable statements that help the next
session avoid repeating the last one. The user's profile and preferences,
durable facts about the environment, procedures that were confirmed to work,
and one-paragraph summaries of past sessions.

It is separate from the **wiki** (`knowledge.*`):

| | Wiki (`knowledge`) | Memory (`memory`) |
|---|---|---|
| Written by | agents and people, deliberately | agents, continuously, under write rules |
| Unit | a page with a body, links, evidence | one statement, at most 600 characters |
| Scope | a band | a user, a band, or an agent family |
| Correction | new page with `supersedes` | new memory that supersedes; never an edit |
| Lifetime | kept until archived by hand | decays when unused; budgets archive the weakest |
| Reader | anyone searching the band | the next session, through a digest and recall |

A durable decision with evidence still belongs in the wiki. Memory is what an
assistant would jot down about the person and the setup.

## 2. Design lineage (Hermes Agent)

The upstream Hermes Agent (NousResearch/hermes-agent) was read for this
design. What it does, and what was taken:

- **Two bounded stores injected at session start** (`tools/memory_tool.py`:
  `MEMORY.md` for the agent's notes, `USER.md` for the user profile, 2,200
  and 1,375 characters). The snapshot is frozen for the session so the prompt
  cache holds; writes land on disk and show next session. Taken: a small,
  budgeted **digest** read at session start, plus per-kind **size budgets**.
- **Write guidance in the tool description**: save corrections, preferences,
  environment facts, conventions; do not save task progress, session
  outcomes or TODO state (recall those from transcripts); save procedures as
  skills. Taken almost verbatim as guidance slots, and enforced where code can
  enforce it (section 4).
- **Injection scanning** of anything that goes back into a prompt (prompt
  injection phrases, exfiltration via `curl $TOKEN`, invisible Unicode).
  Taken, extended with secret masking against the Rook vault.
- **Pluggable providers** (`agent/memory_provider.py`: `prefetch`,
  `sync_turn`, `on_session_end`, `on_pre_compress`). The *holographic*
  provider keeps a SQLite fact store with FTS5, **trust scores** adjusted by
  feedback, a temporal-decay half-life, and regex extraction of preferences
  and decisions at session end. Taken: confidence with reinforcement,
  half-life decay, hybrid retrieval, deterministic extraction from
  transcripts.

What differs: Rook memory is **shared across agents and hosts** (one hub,
many sessions), so it adds scopes, provenance, a propose/commit step for
low-confidence writes, embedding-based dedupe with supersede edges, and a
maintenance job, instead of relying on one agent's file.

## 3. Model

A memory row (`memory.db`, table `memories`):

| Field | Meaning |
|---|---|
| `kind` | `profile` (who the user is), `preference` (how they want things done), `fact` (the environment), `procedure` (a confirmed way to do something), `episode` (a session summary) |
| `scope_kind`, `scope_id` | `user:<id>`, `band:<id>` or `agent:<family>` |
| `state` | `pending`, `active`, `superseded`, `archived`, `retracted`, `rejected` |
| `confidence` | 0-1; set by the rules, raised by reinforcement |
| `reinforced`, `recalls`, `last_used` | usage, feeding decay |
| provenance | `author` (identity), `actor` (compound token.client.host@dir), `session` (MCP session or transcript session id), `journal` (evidence call id), `task` (claimed task), `source` (`agent`, `transcript:<worker>/<agent>/<session>#<range>`, `vault:postit:<id>`) |
| `supersedes`, `superseded_by` | correction edges |
| `reason`, `warnings`, `tags` | why pending/archived; rule findings |

Every state change is appended to `history`. The journal records the
`rook_call` that made the change (its reply carries the memory id), so
provenance runs both ways.

**Default scopes.** A write without a scope goes to the kind's home:
profile, preference and episode to `user:<default_user>`; fact and procedure
to `band:<default_band>`. `agent` scope is explicit (quirks that only matter to
one agent family). A read without a scope searches the caller's user, band and
agent family together. The agent family comes from the caller's token label
(`claude_gpubox` -> `claude`, the same rule the worker vault used).

Tokens are not linked to people yet, so `default_user` (setting, default
`operator`) stands for "the person this band serves". A caller may name
another user (`scope="user:alice"`). Per-scope access control is future
work; today the tier and the `sensitive` tag govern access (section 8).

## 4. Write rules

`memory.propose(text, kind, scope?, confidence?, confirmed?, supersedes?)`
runs the rules in `rules.py`, then dedupe, then commit or hold:

**Never saved (rejected, nothing stored):**

- empty text or an unknown kind;
- **secrets**: any vault value (the hub reads them for masking only, audited
  as one `mask` row) or a key/token shape (private keys, `sk-`, `ghp_`,
  `github_pat_`, `xox*-`, `AKIA`, JWTs, `password=...`, bearer tokens). With
  setting `secrets=mask` they are masked instead; ingest always masks;
- **prompt injection or exfiltration** payloads and invisible Unicode, since
  memory is read back into prompts;
- **code, diffs, stack traces, logs** (fenced blocks, or mostly code-shaped
  lines): derivable from the repository. Save the lesson in a sentence.

**Held for review** (confidence lowered, usually below the commit threshold):

- transient state: "currently", "right now", "in progress", "today", TODO
  and next-step lists, `/tmp` paths, PIDs, call ids (-0.3);
- pointers into code rather than facts: commit hashes, `file.py:123` (-0.2).

**Boosted:** corrections and explicit rules ("don't", "never", "always",
"instead", "remember", "prefer"): +0.15. `confirmed=true` (the user said so
explicitly) sets 1.0 and exempts the memory from decay and budget eviction.

**Starting confidence** by origin: agent 0.7, session summary 0.75, legacy
vault 0.8 (capstones 0.95), transcript extraction 0.5.

**Commit or hold.** At or above `commit_threshold` (0.6) the memory is
active. Below it, it is `pending` with a reason, visible in
`memory.list(state="pending")`, until `memory.commit(id)` (optionally with
corrected text, which creates a new memory) or `memory.commit(id,
reject=true)`. Pending proposals expire after `pending_ttl_days`.

**Dedupe and supersede, never edit.** Within the same scope and kind:

1. an identical normalized text reinforces the existing memory
   (`reinforced += 1`, confidence +0.05) and returns `duplicate`;
2. otherwise the text is embedded; cosine >= `dedupe_similarity` (0.92) is
   also a duplicate;
3. cosine >= `supersede_similarity` (0.8) supersedes the similar memory:
   the new one is active, the old one `superseded` with `superseded_by`.
   The reply lists `superseded`, so the agent can undo a wrong match;
4. without an embedding (no service, or it failed) the fallback is token
   Jaccard: >= 0.85 is a duplicate, and >= 0.5 supersedes only when the new
   text is a correction;
5. `supersedes=[id]` always applies, and an explicitly superseded row is
   never treated as a duplicate of its replacement.

**When to save** (guidance, not code): at the end of a task (what worked,
what the user confirmed), and immediately on a correction. The guidance
slots `memory.propose`, `memory.recall` and `memory.ingest` carry this as
`rook_call` tips; operators edit them on the Agent instructions page.

## 5. Recall and the digest

`memory.recall(query, scope?, limit=5, kinds?, include_archived?)` fuses two
rankings with reciprocal rank (as knowledge search does): FTS5 bm25 over the
text, and cosine similarity (>= 0.3) of the query's embedding against stored
vectors of the same model. Each fused score is weighted by the memory's
current strength (`0.5 + 0.5 * strength`). Returned active memories count a
recall (`recalls`, `last_used`), which slows their decay. Results are compact
rows (`id, kind, scope, text <= 400 chars, confidence, when, score`).

`memory.digest(scope?, max_chars?)` is the session-start briefing, built to
be tiny and deterministic: a header naming the scopes, then Profile,
Preferences, Procedures and Facts (strongest first, each line <= 200 chars),
then the three latest episodes, cut at `digest_chars` (1,200) with a
`(+N more: memory.recall)` line. It is also served as the MCP resource
**`rook://memory/digest`**, which costs nothing in `tools/list` (resources are
read on demand). A resource read carries no per-call attribution, so the
resource shows the default user and band scopes; call `memory.digest` through
`rook_call` to include your agent family's scope.

No MCP tools were added: the 12,000-character `tools/list` budget had about
120 characters left. Everything is a cap on worker `rook`.

## 6. Embeddings: `embed.text`

The hub never runs a model. The memory plugin's `embedder` resource defaults
to `cap://any/embed.text`, served by the worker plugin `embed`:

```
embed.text(texts=[...]) -> {"model": "...", "vectors": [[...]], "dim": N}
```

This is the knowledge plugin's wire shape (`{texts} -> {model, vectors}`), so
the knowledge plugin can use the same worker (`knowledge.embedder =
cap://any/embed.text`), and memory can use the HTTP embedding service
(`http(s)://.../embed`).

Worker plugin settings (worker scope, delivered by config push):

| Setting | Default | Meaning |
|---|---|---|
| `embed.mode` (`ROOK_EMBED_MODE`) | `auto` | `auto`: load only on workers whose facts report a GPU; `on`: wherever a backend is installed; `off` |
| `embed.backend` (`ROOK_EMBED_BACKEND`) | `auto` | `sentence-transformers` (PyTorch, GPU when present), then `fastembed` (ONNX, CPU; `pip install 'rook[embed]'`) |
| `embed.model` (`ROOK_EMBED_TEXT_MODEL`) | `sentence-transformers/all-MiniLM-L6-v2` | reported as `model` |

Neither backend is in the worker bundle; without one `available()` is false
and the cap is not announced, so build-167 and bundle workers are unaffected.
The model loads lazily on the first call, in a thread.

Placement is `not is_hub` with the GPU check in `available()` rather than
`place("has('gpu')")`, so an operator can turn a CPU box into the embedder
with `mode=on` instead of faking a GPU fact.

Memory stores one float32 unit vector per row and model, and compares only
vectors of the same model; `embed_model` (empty) can pin one. When the
service is down the embedder backs off for 60 s and everything degrades to
keyword matching; the maintenance job embeds rows that lack a vector for the
current model.

## 7. Maintenance, decay, budgets

`memory.maintain` runs on demand and every `maintain_interval` seconds
(3,600; 0 = off):

- **index** up to 32 rows without a vector for the current model;
- **expire** pending proposals older than `pending_ttl_days` (14);
- **decay**: `strength = confidence * 0.5 ^ (days since last use / half-life)`
  with `half_life_days` = `{"episode": 30, "fact": 180}` (kinds not listed,
  and confirmed memories, do not decay); below `archive_below` (0.15) a memory
  is archived. Archived memories are still found with
  `include_archived=true`;
- **consolidate**: active pairs in the same scope and kind that are
  duplicates (cosine or Jaccard) are merged: the stronger one is kept and
  reinforced, the other superseded;
- **episode cap**: the newest `episode_keep` (60) episodes per scope stay
  active;
- **budgets**: active characters per scope and kind (`budgets`, default
  profile 1,500, preference 2,500, procedure 4,000, fact 6,000, episode
  20,000); past it the weakest non-confirmed memories are archived. The same
  check runs whenever a memory becomes active.

## 8. Caps, tiers, settings

| Cap | Tier | Tags | |
|---|---|---|---|
| `memory.recall` | read | sensitive | hybrid recall |
| `memory.digest` | read | sensitive | session-start briefing |
| `memory.list` | read | sensitive | by scope/kind/state; `state=pending` is the review queue |
| `memory.show` | read | sensitive | one memory with provenance and history |
| `memory.status` | read | | counts, embedder state, thresholds |
| `memory.propose` | write | | the write rules (section 4) |
| `memory.commit` | write | | commit or reject a pending proposal |
| `memory.forget` | write | destructive | retract; `purge=true` blanks the text |
| `memory.ingest` | write | | transcripts to episodes (section 9) |
| `memory.maintain` | write | | run maintenance now |
| `memory.import_vault` | admin | | bridge the worker vault; also `require_hub_admin` |
| `embed.text` (worker) | read | | embeddings |

Reads are `sensitive` because they disclose personal data: policy can deny
`tag:sensitive` to bridges, and the hub does not journal their replies for
band calls. Band callers reach only read caps (the hub's band risk ceiling).

Settings are on the Settings page under **Memory** (the hub keys share the
page with the worker vault's `memory.notes_dir`) and in
`docs/operations/settings-reference.md`. `enabled` (`ROOK_MEMORY`) and
`db_path` (`ROOK_MEMORY_DB`) are read at start; the rest are live.

## 9. Ingest

### Work-session transcripts

`memory.ingest(worker, agent, session_id)` pages `work.export` on that worker
over the band (`rook.transcript/1`, docs/web/worklog.md) until
`next_offset` is null; `memory.ingest(transcript=[pages])` takes pages
directly. For each session:

- one **episode** in the user scope, summarized by the `summarizer` resource
  when set (`cap://` or `http(s)://`, taking `{format, session, messages,
  max_chars}` and returning `{summary}`), else **extractively** with no
  model: title, agent, project directory and date; the first request; the
  last request; how it ended. Deterministic, so tests pin it;
- **candidates** from what the user said: preferences ("I prefer", "from now
  on", "always/never"), corrections ("no, ...", "don't", "instead") and
  decisions ("we decided"). They are proposed with transcript origin and held
  **pending** (unless `ingest_autocommit`), for an agent or the operator to
  commit;
- secrets are masked, never rejected, since transcripts quote them.

Ingest is idempotent on `(worker, agent, session_id)` and the last message
index: re-running it is a no-op, and a session that grew gets a new episode
that supersedes the previous one.

### The worker `memory.*` vault

`memory.import_vault(path=...)` reads a vault directory on the hub
(read-only; default: setting `legacy_vault`); `memory.import_vault(worker=...)`
fetches post-its from a worker running the vault plugin with
`memory.search`. Post-its become facts (`decision`, `fact`, `change`,
`capstone`; questions are skipped), scoped `agent:<author namespace>` or
`band` for `shared`, with their original timestamps and supersede edges;
capstones get confidence 0.95. `entities/*.md` notes become band facts.
Re-running skips what was imported (keyed by `vault:postit:<id>`).

**Retirement path for the worker vault.** The old caps
(`memory.search/get/put/note/entities` on the vault's worker) are unchanged
and keep working; the hub plugin deliberately uses other cap names in the
same namespace, so a `rook_call` to either is unambiguous. To retire:

1. enable hub memory (`ROOK_MEMORY=1`) and an embedder;
2. run `memory.import_vault` (path or worker); repeat until agents have
   switched, since it is idempotent;
3. point agents at `memory.digest`/`recall`/`propose` (the skill fragment and
   guidance already do);
4. clear `memory.notes_dir` (`ROOK_MEMORY_VAULT`) on the vault worker. The
   plugin unloads there; the directory stays on disk as an archive (and
   Obsidian can still open it).

The worker plugin will be removed from the bundle once no fleet worker sets
`ROOK_MEMORY_VAULT`.

## 10. Storage decision

Memory has **its own tables** (`memory.db` in the plugin's data directory,
migration `001_schema.sql`, namespace `memory` in `_rook_migrations`), not
the knowledge store, and does not `DEPENDS` on knowledge:

- knowledge is off by default; memory must work without it;
- the knowledge store is band-scoped wiki pages with revisions, slugs,
  claims and verification; memory rows need user/agent scopes, confidence,
  decay and budget states that would leak into wiki search and hygiene if
  they were records there;
- privacy: profile memories should not appear in band-wide wiki search;
- vectors differ: knowledge keeps one configured model; memory tolerates a
  model change per row.

What is shared is the **embedding contract** (`{texts} -> {model, vectors}`)
and the hub's `cap://` resource caller, so one embedding worker serves both.

## 11. Compatibility

- Wire: nothing changes. `embed.text` is a new worker cap announced only
  where a backend exists; the hub's memory caps are announced by worker
  `rook` like any hub cap. Build-167 workers are unaffected.
- `rook.core` is untouched apart from the built-in tier table.
- The vault gains `Vault.mask_values()` (one audit row per call; values are
  cached for five minutes by the plugin and never returned to callers).
- MCP: no new tools; one resource `rook://memory/digest` when the plugin is
  enabled. Hub plugins can now return MCP resources through
  `mcp_resources()` (`rook.hub.mcp_tools.register_plugin_resources`).
