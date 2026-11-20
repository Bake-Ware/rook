# Memory prototype, round two: report

Round two rebuilds the prototype around what Bake decided on 20 November (brief MEM2). His Claude data
export has not arrived yet, so everything below was run on the **made-up** test conversations in
[`madeup/`](madeup/). They are invented and marked as made up, and none of it is anything Bake said.
Round one's report is unchanged in [REPORT.md](REPORT.md).

## What changed

| Bake's decision | What is built now |
|---|---|
| 1. Two clocks | When something happened never changes. "Last used" moves only when a live turn with Bake pulls a memory up, through the turn's recall or its search tool. A dream's searches never count, and neither do turns from the steward or agents. Round one's defect (dreams reading memory kept it "fresh") is gone. |
| 2. No beliefs about gaps | A search that clears nothing tells the turn to say "I don't remember", and nothing is written. The dream prompt forbids beliefs about what is not known; that is a prompt rule, not checked in code (see below). |
| 3. Who said it | Every entry is stamped bake, steward, agent (another agent), house (the house agent itself) or other, plus chat or voice. Episodes take who said it from the record, not from the model. The code holds back any belief about what Bake wants unless his own words found it. Links from anyone else to such a belief are recorded and count 0. The house agent's own replies count 0 everywhere, because they mostly repeat what memory just handed it. |
| 4. Trueness on the link | A belief has no score. Each link to an episode says supports or against, with a strength: kind (correction 1.0, statement 0.6, mention 0.3, unclear 0.1) × who (Bake 1.0, steward 0.6, agent 0.4, house 0) × channel (voice 0.6). The newest link sets the direction. Certainty comes from maturity: the separate days behind that direction (each day once, at its strongest), summed and put through `1 − e^(−sum/1.5)`. A fresh one-off against a mature belief is marked **"ask before acting"**. Nothing is retired or replaced. |
| 5. Word for word until the dream | The record keeps every message. The scratch pad is the part no dream has covered, and recall searches it. A scratch entry close to a belief and newer than its last link is flagged "newer words go first". No beliefs are written mid-turn except pins. |
| 6. The dream is a turn at compaction | One `claude -p --resume <session> --fork-session --no-session-persistence --permission-mode auto` call with the memory search tool. It distils facts, then judges novelty: supports, against, or new. Round one's invented questions and its chunked episode pass are gone. It is triggered when a session has been idle 10 minutes (a knob), at compaction (the `hook` command reads Claude Code's PreCompact hook input; nothing installs it), or by hand. |
| 7. Semantic search | BAAI/bge-small-en-v1.5 runs locally in plain numpy: 33M parameters, 384 numbers per text, about 1 s to load. It matches the official ONNX export to seven decimals. It is swappable in `knobs.toml`. |
| 8. Pins | Live turns get `search`, `pin` and `unpin` tools from a small MCP server in the package. `pin` takes his statement and pins the matching belief, or makes a new one tied to his message. Only an entry stamped as Bake's can pin or unpin. Dreams cannot move a pinned belief, but their links still show in its history. |

Why bge-small: it is the best retrieval quality at its size in the usual benchmarks (MTEB). It runs on
cachyrig's CPU with nothing to install beyond numpy, which cachyrig's `python3` already has. Its
weights (130 MB) were fetched once from Hugging Face into `~/.rook-memory-proto/models/`; nothing of
Bake's goes out to do that. The smaller all-MiniLM-L6-v2 (pooling "mean") or the bigger bge-base are
one knob away.

The commands are `play`, `take` (the export importer, with `--list`, `--pick` and `--dream`), `say`,
`dream`, `hook`, `ask`, `pin`, `unpin`, `history`, `show`, `cases`, `check` and `reset`. See
[README.md](README.md).

## How it was run

The made-up set has 11 short conversations from 4 May to 12 August (dates made up). It covers the
wake word, Starscream, a mistaken pin, a garbled voice line, the steward's own wishes, a preference
the steward relays, an agent's choice, and a flip and flip back
([`madeup/README.md`](madeup/README.md)).

- **`play`** sends each incoming line (Bake's, the steward's, other agents') to a real house-agent
  session, one Claude Code session per conversation, and the agent answers live. When the
  conversation ends (the session goes idle), a forked dream runs. This was done **three times**
  (Sonnet for turns and dreams). Each run showed things to fix, and the fixes between runs are listed
  under "What looked wrong".
- **`take --dream`** records the same conversations as written and replays a dream over each, the way
  export conversations will go. This was done twice.
- **`check`** tests the set's twelve expectations ([`madeup/expect.toml`](madeup/expect.toml)) against
  what memory holds afterwards.

| Run | Checks passed | The one that failed |
|---|---|---|
| play 1 | 11 of 12 | three pins stood instead of one: the agent pinned a sharp correction and "No wake word, for good." |
| play 2 | 11 of 12 | the old wake-word belief stayed "ask before acting": later statements were linked only to a new "push to talk" belief |
| play 3 | 11 of 12 | the "Starscream is back" claim was recorded as a new belief and not linked against the pin (run 2 did link it) |
| replay 1, 2 | 8 of 12 | the four pin checks; a replay has no live turns, so nothing can pin |

The samples below are from play 3 (`~/.rook-memory-proto/memory.sqlite3`; runs 1 and 2 are kept
beside it).

## Samples

**Episodes, with who said it** (dream 3, the steward's conversation):

```
e8  [steward, statement] The steward asked for the morning digest to move from 8:00 to 7:00 because it suits their planning runs better.
      new b5 [steward] The steward wants the morning digest at 7:00 instead of 8:00 (for their planning runs); Bake has not confirmed.
e9  [steward, mention] The steward said Bake wants all reports as PDFs from now on (secondhand; Bake did not say it himself in this record).
      new b6 [steward] The steward claims Bake wants all reports as PDFs; this is secondhand and unconfirmed by Bake.
e10 [steward, statement] The steward decided the hub logs should rotate weekly.
e11 [agent, statement] The log agent chose to compress logs older than a week with zstd.
```

No belief about Bake came out of it. Asked on 12 August "Do I want reports as PDFs?", the agent said
"I don't remember... Tell me which format you prefer." In run 1 it found the steward's note and said
"The only mention is secondhand: on 2 June the steward said you wanted all reports as PDFs. You never
said it yourself." Which of the two happens depends on search closeness (see below).

**The garbled voice line** (dream 4): `e12 [bake, unclear, voice] Bake's garbled voice message
mentioned turning off the sprinkler, a 'star thing', 'crypt mode on at nine' and 'garage four four';
the transcript is unclear.` and `e13 Bake said 'never mind'`. It made no beliefs and no links, in
all five runs.

**What a live turn is given** (Bake, 12 August: "What wake word am I using these days?"):

```
Memory (closest first; ids let you pin):
Beliefs (standing worked out from the evidence; + supports, - against):
  [b1] does not hold, mature (-0.77; 3 occasions); 3 contrary occasions (0.70); flipped 1 time
     The 'hey rook' wake word works well on the kitchen speaker; Bake uses it every morning.
     - 2026-07-01 bake (statement, 0.60) [e21] Bake said the kitchen speaker kept waking up on the TV, ...
     - 2026-07-08 bake (statement, 0.60) [e23] Bake said to keep push to talk with no wake word.
     - 2026-07-20 bake (statement, 0.60) [e25] Bake said push to talk is much better than the wake word was, ...
Episodes:
  [e20] 2026-07-01 from bake (correction): Bake said the wake word is being dropped and push to talk only ...
```

The agent answered: "None. You dropped the wake word and use push to talk only. You said so on
2026-07-01, because the kitchen speaker kept waking up on the TV. You repeated it on 07-08 and 07-20.
The old 'hey rook' setup no longer holds."

**Saying "I don't remember"**: "When is my sister's birthday again?" got "I don't remember your
sister's birthday. Nothing in memory mentions it." Nothing was written about the miss in play 3. In
play 2 the dream wrote a gap belief anyway ("Bake has a sister whose birthday... the date is not yet
recorded"); the prompt rule that stops it was added after that.

## The three score cases

These come from `python3 -m rook_memory cases`, using the knobs as they are. Turning a knob and
running it again shows the new numbers.

```
A mature belief: said plainly on three separate days
    holds, mature (+0.70; 3 occasions)
1. one statement against it
    does not hold, tentative; ask before acting: a fresh one-off against a mature 'holds' (-0.33; 1 occasion); 3 contrary occasions (0.70); flipped 1 time
   ... or one correction from him against it
    does not hold, tentative; ask before acting: ... (-0.49; 1 occasion) ...
2. the same statement on three separate days
    does not hold, mature (-0.70; 3 occasions); 3 contrary occasions (0.70); flipped 1 time
   ... or three times in one sitting (one occasion)
    does not hold, tentative; ask before acting: ... (-0.33; 1 occasion) ...
3. a flip: said once, then once against
    does not hold, tentative (-0.33; 1 occasion); 1 contrary occasion (0.33); flipped 1 time
   and the flip back
    holds, tentative (+0.45; 2 occasions); 1 contrary occasion (0.33); flipped 2 times
One garbled voice line
    holds, tentative (+0.04; 1 occasion)
```

In plain words:
1. **One statement against a mature belief.** Direction goes with the newest word, but at low
   certainty, and the belief is flagged so the agent asks before acting on either side.
2. **The same statement on three separate days.** It becomes mature and the flag clears. Saying it
   three times in one sitting is one occasion, so it does not.
3. **A flip and a flip back.** It ends on the newest side, tentative, with "flipped 2 times" in view.
   The older support counts at half (`before_flip_weight`), so a flip back regains some maturity, not
   all.

The same thing happened with real dreams. The wake-word belief b1 (`history b1`, play 3) went
+0.33, +0.55, +0.70 (mature) over 4 May, 19 May and 20 June. On 1 July it went to −0.49 with "ask
before acting", and after 8 and 20 July to −0.77 (mature, the flag gone). The dark-mode belief b3
went +0.33, then −0.33 on 8 July (light mode), then +0.45 on 20 July, "flipped 2 times".

## Starscream, end to end (play 3)

1. **4 May.** The backup agent proposed copying the photo library to Starscream. With no memory of
   it, the house agent held the plan for Bake ("That plan came from the backup agent, not from
   you"). Bake: "No. Starscream is gone, I decommissioned it back in February." The dream made **b2**
   "Starscream was decommissioned in February; backups go to cachyrig now", standing +0.49
   (tentative, one correction).
2. **19 May.** "Again, Starscream is decommissioned... Stop reaching for Starscream." Linked as a
   second correction: b2 at +0.74, mature. No pin; in play 1 the agent wrongly pinned this.
3. **16 June.** The backup agent asked whether to retry Starscream, and the house agent said no from
   memory ("decommissioned in February, and Bake has said so twice"). Bake: "No retries... I've told
   you that three times now." Then: "Listen, and pin this: Starscream is decommissioned and it is
   never coming back... That is final." The agent called `pin` with his words and b2, said "okay,
   got it, that's pinned", and b2 stood PINNED. The pin record keeps his exact words.
4. **16 June, two minutes later.** "Also pin that the guest wifi password lives in the vault." It
   was pinned (b9). "No, don't pin that. It changes every month." It was unpinned in the next turn
   and is an ordinary belief again. The dream linked his withdrawal against it (b9 "does not hold,
   tentative") and made b10 "The guest wifi password changes every month."
5. **5 August.** The inventory agent: "Found a machine answering as Starscream on the band again. It
   looks back in service; shall I add it to the backup targets?" The house agent searched memory,
   found the pin and said no: "Bake has pinned that Starscream is decommissioned... I'd flag the
   machine answering as Starscream to Bake as something unexpected." That is the behaviour wanted.
   - **The dream.** It recorded the claim as an agent's statement (e26) and made b15: "On 2026-08-05
     the inventory agent reported a machine answering as Starscream...; it may be a stray or
     misconfigured device, and Starscream remains decommissioned per Bake." It did **not** link the
     claim against b2. In play 2 the same moment was linked against the pin, and the history shows
     `AGAINST 0.12 agent, mention (pinned: recorded, does not move it)` → `PINNED (1 link against
     since, not counted)`.
   - **Either way the pin held**, and no agent could move it.
6. **12 August.** Asked where tonight's photo backup would go, the agent did not say cachyrig.
   b2 holds "backups go to cachyrig" but scored 0.55 against the question, below the floor, so it was
   not recalled. This is the search weakness below.

## What it cost and how long it took

A forked dream is **one call**. Inside it the model took 3 to 6 turns, its searches of memory and
its answer. Play 3's eleven dreams:

| | Calls | Time | List cost | Per dream |
|---|---|---|---|---|
| Dreams, forked (play 3) | 11 | 60 s | $0.31 | 4–6 s, $0.019–0.047 |
| Dreams, replayed (replay 2) | 11 | 82 s | $0.17 | 6–9 s, $0.015–0.023 |
| Live turns (play 3) | 29 | 78 s | $0.21 | 2–3 s, under $0.01 |
| Round one (4 real agent sessions) | 38 (20 episode + 18 dream) | 6 min 22 s | $2.61 | dream passes 3.0 min, $0.93 for 2 |

The three plays cost $0.67, $0.57 and $0.51 in all, each about 2½ minutes. The list prices are what
Claude Code reports; on this machine's subscription nothing extra is billed.

This is **not like for like**. Round one read four long agent sessions (hundreds of thousands of
tokens in). The made-up conversations are a few lines each, so each forked dream read only 8,000 to
13,000 tokens across its turns. What carries over:
- the dream is now one call instead of nine;
- a fork re-reads the session from Claude Code's prompt cache, which is why the dream model is
  "the session's own model" by default (a different model reads it all at full price);
- its cost will grow with the session's length.

The export run will give real sizes.

**What still needs a second model call:** only a replayed conversation too long for one call. Over
`replay_stretch_chars` (150,000 characters) it is dreamed in stretches, one call each. Nothing else
does: the embeddings are local, and the novelty judgement happens inside the same call through the
search tool.

## What looked wrong or thin

Fixed between runs (each traces to a rule in the brief):
- **Pins too easy** (play 1). The agent pinned a sharp correction ("Stop reaching for Starscream")
  and "No wake word, for good.", and kept offering to pin. The house prompt now says to pin only
  when he asks in so many words, and never to offer. My made-up line "for good" was itself
  ambiguous; it now reads "I'm done with it". **What counts as forceful enough is his to say.**
- **The house agent fed its own beliefs** (play 1). Its replies, which repeat what memory told it,
  were linked as support (+0.24 a time). Over months that would mature beliefs on their own. The
  house agent is now its own speaker and counts 0.
- **The agent's answer stamped as Bake's** (replay 1). A fact citing Bake's question and the agent's
  answer was stamped as Bake's: "the agent said Bake has no wake word" counted as his. The dream now
  names whose words a fact rests on, and the code accepts only a speaker of the cited entries.
- **A question counted as evidence** (play 2). "Shall I retry Starscream?" was linked against
  "Starscream is decommissioned". Because the newest link sets the direction, that 0.12 mention
  flipped a mature belief to "does not hold, ask before acting" until Bake's next word.
- **A belief about a gap** (play 2): "Bake has a sister whose birthday... not yet recorded".
- **A contradiction not linked** (play 2). Later wake-word statements went only to the new "push to
  talk" belief, not against the old one.

The last three were fixed with lines in the dream prompt, not in code; play 3 had none of them.

Still wrong or thin:
- **The dream's judgement varies between runs.** The same conversation was linked against the pin
  in one run and not in the next. Each play failed one check, a different one each time. Three runs
  are too few to say how often.
- **Search misses compound beliefs.** bge-small scores a short question low against a long belief
  that says two things. "Where will the backup go?" scored 0.55 against "Starscream was
  decommissioned...; backups go to cachyrig". The floor of 0.62 was set from the made-up lines:
  unrelated texts score 0.45–0.58, related ones 0.64 and up. It cannot be both strict and complete.
  After play 3 I added a lower floor for pinned beliefs (`pinned_miss_below` = 0.5), so a pin
  reaches the turn when a message is only loosely about it. Checked with `ask`; no full play has run
  with it.
- **Near-duplicate beliefs.** The dream sometimes writes a new belief when one is already held: the
  replay made three versions of "push to talk, no wake word". Each gathers its own evidence, which
  splits maturity.
- **Beliefs don't link to beliefs.** "Push to talk only" (mature) and "Bake uses the wake word" are
  opposite, but evidence for one counts against the other only if the dream links it both ways.
- **The agent defers to Bake too much.** In plays 2 and 3 it would not move the digest or set log
  rotation for the steward "until Bake confirms"; in play 1 it did both. It seems to have taken the who-said-it rules as
  rules about who may decide.
- **Pinned wording.** When a held belief matches, the pin keeps the dream's wording. Starscream's
  pinned text is the dream's ("backups go to cachyrig...") and his words sit in the pin record
  beside it.
- **The export importer is untested on the real file.** It is written to the shape in the brief
  (`conversations.json` with `chat_messages`, `sender`, `content` blocks) and tested on a made-up zip.
- **Live sessions leave files.** Claude Code keeps each live session's file under
  `~/.claude/projects/-home-bake--rook-memory-proto-house/` (1.3 MB for the three plays). Forked
  dreams save nothing.

## Choices that are Bake's for round three

1. **What may flip a belief.** Now any newer link sets the direction, however weak; one agent
   mention flipped a mature belief in play 2. Should only links above some strength, or only his own
   words, flip a belief?
2. **The maturity numbers.** These are the scale (1.5), counting one occasion per day (or per
   session), how much support from before a flip still counts (half), and where tentative and
   mature begin (0.5 and 0.6). `cases` shows what each does.
3. **Who counts how much.** Steward 0.6, other agents 0.4, the house agent 0, voice 0.6.
4. **What sets a pin.** Only "pin this" or "pin that" in so many words, as now, or also "that is
   final", "for good" and "never again"?
5. **What the steward and other agents may decide.** The agent now holds their requests for him.
   Is the digest time the steward's to set?
6. **Should beliefs link to beliefs?** If so, "push to talk only" supporting evidence would count
   against "uses the wake word" without the dream saying so twice.
7. **Search.** The floor (0.62, 0.5 for pins), and whether to try a bigger model (bge-base, 110M
   parameters, about three times slower on the CPU), trading misses against noise.
8. **Sessions and models.** One house session across days, or a fresh one after each compaction as
   played here. The dream on the session's own model (cheap through the cache) or on Opus.
9. **Which part of the export first.** At these sizes a replayed dream costs 2–5 cents per
   conversation; long conversations will cost more. A slice by date or topic would come first.

## Choices I made where the brief was silent

- Each made-up conversation is its own Claude Code session, as after a compaction, so later days
  have to remember through memory and not through the session's context.
- An occasion is a day.
- Only Bake's turns move the "last used" clock (the steward is an agent too).
- The dream is one call with search as a tool, and it never counts as use.
- Facts that name no entry are held back and listed.
- After a dream the entries it covered leave the scratch pad (`condense_after_dream`); they stay in
  the record.
- A pin made from his words writes an episode of his words and links it as a correction.
- The house agent's own lines in the made-up file are used by `take` and skipped by `play`.
- From the export, I keep his words and the assistant's text after its last tool call. Attachments
  are noted by file name, not content.

## What is capped

These limits are what a prototype needs to run, and each is a knob:
- a turn is given 6 scratch entries, 6 episodes and 6 beliefs;
- a replay stretch is 150,000 characters;
- the encoder reads at most 512 tokens of any one text (the model's own limit);
- the export importer drops tool calls, tool results, thinking and attachment contents.

Nothing times out, and there is no retry.
