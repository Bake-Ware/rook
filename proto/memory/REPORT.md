# Memory prototype: first report (MEM1)

## What I built

A small Python package, `proto/memory/rook_memory`, with a command line. It uses only the standard
library and is not wired into anything: not Rookie, the hub, a plugin or the beta. It reads closed
Claude Code session files and keeps three kinds of memory in one sqlite file
(`~/.rook-memory-proto/memory.sqlite3`, 684 KB after this run).

- **Scratch pad.** Everything said and done in a session, word for word: the person's messages, the
  agent's messages, each tool call, and the start of each tool's output (cut at a knob's length).
  Each entry keeps its line in the source file. It stays until a dream pass condenses it.
- **Episodes.** The model reads a session in stretches of about 40,000 characters and writes down
  what happened, in plain sentences. Each episode carries:
  - when it happened;
  - when it was last read;
  - an importance from 1 to 10, given by the model;
  - the session file and the lines it came from.
- **Beliefs.** A dream pass writes these. It works in three steps:
  1. It looks at the episodes it has not dreamed over yet and asks itself the five most salient
     questions about them.
  2. For each question it pulls up memories the way a turn would.
  3. It writes beliefs from them. Each belief has a confidence (0 to 1), the episodes and beliefs
     that support it, and, if it contradicts or refines an older belief, a link to that belief.

  The old belief is kept and marked superseded, never deleted. Beliefs written for one question can
  be pulled up, and superseded, by the next question. After a dream, the scratch pad of every session
  but the most recent is dropped. The episodes still point at the source lines.

A dream pass starts by itself once the importance of the undreamed episodes adds up to 150, as in the
paper, and the `dream` command starts one by hand.

**How a turn's memories are chosen (`ask`).** Every episode and standing belief gets a score made of
three parts, as in the generative agents paper:

- **Recency:** 0.995 to the power of the hours since it was last read.
- **Importance:** the model's rating.
- **Relevance:** how many words it shares with the question.

Each part is scaled to 0..1 across all the memories, then the three are added, each times a weight.
The top 10 go to the turn. The scratch pad is ranked the same way, and its top 8 go to the turn as
well. `ask` prints every part of every item's score and the words that matched, so you can see why
each one ranked.

**Relevance is BM25**, a classic way of scoring shared words in which rare words count more than
common ones. I chose it for four reasons:

- it runs locally with nothing to install or download (the disk is 96% full);
- it is instant;
- it shows exactly which words matched;
- what people ask the house agent about is mostly names (PR 397, the Galaxy Watch, cachyrig,
  net.http), and those match word for word.

It is poor at anything said in other words; see below.

**Model calls.** Every model call goes through Claude Code headless (`claude -p`), the same way the
house agent's turns do. Each call runs with no tools, no settings, no MCP servers and no saved
session, in an empty folder, with no API key. Episodes use Sonnet and dreams use Opus. Answers come
back held to a JSON shape by Claude Code's `--json-schema`.

**Knobs.** All of them are in `knobs.toml`, each with a comment. The file covers:

- the models;
- the stretch size;
- how much of tool input and output is kept;
- the dream threshold, how many questions, memories and beliefs a dream uses, and how many recent
  sessions keep their scratch pad;
- the recency decay and the three weights;
- how many items a turn gets;
- every prompt.

**Tests.** Ten tests cover the plain mechanics: the store, scoring, supersedence, condensing, reading
session files, and the dream's wiring with a stand-in model. They do not test model output.

## What went in

Four closed Rook agent sessions, read only, taken in from oldest to newest. Only two closed sessions
exist in the `core-0N` folders; the others there are still being written. So I added two closed Rook
sessions from `~/.claude/projects/-home-bake-rook`.

| # | Session | When | Lines | Episodes |
|---|---|---|---|---|
| 1 | `-home-bake-rook/29b8e881` (the watch "live conversation + colored icon" work) | 27–28 Oct | 2,266 | 34 |
| 2 | `-home-bake-rook/91efae29` (Castling cap inventory) | 30 Oct | 399 | 8 |
| 3 | `core-01/2899dc49` (core briefs 01, 05, 07, 09, 11) | 12–13 Nov | 3,415 | 66 |
| 4 | `core-03/359e18c3` (core briefs 03, 06, 08, 10, 12) | 12–13 Nov | 2,536 | 40 |

Three dream passes started by themselves (after sessions 1, 3 and 4). Together they made 49 beliefs,
and 10 of those superseded an older one. Of the 148 episodes, most were rated 4 to 6, 5 were rated 8,
and none 9 or 10. Confidence ranged from 0.55 to 0.90, averaging 0.75. Every belief names at least one
support, 3.5 on average.

## Samples from the run

### Ten episodes

- **[e1]** 27 Oct 12:04, importance 8, session 1 L6. Bake asked by voice for two watch changes: show
  the conversation live like the phone app, and replace the inconsistent listening/not-listening text
  on the talk button and tile with a small Rook icon whose color follows state (idle, listening,
  thinking, speaking, error) from one source of truth. Constraints: worktree watch-convo, don't touch
  services/voice, no Codex, merge only by hub vote, install over wireless adb, ship a beta APK if the
  phone side changes, no cards or asks for Bake.
- **[e12]** 27 Oct 12:29, importance 7, L862–895. The agent opened PR #236 (feat/watch-convo): the
  watch shows a colored Rook icon driven by one state and a live scrolling conversation, and the phone
  sends the watch the last 20 chat lines. 15 files (+459/−76).
- **[e29]** 27 Oct 14:59, importance 7, L1776. The session had been killed about 09:40 when cachyrig's
  Rook worker updated itself to build 1171. It was asked to re-check and make sure the watch app is
  installed and works with the phone's beta.
- **[e35]** 30 Oct 15:16, importance 8, session 2 L6. Bake asked for a first-pass inventory of all
  457 Rook caps for Castling: keep, reshape or bin, with home, form, 0.5 name and reason. Guiding
  principle: caps must be ergonomic for agents.
- **[e37]** 30 Oct 15:25, importance 7, L251–282. The decision table came out at 164 keep, 83 reshape
  and 210 bin. The 247 survivors become 261 names in 59 namespaces.
- **[e74]** 12 Nov 23:49, importance 7, session 3 L1729–1744. The reviewer approved and merged PR
  #390 into core (efe5f557) with no changes; the Claude Code manual check was the evidence needed.
  Next: brief 07.
- **[e88]** 13 Nov 00:51, importance 8, L2526–2530. Review of PR 394 required two changes. The
  scrubber was measured at 1.12 s per million characters with the hub lock held and had to be made
  fast, and origin/core had to be merged.
- **[e122]** 12 Nov 23:17, importance 6, session 4 L885–908. Found 15 caps declared in the ESP32-S3
  firmware's C code that the Python scan had missed. The 0.4 count went to 484.
- **[e127]** 12 Nov 23:19, importance 6, L1021–1047. Opened PR #389, the inventory of 0.4 (484 caps)
  and 0.5 (407 caps) against PLAN.md. Raised for Bake: P4 says "eight" core pages but names seven.
- **[e140]** 13 Nov 02:25, importance 7, L2321–2325. The reviewer (Claude, steward of PLAN.md for
  Bake) required four changes on PR 397:
  - net.http accepts only http and https;
  - net.tcp with wait 0 sends without reading;
  - a job can call a device;
  - merge origin/core and run the suite once under /home.

The episodes are good: specific, true to the source, with numbers and PR numbers, and each points at
its lines.

### Beliefs (a selection; `show beliefs` lists all 49 with their support)

- **[b11]** confidence 0.85, supported by e27, e29, e31, e33 and b5. Watch app 0.2.1 never reached
  Bake's Galaxy Watch, for two reasons:
  - wireless debugging on the watch has been off, and the watch is often Bluetooth-only with an IP
    that keeps changing;
  - separately, the first background installer died when the worker updated itself to build 1171,
    leaving an empty install.log.
- **[b13]** 0.75. cachyrig's auto-install window for the watch ended on 30 Oct 23:59. To get 0.2.1 on,
  Bake has to turn on wireless debugging, re-pair if adb shows offline, and run `adb install -r` from
  cachyrig.
- **[b20]** 0.85, superseding b10. Worker self-updates on cachyrig kill agent sessions and background
  installers with no sign other than an empty install.log. After any update, re-check the state
  yourself.
- **[b27]** 0.75, from e35. Bake's main design rule for 0.5 caps: easy for agents to use. That means
  fewer, clearer, real caps, no action arguments, and consistent verbs.
- **[b35]** 0.90, 7 supports, superseding b23. The core rebuild goes one numbered brief at a time:
  - branch from origin/core;
  - open exactly one PR, with a manual check and real-disk timings;
  - then stop until the reviewer merges.
- **[b41]** 0.65. The /home disk on cachyrig was 99% full as of 13 Nov.
- **[b43]** 0.80. The four changes required on PR 397, as in e140.

### What superseded what (all 10)

| Old | How | New | Was it right? |
|---|---|---|---|
| b4 (0.80) | refines | b12 (0.85) | Yes: "merged, not installed" became "only the watch install is left". |
| b5 (0.75) | refines | b11 (0.85) | Yes: a vague "watch often unreachable" became the two actual causes. |
| b7 (0.65) | refines | b22 (0.80) | Yes: adds "198 tests pass once the ignored inputs are copied". |
| b10 (0.70) | refines | b20 (0.85) | Yes. |
| b23 (0.90) | refines | b35 (0.90) | Mostly. It adds the evidence rule, but also "only the *human* reviewer merges", which is wrong (below). |
| b25 (0.70) | refines | b30 (0.80) | Yes. |
| b26 (0.80) | refines | b36 (0.80) | Half. Still lists only 3 of the 7 merged core PRs the episodes record. |
| b28 (0.65) | refines | b42 (0.60) | **No.** A true belief about the Castling inventory was replaced by "memory has no record of the 0.4/0.5 inventory against PLAN.md", which is false: e113, e122 and e127 hold it. |
| b30 (0.80) | refines | b44 (0.70) | Half. It adds what review checks, but drops the list of merged PRs that b30 had. |
| b33 (0.70) | refines | b39 (0.85) | Yes. |

There was no "contradicts" at all: the sessions never reversed an earlier fact. To test contradiction
we need material where something really changed, such as a decision Bake made and later took back.

### Five questions and what came back

Each was asked with `ask --peek`, which leaves the last-read times alone. Scores are recency +
importance + relevance, each 0..1.

1. **"Why is the watch app still not installed on Bake's Galaxy Watch?"** Good.
   - Long-term: b11 (2.91, the two causes), e29 (the update that killed the session), b17, e1 (the
     original ask), b21 (how to fix it).
   - Scratch pad: only session 4's detail (core work) is left. It offered net.tcp lines that matched
     on "s" and "still", which is noise.
2. **"How does a core PR get merged, and who merges it?"** Right shape, one wrong fact.
   - Long-term: b32 (3.00), b35, b39, then the episodes of PRs #390 and #394 being merged.
   - Scratch pad: the word-for-word "PR 395 (plugins) is approved and merged into core as 40a1cdd5".
   - The wrong fact is in b32, which says "merged only by the human reviewer"; the reviewer is Claude,
     the steward.
3. **"What did the inventory of Rook 0.4 and 0.5 find against PLAN.md?"** Bad.
   - The top answer is b42, "memory has no record of it" (2.67).
   - Next come the *Castling* inventory (e35) and unrelated beliefs.
   - The episodes that answer it (e113: 48 Not covered, 38 Covered…; e127: 484 and 407 caps) are not
     in the top 10.
   - The scratch pad did better: its second item is the assembler output for docs/inventory.md.
   - With the recency weight set to 0 (`--knobs` with `weight_recency = 0.0`), e113, e109 and e127
     come in at places 2, 3 and 5.
4. **"What did the reviewer ask to change on PR 397?"** Mixed.
   - The top answer is b46, "memory has no record of how the PR 397 changes were done" (wrong: e141,
     e143 and e146 have them).
   - Then b48, then the right episode e140 and belief b43.
   - The scratch pad's top two are the word-for-word re-review and the review itself, which are the
     best answer of all.
5. **"How full is the disk on cachyrig?"** Good.
   - Long-term: b41 (99% full as of 13 Nov) first.
   - Scratch pad: the line where it was noticed ("/home is a real disk; it's 99% full").

## What looked wrong or thin

1. **The dream's own reading makes memories look fresh, and that hides the newest session.** The
   paper counts every retrieval as a read and resets recency. Here the dreams ran minutes apart over
   sessions weeks old. So by dream 3:
   - everything dreams 1 and 2 had pulled up, and every belief, had recency 1.0;
   - session 4's episodes still had the recency of 12 Nov, about 0.4.

   The dream's question about the inventory therefore never saw the inventory episodes. It wrote
   "memory has no record of it", superseded a true belief with that, and the false belief now ranks
   first for that very question. It feeds itself.

   Part of this comes from feeding weeks-old sessions in six minutes; with the house agent's own
   sessions closing as they go, the clocks would be closer. Still, the mechanism is real.
2. **Beliefs about memory's own gaps.** 5 of 49 beliefs are mainly "memory has no record of…" (b31,
   b37, b42, b46, b47), and two more end that way (b28, b33). They are true of what the dream happened to pull up, not of memory, and they
   outrank the real answers later. The questions step asks about specifics (by name: "net.http
   redirect safety", "blob records") that the retrieval step then fails to find.
3. **Who is who.** The model cannot tell Bake from the steward or reviewer relaying Bake. It wrote "the
   human reviewer merges core PRs" (b24, b32, b35), and b48 even says "the human reviewer (Claude,
   steward of PLAN.md for Bake)". That contradiction was never caught. It also guessed a pronoun for Bake ("He also wants…" in b1).
4. **Refining can lose detail.** A refined belief replaces the old one in retrieval (superseded beliefs
   are kept but not retrieved, a knob). b44 dropped the merged-PR list that b30 had.
5. **Word matching is crude.**
   - "installed" does not match "install".
   - "0.4" also counts as "0" and "4".
   - The "s" of "Bake's" counts as a word.
   - Nothing said in other words matches at all.

   It worked when questions used the same names as the memories, which was most of the time.
6. **Thin stretches.** With the answer held to a JSON shape, two stretches gave a single episode
   (session 1 L1882–2225 and session 4 L1502–2005). In the first run, without the JSON shape, they gave
   7 and 12. Without the shape, though, 2 of 20 replies came back as broken JSON and their stretches
   were lost entirely. So the shape is on, and this is the cost.
7. **The scratch pad has nothing near the question's time.** Only the newest session keeps its scratch
   pad, so a question about the watch got core-work detail. Its importance is a flat 5 for every entry.

## What it cost

Each run made 38 model calls, one after another, and took 6 minutes 22 seconds from start to end. The
cost figures are what Claude Code reports at list price; on this machine's subscription they are
usage, not a bill.

| | Calls | Model time | List cost | Tokens in / out |
|---|---|---|---|---|
| Episodes (Sonnet) | 20 | 3.4 min | $1.68 | 437,000 / 26,000 |
| Dreams (Opus) | 18 | 3.0 min | $0.93 | 88,000 / 15,000 |
| **Total** | **38** | **6.4 min** | **$2.61** | |

That is about 4 sessions, 8,600 session lines and 0.7 MB of transcript. The first run, without the
JSON shape, cost the same ($2.66, 38 calls, 2 lost stretches). `ask` makes no model call and answers
in well under a second.

## Choices only Bake can make for the next round

1. **What clock does recency run on?**
   - When the thing happened, when it was last used, or both?
   - Should a dream's own reading count as use? Today it does, as in the paper, and that caused
     finding 1.
2. **May the agent hold beliefs about what it doesn't remember?** ("memory has no record of X".) If not,
   the belief prompt says so and they are dropped.
3. **Who is who in a session?** Should taking a session in be told who the people are (Bake, the
   steward, the reviewer, Claude web), so "Bake said" and "the reviewer said" stay apart? Where would
   that list come from?
4. **When a belief is refined, what happens to the old one?**
   - Should it stay retrievable (at lower weight)?
   - Or must the new one carry everything forward?
   - And should a "refines" that drops facts be allowed?
5. **How long does word-for-word detail live?** Today it is the scratch pad of the last 1 session.
   Should it instead be the last N hours, the last N sessions, or until the next dream?
6. **What should a dream ask itself?** Today it asks the five broadest questions about the new
   episodes. Other choices: questions about Bake and their wishes only, or questions about what
   changed since the last dream (which is where contradictions would show up).
7. **Is word matching good enough to go on with, or should relevance use the model?** The model could
   rate relevance for the top 50 candidates, at about one Sonnet call per turn.
8. **What material next?** Sessions where Bake really changed their mind, to see "contradicts" work.
   The house agent's own conversation would be the natural next source, once it exists.
