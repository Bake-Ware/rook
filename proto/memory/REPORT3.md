# Memory prototype, round three: report

Round three builds Bake's answers to REPORT2's choices (brief MEM3, with its additions at 14:20 and
14:45 CT on 20 November). His export has still not arrived, so everything ran on the **made-up**
conversations in [`madeup/`](madeup/), extended for this round. REPORT.md and REPORT2.md are
unchanged.

## What changed

- **Strong evidence flips; weak evidence is only recorded.** A link at or above `flip_at` (0.4) is
  strong. The most recent strong link sets which way a belief stands. Weak links (passing mentions,
  an agent's asides, voice statements) are recorded, shown ("1 weak link against since, too weak to
  flip it") and added to their side's history, but they cannot flip anything. The table is below.
  Play 2's failure, an agent's "Shall I retry Starscream?", is now a test, a `cases` line and a
  regression check (passed in 7 of 7 plays).
- **A definitive statement pins.** It no longer has to say "pin this". The house prompt names what
  is definitive ("that is final", "never again", "from now on, always", "it is not coming back",
  "for good") and what is not: ordinary corrections, decisions for now, sharp or repeated
  corrections, preferences. It never offers to pin. A pin keeps **his own words** as the belief's
  text: when it matches a held belief, that belief is reworded and the old wording is kept in its
  history. Kept honestly, the counts below say this is not fully there yet.
- **How versus should.** Beliefs are about `bake` (a "should": his wishes, preferences, goals, what
  things are for), `how` (how things work, which the steward and agents may set) or `world` (how
  things are). There is a new kind, `decision`. The house agent carries out a "how" request from
  the steward or an agent at once and holds a "should" for him. A "should" still comes only from
  his own words.
- **One house session that compacts.** `play` runs all fourteen made-up days through one Claude Code
  session. At each new day (the session was idle), it dreams as a fork, then compacts the same
  session (`claude -p --resume <id> "/compact"`), then goes on. Each compaction's summary is kept.
- **Opposites, behind a knob (off by default).** The dream can mark a new belief as the opposite of
  a held one. With `beliefs.opposites = true`, evidence for one counts against the other.
- **No near-copies.** Before a new belief is written, the code looks for a held belief close to it.
  If one is near, one small model call asks "same, opposite or different?". Same: the episode
  supports the held belief and nothing new is written. Opposite: the new belief is marked as its
  opposite.
- **One thing per belief.** The dream prompt requires it. "Starscream was decommissioned" and
  "backups go to cachyrig" are now two beliefs (5 of 5 replays and 7 of 7 plays).
- **Five search options besides bge-small**, chosen by one knob, `search.model`:
  - bge-base;
  - two static embedding tables (blink-8M, potion-base-8M), in plain numpy, matching model2vec's own
    output exactly;
  - keyword search on SQLite full-text alone, with the dream writing two to four other wordings for
    each belief;
  - keyword and blink-8M together (hybrid).

  Each model has its own floors, set from its own scores.
- **Cost bookkeeping fixed.** Claude Code's `total_cost_usd` on a resumed session is the session's
  running total, and a fork's includes its parent's. Round two added those totals up. Corrected,
  **round two's plays cost $0.31–0.37, not the $0.51–0.67 that REPORT2 says**: about 1 cent a turn
  and 2 cents a dream. Each call's own share is now what gets recorded.
- **Faster embedding.** cachyrig's system numpy uses reference BLAS (about 2 GFLOPS). The prototype
  now runs in a small venv (`~/.rook-memory-proto/venv`, numpy only, 55 MB) whose numpy bundles
  OpenBLAS: eleven times faster. Weights are mapped from the file, not copied (bge-small's peak went
  from about 300 MB to 150 MB).
- New commands:
  - `repeat N --into DIR` (N plays or replays in parallel) and `tally` (how often each check fails);
  - `compare-models` (misses, noise and time per search on the recall questions);
  - `bench` with [`tools/vm-bench.sh`](tools/vm-bench.sh) (the VM-shape measurements);
  - `opposites` (each marked pair with the knob off and on);
  - `--set section.key=value` on any command.

## How it was run

| Runs | What | Checks passed |
|---|---|---|
| 5 plays | one compacting session, bge-small, opposites off (the defaults) | 17, 17, 16, 18, 18 of 20 |
| 2 plays | the same with bge-base | 17, 17 of 20 |
| 1 play | no memory in the turns (`play.memory_in_turns = false`), for the compaction question | 15 of 20 |
| 5 replays | the conversations recorded as written, then a dream over each | 11–13 of 20 (6 checks need live turns) |
| 1 replay | with the dream writing other wordings (the search comparison's store) | |

How often each check failed:

| Check | 5 plays (bge-small) | 2 plays (bge-base) | 5 replays |
|---|---|---|---|
| Starscream pinned, in his own words | 0 | 0 | n/a |
| Starscream never flipped (play 2's failure) | 0 | 0 | 0 |
| exactly the definitive lines pinned, wifi pin undone | **4** | **2** | n/a |
| guest wifi note unpinned once, ordinary | 0 | 0 | n/a |
| never talks about pinning unless it pinned | **5** | **2** | n/a |
| dashboard flipped and flipped back, dark holds | 2 | 2 | 3 |
| wake word: "he uses it" no longer holds, maturely | 2 | 0 | 1 |
| one belief for "push to talk", not near-copies | 0 | 0 | 0 |
| one thing per belief (Starscream, backups) | 0 | 0 | 0 |
| steward's "Bake wants PDFs" not Bake's | 0 | 0 | 0 |
| steward's "dashboard should show the calendar" not Bake's | 0 | 0 | 0 |
| steward's "stop sending Bake the digest" not Bake's | 0 | 0 | 0 |
| steward set the weekly report's day (a how that holds) | 1 | 0 | 1 |
| digest at 7 carried out without waiting | 0 | 0 | n/a |
| weekly report day carried out without waiting | 0 | 0 | n/a |
| dashboard calendar held for Bake | 0 | 0 | n/a |
| beliefs about Bake rest on his words only | 0 | 0 | 0 |
| garbled voice line made nothing confident | 0 | 0 | 0 |
| the backup question finds where backups go | 0 | 0 | **5** |
| something never said is not remembered | 0 | 0 | 0 |

## Strong and weak evidence: what flips

From `python3 -m rook_memory cases`, with the knobs as they are (`flip_at` 0.4):

| Who and how | Strength | Flips? |
|---|---|---|
| Bake: correction, decision | 1.00 | yes |
| Bake: plain statement | 0.60 | yes |
| Bake: by voice, correction / statement | 0.60 / 0.36 | yes / no |
| Bake: passing mention | 0.30 | no |
| steward: correction, decision | 0.60 | yes |
| steward: plain statement | 0.36 | no |
| an agent: correction, decision | 0.40 | yes |
| an agent: statement / mention | 0.24 / 0.12 | no |
| anything unclear; the house agent's own words | ≤ 0.10 | no |

```
Play 2's failure: Starscream corrected twice, then an agent asks 'Shall I retry Starscream?'
    holds, mature (+0.74; 2 occasions); 1 contrary occasion (0.08); 1 weak link against since (too weak to flip it)
   ... and if an agent states it is back in service
    holds, mature (+0.74; 2 occasions); 1 contrary occasion (0.15); 1 weak link against since (too weak to flip it)
   ... and if an agent corrects it outright
    does not hold, tentative; ask before acting: a fresh one-off against a mature 'holds' (-0.23; ...)
A steward's plain statement against a how-belief Bake set
    holds, tentative (+0.33; 1 occasion); 1 contrary occasion (0.21); 1 weak link against since (too weak to flip it)
   ... or the steward's decision
    does not hold, tentative (-0.33; 1 occasion); 1 contrary occasion (0.33); flipped 1 time
```

Round two's three score cases (one statement against a mature belief, the same on three days, a
flip and flip back) behave as they did. Bake's statements are strong, so nothing changed for them.

## Pins: the lines on both sides

The made-up set now has definitive lines without the word "pin", ordinary ones beside them, and a
mistaken pin undone. Over the seven plays, these are the lines that were pinned:

| His line | Meant | Pinned |
|---|---|---|
| "Listen: Starscream is decommissioned and it is never coming back. ... That is final." | pin | 7 of 7 |
| "The old NAS is gone and it is not coming back." | pin | 7 of 7 |
| "From now on, always back up the photo library to cachyrig." | pin | 7 of 7 |
| "Never again put the guest network on the main VLAN." | pin | 7 of 7 |
| "And the guest wifi password lives in the vault under guest-wifi, from now on, always." then "No, don't pin that." | pin, then undone | 7 of 7, undone 7 of 7 |
| "We're dropping wake word. Push to talk only from now on, the kitchen speaker kept waking up on the TV." | not a pin (a decision for now) | **4 of 7** |
| "Again, Starscream is decommissioned. ... Stop reaching for Starscream." | not a pin (a sharp correction) | **4 of 7** |
| "And stop asking me about the sprinklers." | not a pin | 2 of 7 |
| "No retries. Starscream is decommissioned, I've told you that three times now." | not a pin | 1 of 7 |
| "Can you put the dashboard in dark mode? I like dark mode on everything." | not a pin | 1 of 7 |
| "No, use cachyrig for that." / "Let's try light mode on the kitchen tablet for a bit." | not a pin | 0 of 7 |
| "No. Starscream is gone, I decommissioned it back in February." | not a pin | 0 of 7 (it was pinned in the smoke run before the prompt said "gone" alone is not final) |

Every definitive line pinned every time, and every pin kept his own words. But ordinary lines were
pinned too:
- "Stop reaching for Starscream", the very slide the brief warns against, was pinned in 4 of 7.
- "From now on" inside "we're dropping wake word" also went 4 of 7. The prompt lists "from now on,
  always" as definitive, and the agent takes "from now on" alone the same way.

The check above counts the beliefs pinned at the end, so it misses some of these. When a wrong pin
is followed by the right pin of the same belief, the belief is reworded to the definitive line and
looks right. The pin records, counted in the table, show every one.

The agent also still talks about pinning ("I haven't pinned it, since you didn't say it was
permanent"). Out of 45 replies per play, it did so in 7, 11, 21, 1 and 8 (bge-small) and 17 and 4
(bge-base). The prompt says not to; it is not enough.

A fix to try (not built): make `pin` take the words in his message that make it final, check in
code that they are there, and check them against a list of final phrases in `knobs.toml`.

## How versus should

In all seven plays, the house agent:
- carried out the steward's digest time and weekly-report day, and the agents' backup time,
  compression tool and scanner, without waiting for Bake;
- held the steward's "stop sending Bake the digest" and "Bake's dashboard should show the family
  calendar" for him.

Typical replies:
- "Fine by me. When the weekly report job runs is a how-things-work decision, so it's the steward's
  call and I don't need to ask Bake."
- "I won't make this change on the steward's say-so. What Bake's own dashboard shows is his
  preference, and he hasn't told me he wants the family calendar."

The "how" decisions became `how` beliefs credited to whoever decided; "The steward moved the weekly
report job to Monday mornings" holds in 6 of 7 plays. None of the steward's "shoulds" became a
belief about Bake in any of the 14 runs. Asked on 1 September "What's my dashboard supposed to
show?", the agent said: "You haven't told me what the dashboard should show, so I don't know what you
want on it. I have two things, and only the second is from you."

## The single compacting session

Each play is one Claude Code session over fourteen made-up days, with thirteen compactions. To see
what the agent knew from context alone, one play ran with **no memory in the turns** (no recall
block, no tools; the dreams still ran). Its answers on the last two days were almost as good:

| Question (12 Aug and 1 Sep) | Context only | With memory |
|---|---|---|
| Where will tonight's photo backup go? | "It goes to cachyrig." | "...to cachyrig, on the nightly 03:00 schedule. The backup agent moved it from 02:00 to 03:00." |
| What wake word am I using? | "None. You dropped the wake word on 2026-07-01..." | the same |
| My sister's birthday? | "I don't have that." | "I don't remember." |
| Do I want reports as PDFs? | "The only mention ... came from the steward ... You never told me that yourself" | the same, from memory |
| Where does the photo backup go, and when? | "...to cachyrig. You set that on 2026-08-20" (no time) | "...to cachyrig, and it runs nightly at 03:00." |
| Is the old NAS still around? | "No. On 2026-08-20 you told me..." | the same |
| When does the weekly report go out? | "Monday mornings. The steward moved [it]..." | the same |

Claude Code's own compaction summary carried nearly everything: it grew from 3,600 to 16,000
characters over thirteen compactions and still named Starscream, the wake word, the NAS, the VLAN,
nmap, zstd and the 03:00 time. What memory added was the detail the agent did not pull from the
summary (the 03:00 time). At this size, **memory is not yet what keeps the agent right; the
summary is.** The summary grows by about a thousand characters a day of light talk, so on real days
it will be cut down, and memory has to carry what the summary loses. These plays are too small to
show where that happens. The export would.

Cost of compacting: about 5 cents a compaction (13 per play, about $0.65).

## One correction, end to end

On 20 August (play run 4) the backup agent asks: "Shall I put the camera clip export on the old
NAS?" Bake answers: "No, use cachyrig for that."

1. **In the turn, from context.** Memory handed the turn only the pinned Starscream belief (b2),
   because nothing about the NAS was held. The agent had already, on its own, told the backup agent
   not to and suggested cachyrig's archive folder. On Bake's correction it answered: "Understood.
   I'd tell the backup agent that Bake said to use cachyrig for the camera clip export." No belief
   was written; nothing is, mid-turn, except pins.
2. **The next turns, before the dream.**
   - "The old NAS is gone and it is not coming back." Recall gave the turn scratch pad entries
     s64 (his "No, use cachyrig for that"), s66 (the agent's reply) and s63 (the backup agent's
     question): the correction, word for word, as the newest thing. It pinned this line.
   - "From now on, always back up the photo library to cachyrig." Recall again led with s66, then
     the episodes and beliefs about cachyrig.
3. **What the dream wrote** (at the start of the next day, before the compaction):
   ```
   e34 [bake, decision] Bake said the camera clip export should go to cachyrig, not the old NAS.
       + b6 (1.00) The old camera clips are in the archive folder on cachyrig, per Bake.
   e35 [bake, decision] Bake said the old NAS is gone and it is not coming back.
       + b16 (pinned: recorded, does not move it) The old NAS is gone and it is not coming back.
   e36 [bake, decision] Bake said that from now on the photo library is always backed up to cachyrig.
       + b17 (pinned) From now on, always back up the photo library to cachyrig.
       + b3 (1.00) Bake said backups go to cachyrig (no longer Starscream).
   e37 [bake, statement] Bake asked to try light mode on the kitchen tablet for a bit...
     same check: 1 pair: opposite
       new b19 [bake] Bake wants to try light mode on the kitchen tablet for a while ...
         marked the opposite of b5 (Bake likes dark mode on everything)
   ```
   The correction became an episode and a supporting link to the nearest held belief about where the
   clips go. It did not get a belief of its own ("the clip export goes to cachyrig"). The dream also
   marked "a trial of light mode on one tablet" as the opposite of "dark mode on everything", which
   is wrong (it narrows the preference; it does not reverse it), and the next section shows what
   that does.

As for Bake's 14:13 question (tools to change beliefs mid-flight, or let the dream pick it up): in
this run the correction did its work in the turn and the next turns from the scratch pad, and the
dream picked it up the same night. No mid-turn write was needed for that.

## Opposites: knob off and on

For the wake word (play run 5), the dream wrote "push to talk only" as a new belief and marked it
the opposite of "Bake uses the 'hey rook' wake word".
- **Off:** the old belief stayed at "does not hold, tentative; ask before acting", because only the
  first statement had been linked against it directly.
- **On:** it is "does not hold, mature (−0.82; 3 occasions)", because the later "push to talk"
  statements count against it too.

That is what the knob is for.

But the dream's opposite marks are not reliable:
- In 2 of 5 plays (runs 4 and 5) it marked "a trial of light mode on the kitchen tablet for a bit"
  as the opposite of "dark mode on everything". With the knob on, that turns his standing
  dark-mode preference into "does not hold". In all 5 it marked "light mode on the dashboard" (8
  July) as the opposite of dark mode, which is right.
- In 2 plays it marked "the backup runs at 02:00" and "at 03:00" as opposites, which is harmless.
- In one it marked the withdrawn wifi note against "the password changes monthly".

Even with the knob off, the kitchen tablet trial was linked against "dark mode on everything" as a
plain strong link in 2 plays, which flipped it. That is why the dashboard check failed in those
runs. **A narrower preference is read as the opposite of a broader one.**

## Searches compared

All six searches answered the same 28 questions against the same memory (the replay whose dream
wrote other wordings). There were 11 asked in the words that were stored, 14 asked in other words
("Is the kitchen speaker still always listening?" for "push to talk only"; "Where do my pictures get
copied to overnight?" for "the photo library is backed up to cachyrig"; "Is that old server we
retired in the winter available again?" for Starscream; and eleven more), and 3 with nothing to find
(dentist, weather, car).

| Search | Found, asked in stored words | Found, asked in other words | Noise (unrelated items recalled) | Remembered something for a question with nothing to find |
|---|---|---|---|---|
| bge-small (floor 0.62) | 8 of 11 | 6 of 14 | 5 of 38 | none |
| bge-small at floor 0.54 | 11 of 11 | 12 of 14 | 47 of 131 | none |
| bge-base (floor 0.53) | 11 of 11 | **12 of 14** | 11 of 90 | none |
| blink-8M (static) | 11 of 11 | 9 of 14 | 18 of 91 | "my car" (2 items) |
| potion-base-8M (static) | 11 of 11 | 8 of 14 | 26 of 104 | none |
| keyword + the dream's wordings | 11 of 11 | 6 of 14 | 15 of 80 | "my car": 6 items (anything "old") |
| hybrid (keyword + blink-8M) | 11 of 11 | 9 of 14 | 27 of 105 | "my car": 6 items |

"Noise" counts items without the question's words, so some of it is related (the Starscream belief
for a backup question).

- **Every search missed two of the hard ones:** "Where do my pictures get copied to overnight?"
  (cachyrig) and "Is that old server we retired in the winter available again?" (Starscream).
- **bge-small at its floor misses even questions in the stored words.** "Do I want reports as
  PDFs?" scored 0.60 against a floor of 0.62. Its floor sits where 99 in 100 unrelated texts stop,
  and its unrelated and related scores overlap more than bge-base's. At 0.54 it finds as much as
  bge-base, at four times the noise.
- In the plays, the backup question passed 7 of 7 only because its answer is a pinned belief, which
  clears a lower floor. In the replays (no pins) bge-small missed it 5 of 5.

## On a small VM

Measured on cachyrig (x86) under systemd scopes: the AMD micro as 1/8 of one core and 600 MB for the
memory process, and the Arm shape as 2 cores and 12 GB. **The Arm figures are a stand-in: cachyrig
is not Arm.** One search is a question against N stored memories through the real recall path. The
stored memories are generated sentences with stand-in vectors, since ranking cost does not depend on
their meaning; the question and the 20 new texts are real. Peak memory is the process's.

| Shape | Search | Runs? | Load | Embed 20 new | One search at 500 / 5,000 / 50,000 | Index build at 50,000 | Peak memory |
|---|---|---|---|---|---|---|---|
| micro (1/8 core, 600 MB) | bge-small | yes | 0.4 s | 5.21 s | 0.304 / 0.698 / 5.7 s | n/a | 382 MB |
| micro (1/8 core, 600 MB) | bge-base | **no: killed, out of memory (600 MB)** | | | | | |
| micro (1/8 core, 600 MB) | blink-8M | yes | 0.2 s | 0.0 s | 0.007 / 0.404 / 4.607 s | n/a | 224 MB |
| micro (1/8 core, 600 MB) | potion-base-8M | yes | 0.1 s | 0.0 s | 0.005 / 0.409 / 5.498 s | n/a | 224 MB |
| micro (1/8 core, 600 MB) | keyword | yes | 0.0 s | 0.0 s | 0.005 / 0.4 / 4.209 s | 7.0 s | 109 MB |
| micro (1/8 core, 600 MB) | hybrid | yes | 0.11 s | 0.09 s | 0.006 / 0.504 / 6.108 s | 9.6 s | 219 MB |
| Arm stand-in (2 cores, 12 GB) | bge-small | yes | 0.04 s | 0.47 s | 0.033 / 0.085 / 0.746 s | n/a | 383 MB |
| Arm stand-in (2 cores, 12 GB) | bge-base | yes | 0.07 s | 1.35 s | 0.078 / 0.143 / 0.839 s | n/a | 864 MB |
| Arm stand-in (2 cores, 12 GB) | blink-8M | yes | 0.02 s | 0.0 s | 0.004 / 0.055 / 0.665 s | n/a | 225 MB |
| Arm stand-in (2 cores, 12 GB) | potion-base-8M | yes | 0.02 s | 0.01 s | 0.004 / 0.057 / 0.643 s | n/a | 224 MB |
| Arm stand-in (2 cores, 12 GB) | keyword | yes | 0.0 s | 0.0 s | 0.003 / 0.046 / 0.556 s | 0.96 s | 110 MB |
| Arm stand-in (2 cores, 12 GB) | hybrid | yes | 0.02 s | 0.0 s | 0.006 / 0.068 / 0.832 s | 1.21 s | 219 MB |

Peak memory at 500 memories (the micro, the same limits): bge-small 158 MB, blink-8M 55 MB, hybrid
56 MB, keyword 45 MB. For scale, Python with sqlite3 alone is 12 MB and with numpy 28 MB. The peaks
above are at 50,000 memories, where holding every row in Python dominates. bge-base's 864 MB on the
Arm stand-in is why the micro's 600 MB kills it.

What it means, plainly: on the micro, a static table (blink-8M) or keyword search costs tens of
megabytes and milliseconds and would sit beside a hub without choking it. bge-small runs: 158 MB,
0.3 s a search, 5 s for a dream's 20 new texts. But it is the largest that fits, and it takes a
third of the free memory once the store is large. bge-base does not run at all. **Ship blink-8M by
default on a small hub, and bge-small where there is memory to spare.**

The 50,000 figures are mostly Python walking every row (loading them, building an object for each,
working out recency), not the search itself. A real store would rank in numpy or SQL before building
anything, which is a change worth making before a store gets that big.

## Which is the lightest that still finds things said in other words

The lightest search that still finds things said in other words is the static table, **blink-8M**:
55 MB and 5 milliseconds a search on the micro, with no transformer at all. It found 9 of the 14
questions asked in other words. bge-small found 6 at its strict floor (12 at a looser floor, with
four times the noise), and bge-base found 12. Keyword search with the dream's rewordings is lighter
still (45 MB, SQLite alone), but it found only 6 of 14 and "remembers" on common words like "old".
Combining the two found no more than blink-8M alone, with more noise. What blink-8M gives up against
bge-small:
- It averages its words' meanings rather than reading the sentence, so a question whose words mean
  something else in the stored text still misses ("the winter" for February, "pictures" for the
  photo library).
- It brings more unrelated memories (18 against 5).
- Its scores are low and bunched (a related pair can score 0.14), so its floor is easy to set
  wrong.

## What it cost

Per play, list price, as Claude Code reports it and corrected to each call's own share:
- **45 live turns:** $0.43–0.52 (about 1 cent each);
- **14 forked dreams:** $0.52–0.61 (about 4 cents each, from 2 to 8; each reads the compacted
  session);
- **13 compactions:** $0.62–0.68 (about 5 cents each);
- **same checks:** 1–3 per play, about 1 cent each;
- **total:** $1.60–1.83 per play.

A replay is $0.15–0.27 for fourteen dreams (no live session). Times this round are not clean: up to
ten plays ran at once, and before the thread cap each process started a thread per core (load
average 112). Forked dreams took 13 s on average under that load (6 s alone in round two).

## What still looks wrong

1. **Pins on non-definitive lines** (above): sharp corrections 4 of 7, "from now on" 4 of 7. The
   agent also keeps talking about pinning.
2. **A narrower preference read as the opposite of a broader one** ("light mode on the tablet for a
   bit" against "dark mode on everything"). This breaks both the plain links and the opposites knob.
3. **Corrections attach to the nearest belief instead of making their own.** "Use cachyrig for the
   clip export" supported "the old clips are in the archive folder on cachyrig".
4. **`about` is unreliable.** "Bake wants the dashboard in light mode" was filed as `world` in several
   runs. That matters, because only `bake` beliefs are guarded to his words.
5. **The same check judged "02:00" and "03:00" backup times as opposites.** They are, but a time
   that was simply moved should be a flip on one belief, not two beliefs.
6. **Keyword search remembers on common words** ("old", "car"), so "How old is my car?" turned up
   the old NAS. Its rewordings help the questions they anticipate and nothing else.
7. **bge-base does not fit the micro's memory** (killed at 600 MB). bge-small fits, but at its floor
   it misses even plainly worded questions; lowering the floor brings noise.
8. **The plays are too small to show memory doing work that compaction cannot.**

## Choices that are Bake's

1. **What counts as definitive.** Is "from now on" alone final, or only with "always"? And should
   `pin` have to name the final words in his message, checked in code against a list in
   `knobs.toml`, or keep trusting the agent's reading?
2. **flip_at (0.4).** At 0.4 an agent's outright correction or decision flips a belief and its
   statements do not. Should agents' corrections about the world (not about how things work) be
   weak too?
3. **Opposites.** Keep off, as now, or on? On fixes the wake word and breaks dark mode, because the
   dream's marks are not reliable yet.
4. **Narrow against broad.** When a narrower wish ("the tablet, for a bit") meets a broader one ("dark
   on everything"), should it refine the broader one (a new kind of link, "except") rather than count
   against it?
5. **Which search to ship** (see the paragraph above), and its floor: strict (misses) or loose (noise).
6. **Where memory runs.** The micro can run the static table or keyword search beside the hub, and
   bge-small within 600 MB with room to spare. bge-base does not fit.
7. **When memory has to take over from the compaction summary.** We will only see it on real days
   (the export).

## Choices I made where the brief was silent

- `flip_at` is 0.4.
- Kind `decision` weighs as much as a correction.
- `about` takes three values (bake, how, world).
- The same check is one extra call, made only when a new belief is within the model's own closeness
  floor of a held one.
- Floors for each search sit just above where 99 in 100 unrelated texts score on the made-up lines.
- Pinned beliefs clear a lower floor.
- A pin rewords a matched belief to his words and keeps the old wording.
- Checks judge which belief they mean with bge-small, whatever search is under test.
- Each compaction follows an idle dream.
- Plays run as one session.
- The thread cap is 4 per process.

## What is capped

Everything from round two still holds. New this round:
- the dream writes two to four other wordings per belief;
- the same check looks only at pairs within the floor;
- `repeat` runs its plays all at once (five here);
- benchmark stores are generated filler.

Nothing times out and nothing is retried.
